"""One-time interactive Zoom login. Saves cookies for the joiner to reuse.

Run this on a machine with a screen (or over VNC / X-forwarding to the Pi):

    python -m bot.zoom_login

Log in with your Zoom email + password, tick "Stay signed in", and once you
land on the Zoom home page press Enter in the terminal.
"""
from playwright.sync_api import sync_playwright
from . import config


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, channel=None,
            executable_path=_chromium_path(),
            # Google's OAuth login blocks sign-in ("This browser or app may
            # not be secure") when it detects navigator.webdriver / the
            # automation banner -- both are standard Playwright/CDP tells.
            # Suppressing them here is what actually gets us past that wall.
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://zoom.us/signin", timeout=60_000)
        print("\nLog in in the browser window, then press Enter here...")
        input()
        ctx.storage_state(path=str(config.ZOOM_STATE))
        config.ZOOM_STATE.chmod(0o600)
        print("Saved Zoom session to", config.ZOOM_STATE)
        browser.close()


def _chromium_path():
    """Playwright ships no arm64 Chromium -- use the system package."""
    import shutil
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None  # fall back to Playwright's bundled build (x86 only)


if __name__ == "__main__":
    main()
