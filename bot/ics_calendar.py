"""Published ICS calendar feed: fetch over plain HTTP, filter tagged upcoming events."""
import logging
import datetime as dt
import requests
from icalendar import Calendar
from dateutil.rrule import rrulestr
from . import config

log = logging.getLogger(__name__)


def _aware(d):
    """icalendar hands back date or naive/aware datetime depending on the property."""
    if isinstance(d, dt.date) and not isinstance(d, dt.datetime):
        d = dt.datetime(d.year, d.month, d.day)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _exdates(comp):
    out = set()
    exd = comp.get("EXDATE")
    if not exd:
        return out
    for prop in (exd if isinstance(exd, list) else [exd]):
        for d in prop.dts:
            out.add(_aware(d.dt))
    return out


def _occurrences(comp, window_start, window_end):
    """Start datetimes of this VEVENT (expanding RRULE, if any) inside the window."""
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return []
    start = _aware(dtstart.dt)

    rrule_prop = comp.get("RRULE")
    if not rrule_prop:
        return [start] if window_start <= start <= window_end else []

    exdates = _exdates(comp)
    rule = rrulestr(rrule_prop.to_ical().decode(), dtstart=start)
    out = []
    for occ in rule.between(window_start, window_end, inc=True):
        occ = _aware(occ)
        if occ not in exdates:
            out.append(occ)
    return out


def _fetch() -> Calendar:
    r = requests.get(config.ICS_URL, timeout=30)
    r.raise_for_status()
    return Calendar.from_ical(r.text)


def upcoming_events(lookahead_minutes: int):
    """Non-cancelled events, tagged with AUTOJOIN_SUBJECT_TAG, starting in the next N minutes."""
    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(minutes=lookahead_minutes)

    cal = _fetch()
    out = []
    for comp in cal.walk("VEVENT"):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue

        subject = str(comp.get("SUMMARY", ""))
        if config.AUTOJOIN_SUBJECT_TAG not in subject:
            continue

        dtend = comp.get("DTEND")
        duration = None
        if dtend is not None and comp.get("DTSTART") is not None:
            duration = _aware(dtend.dt) - _aware(comp.get("DTSTART").dt)

        organizer = comp.get("ORGANIZER")
        organizer_addr = str(organizer).replace("mailto:", "").replace("MAILTO:", "") \
            if organizer else None

        uid = str(comp.get("UID", ""))

        # Different hosts put the join link in different fields (DESCRIPTION,
        # LOCATION, even COMMENT) -- concatenate everything so extract.parse_event()
        # finds it regardless of where it landed.
        blob_parts = [subject]
        for field in ("DESCRIPTION", "LOCATION", "COMMENT"):
            val = comp.get(field)
            if val:
                blob_parts.append(str(val))
        blob = " ".join(blob_parts)

        for start in _occurrences(comp, now, end):
            finish = start + duration if duration is not None else start
            out.append({
                "id": f"{uid}:{start.isoformat()}" if comp.get("RRULE") else uid,
                "subject": subject,
                "organizer": organizer_addr,
                "start": start,
                "end": finish,
                "body": blob,
            })

    out.sort(key=lambda e: e["start"])
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for ev in upcoming_events(config.LOOKAHEAD_MINUTES):
        print(ev["start"].isoformat(), "|", ev["subject"])
