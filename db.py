import sqlite3
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance (
    event_id        TEXT PRIMARY KEY,
    subject         TEXT,
    organizer       TEXT,
    scheduled_start TEXT,
    scheduled_end   TEXT,
    join_url        TEXT,
    passcode        TEXT,
    link_source     TEXT,   -- 'registration' | 'calendar'
    status          TEXT,   -- scheduled|joining|in_meeting|done|failed|skipped
    joined_at       TEXT,
    left_at         TEXT,
    exit_reason     TEXT,
    error           TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def upsert_scheduled(event):
    """Insert a newly-seen event. Does not clobber an in-flight join."""
    with conn() as c:
        c.execute(
            """INSERT INTO attendance
               (event_id, subject, organizer, scheduled_start, scheduled_end,
                join_url, passcode, link_source, status)
               VALUES (?,?,?,?,?,?,?,?, 'scheduled')
               ON CONFLICT(event_id) DO UPDATE SET
                 subject=excluded.subject,
                 scheduled_start=excluded.scheduled_start,
                 scheduled_end=excluded.scheduled_end,
                 join_url=excluded.join_url,
                 passcode=excluded.passcode,
                 link_source=excluded.link_source,
                 updated_at=CURRENT_TIMESTAMP
               WHERE attendance.status='scheduled'""",
            (event["event_id"], event["subject"], event["organizer"],
             event["scheduled_start"], event["scheduled_end"],
             event["join_url"], event.get("passcode"), event["link_source"]),
        )


def set_status(event_id, status, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE attendance SET status=?, updated_at=CURRENT_TIMESTAMP"
    if cols:
        sql += ", " + cols
    sql += " WHERE event_id=?"
    with conn() as c:
        c.execute(sql, (status, *fields.values(), event_id))


def get(event_id):
    with conn() as c:
        row = c.execute("SELECT * FROM attendance WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None


def pending(status="scheduled"):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM attendance WHERE status=? ORDER BY scheduled_start", (status,))]
