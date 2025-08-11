# stream_orchestrator.py
import os
import signal
import subprocess
import sys
import time
import fcntl
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    find_stream_by_key,
    get_stream_ingestion_key,
)

# ---------- helpers ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_hours(csv: str) -> list[int]:
    return [int(h.strip()) for h in csv.split(",") if h.strip()]


def next_fixed_start_utc(tz_name: str, hours: list[int]) -> datetime:
    """
    Return the next scheduled start time in UTC for the given local tz and start hours (e.g., [0, 12]).
    """
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    candidates = []
    for h in hours:
        cand = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand <= now_local:
            cand += timedelta(days=1)
        candidates.append(cand)
    next_local = min(candidates)
    return next_local.astimezone(timezone.utc)


def read_env() -> dict:
    load_dotenv()  # loads .env if present
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

    # Rotation + health defaults
    env.setdefault("ROTATION_HOURS", "12")
    env.setdefault("HEALTH_CHECK_INTERVAL_SECS", "10")
    env.setdefault("FFMPEG_EXIT_RETRY_DELAY_SECS", "5")
    env.setdefault("MAX_CONSECUTIVE_FFMPEG_ERRORS", "20")

    # Fixed scheduling (midnight/noon etc.)
    env.setdefault("FIXED_START_HOURS", "")   # empty string = disabled (free-running 12h)
    env.setdefault("PREROLL_SECONDS", "150")  # start ingest/testing this many seconds before the slot
    env.setdefault("LIVE_LEAD_SECONDS", "5")  # go live this many seconds before the slot

    # Ingest recovery while live
    env.setdefault("INGEST_INACTIVE_RESTART_AFTER_SECS", "60")
    env.setdefault("MAX_LIVE_RECOVERY_RESTARTS", "3")

    # Logging
    env.setdefault("LOG_DIR", os.path.join(env["BASE_DIR"], "logs"))
    return env


def mk_logpath(log_dir: str, prefix: str | None):
    if not log_dir:
        return None
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pfx = prefix or "ffmpeg"
    return os.path.join(log_dir, f"{pfx}-{ts}.log")


def start_ffmpeg(cmd, log_path=None):
    stdout = open(log_path, "a") if log_path else subprocess.DEVNULL
    # merge stderr into stdout for a single file
    return subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.STDOUT, preexec_fn=os.setsid)


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


