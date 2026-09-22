"""Joins one meeting (Zoom, Teams, Meet, Webex, or anything else with a
guest-joinable web link), sits in it, leaves, records the result.

Run as a subprocess (one per meeting) so a crash or a memory leak can never
take down the scheduler:

    python -m bot.joiner <event_id>

Selectors for each step are resolved per (platform, url-shape) "variant":
learned selectors from past successful joins first, then any hand-written
baseline (currently just Zoom), and only when both come up empty does this
fall back to the AI agent (bot/ai_agent.py) -- which also records whatever
it finds, so future joins of the same shape of meeting need it less and
less. See bot/selector_store.py and bot/platforms.py for that machinery.
"""
import sys
import time
import logging
import datetime as dt
import hashlib
import shutil
import subprocess
import fcntl

from playwright.sync_api import sync_playwright

from . import ai_agent, attendance_watch, config, db, notify, platforms, selector_store
from .browser_util import first

log = logging.getLogger("joiner")

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",      # auto-accept mic/cam prompts
    "--use-fake-device-for-media-stream",  # synthetic camera + mic, no real hardware needed
    "--autoplay-policy=no-user-gesture-required",
    "--disable-dev-shm-usage",              # /dev/shm is tiny on the Pi
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
]


def _resolve(page, variant_key, role, event_id, rec, timeout_s=3, retry=False, required=False):
    """Selector-store lookup, falling back to a single agent call when
    nothing known works. Returns an element or None (raises if required and
    still not found)."""
    el = selector_store.resolve(page, variant_key, role, timeout_s=timeout_s, retry=retry)
    if not el and config.AGENT_ENABLED:
        log.info("no known selector for %s/%s, asking the agent", variant_key, role)
        el = ai_agent.find_role(page, variant_key, role, rec)
    if not el and required:
        _shot(page, event_id, f"no_{role}")
        raise RuntimeError(f"couldn't find an element for role {role!r} "
                           f"(variant {variant_key!r})")
    return el


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


def _chromium_path():
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _participant_count(page, variant_key, event_id, rec):
    el = _resolve(page, variant_key, "participant_count", event_id, rec, timeout_s=1)
    if not el:
        return None
    try:
        txt = el.inner_text(timeout=1000).strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
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


def _acquire_browser_lock():
    """Cooperative lock shared with the Pi's other Chromium user (ApplyPilot's
    pi_enrich_runner.py, which takes it non-blocking and skips its turn when
    this holds it). This side takes it BLOCKING -- joining on time matters
    more than a clean handoff -- but first touches BROWSER_YIELD_PATH so the
    enrichment side, mid-batch, notices and bails out of its current batch
    early instead of making us wait for the whole thing. Returns the open
    file handle; release with _release_browser_lock."""
    config.BROWSER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        config.BROWSER_YIELD_PATH.touch()
    except Exception:
        pass
    fh = open(config.BROWSER_LOCK_PATH, "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        config.BROWSER_YIELD_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return fh


def _release_browser_lock(fh):
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def _needs_manual_login(page, event_id, rec, variant_key, platform, why: str) -> int:
    _shot(page, event_id, "needs_login")
    db.set_status(event_id, "needs_manual_login", error=why)
    selector_store.mark_needs_login(variant_key, platform, rec.get("join_url") or "")
    notify.push("Needs your login",
               f"{rec['subject']} -- {why}. This meeting requires signing in "
               "with your personal account; the bot won't attempt that. "
               "Join it yourself if you want to attend.",
               priority="high", tags="lock")
    log.warning("needs manual login: %s (%s)", rec["subject"], why)
    return 0  # handled outcome, not a crash


def run(event_id: str) -> int:
    rec = db.get(event_id)
    if not rec:
        log.error("no such event %s", event_id)
        return 2

    _ensure_audio()

    subject = rec["subject"]
    url = rec["join_url"]
    platform = rec.get("platform") or platforms.detect_platform(url)
    variant_key = platforms.variant_key(url)
    end_at = dt.datetime.fromisoformat(rec["scheduled_end"])
    hard_stop = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=config.MAX_SESSION_MINUTES)
    deadline = min(end_at, hard_stop)

    db.set_status(event_id, "joining")
    db.touch_variant(variant_key, platform, url)

    state_path = config.platform_state_path(platform)

    lock_fh = _acquire_browser_lock()
    try:
        return _join_and_attend(event_id, rec, subject, url, platform, variant_key,
                                 deadline, state_path)
    finally:
        _release_browser_lock(lock_fh)


