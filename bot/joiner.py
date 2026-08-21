"""Joins one Zoom meeting, sits in it, leaves, records the result.

Run as a subprocess (one per meeting) so a crash or a memory leak can never
take down the scheduler:

    python -m bot.joiner <event_id>
"""
import sys
import time
import logging
import datetime as dt
import shutil

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from . import config, db, notify

log = logging.getLogger("joiner")

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",      # auto-accept mic/cam prompts
    "--use-fake-device-for-video-capture",  # synthetic camera, no hardware
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",              # /dev/shm is tiny on the Pi
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
]

# Zoom rewrites its web client fairly often. Each of these is a list of
# candidates tried in order -- when a join breaks, this is the first place
# to look. Grab a screenshot from ~/.zoombot/screenshots to see what changed.
SEL_NAME_INPUT = ["#input-for-name", "input[placeholder*='name' i]", "#inputname"]
SEL_PASSCODE = ["#input-for-pwd", "input[type='password']"]
SEL_JOIN_BTN = ["button:has-text('Join')", "#joinBtn", ".preview-join-button"]
SEL_JOIN_FROM_BROWSER = ["button:has-text('Join from browser')",
                         ":text('Join from browser')"]
SEL_CONTINUE_WITHOUT_MEDIA = ["button:has-text('Continue without microphone and camera')",
                              "button:has-text('Continue without')"]
SEL_IN_MEETING = ["#foot-bar", ".footer-participants-button",
                  "[aria-label*='open the participants' i]", ".meeting-info-icon"]
SEL_WAITING_ROOM = [":text('Please wait')", ":text('waiting room')",
                    ":text('host will let you in')",
                    ":text('Host has joined')",
                    ":text('We've let them know')"]
SEL_LEAVE_BTN = ["button:has-text('Leave')", ".footer__leave-btn"]
SEL_LOGIN_WALL = ["input[type='password'][name='password']", ":text('Sign In to Join')"]
SEL_PARTICIPANT_COUNT = [".footer-button__number-counter",
                         "[aria-label*='open the participants' i] span"]


