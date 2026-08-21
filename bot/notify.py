import logging
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
