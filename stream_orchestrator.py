# stream_orchestrator.py
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from titles import make_title
from ffmpeg_args import build_ffmpeg_cmd
from youtube_api import (
    _svc,
    ensure_stream,
    create_broadcast,
    bind_broadcast_to_stream,
    transition_broadcast,
    get_stream_status,
    get_broadcast_lifecycle,
    find_reusable_broadcast,
)

# Single-instance lock
import fcntl
def acquire_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another orchestrator instance is running; exiting.", file=sys.stderr)
        sys.exit(1)
    return f  # keep handle open

# in main():
def main():
    env = read_env()
    _lock = acquire_lock(os.path.join(env["BASE_DIR"], "run", "orchestrator.lock"))
    ...


def now_utc():
    return datetime.now(timezone.utc)


def read_env():
    load_dotenv()  # ok if present; systemd may have already set env
    # Expand ${VARS} and ~ using the current environment (which includes BASE_DIR)
    raw = dict(os.environ)
    def _expand(v: str) -> str:
        return os.path.expanduser(os.path.expandvars(v))
    env = {k: _expand(v) for k, v in raw.items()}

    required = [
        "BASE_DIR",
        "GOOGLE_CLIENT_SECRETS",
        "GOOGLE_TOKEN_FILE",
        "RTSP_URL",
        "RTMP_INGEST",
        "LOCAL_TZ",
        "YT_STREAM_KEY_FILE",
    ]
    for key in required:
        if not env.get(key):
            print(f"Missing required env: {key}", file=sys.stderr)
            sys.exit(2)
    env.setdefault("ROTATION_HOURS", "12")
    env.setdefault("HEALTH_CHECK_INTERVAL_SECS", "10")
    env.setdefault("FFMPEG_EXIT_RETRY_DELAY_SECS", "5")
    env.setdefault("MAX_CONSECUTIVE_FFMPEG_ERRORS", "20")
    env.setdefault("LOG_DIR", os.path.join(env["BASE_DIR"], "logs"))
    return env



def mk_logpath(log_dir: str, prefix: str):
    if not log_dir:
        return None
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(log_dir, f"{prefix}-{ts}.log")


def start_ffmpeg(cmd, log_path=None):
    stdout = open(log_path, "a") if log_path else subprocess.DEVNULL
    # merge stderr into stdout to keep one log file
    return subprocess.Popen(
        cmd, stdout=stdout, stderr=subprocess.STDOUT, preexec_fn=os.setsid
    )


