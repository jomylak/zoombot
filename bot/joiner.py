"""Joins one Zoom meeting, sits in it, leaves, records the result.

Run as a subprocess (one per meeting) so a crash or a memory leak can never
take down the scheduler:

    python -m bot.joiner <event_id>
"""
import sys
import time
import logging
import datetime as dt
import hashlib
import shutil
import subprocess

from playwright.sync_api import sync_playwright

from . import attendance_watch, config, db, notify

log = logging.getLogger("joiner")

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",      # auto-accept mic/cam prompts
    "--use-fake-device-for-media-stream",  # synthetic camera + mic, no real hardware needed
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",              # /dev/shm is tiny on the Pi
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
]

# Zoom rewrites its web client fairly often. Each of these is a list of
# candidates tried in order -- when a join breaks, this is the first place
# to look. Grab a screenshot from ~/.zoombot/screenshots to see what changed.
SEL_NAME_INPUT = ["#input-for-name", "input[placeholder*='name' i]", "#inputname",
                  "input[type='text']"]
SEL_PASSCODE = ["#input-for-pwd", "input[type='password']",
                "input[aria-label*='passcode' i]", "input[aria-label*='password' i]"]
SEL_JOIN_BTN = ["button:has-text('Join')", "#joinBtn", ".preview-join-button",
               "text=Join"]
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
# Unambiguous end-of-meeting states -- act on these immediately, no debounce
# needed, unlike the footer-bar check below which can false-positive on a
# transient re-render.
SEL_MEETING_ENDED = [":text('This meeting has been ended by')",
                     ":text('has ended')", ":text('Meeting ended')"]
SEL_REMOVED = [":text('You have been removed')", ":text('removed by the host')"]
SEL_LOGIN_WALL = ["input[type='password'][name='password']", ":text('Sign In to Join')"]
SEL_PARTICIPANT_COUNT = [".footer-button__number-counter",
                         "[aria-label*='open the participants' i] span"]


def _content_frames(page):
    """The pre-join and in-meeting UI render inside a nested iframe whose URL
    carries per-session join tokens (wpk=, _x_zm_rtaid=, ...) -- never the
    top-level document. page.locator()/wait_for_selector() only search the
    main frame, so every lookup has to walk all frames instead. Skip
    reCAPTCHA and blank placeholder frames; they never hold anything we want
    and just slow every check down."""
    return [f for f in page.frames if "recaptcha" not in f.url and f.url != "about:blank"]


def _first(page, selectors, timeout=3000):
    """Return the first selector that resolves in any content frame, else None."""
    for frame in _content_frames(page):
        for sel in selectors:
            try:
                el = frame.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
    return None


def _retry_for_any(page, selectors, timeout_s, poll_s=3, per_try_timeout=3000):
    """Keep re-scanning the whole selector list every few seconds until one
    resolves or timeout_s elapses -- a single _first() pass gives up for good
    once it's cycled through the list, even if the field just hadn't rendered
    yet. Also rides through transient errors (mid re-render, brief overlay)
    that would otherwise abort a one-shot wait_for_selector call."""
    deadline = time.time() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            el = _first(page, selectors, timeout=per_try_timeout)
        except Exception as e:
            log.info("retry attempt %d: transient error %s", attempt, e)
            el = None
        if el:
            return el
        if time.time() >= deadline:
            return None
        log.info("retry attempt %d: none of %s found yet, retrying in %ds",
                 attempt, selectors, poll_s)
        page.wait_for_timeout(poll_s * 1000)


def _visible(page, selectors) -> bool:
    for frame in _content_frames(page):
        for sel in selectors:
            try:
                if frame.locator(sel).first.is_visible(timeout=1000):
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


def _shot_id(event_id):
    """Exchange global object IDs share a long common header across every
    event on a calendar -- event_id[:24] was landing on that shared prefix,
    so screenshots from completely different meetings looked identical by
    filename. Hash the whole id instead for a short, actually-unique tag."""
    return hashlib.sha1(event_id.encode()).hexdigest()[:10]


def _shot(page, event_id, tag):
    try:
        p = config.SHOTS_DIR / f"{_shot_id(event_id)}_{tag}_{int(time.time())}.png"
        page.screenshot(path=str(p))
        _shot.last_taken = time.time()
        return p
    except Exception:
        return None


_shot.last_taken = 0


def _save_image(image, event_id, tag):
    try:
        p = config.SHOTS_DIR / f"{_shot_id(event_id)}_{tag}_{int(time.time())}.png"
        image.save(str(p))
        return p
    except Exception:
        return None


def _participant_count(page):
    for frame in _content_frames(page):
        for sel in SEL_PARTICIPANT_COUNT:
            try:
                txt = frame.locator(sel).first.inner_text(timeout=1000).strip()
                if txt.isdigit():
                    return int(txt)
            except Exception:
                continue
    return None




