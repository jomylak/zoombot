"""Scheduler loop: poll Outlook, resolve links, spawn a joiner per meeting."""
import sys
import time
import signal
import logging
import subprocess
import datetime as dt

from . import config, db, ics_calendar, extract, notify, check_session

log = logging.getLogger("main")
_running = True
_children = {}


def _stop(*_):
    global _running
    _running = False
    log.info("shutting down...")


def poll():
    events = ics_calendar.upcoming_events(config.LOOKAHEAD_MINUTES)
    log.info("%d tagged event(s) in the next %dm", len(events), config.LOOKAHEAD_MINUTES)

    for ev in events:
        eid = ev["id"]
        if db.get(eid):
            continue  # already tracked

        parsed = extract.parse_event(ev.get("subject", ""), ev.get("body", ""), "")

        if not parsed:
            # Don't alert now -- LOOKAHEAD_MINUTES can be weeks, and a "no
            # link" alert that far out is easy to forget by the time it
            # actually matters. Stays 'scheduled' so launch_due() raises the
            # same JOIN FAILED alert a real join failure would, right at
            # the moment it would have joined -- that's the useful time to
            # tell someone to join manually.
            log.warning("no meeting link in %r yet -- will alert at join time", ev.get("subject"))
            db.upsert_scheduled({
                "event_id": eid, "subject": ev.get("subject"),
                "organizer": ev.get("organizer"),
                "scheduled_start": ev["start"].isoformat(), "scheduled_end": ev["end"].isoformat(),
                "join_url": None, "passcode": None, "link_source": "none", "platform": None})
            continue

        url = extract.resolve_join_url(parsed)

        db.upsert_scheduled({
            "event_id": eid, "subject": ev.get("subject"),
            "organizer": ev.get("organizer"),
            "scheduled_start": ev["start"].isoformat(), "scheduled_end": ev["end"].isoformat(),
            "join_url": url, "passcode": parsed["passcode"], "link_source": "calendar",
            "platform": parsed["platform"]})
        log.info("queued %r (%s)", ev.get("subject"), parsed["platform"])


def launch_due():
    now = dt.datetime.now(dt.timezone.utc)
    for rec in db.pending("scheduled"):
        if rec["event_id"] in _children:
            continue
        start = dt.datetime.fromisoformat(rec["scheduled_start"])
        end = dt.datetime.fromisoformat(rec["scheduled_end"])
        if now > end:
            db.set_status(rec["event_id"], "skipped", error="missed (start already passed)")
            continue
        if now < start - dt.timedelta(minutes=config.JOIN_LEAD_MINUTES):
            continue
        if not rec.get("join_url"):
            db.set_status(rec["event_id"], "failed", error="no meeting link found in invite")
            notify.push("JOIN FAILED", f"{rec['subject']} -- no meeting link found, join manually",
                        priority="high", tags="rotating_light")
            log.warning("no meeting link at join time: %r", rec["subject"])
            continue
        log.info("launching joiner for %r", rec["subject"])
        notify.push("Launching", rec["subject"])
        proc = subprocess.Popen(
            [sys.executable, "-m", "bot.joiner", rec["event_id"]],
            cwd=str(config.Path(__file__).resolve().parent.parent))
        _children[rec["event_id"]] = proc


def reap():
    for eid, proc in list(_children.items()):
        if proc.poll() is not None:
            log.info("joiner for %s exited rc=%s", eid, proc.returncode)
            if proc.returncode != 0:
                rec = db.get(eid)
                if rec and rec["status"] == "scheduled":
                    db.set_status(eid, "failed", error=f"joiner crashed (exit {proc.returncode})")
                    notify.push("Joiner crashed", rec["subject"], priority="high")
            del _children[eid]


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.init()
    notify.push("zoombot", "scheduler started")

    last_poll = 0
    last_session_check = 0
    consecutive_errors = 0
    while _running:
        try:
            if time.time() - last_poll > config.POLL_INTERVAL_MINUTES * 60:
                poll()
                last_poll = time.time()
            if time.time() - last_session_check > config.SESSION_CHECK_INTERVAL_HOURS * 3600:
                check_session.check()
                last_session_check = time.time()
            launch_due()
            reap()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.exception("loop error (%d in a row)", consecutive_errors)
            # A single blip (Outlook hiccup, brief network drop) shouldn't
            # page anyone -- the loop retries every 20s regardless. Only
            # alert once failures are actually persisting.
            if consecutive_errors >= 3:
                notify.push("zoombot error",
                            f"{e}"[:300] + f" ({consecutive_errors} in a row)",
                            priority="high")
        time.sleep(20)

    for proc in _children.values():
        proc.terminate()


if __name__ == "__main__":
    main()
