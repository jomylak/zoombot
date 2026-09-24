import json
import logging
import time
from pathlib import Path

import requests
from . import config

log = logging.getLogger(__name__)
_RATE_FILE = config.STATE_DIR / "notify_rate.json"


def _allow(title: str, message: str) -> bool:
    """Rate limit shared by every process (main + joiners) and surviving
    restarts: identical alerts are dropped for NOTIFY_DEDUPE_MINUTES, and at
    most NOTIFY_MAX_PER_WINDOW pushes go out per NOTIFY_WINDOW_MINUTES."""
    if config.NOTIFY_PAUSED:
        return False
    now = time.time()
    try:
        sent = json.loads(_RATE_FILE.read_text())
    except Exception:
        sent = []  # [[ts, key], ...]
    sent = [e for e in sent if now - e[0] < max(config.NOTIFY_DEDUPE_MINUTES,
                                                config.NOTIFY_WINDOW_MINUTES) * 60]
    key = f"{title}|{message}"
    recent = [e for e in sent if now - e[0] < config.NOTIFY_WINDOW_MINUTES * 60]
    ok = (len(recent) < config.NOTIFY_MAX_PER_WINDOW and
          not any(e[1] == key and now - e[0] < config.NOTIFY_DEDUPE_MINUTES * 60 for e in sent))
    if ok:
        sent.append([now, key])
    try:
        _RATE_FILE.write_text(json.dumps(sent))
    except Exception:
        pass
    return ok


def push(title: str, message: str, priority: str = "default", tags: str = ""):
    """Best-effort push notification. Never raises -- a failed notification
    must not take down a join."""
    if not config.NTFY_TOPIC:
        log.info("NOTIFY [%s] %s", title, message)
        return
    if not _allow(title, message):
        log.warning("NOTIFY suppressed (paused/rate-limited): [%s] %s", title, message)
        return
    _post(title, message, priority, tags)


def _post(title, message, priority, tags):
    try:
        requests.post(
            f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception as e:
        log.warning("ntfy push failed: %s", e)


def push_with_attachment(title: str, message: str, filepath: Path,
                         priority: str = "urgent", tags: str = ""):
    """Best-effort push notification with an image attached, so it can be
    viewed straight from the notification with no extra login. Falls back to
    a plain push() if the attachment upload fails."""
    if not config.NTFY_TOPIC:
        log.info("NOTIFY [%s] %s (attachment: %s)", title, message, filepath)
        return
    if not _allow(title, message):
        log.warning("NOTIFY suppressed (paused/rate-limited): [%s] %s", title, message)
        return
    try:
        with open(filepath, "rb") as f:
            requests.put(
                f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}",
                data=f,
                headers={
                    "Title": title,
                    "Message": message,
                    "Priority": priority,
                    "Tags": tags,
                    "Filename": Path(filepath).name,
                },
                timeout=30,
            )
    except Exception as e:
        log.warning("ntfy push_with_attachment failed: %s", e)
        _post(title, message, priority, tags)
