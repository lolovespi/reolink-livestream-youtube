# Weather Stream (Reolink → FFmpeg → YouTube, 12h auto-rotation)

Continuous livestream from a Reolink (optimized for **RLC‑810WA** on **Raspberry Pi 4**) to YouTube, running on a Raspberry Pi 4 or any Linux host.  

   
    Keeps secrets out of code



## Features
- Pulls RTSP from Reolink over TCP for stability
- Transcodes to 720p H.264 + AAC (Pi 4 friendly)
- Adds silent stereo audio so YouTube always detects audio
- Auto-creates/rotates broadcasts every `ROTATION_HOURS` (default 12)
- Reuses an existing bound broadcast if found (prevents duplicated on restarts)
- Starts FFmpeg first, waits for YouTube ingest to be active, then transitions testing→live
- Title format: "weather stream – mm/dd/yy (Morning|Afternoon)"
- Restarts FFmpeg if it crashes; early-rotates after repeated failures
- No secrets in code: stream key, camera creds, API tokens in private files 

---
#Requirements
- **Hardware**
  - Raspberry Pi 4 (4GB or 8GB recommended)
  - microSD card (32GB+)
  - Stable power supply
  - Ethernet or strong Wi-Fi

- **Software**
  - Raspberry Pi OS (64-bit recommended)
  - `ffmpeg`
  - Python 3.9+
  - `pip` + virtualenv
  - Google API client libraries
  - `systemd` (for process management)
  - Google Cloud project with **YouTube Data API v3** enabled
  - A **YouTube reusable stream** in YouTube Studio (for a stable stream key)

## 2) Project Layout
    weather-stream/
        .env.example
        ffmpeg_args.py
        stream_orchestrator.py
        titles.py
        youtube_api.py
        requirements.txt
        systemd/
            weather-stream.service
---

## 3) Install
Replace /home/<YOURUSER>/weather-stream with your preferred path.
Recommendation: Use absolute paths in .env to avoid systemd expansion issues.

    ```bash
    # choose a base path
    mkdir -p /home/<YOURUSER>/weather-stream
    cd /home/<YOURUSER>/weather-stream
    
    # copy project files into this directory
    
    # create venv + install deps
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

    # log directory (optional, created on first run too)
    mkdir -p logs

## 4) Reolink Camera Config
    Set camera to H.264, CBR, 20–30 fps. (recommend 20 if camera is on wifi)

    RTSP URLs:

        Main (high bitrate/4K): rtsp://USER:PASS@CAM_IP:554/h264Preview_01_main

        Sub (lower bitrate): rtsp://USER:PASS@CAM_IP:554/h264Preview_01_sub

    Default pipeline transcodes to 720p for stability on a Pi. If you switch to the sub-stream and it’s YouTube-compatible H.264, you can try -c:v copy in ffmpeg_args.py.

## 5) YouTube OAuth (authorize off the Raspberry Pi)
    Do this once on a laptop/desktop with a browser, then copy the token to the Pi.

    1.  In Google Cloud Console:
        Enable YouTube Data API v3
        Create an OAuth client of type Desktop application
        Download client_secret.json
    
    2.  On your laptop/desktop:
        ```bash
        mkdir -p ~/yt-auth && cd ~/yt-auth
        python3 -m venv .venv
        source .venv/bin/activate
        pip install google-api-python-client google-auth google-auth-oauthlib

        # put client_secret.json in this folder
        python - << ''PY''
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/youtube"]
        flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
        creds = flow.run_local_server(port=0) # opens your browser
        open("token.json","w").write(creds.to_json())
        print("Auth complete. token.json written.")
        PY'

    3.  Copy `client_secret.json` and `token.json` to the Pi:
        ```bash
        scp client_secret.json token.json <YOURUSER>@<YOUR_PI_HOST>:/home/<YOURUSER>/weather-stream/
        ssh <YOURUSER>@<YOUR_PI_HOST> "chmod 600 /home/<YOURUSER>/weather-stream/token.json"
## 6) Stream Key (private file)
    Create a user-only file that contains only your reusable YouTube stream key (no quotes/newline preferred):
    ```bash
    install -d -m 700 /home/<YOURUSER>/.config/weather-stream
    printf '%s' 'YOUR_YT_STREAM_KEY' > /home/<YOURUSER>/.config/weather-stream/yt_key
    chmod 600 /home/<YOURUSER>/.config/weather-stream/yt_key