def _join_and_attend(event_id, rec, subject, url, platform, variant_key, deadline, state_path):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS,
                                    executable_path=_chromium_path())
        ctx = browser.new_context(
            storage_state=str(state_path) if state_path.exists() else None,
            viewport={"width": 1024, "height": 700},  # small = cheaper on a Pi 4
            permissions=["microphone", "camera"],
        )
        page = ctx.new_page()
        page.on("console", lambda msg: log.info("browser console: %s", msg.text))
        page.on("pageerror", lambda exc: log.error("browser page error: %s", exc))
        try:
            log.info("navigating: %s (%s)", url, platform)
            page.goto(url, timeout=90_000, wait_until="domcontentloaded")
            page.wait_for_timeout(5_000)

            if platforms.is_idp_login_host(page.url) or selector_store.is_visible(
                    page, variant_key, "login_wall"):
                return _needs_manual_login(page, event_id, rec, variant_key, platform,
                                           "saved session expired or account sign-in required")

            if selector_store.is_new_variant(variant_key):
                log.info("brand-new variant %s -- handing the whole pre-join "
                         "sequence to the agent", variant_key)
                outcome = ai_agent.drive(page, variant_key, rec)
                if outcome == "needs_login":
                    return _needs_manual_login(page, event_id, rec, variant_key, platform,
                                               "no guest join path found")
                if outcome != "in_meeting":
                    _shot(page, event_id, "agent_stuck")
                    raise RuntimeError(f"agent couldn't get into the meeting (outcome={outcome})")
            else:
                # Registration links land on an intermediate "which app"
                # chooser before the actual join screen -- direct links skip it.
                chooser = _resolve(page, variant_key, "join_from_browser", event_id, rec, timeout_s=5)
                if chooser:
                    log.info("hit the app-vs-browser chooser, clicking 'Join from browser'")
                    chooser.click()
                    page.wait_for_timeout(2_000)

                # Some clients show up to two device-permission prompts before
                # the name field is usable. Dismiss without granting -- the
                # bot doesn't need mic/camera.
                for _ in range(2):
                    dismiss = _resolve(page, variant_key, "dismiss_media_prompt", event_id, rec, timeout_s=3)
                    if not dismiss:
                        break
                    log.info("dismissing a device-permission prompt")
                    dismiss.click()
                    page.wait_for_timeout(1_000)

                # If the saved session belongs to the same account hosting
                # this meeting (e.g. testing with your own account/meeting),
                # the host session skips the pre-join lobby entirely and
                # drops straight into the meeting -- no name field or join
                # button to find.
                if selector_store.is_visible(page, variant_key, "in_meeting"):
                    log.info("landed directly in the meeting -- host session "
                             "skipped the pre-join lobby")
                else:
                    el = _resolve(page, variant_key, "name_input", event_id, rec,
                                 timeout_s=45, retry=True, required=True)
                    el.fill(config.DISPLAY_NAME)

                    if rec["link_source"] != "registration":
                        pw_el = _resolve(page, variant_key, "passcode_input", event_id, rec,
                                        timeout_s=10, retry=True)
                        if pw_el and rec.get("passcode"):
                            pw_el.fill(rec["passcode"])

                    btn = _resolve(page, variant_key, "join_button", event_id, rec,
                                   timeout_s=20, retry=True, required=True)
                    btn.click()

                # Wait to actually land in the meeting -- may sit in a waiting room.
                join_deadline = time.time() + config.JOIN_TIMEOUT_MINUTES * 60
                while time.time() < join_deadline:
                    if selector_store.is_visible(page, variant_key, "in_meeting"):
                        break
                    if selector_store.is_visible(page, variant_key, "waiting_room"):
                        log.info("in waiting room, holding...")
                    page.wait_for_timeout(5_000)
                else:
                    _shot(page, event_id, "jointimeout")
                    raise RuntimeError(
                        f"never got in within {config.JOIN_TIMEOUT_MINUTES}m "
                        "(waiting room never opened?)")

            joined_at = dt.datetime.now(dt.timezone.utc)
            db.set_status(event_id, "in_meeting", joined_at=joined_at.isoformat())
            selector_store.mark_ok(variant_key, platform, url)
            # Persist whatever cookies the server just issued -- sessions
            # can rotate tokens on use, and this snapshot only gets staler
            # every time it's read without being written back.
            try:
                ctx.storage_state(path=str(state_path))
            except Exception:
                pass
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
                if selector_store.is_visible(page, variant_key, "meeting_ended"):
                    exit_reason = "host_ended_meeting"
                    break
                if selector_store.is_visible(page, variant_key, "removed"):
                    exit_reason = "removed_from_meeting"
                    break

                if not selector_store.is_visible(page, variant_key, "in_meeting"):
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

                n = _participant_count(page, variant_key, event_id, rec)
                if n is not None and n < config.MIN_PARTICIPANTS:
                    low_streak += 1
                    if low_streak >= 3:  # ~90s sustained, not a blip
                        exit_reason = f"participants_below_{config.MIN_PARTICIPANTS}"
                        break
                else:
                    low_streak = 0

            leave = _resolve(page, variant_key, "leave_button", event_id, rec, timeout_s=3)
            if leave:
                try:
                    leave.click()
                    page.wait_for_timeout(1_500)
                    lb = first(page, ["button:has-text('Leave Meeting')"], timeout=2_000)
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
            # A specific failure point above already grabbed its own shot --
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
