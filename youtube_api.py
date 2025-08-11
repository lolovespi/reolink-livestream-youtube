# youtube_api.py
import os
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _creds(client_secret_path: str, token_path: str) -> Credentials:
    """
    Headless-friendly auth:
    - Uses token.json if present (refreshes as needed).
    - If no valid token is found, we DO NOT try to open a browser on the Pi.
      Instead, we raise with clear instructions to authorize on another machine.
    - If you *really* want to launch a local-server browser flow here, set
      YT_ALLOW_LOCAL_SERVER=1 and run this on a machine with a browser.
    """
    creds = None

    # Use existing token if present
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            return creds

    # No valid token -> default to headless-safe error with instructions
    if os.environ.get("YT_ALLOW_LOCAL_SERVER", "0") == "1":
        # Only use this on a machine with a browser
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        return creds

    raise RuntimeError(
        "YouTube OAuth token not found or invalid at:\n"
        f"  {token_path}\n\n"
        "This project is configured for headless use. To authorize:\n"
        "  1) On a laptop/desktop with a browser, create a venv and install deps:\n"
        "       python -m venv .venv && source .venv/bin/activate && \\\n"
        "       pip install google-api-python-client google-auth google-auth-oauthlib\n"
        "  2) Put your client_secret.json alongside, then run this Python snippet:\n"
        "       from google_auth_oauthlib.flow import InstalledAppFlow\n"
        "       SCOPES=['https://www.googleapis.com/auth/youtube']\n"
        "       flow=InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)\n"
        "       creds=flow.run_local_server(port=0)\n"
        "       open('token.json','w').write(creds.to_json())\n"
        "  3) Copy token.json to the Pi at the path above and chmod 600 it.\n"
        "See README.md for copy/paste commands."
    )


def _svc(client_secret_path: str, token_path: str):
    creds = _creds(client_secret_path, token_path)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def ensure_stream(service, stream_id: str | None) -> str:
    """
    Reuse an existing stream ID if provided; otherwise create a new RTMP stream.
    NOTE: API does not expose the stream KEY. Create a reusable stream in Studio
    and store the key outside of code.
    """
    if stream_id:
        return stream_id
    body = {
        "snippet": {"title": "Pi Weather – Ingest"},
        "cdn": {"frameRate": "30fps", "resolution": "720p", "ingestionType": "rtmp"},
    }
    resp = service.liveStreams().insert(
        part="snippet,cdn,contentDetails", body=body
    ).execute()
    return resp["id"]


def create_broadcast(service, title: str, scheduled_start_iso: str, privacy="public") -> str:
    body = {
        "snippet": {"title": title, "scheduledStartTime": scheduled_start_iso},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        "contentDetails": {"enableAutoStart": False, "enableAutoStop": False},
    }
    resp = service.liveBroadcasts().insert(
        part="snippet,status,contentDetails", body=body
    ).execute()
    return resp["id"]


def bind_broadcast_to_stream(service, broadcast_id: str, stream_id: str):
    service.liveBroadcasts().bind(
        part="id,contentDetails", id=broadcast_id, streamId=stream_id
    ).execute()


def transition_broadcast(service, broadcast_id: str, status: str):
    # status: "testing" -> "live" -> "complete"
    service.liveBroadcasts().transition(
        part="status", id=broadcast_id, broadcastStatus=status
    ).execute()


def get_stream_status(service, stream_id: str) -> tuple[str | None, str | None]:
    """
    Returns (streamStatus, healthStatus), e.g. ('active'|'inactive'|'error', 'good'|'ok'|'bad'|None).
    """
    resp = service.liveStreams().list(part="status", id=stream_id).execute()
    items = resp.get("items", [])
    if not items:
        return None, None
    s = items[0].get("status", {}) or {}
    return s.get("streamStatus"), (s.get("healthStatus") or {}).get("status")


def get_broadcast_lifecycle(service, broadcast_id: str) -> str | None:
    """
    Returns broadcast lifeCycleStatus: 'created'|'ready'|'testing'|'live'|'complete'.
    """
    resp = service.liveBroadcasts().list(part="status", id=broadcast_id).execute()
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["status"].get("lifeCycleStatus")


def list_all_broadcasts(service):
    """
    Returns ALL broadcasts for the authenticated channel (paged), using mine=true.
    We avoid 'broadcastStatus' here due to API incompatibilities on some accounts.
    """
    items = []
    req = service.liveBroadcasts().list(
        part="id,snippet,contentDetails,status", mine=True, maxResults=50
    )
    while req is not None:
        resp = req.execute()
        items.extend(resp.get("items", []))
        tok = resp.get("nextPageToken")
        if tok:
            req = service.liveBroadcasts().list(
                part="id,snippet,contentDetails,status",
                mine=True,
                maxResults=50,
                pageToken=tok,
            )
        else:
            req = None
    return items


def find_reusable_broadcast(service, stream_id: str):
    """
    Look for a broadcast already bound to `stream_id` that we can reuse.
    Preference order:
      1) active (lifeCycleStatus == 'live')
      2) upcoming in 'testing'
      3) upcoming in 'ready'
      4) upcoming in 'created'
    Returns dict with keys: id, lifeCycleStatus, scheduledStartTime (may be None), or None.
    """

    def best_of(items):
        # rank lifecycle: live > testing > ready > created
        rank = {"live": 4, "testing": 3, "ready": 2, "created": 1}
        best = None
        best_score = -1
        for it in items:
            cd = it.get("contentDetails", {}) or {}
            if cd.get("boundStreamId") != stream_id:
                continue
            st = it.get("status", {}) or {}
            lc = st.get("lifeCycleStatus")
            if lc not in ("live", "testing", "ready", "created"):
                continue  # ignore completed, revoked, etc.
            score = rank.get(lc, 0)
            if score > best_score:
                best_score = score
                best = {
                    "id": it["id"],
                    "lifeCycleStatus": lc,
                    "scheduledStartTime": (it.get("snippet", {}) or {}).get(
                        "scheduledStartTime"
                    ),
                }
        return best

    all_items = list_all_broadcasts(service)

    # Pick best candidate across all statuses
    candidate = best_of(all_items)
    return candidate

def get_stream_ingestion_key(service, stream_id: str) -> str | None:
    """Returns ingestionInfo.streamName (the stream key) for a given stream."""
    resp = service.liveStreams().list(part="cdn", id=stream_id).execute()
    items = resp.get("items", [])
    if not items:
        return None
    cdn = items[0].get("cdn") or {}
    ing = cdn.get("ingestionInfo") or {}
    return ing.get("streamName")


def find_stream_by_key(service, stream_key: str) -> str | None:
    """Find your stream ID whose ingestion key matches stream_key."""
    req = service.liveStreams().list(part="id,cdn", mine=True, maxResults=50)
    while req is not None:
        resp = req.execute()
        for s in resp.get("items", []):
            cdn = s.get("cdn") or {}
            ing = cdn.get("ingestionInfo") or {}
            if ing.get("streamName") == stream_key:
                return s["id"]
        tok = resp.get("nextPageToken")
        req = service.liveStreams().list(part="id,cdn", mine=True, maxResults=50, pageToken=tok) if tok else None
    return None
