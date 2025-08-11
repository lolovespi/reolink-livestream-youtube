from datetime import datetime
from zoneinfo import ZoneInfo  # built-in since Python 3.9

def make_title(tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    tod = "Morning" if now.hour < 12 else "Afternoon"
    stamp = now.strftime("%m/%d/%y")
    return f"weather stream – {stamp} ({tod})"
