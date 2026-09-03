import logging
from pathlib import Path

import requests
from . import config

log = logging.getLogger(__name__)


def push(title: str, message: str, priority: str = "default", tags: str = ""):
    """Best-effort push notification. Never raises -- a failed notification
    must not take down a join."""
    if not config.NTFY_TOPIC:
        log.info("NOTIFY [%s] %s", title, message)
        return
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
        push(title, message, priority=priority, tags=tags)
