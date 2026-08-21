"""Scheduler loop: poll Outlook, resolve links, spawn a joiner per meeting."""
import time
import signal
import logging
import subprocess
import datetime as dt

from . import config, db, graph, extract, notify

log = logging.getLogger("main")
_running = True
_children = {}


def _stop(*_):
    global _running
    _running = False
    log.info("shutting down...")


def _iso(v):
    """Graph hands back naive UTC strings; make them tz-aware."""
    d = dt.datetime.fromisoformat(v["dateTime"].split(".")[0])
    return d.replace(tzinfo=dt.timezone.utc).isoformat()


def poll(token):
    events = graph.upcoming_events(token, config.LOOKAHEAD_MINUTES)
    log.info("%d tagged event(s) in the next %dm", len(events), config.LOOKAHEAD_MINUTES)

    for ev in events:
        eid = ev["id"]
        if db.get(eid):
            continue  # already tracked

        parsed = extract.parse_event(
            ev.get("subject", ""),
            (ev.get("body") or {}).get("content", ""),
            (ev.get("location") or {}).get("displayName", ""))

        if not parsed:
            log.warning("no Zoom details in %r -- skipping", ev.get("subject"))
            db.upsert_scheduled({
                "event_id": eid, "subject": ev.get("subject"),
                "organizer": (ev.get("organizer") or {}).get("emailAddress", {}).get("address"),
                "scheduled_start": _iso(ev["start"]), "scheduled_end": _iso(ev["end"]),
                "join_url": None, "passcode": None, "link_source": "none"})
            db.set_status(eid, "skipped", error="no zoom link found")
            notify.push("No Zoom link", ev.get("subject", ""), priority="high")
            continue

        # Prefer a per-registrant link from the confirmation email.
        url, source = parsed["join_url"], "calendar"
        reg = graph.find_registration_link(token, parsed["meeting_id"])
        if reg:
            url, source = reg, "registration"
        url = extract.web_client_url(parsed["meeting_id"], parsed["passcode"], url)

        db.upsert_scheduled({
            "event_id": eid, "subject": ev.get("subject"),
            "organizer": (ev.get("organizer") or {}).get("emailAddress", {}).get("address"),
            "scheduled_start": _iso(ev["start"]), "scheduled_end": _iso(ev["end"]),
            "join_url": url, "passcode": parsed["passcode"], "link_source": source})
        log.info("queued %r (%s link)", ev.get("subject"), source)


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
        log.info("launching joiner for %r", rec["subject"])
        proc = subprocess.Popen(
            ["python3", "-m", "bot.joiner", rec["event_id"]],
            cwd=str(config.Path(__file__).resolve().parent.parent))
        _children[rec["event_id"]] = proc


def reap():
    for eid, proc in list(_children.items()):
        if proc.poll() is not None:
            log.info("joiner for %s exited rc=%s", eid, proc.returncode)
            del _children[eid]


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    db.init()
    notify.push("zoombot", "scheduler started")

    last_poll = 0
    while _running:
        try:
            if time.time() - last_poll > config.POLL_INTERVAL_MINUTES * 60:
                poll(graph.get_token())
                last_poll = time.time()
            launch_due()
            reap()
        except Exception as e:
            log.exception("loop error")
            notify.push("zoombot error", str(e)[:300], priority="high")
        time.sleep(20)

    for proc in _children.values():
        proc.terminate()


if __name__ == "__main__":
    main()
