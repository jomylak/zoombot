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
# Generic alias -- used by joins on any platform, not just Zoom. Keeps the
# existing env var name since that's what's already in people's .env.
DISPLAY_NAME = ZOOM_DISPLAY_NAME

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() == "true" and bool(OPENROUTER_API_KEY)
AGENT_MAX_STEPS = _int("AGENT_MAX_STEPS", 20)
AGENT_STEP_TIMEOUT_S = _int("AGENT_STEP_TIMEOUT_S", 25)

MIN_PARTICIPANTS = _int("MIN_PARTICIPANTS", 3)
MAX_SESSION_MINUTES = _int("MAX_SESSION_MINUTES", 180)
JOIN_TIMEOUT_MINUTES = _int("JOIN_TIMEOUT_MINUTES", 10)
SESSION_CHECK_INTERVAL_HOURS = _int("SESSION_CHECK_INTERVAL_HOURS", 48)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NOTIFY_PAUSED = os.getenv("NOTIFY_PAUSED", "false").lower() == "true"
NOTIFY_MAX_PER_WINDOW = _int("NOTIFY_MAX_PER_WINDOW", 6)
NOTIFY_WINDOW_MINUTES = _int("NOTIFY_WINDOW_MINUTES", 10)
NOTIFY_DEDUPE_MINUTES = _int("NOTIFY_DEDUPE_MINUTES", 30)

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

PLATFORM_STATE_DIR = STATE_DIR / "state"
PLATFORM_STATE_DIR.mkdir(exist_ok=True)

# Shared with the Pi's other Chromium user (ApplyPilot's pi_enrich_runner.py)
# so the two never launch a browser at the same time on a memory-constrained
# Pi. Same env var name / default path on both sides so they agree on one
# lock file without either needing the other's config. BROWSER_YIELD_PATH is
# a second, separate flag: touched right before this bot blocks on the lock
# so the enrichment side can bail out of its current batch early instead of
# grinding through it -- see joiner.py's _acquire_browser_lock().
BROWSER_LOCK_PATH = Path(os.path.expanduser(
    os.getenv("BROWSER_LOCK_PATH", "~/.applypilot/browser.lock")))
BROWSER_YIELD_PATH = Path(os.path.expanduser(
    os.getenv("BROWSER_YIELD_PATH", "~/.applypilot/browser_yield_requested")))


def platform_state_path(platform: str) -> Path:
    """Per-platform saved browser storage_state (cookies), kept separate so
    one platform's session can't collide with another's. Zoom keeps its
    original path for backward compatibility with existing setups."""
    if platform == "zoom":
        return ZOOM_STATE
    return PLATFORM_STATE_DIR / f"{platform}.json"