def acquire_lock(lock_path: str):
    """
    Ensure only one orchestrator instance runs.
    Keeps the lock file handle open for the life of the process.
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another orchestrator instance is running; exiting.", file=sys.stderr)
        sys.exit(1)
    return f  # keep handle referenced so lock stays held


# ---------- main ----------

def main():
    env = read_env()

    # Single-instance lock under BASE_DIR/run/orchestrator.lock
    lock_handle = acquire_lock(os.path.join(env["BASE_DIR"], "run", "orchestrator.lock"))

    service = _svc(env["GOOGLE_CLIENT_SECRETS"], env["GOOGLE_TOKEN_FILE"])

    rotation = timedelta(hours=int(env["ROTATION_HOURS"]))
    health_interval = int(env["HEALTH_CHECK_INTERVAL_SECS"])
    retry_delay = int(env["FFMPEG_EXIT_RETRY_DELAY_SECS"])
    max_consec_errors = int(env["MAX_CONSECUTIVE_FFMPEG_ERRORS"])
    tz = env["LOCAL_TZ"]

    # Stream/key alignment
    stream_id = ensure_stream(service, env.get("YT_STREAM_ID") or None)
    stream_key = read_private_key(env["YT_STREAM_KEY_FILE"])

    # If your key belongs to a different reusable stream, switch to the matching stream_id
    correct_stream_id = find_stream_by_key(service, stream_key)
    if correct_stream_id and correct_stream_id != stream_id:
        print(f"Detected stream/key mismatch. Using stream ID {correct_stream_id} that matches the provided key.")
        stream_id = correct_stream_id
    elif not correct_stream_id:
        print("Warning: Could not find a stream matching your key; if Studio shows 'no data', verify the key belongs to this account.",
              file=sys.stderr)

    fixed_hours_csv = env.get("FIXED_START_HOURS", "").strip()
    use_fixed = bool(fixed_hours_csv)
    hours = _parse_hours(fixed_hours_csv) if use_fixed else []
    preroll = int(env["PREROLL_SECONDS"])
    live_lead = int(env["LIVE_LEAD_SECONDS"])

    while True:
        # ---------- schedule & broadcast ----------
        if use_fixed:
            # Compute next scheduled start (midnight/noon local)
            next_start_utc = next_fixed_start_utc(tz, hours)

            # Sleep until (scheduled - preroll)
            wake_at = next_start_utc - timedelta(seconds=preroll)
            while True:
                now = now_utc()
                if now >= wake_at:
                    break
                time.sleep(min(10, max(1, (wake_at - now).total_seconds())))

            # Reuse an existing broadcast bound to our stream, else create a new one
            reuse = find_reusable_broadcast(service, stream_id)
            if reuse:
                broadcast_id = reuse["id"]
                existing_lifecycle = reuse["lifeCycleStatus"]
                print(f"Reusing broadcast {broadcast_id} (lifecycle={existing_lifecycle})")
            else:
                title = make_title(tz)
                try:
                    broadcast_id = create_broadcast(service, title, next_start_utc.isoformat())
                    bind_broadcast_to_stream(service, broadcast_id, stream_id)
                    existing_lifecycle = "created"
                    print(f"Created new broadcast {broadcast_id} titled '{title}' for {next_start_utc.isoformat()}")
                except HttpError as e:
                    print(f"YouTube API error while creating/binding broadcast: {e}", file=sys.stderr)
                    time.sleep(15)
                    continue

            # Ensure bound to the stream that matches our key; verify key matches
            try:
                bind_broadcast_to_stream(service, broadcast_id, stream_id)
                actual_key = get_stream_ingestion_key(service, stream_id)
                if actual_key and actual_key != stream_key:
                    print("Bound stream's ingestion key differs from configured key; rebinding to matching stream.")
                    alt_id = find_stream_by_key(service, stream_key)
                    if alt_id:
                        stream_id = alt_id
                        bind_broadcast_to_stream(service, broadcast_id, stream_id)
                        print(f"Rebound broadcast {broadcast_id} to stream {stream_id} matching the provided key.")
                    else:
                        print("Warning: No stream found that matches the provided key; proceeding anyway.", file=sys.stderr)
                else:
                    print(f"Ensured broadcast {broadcast_id} is bound to stream {stream_id}")
            except HttpError as e:
                print(f"Rebind to correct stream failed (will proceed anyway): {e}", file=sys.stderr)

            # ---------- ingest start ----------
            rtmp = f"{env['RTMP_INGEST']}/{stream_key}"
            ffmpeg_cmd = build_ffmpeg_cmd(env, rtmp)
            log_path = mk_logpath(env["LOG_DIR"], "ffmpeg")
            proc = start_ffmpeg(ffmpeg_cmd, log_path=log_path)
            print(f"FFmpeg started with PID {proc.pid}")

            # Wait until YouTube reports ingest ACTIVE
            max_wait_secs = 180
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

            # Move to TESTING if not already testing/live
            if existing_lifecycle not in ("testing", "live"):
                try:
                    transition_broadcast(service, broadcast_id, "testing")
                except HttpError as e:
                    print(f"Transition to TESTING failed (retrying): {e}", file=sys.stderr)
                    time.sleep(5)
                    try:
                        transition_broadcast(service, broadcast_id, "testing")
                    except Exception as e2:
                        print(f"Second TESTING attempt failed: {e2}", file=sys.stderr)
                        stop_ffmpeg(proc)
                        time.sleep(5)
                        continue

            # Go LIVE a few seconds before the scheduled time
            live_at = next_start_utc - timedelta(seconds=live_lead)
            while now_utc() < live_at:
                time.sleep(1)

            try:
                transition_broadcast(service, broadcast_id, "live")
            except HttpError as e:
                print(f"YouTube transition->live failed (will retry once): {e}", file=sys.stderr)
                time.sleep(5)
                try:
                    transition_broadcast(service, broadcast_id, "live")
                except Exception as e2:
                    print(f"Second LIVE attempt failed: {e2}", file=sys.stderr)

            # Health loop runs until the NEXT fixed start (noon/midnight)
            end_deadline = next_fixed_start_utc(tz, hours)

        else:
            # Free-running 12-hour rotation (original behavior)
            reuse = find_reusable_broadcast(service, stream_id)
            if reuse:
                broadcast_id = reuse["id"]
                existing_lifecycle = reuse["lifeCycleStatus"]
                print(f"Reusing broadcast {broadcast_id} (lifecycle={existing_lifecycle})")
            else:
                title = make_title(tz)
                start_time = now_utc() + timedelta(minutes=2)
                try:
                    broadcast_id = create_broadcast(service, title, start_time.isoformat())
                    bind_broadcast_to_stream(service, broadcast_id, stream_id)
                    existing_lifecycle = "created"
                    print(f"Created new broadcast {broadcast_id} titled '{title}' for {start_time.isoformat()}")
                except HttpError as e:
                    print(f"YouTube API error while creating/binding broadcast: {e}", file=sys.stderr)
                    time.sleep(15)
                    continue

            # Ensure bound and verify ingestion key matches configured key
            try:
                bind_broadcast_to_stream(service, broadcast_id, stream_id)
                actual_key = get_stream_ingestion_key(service, stream_id)
                if actual_key and actual_key != stream_key:
                    print("Bound stream's ingestion key differs from configured key; rebinding to matching stream.")
                    alt_id = find_stream_by_key(service, stream_key)
                    if alt_id:
                        stream_id = alt_id
                        bind_broadcast_to_stream(service, broadcast_id, stream_id)
                        print(f"Rebound broadcast {broadcast_id} to stream {stream_id} matching the provided key.")
                    else:
                        print("Warning: No stream found that matches the provided key; proceeding anyway.", file=sys.stderr)
                else:
                    print(f"Ensured broadcast {broadcast_id} is bound to stream {stream_id}")
            except HttpError as e:
                print(f"Rebind to correct stream failed (will proceed anyway): {e}", file=sys.stderr)

            rtmp = f"{env['RTMP_INGEST']}/{stream_key}"
            ffmpeg_cmd = build_ffmpeg_cmd(env, rtmp)
            log_path = mk_logpath(env["LOG_DIR"], "ffmpeg")
            proc = start_ffmpeg(ffmpeg_cmd, log_path=log_path)
            print(f"FFmpeg started with PID {proc.pid}")

            # Wait for ACTIVE
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

            # Transitions
            try:
                if existing_lifecycle == "live":
                    pass
                elif existing_lifecycle == "testing":
                    transition_broadcast(service, broadcast_id, "live")
                else:
                    try:
                        transition_broadcast(service, broadcast_id, "testing")
                    except HttpError as e:
                        print(f"Transition to TESTING failed (retrying): {e}", file=sys.stderr)
                        time.sleep(5)
                        transition_broadcast(service, broadcast_id, "testing")
                    transition_broadcast(service, broadcast_id, "live")
            except HttpError as e:
                lc = get_broadcast_lifecycle(service, broadcast_id)
                sstat, health = get_stream_status(service, stream_id)
                print(
                    f"YouTube transition failed: {e}\nContext: lifecycle={lc}, streamStatus={sstat}, health={health}",
                    file=sys.stderr,
                )
                time.sleep(8)
                try:
                    transition_broadcast(service, broadcast_id, "live")
                except Exception as e2:
                    print(f"Second LIVE attempt failed: {e2}", file=sys.stderr)

            end_deadline = now_utc() + rotation

        # ---------- health loop (with ingest inactivity recovery) ----------
        consec_errors = 0
        inactive_accum = 0
        inactive_threshold = int(env["INGEST_INACTIVE_RESTART_AFTER_SECS"])
        max_live_restarts = int(env["MAX_LIVE_RECOVERY_RESTARTS"])
        live_restarts = 0

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
                continue

            # FFmpeg running — monitor ingest status
            sstat, health = get_stream_status(service, stream_id)
            if sstat != "active":
                inactive_accum += health_interval
                print(f"Ingest status={sstat} (health={health}); inactive for {inactive_accum}s")
                if inactive_accum >= inactive_threshold:
                    print("Ingest inactive too long; restarting FFmpeg to recover.")
                    stop_ffmpeg(proc)
                    time.sleep(retry_delay)
                    proc = start_ffmpeg(ffmpeg_cmd, log_path=log_path)
                    inactive_accum = 0
                    live_restarts += 1
                    if live_restarts > max_live_restarts:
                        print("Exceeded live recovery restarts; exiting for systemd to restart service.")
                        stop_ffmpeg(proc)
                        sys.exit(1)
            else:
                inactive_accum = 0
                consec_errors = 0

            time.sleep(health_interval)

        # ---------- rotate: stop & complete ----------
        stop_ffmpeg(proc)
        try:
            transition_broadcast(service, broadcast_id, "complete")
        except HttpError as e:
            print(f"YouTube transition->complete failed: {e}", file=sys.stderr)
        time.sleep(5)


if __name__ == "__main__":
    main()
