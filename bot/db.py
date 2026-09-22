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
    status          TEXT,   -- scheduled|joining|in_meeting|done|failed|skipped|needs_manual_login
    joined_at       TEXT,
    left_at         TEXT,
    exit_reason     TEXT,
    error           TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Selectors the agent (or a hand-written baseline) has found for a given
-- role on a given variant_key. Ordered by last_success on lookup so a
-- selector that keeps working floats to the top; one that starts failing
-- naturally sinks and gets retried behind newer finds.
CREATE TABLE IF NOT EXISTS learned_selectors (
    variant_key  TEXT,
    role         TEXT,
    selector     TEXT,
    hits         INTEGER DEFAULT 1,
    last_success TEXT,
    PRIMARY KEY (variant_key, role, selector)
);

-- One row per structurally-distinct join flow we've encountered, so we
-- know whether a variant is still being learned or is a known login-wall.
CREATE TABLE IF NOT EXISTS join_variants (
    variant_key TEXT PRIMARY KEY,
    platform    TEXT,
    example_url TEXT,
    status      TEXT DEFAULT 'learning',  -- learning | ok | needs_login
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
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


def _migrate(c):
    cols = {row["name"] for row in c.execute("PRAGMA table_info(attendance)")}
    if "platform" not in cols:
        c.execute("ALTER TABLE attendance ADD COLUMN platform TEXT")


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def upsert_scheduled(event):
    """Insert a newly-seen event. Does not clobber an in-flight join."""
    with conn() as c:
        c.execute(
            """INSERT INTO attendance
               (event_id, subject, organizer, scheduled_start, scheduled_end,
                join_url, passcode, link_source, platform, status)
               VALUES (?,?,?,?,?,?,?,?,?, 'scheduled')
               ON CONFLICT(event_id) DO UPDATE SET
                 subject=excluded.subject,
                 scheduled_start=excluded.scheduled_start,
                 scheduled_end=excluded.scheduled_end,
                 join_url=excluded.join_url,
                 passcode=excluded.passcode,
                 link_source=excluded.link_source,
                 platform=excluded.platform,
                 updated_at=CURRENT_TIMESTAMP
               WHERE attendance.status='scheduled'""",
            (event["event_id"], event["subject"], event["organizer"],
             event["scheduled_start"], event["scheduled_end"],
             event["join_url"], event.get("passcode"), event["link_source"],
             event.get("platform")),
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


def learned_selectors_for(variant_key, role):
    """Selectors learned for this (variant_key, role), most recently
    successful first."""
    with conn() as c:
        rows = c.execute(
            """SELECT selector FROM learned_selectors
               WHERE variant_key=? AND role=?
               ORDER BY last_success DESC""",
            (variant_key, role))
        return [r["selector"] for r in rows]


def record_selector(variant_key, role, selector):
    with conn() as c:
        c.execute(
            """INSERT INTO learned_selectors (variant_key, role, selector, hits, last_success)
               VALUES (?,?,?,1,CURRENT_TIMESTAMP)
               ON CONFLICT(variant_key, role, selector) DO UPDATE SET
                 hits=hits+1, last_success=CURRENT_TIMESTAMP""",
            (variant_key, role, selector))


def get_variant(variant_key):
    with conn() as c:
        row = c.execute("SELECT * FROM join_variants WHERE variant_key=?",
                        (variant_key,)).fetchone()
        return dict(row) if row else None


def touch_variant(variant_key, platform, example_url, status=None):
    with conn() as c:
        c.execute(
            """INSERT INTO join_variants (variant_key, platform, example_url, status)
               VALUES (?,?,?, COALESCE(?, 'learning'))
               ON CONFLICT(variant_key) DO UPDATE SET
                 updated_at=CURRENT_TIMESTAMP,
                 status=COALESCE(?, join_variants.status)""",
            (variant_key, platform, example_url, status, status))


def mark_variant_status(variant_key, status):
    with conn() as c:
        c.execute(
            """UPDATE join_variants SET status=?, updated_at=CURRENT_TIMESTAMP
               WHERE variant_key=?""",
            (status, variant_key))