def _ensure_audio():
    """PulseAudio is socket-activated on this Pi and has been observed
    sitting dead or with its null-sink suspended between runs -- both
    states can trigger Zoom's 'browser is preventing access to your
    microphone' warning, which silently stalls the join. Force it into a
    known-good state before every attempt rather than trusting whatever
    was left over from a prior run."""
    try:
        subprocess.run(["systemctl", "--user", "restart", "pulseaudio.service"],
                       capture_output=True, timeout=10)
        time.sleep(2)
        result = subprocess.run(["pactl", "list", "short", "sinks"],
                                capture_output=True, text=True, timeout=5)
        if "virtual" not in result.stdout and "auto_null" not in result.stdout:
            subprocess.run(["pactl", "load-module", "module-null-sink",
                           "sink_name=virtual"], capture_output=True, timeout=5)
            subprocess.run(["pactl", "load-module", "module-null-source",
                           "source_name=virtmic"], capture_output=True, timeout=5)
        sink_name = "virtual" if "virtual" in result.stdout else "auto_null"
        # A SUSPENDED sink can still trip getUserMedia checks -- resume it.
        subprocess.run(["pactl", "set-default-sink", sink_name],
                       capture_output=True, timeout=5)
        subprocess.run(["pactl", "suspend-sink", sink_name, "0"],
                       capture_output=True, timeout=5)
        log.info("audio sink verified before join attempt")
    except Exception as e:
        log.warning("audio sink check failed (continuing anyway): %s", e)


def run(event_id: str) -> int:
    rec = db.get(event_id)
    if not rec:
        log.error("no such event %s", event_id)
        return 2

    _ensure_audio()

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
        page.on("console", lambda msg: log.info("browser console: %s", msg.text))
        page.on("pageerror", lambda exc: log.error("browser page error: %s", exc))
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

            # If the saved session belongs to the same Zoom account hosting
            # this meeting (e.g. testing with your own account/meeting),
            # Zoom skips the pre-join lobby entirely and drops straight into
            # the meeting -- there's no name field or join button to find.
            # Without this check, the broad "input[type='text']" fallback in
            # SEL_NAME_INPUT can still match some unrelated in-meeting text
            # box (search, chat) and "succeed", only to then hang looking
            # for a join button that will never exist and fail loudly on an
            # already-successful join.
            if _visible(page, SEL_IN_MEETING):
                log.info("landed directly in the meeting -- host session "
                         "skipped the pre-join lobby")
            else:
                # Retry (not one-shot) -- the page can be slow to render on
                # this hardware, and a single timed check can miss an
                # element that shows up a few seconds later.
                el = _retry_for_any(page, SEL_NAME_INPUT, timeout_s=45)
                if el:
                    el.fill(config.ZOOM_DISPLAY_NAME)
                else:
                    _shot(page, event_id, "no_name_field")
                    raise RuntimeError(
                        "name input never appeared -- page may be slow to load "
                        "on this hardware, or the pre-join screen changed")

                if rec["link_source"] != "registration":
                    pw_el = _retry_for_any(page, SEL_PASSCODE, timeout_s=10)
                    if pw_el and rec.get("passcode"):
                        pw_el.fill(rec["passcode"])

                btn = _retry_for_any(page, SEL_JOIN_BTN, timeout_s=20)
                if btn:
                    btn.click()
                else:
                    _shot(page, event_id, "no_join_button")
                    raise RuntimeError("Join button never appeared or never became clickable")

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
            if config.ZOOM_STATE.exists():
                # Persist whatever cookies the server just issued -- Google/Zoom
                # can rotate session tokens on use, and this snapshot only gets
                # staler every time it's read without being written back.
                ctx.storage_state(path=str(config.ZOOM_STATE))
            _shot(page, event_id, "joined")
            notify.push("Joined", f"{subject}", tags="white_check_mark")
            log.info("in meeting: %s", subject)

            exit_reason = "scheduled_end"
            low_streak = 0
            gone_streak = 0
            near_end_shot_taken = False
            NEAR_END_WINDOW = dt.timedelta(minutes=5)
            last_attendance_alert = 0.0

            while dt.datetime.now(dt.timezone.utc) < deadline:
                page.wait_for_timeout(30_000)

                # Explicit end/removal screens are unambiguous -- act on the
                # first sighting. The plain "footer bar gone" signal below
                # isn't: it can also just mean the page is mid-re-render, so
                # it needs a second consecutive miss before we trust it,
                # same debounce style as the participant-count check.
                if _visible(page, SEL_MEETING_ENDED):
                    exit_reason = "host_ended_meeting"
                    break
                if _visible(page, SEL_REMOVED):
                    exit_reason = "removed_from_meeting"
                    break

                if not _visible(page, SEL_IN_MEETING):
                    gone_streak += 1
                    if gone_streak >= 2:  # ~60s sustained, not a blip
                        exit_reason = "host_ended_or_dropped"
                        break
                    continue
                else:
                    gone_streak = 0

                if config.ATTENDANCE_CHECK_ENABLED:
                    cooldown_s = config.ATTENDANCE_ALERT_COOLDOWN_MINUTES * 60
                    if time.time() - last_attendance_alert > cooldown_s:
                        matched, reason, image = attendance_watch.check(page)
                        if matched:
                            last_attendance_alert = time.time()
                            shot_path = _save_image(image, event_id, "attendance")
                            log.info("attendance prompt detected (%s)", reason)
                            if shot_path:
                                notify.push_with_attachment(
                                    "Attendance check necessary",
                                    f"{subject} ({reason})",
                                    shot_path, priority="urgent",
                                    tags="qr_code,warning")
                            else:
                                notify.push("Attendance check necessary",
                                           f"{subject} ({reason})",
                                           priority="urgent", tags="qr_code,warning")

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
                    lb = _first(page, ["button:has-text('Leave Meeting')"], timeout=2_000)
                    if lb:
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
            # A specific failure point above (loginwall / no_name_field /
            # no_join_button / jointimeout) already grabbed its own shot --
            # don't also fire this generic one for the same failure.
            if time.time() - _shot.last_taken > 5:
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