def _first(page, selectors, timeout=3000):
    """Return the first selector that resolves, else None."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                return el
        except PWTimeout:
            continue
    return None


def _visible(page, selectors) -> bool:
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    return False


def _chromium_path():
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _shot(page, event_id, tag):
    try:
        p = config.SHOTS_DIR / f"{event_id[:24]}_{tag}_{int(time.time())}.png"
        page.screenshot(path=str(p))
        return p
    except Exception:
        return None


def _participant_count(page):
    for sel in SEL_PARTICIPANT_COUNT:
        try:
            txt = page.locator(sel).first.inner_text(timeout=1000).strip()
            if txt.isdigit():
                return int(txt)
        except Exception:
            continue
    return None


def run(event_id: str) -> int:
    rec = db.get(event_id)
    if not rec:
        log.error("no such event %s", event_id)
        return 2

    subject = rec["subject"]
    url = rec["join_url"]
    end_at = dt.datetime.fromisoformat(rec["scheduled_end"])
    hard_stop = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=config.MAX_SESSION_MINUTES)
    deadline = min(end_at, hard_stop)

    db.set_status(event_id, "joining")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS,
                                    executable_path=_chromium_path())
        ctx = browser.new_context(
            storage_state=str(config.ZOOM_STATE) if config.ZOOM_STATE.exists() else None,
            viewport={"width": 1024, "height": 700},  # small = cheaper on a Pi 4
            permissions=["microphone", "camera"],
        )
        page = ctx.new_page()
        try:
            log.info("navigating: %s", url)
            page.goto(url, timeout=90_000, wait_until="domcontentloaded")
            page.wait_for_timeout(5_000)

            if _visible(page, SEL_LOGIN_WALL):
                _shot(page, event_id, "loginwall")
                raise RuntimeError(
                    "Zoom is asking us to sign in -- saved session expired. "
                    "Re-run: python -m bot.zoom_login")

            # Registration links land on an intermediate "which app" chooser
            # before the actual join screen -- direct /wc/ links skip this.
            chooser = _first(page, SEL_JOIN_FROM_BROWSER, timeout=5_000)
            if chooser:
                log.info("hit the app-vs-browser chooser, clicking 'Join from browser'")
                chooser.click()
                page.wait_for_timeout(2_000)

            # Zoom sometimes shows up to two device-permission prompts
            # ("see you" then "hear you") before the name field is usable.
            # Dismiss without granting -- the bot doesn't need mic/camera.
            for _ in range(2):
                dismiss = _first(page, SEL_CONTINUE_WITHOUT_MEDIA, timeout=3_000)
                if not dismiss:
                    break
                log.info("dismissing a device-permission prompt")
                dismiss.click()
                page.wait_for_timeout(1_000)

            el = _first(page, SEL_NAME_INPUT, timeout=15_000)
            if el:
                el.fill(config.ZOOM_DISPLAY_NAME)

            if rec["link_source"] != "registration":
                pw_el = _first(page, SEL_PASSCODE, timeout=3_000)
                if pw_el and rec.get("passcode"):
                    pw_el.fill(rec["passcode"])

            btn = _first(page, SEL_JOIN_BTN, timeout=10_000)
            if btn:
                btn.click()

            # Wait to actually land in the meeting -- may sit in a waiting room.
            join_deadline = time.time() + config.JOIN_TIMEOUT_MINUTES * 60
            while time.time() < join_deadline:
                if _visible(page, SEL_IN_MEETING):
                    break
                if _visible(page, SEL_WAITING_ROOM):
                    log.info("in waiting room, holding...")
                page.wait_for_timeout(5_000)
            else:
                _shot(page, event_id, "jointimeout")
                raise RuntimeError(
                    f"never got in within {config.JOIN_TIMEOUT_MINUTES}m "
                    "(waiting room never opened?)")

            joined_at = dt.datetime.now(dt.timezone.utc)
            db.set_status(event_id, "in_meeting", joined_at=joined_at.isoformat())
            _shot(page, event_id, "joined")
            notify.push("Joined", f"{subject}", tags="white_check_mark")
            log.info("in meeting: %s", subject)

            exit_reason = "scheduled_end"
            low_streak = 0
            near_end_shot_taken = False
            NEAR_END_WINDOW = dt.timedelta(minutes=5)

            while dt.datetime.now(dt.timezone.utc) < deadline:
                page.wait_for_timeout(30_000)

                if not _visible(page, SEL_IN_MEETING):
                    exit_reason = "host_ended_or_dropped"
                    break

                # One shot ~5 min before scheduled end, only while still
                # genuinely in the meeting -- if it already ended, the
                # break above already exited the loop before this runs.
                if (not near_end_shot_taken
                        and deadline - dt.datetime.now(dt.timezone.utc) <= NEAR_END_WINDOW):
                    _shot(page, event_id, "near_end")
                    near_end_shot_taken = True
                    log.info("near-end screenshot taken for %s", subject)

                n = _participant_count(page)
                if n is not None and n < config.MIN_PARTICIPANTS:
                    low_streak += 1
                    if low_streak >= 3:  # ~90s sustained, not a blip
                        exit_reason = f"participants_below_{config.MIN_PARTICIPANTS}"
                        break
                else:
                    low_streak = 0

            leave = _first(page, SEL_LEAVE_BTN, timeout=3_000)
            if leave:
                try:
                    leave.click()
                    page.wait_for_timeout(1_500)
                    lb = page.locator("button:has-text('Leave Meeting')").first
                    if lb.is_visible(timeout=2_000):
                        lb.click()
                except Exception:
                    pass

            left_at = dt.datetime.now(dt.timezone.utc)
            mins = round((left_at - joined_at).total_seconds() / 60, 1)
            db.set_status(event_id, "done", left_at=left_at.isoformat(),
                          exit_reason=exit_reason)
            notify.push("Left", f"{subject} -- {mins} min ({exit_reason})")
            log.info("done: %s (%s min, %s)", subject, mins, exit_reason)
            return 0

        except Exception as e:
            log.exception("join failed")
            _shot(page, event_id, "error")
            db.set_status(event_id, "failed", error=str(e)[:500])
            notify.push("JOIN FAILED", f"{subject}\n{e}", priority="high", tags="rotating_light")
            return 1
        finally:
            try:
                ctx.close(); browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init()
    sys.exit(run(sys.argv[1]))