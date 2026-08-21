import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(key, default):
    return int(os.getenv(key, default))


MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
AUTOJOIN_CATEGORY = os.getenv("AUTOJOIN_CATEGORY", "AutoJoin")

LOOKAHEAD_MINUTES = _int("LOOKAHEAD_MINUTES", 120)
POLL_INTERVAL_MINUTES = _int("POLL_INTERVAL_MINUTES", 5)
JOIN_LEAD_MINUTES = _int("JOIN_LEAD_MINUTES", 2)

ZOOM_DISPLAY_NAME = os.getenv("ZOOM_DISPLAY_NAME", "")
ZOOM_EMAIL = os.getenv("ZOOM_EMAIL", "")

MIN_PARTICIPANTS = _int("MIN_PARTICIPANTS", 3)
MAX_SESSION_MINUTES = _int("MAX_SESSION_MINUTES", 180)
JOIN_TIMEOUT_MINUTES = _int("JOIN_TIMEOUT_MINUTES", 10)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

STATE_DIR = Path(os.path.expanduser(os.getenv("STATE_DIR", "~/.zoombot")))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = STATE_DIR / "attendance.db"
MSAL_CACHE = STATE_DIR / "msal_cache.json"
ZOOM_STATE = STATE_DIR / "zoom_state.json"
SHOTS_DIR = STATE_DIR / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)