def stop_ffmpeg(proc):
    if not proc:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def read_private_key(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if not key:
                raise ValueError("YT key file is empty")
            return key
    except Exception as e:
        print(f"ERROR reading YT key file: {e}", file=sys.stderr)
        sys.exit(3)


def main():
    env = read_env()
    #Single-instance lock
    _lock = acquire_lock(os.path.join(env["BASE_DIR"], "run", "orchestrator.lock"))

    service = _svc(env["GOOGLE_CLIENT_SECRETS"], env["GOOGLE_TOKEN_FILE"])

    rotation = timedelta(hours=int(env["ROTATION_HOURS"]))
    health_interval = int(env["HEALTH_CHECK_INTERVAL_SECS"])
    retry_delay = int(env["FFMPEG_EXIT_RETRY_DELAY_SECS"])
    max_consec_errors = int(env["MAX_CONSECUTIVE_FFMPEG_ERRORS"])
    tz = env["LOCAL_TZ"]
    stream_id = ensure_stream(service, env.get("YT_STREAM_ID") or None)
    stream_key = read_private_key(env["YT_STREAM_KEY_FILE"])

    while True:
        # Reuse an existing broadcast bound to our reusable stream if possible
        reuse = find_reusable_broadcast(service, stream_id)
        if reuse:
            broadcast_id = reuse["id"]
            existing_lifecycle = reuse["lifeCycleStatus"]
            print(f"Reusing broadcast {broadcast_id} (lifecycle={existing_lifecycle})")
            # Avoid modifying title/schedule on reuse to minimize API surprises
        else:
            # No suitable broadcast exists; create a new one a couple minutes out
            title = make_title(tz)
            start_time = now_utc() + timedelta(minutes=2)
            try:
                broadcast_id = create_broadcast(service, title, start_time.isoformat())
                bind_broadcast_to_stream(service, broadcast_id, stream_id)
                existing_lifecycle = "created"
                print(
                    f"Created new broadcast {broadcast_id} titled '{title}' "
                    f"for {start_time.isoformat()}"
                )
            except HttpError as e:
                print(
                    f"YouTube API error while creating/binding broadcast: {e}",
                    file=sys.stderr,
                )
                time.sleep(15)
                continue

        # Start ingest FIRST so transitions won't 403
        rtmp = f"{env['RTMP_INGEST']}/{stream_key}"
        ffmpeg_cmd = build_ffmpeg_cmd(env, rtmp)
        log_path = mk_logpath(env["LOG_DIR"], "ffmpeg")

        proc = start_ffmpeg(ffmpeg_cmd, log_path=log_path)
        print(f"FFmpeg started with PID {proc.pid}")

        # Wait for stream to become ACTIVE
        max_wait_secs = 120
        waited = 0
        sstat, health = None, None
        while waited < max_wait_secs:
            sstat, health = get_stream_status(service, stream_id)
            print(f"Stream status: {sstat}, health: {health}")
            if sstat == "active":
                break
            time.sleep(3)
            waited += 3

        if sstat != "active":
            print("Stream never became ACTIVE; restarting cycle.")
            stop_ffmpeg(proc)
            time.sleep(5)
            continue

        # Transition state machine based on current lifecycle
        try:
            if existing_lifecycle == "live":
                # Already live; nothing to do
                pass
            elif existing_lifecycle == "testing":
                transition_broadcast(service, broadcast_id, "live")
            else:
                # created/ready (or unknown): go testing -> live
                try:
                    transition_broadcast(service, broadcast_id, "testing")
                except HttpError as e:
                    print(
                        f"Transition to TESTING failed (retrying): {e}",
                        file=sys.stderr,
                    )
                    time.sleep(5)
                    transition_broadcast(service, broadcast_id, "testing")

                # Confirm testing (best-effort)
                max_wait_testing = 30
                t_waited = 0
                while t_waited < max_wait_testing:
                    lc = get_broadcast_lifecycle(service, broadcast_id)
                    print(f"Broadcast lifecycle: {lc}")
                    if lc == "testing":
                        break
                    time.sleep(2)
                    t_waited += 2

                # Now go live
                transition_broadcast(service, broadcast_id, "live")
        except HttpError as e:
            lc = get_broadcast_lifecycle(service, broadcast_id)
            sstat, health = get_stream_status(service, stream_id)
            print(
                "YouTube transition failed: {}\nContext: lifecycle={}, "
                "streamStatus={}, health={}".format(e, lc, sstat, health),
                file=sys.stderr,
            )
            time.sleep(8)
            try:
                transition_broadcast(service, broadcast_id, "live")
            except Exception as e2:
                print(f"Second LIVE attempt failed: {e2}", file=sys.stderr)

        # === 12-hour health loop ===
        end_deadline = now_utc() + rotation
        consec_errors = 0
        while now_utc() < end_deadline:
            ret = proc.poll()
            if ret is not None:
                print(f"FFmpeg exited with code {ret}; restarting in {retry_delay}s")
                consec_errors += 1
                if consec_errors >= max_consec_errors:
                    print("Too many consecutive FFmpeg errors; rotating broadcast early.")
                    break
                time.sleep(retry_delay)
                proc = start_ffmpeg(ffmpeg_cmd, log_path=log_path)
            else:
                consec_errors = 0
                time.sleep(health_interval)

        stop_ffmpeg(proc)
        try:
            transition_broadcast(service, broadcast_id, "complete")
        except HttpError as e:
            print(f"YouTube transition->complete failed: {e}", file=sys.stderr)
        time.sleep(5)


if __name__ == "__main__":
    main()
