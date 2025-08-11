# Weather Stream (Reolink → FFmpeg → YouTube, 12h auto-rotation)

Continuous livestream from a Reolink (e.g., **RLC‑810WA**) to YouTube, running on a Raspberry Pi 4 or any Linux host.  
Creates a **new broadcast every 12 hours** with title format:


## Features
- Pulls RTSP from Reolink over **TCP** for stability
- Transcodes to **720p H.264 + AAC** (Pi 4 friendly)
- **Adds silent stereo audio** so YouTube always detects audio
- Auto-creates/rotates broadcasts every `ROTATION_HOURS` (default 12)
- Restarts FFmpeg if it crashes; early-rotates after repeated failures
- **No secrets in code**: stream key is in a **private file**, and API tokens/config are outside VCS

---

## 1) Prereqs
- Python 3.9+ (Pi OS Bookworm works)
- FFmpeg (`sudo apt install -y ffmpeg`)
- Google Cloud project with **YouTube Data API v3** enabled
- A **YouTube reusable stream** in YouTube Studio (for a stable stream key)

---

## 2) Install
```bash
# choose a base path
sudo mkdir -p /srv/weather-stream
sudo chown $USER:$USER /srv/weather-stream
cd /srv/weather-stream

# bring in the project files (copy/paste or git clone your fork)
# then create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# logs directory
mkdir -p logs
