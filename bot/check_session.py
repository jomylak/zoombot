"""Periodic check that the saved Zoom login is still valid.

Catches an expired session on its own schedule, independent of whether any
meeting is due -- joiner.py already notifies on a login-wall failure, but
only when a real meeting tries (and fails) to join. This closes the gap
between those tries so an expired session gets caught before it costs a
missed meeting.

Runs standalone:

    python -m bot.check_session

Or gets called every SESSION_CHECK_INTERVAL_HOURS by the scheduler.
"""
import logging
import shutil

from playwright.sync_api import sync_playwright

from . import config, notify

log = logging.getLogger("check_session")

PROFILE_URL = "https://zoom.us/profile"


def _chromium_path():
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def check() -> bool:
    """Returns True if the saved Zoom session is still authenticated."""
    if not config.ZOOM_STATE.exists():
        log.warning("no saved Zoom session (zoom_state.json missing)")
        notify.push("Zoom session missing",
                     "No saved login -- run: python -m bot.zoom_login",
                     priority="high", tags="rotating_light")
        return False

    signed_in = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"],
                                    executable_path=_chromium_path())
        try:
            ctx = browser.new_context(storage_state=str(config.ZOOM_STATE))
            page = ctx.new_page()
            page.goto(PROFILE_URL, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            signed_in = "signin" not in page.url and "login" not in page.url
        except Exception as e:
            log.warning("session check failed to load: %s", e)
        finally:
            browser.close()

    if signed_in:
        log.info("Zoom session still valid")
    else:
        log.warning("Zoom session expired")
        notify.push("Zoom session expired",
                     "Saved login no longer works -- run: python -m bot.zoom_login",
                     priority="high", tags="rotating_light")
    return signed_in


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print("OK" if check() else "EXPIRED")
