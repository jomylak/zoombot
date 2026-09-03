import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(key, default):
    return int(os.getenv(key, default))


ICS_URL = os.getenv("ICS_URL", "")
AUTOJOIN_SUBJECT_TAG = os.getenv("AUTOJOIN_SUBJECT_TAG", "[AutoJoin]")

LOOKAHEAD_MINUTES = _int("LOOKAHEAD_MINUTES", 120)
POLL_INTERVAL_MINUTES = _int("POLL_INTERVAL_MINUTES", 5)
JOIN_LEAD_MINUTES = _int("JOIN_LEAD_MINUTES", 2)

ZOOM_DISPLAY_NAME = os.getenv("ZOOM_DISPLAY_NAME", "")
ZOOM_EMAIL = os.getenv("ZOOM_EMAIL", "")

MIN_PARTICIPANTS = _int("MIN_PARTICIPANTS", 3)
MAX_SESSION_MINUTES = _int("MAX_SESSION_MINUTES", 180)
JOIN_TIMEOUT_MINUTES = _int("JOIN_TIMEOUT_MINUTES", 10)
SESSION_CHECK_INTERVAL_HOURS = _int("SESSION_CHECK_INTERVAL_HOURS", 48)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

ATTENDANCE_CHECK_ENABLED = os.getenv("ATTENDANCE_CHECK_ENABLED", "true").lower() == "true"
ATTENDANCE_KEYWORDS = os.getenv(
    "ATTENDANCE_KEYWORDS",
    "attendance,qr code,scan code,scan the code,check-in,checkin")
ATTENDANCE_ALERT_COOLDOWN_MINUTES = _int("ATTENDANCE_ALERT_COOLDOWN_MINUTES", 10)

STATE_DIR = Path(os.path.expanduser(os.getenv("STATE_DIR", "~/.zoombot")))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = STATE_DIR / "attendance.db"
ZOOM_STATE = STATE_DIR / "zoom_state.json"
SHOTS_DIR = STATE_DIR / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)