## 7) Configure .env
    Create .env from the example, and use absolute paths (Using absolute paths avoids ${BASE_DIR} expansion pitfalls with systemd.):

    ```bash    
    cp sample.env .env
    nano .env

    Minimum fields to set:
        BASE_DIR=/home/<YOURUSER>/weather-stream
        LOCAL_TZ=America/Chicago

        # YouTube API files (copied in step 5)
        GOOGLE_CLIENT_SECRETS=/home/<YOURUSER>/weather-stream/client_secret.json
        GOOGLE_TOKEN_FILE=/home/<YOURUSER>/weather-stream/token.json

        # Optional reusable stream ID (from YouTube Studio). Leave blank to auto-create.
        YT_STREAM_ID=

        # RTMP ingest
        RTMP_INGEST=rtmp://a.rtmp.youtube.com/live2

        # Your private key file (from step 6)
        YT_STREAM_KEY_FILE=/home/<YOURUSER>/.config/weather-stream/yt_key

        # Camera RTSP URL
        RTSP_URL=rtsp://USER:PASS@CAM_IP:554/h264Preview_01_main

        # Video/audio and rotation defaults are fine to start

## 8) Test run (foreground)
        ```bash
        cd /home/<YOURUSER>/weather-stream
        source .venv/bin/activate
        python stream_orchestrator.py

    You should see logs when:
        FFmpeg starts
        Stream status becomes active
        Broadcast transitions to testing then live
    
    Stop with Ctrl+C.

## 9) Install as systemd service
        ```bash
        sudo cp systemd/weather-stream.service /etc/systemd/system/weather-stream.service
        sudoedit /etc/systemd/system/weather-stream.service

    Set User, WorkingDirectory, EnvironmentFile, and ExecStart to your paths, e.g.:
            [Unit]
            Description=Weather RTSP to YouTube 12h Rotator
            After=network-online.target
            Wants=network-online.target

            [Service]
            User=<YOURUSER>
            WorkingDirectory=/home/<YOURUSER>/weather-stream
            EnvironmentFile=/home/<YOURUSER>/weather-stream/.env
            ExecStart=/home/<YOURUSER>/weather-stream/.venv/bin/python /home/<YOURUSER>/weather-stream/stream_orchestrator.py
            Restart=always
            RestartSec=10
            NoNewPrivileges=true
            PrivateTmp=true
            ProtectSystem=full
            # If your secrets/logs live under /home, use read-only:
            # ProtectHome=read-only
            # (If ProtectHome=true, the service cannot read files under /home.)

            [Install]
            WantedBy=multi-user.target
   
    Enable & start:
            ```bash
            sudo systemctl daemon-reload
            sudo systemctl enable --now weather-stream.service
            journalctl -u weather-stream.service -f

## 10) Log rotation (FFmpeg logs)
    Create a logrotate rule (adjust path if your LOG_DIR differs):
        ```bash
        sudo tee /etc/logrotate.d/weather-stream >/dev/null <<'EOF'
        /home/<YOURUSER>/weather-stream/logs/*.log {
        daily
        rotate 14
        size 25M
        missingok
        notifempty
        compress
        delaycompress
        copytruncate
        dateext
        dateformat -%Y%m%d
        }
        EOF

    # test it
        ```bash
        sudo logrotate -d /etc/logrotate.d/weather-stream   # dry run
        sudo logrotate -f /etc/logrotate.d/weather-stream   # force now

    Optional: cap journal size
        ```bash
        sudo bash -c 'cat >/etc/systemd/journald.conf.d/99-weather.conf <<EOF
        [Journal]
        SystemMaxUse=200M
        SystemMaxFileSize=20M
        RuntimeMaxUse=100M
        EOF'
        sudo systemctl restart systemd-journald
        # one-time prune:
        sudo journalctl --vacuum-time=14d

## 11) How it works (quick internals)

    Titles generated with zoneinfo → weather stream – mm/dd/yy (Morning|Afternoon)

    Orchestrator:

        Reuses an existing broadcast bound to your reusable stream if found (live/testing/ready/created)

        Starts FFmpeg → polls stream until active → transitions testing → live

        Runs for ROTATION_HOURS (default 12), then completes the broadcast and starts a new cycle

        Restarts FFmpeg on exit; early-rotates after repeated failures

    Secrets:

        Stream key only in YT_STREAM_KEY_FILE (0600)

        OAuth files (client_secret.json, token.json) stored locally; token is created on your laptop, not the Pi

## 12) Tuning & tips

    Bitrate/Resolution: adjust VIDEO_BITRATE, VIDEO_WIDTH, VIDEO_HEIGHT in .env

    Keyframe interval: KEYINT=fps*2 (e.g., 60 for 30 fps) for YouTube

    Sub-stream: if using *_sub, try -c:v copy in ffmpeg_args.py to avoid re-encode

    “More than one ingestion” warning: ensure only one orchestrator/FFmpeg is running to Primary; stop other instances. (Backup ingest is optional and off by default.)

## 13) Troubleshooting

    Invalid transition (403): Usually means YouTube didn’t see ingest yet. The orchestrator already waits for streamStatus=active before transitions. If you still see it, increase the pre-roll: change start_time = now + 3 minutes in the code.

    token.json not found or ${BASE_DIR} literal in logs: Use absolute paths in .env as shown above.

    No audio error in Studio: The pipeline injects silent stereo via anullsrc; confirm audio args in ffmpeg_args.py.

    Frequent reconnects: Lower VIDEO_BITRATE, keep -rtsp_transport tcp, and check Wi-Fi strength to the camera.

## 14) Requirements
    google-api-python-client==2.141.0
    google-auth==2.34.0
    google-auth-oauthlib==1.2.1
    python-dotenv==1.0.1
