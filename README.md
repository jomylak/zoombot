# zoombot

Joins Zoom meetings tagged in your Outlook calendar, sits in them, leaves, and
logs what happened. Built for a Raspberry Pi 4 (64-bit, 4GB+).

```
Outlook (published ICS feed)  ->  filter by [AutoJoin] in the title  ->  extract Zoom link
                                                                          |
                                          headless Chromium joins  ->  SQLite log + ntfy push
```
Check schedule: sqlite3 -header -column ~/.zoombot/attendance.db \
'SELECT scheduled_start, subject, status
 FROM attendance
 ORDER BY scheduled_start;'

 check uptime:
 systemctl --user status zoombot
 
## 1. Publish your calendar (2 min, one time)

1. In Outlook on the web: **Settings** -> **Calendar** -> **Shared calendars**.
2. Under **Publish a calendar**, pick the calendar you want (usually your
   main one) and permission **Can view all details**.
3. Click **Publish**, then copy the **ICS** link it gives you.
4. Paste it into `.env` as `ICS_URL`.

> **This link is a bearer secret, not a login.** Anyone who has the URL can
> read your entire calendar -- title, body, attendees, everything -- with no
> further authentication. Don't commit it, don't paste it into chat, don't
> put it in a public gist. If it ever leaks, go back to **Shared calendars**
> and use **Reset** next to the published link to invalidate it and generate
> a new one.

## 2. Install on the Pi

```bash
git clone <your-repo> ~/zoombot && cd ~/zoombot
./scripts/setup_pi.sh
cp .env.example .env && nano .env
```

`ZOOM_DISPLAY_NAME` must match your Zoom account name exactly — it's what a
host's attendance report matches against.

## 3. Authenticate to Zoom

```bash
./.venv/bin/python -m bot.zoom_login     # needs a screen -- see note below
```

`zoom_login` opens a real browser window. On a headless Pi, either run it once
on your laptop and copy `~/.zoombot/zoom_state.json` over, or install
`realvnc-vnc-server` and do it over VNC. Cookies last weeks; when they expire
the bot detects the login wall, fails loudly, and pushes you a notification.

There's no separate Microsoft/Outlook auth step -- the ICS feed is fetched by
plain HTTP GET, no login or token involved.

## 4. Tag a meeting

Put the literal tag `AUTOJOIN_SUBJECT_TAG` (default `[AutoJoin]`) directly in
the meeting's **title** in Outlook, e.g. `Weekly sync [AutoJoin]`. This
replaces the old category-based tagging -- published ICS feeds generally
don't carry Outlook categories, so filtering is now a substring match against
the event subject. You apply this yourself; it doesn't depend on the
organizer.

Verify it's visible:

```bash
./.venv/bin/python -m bot.ics_calendar
```

## 5. Run it

```bash
mkdir -p ~/.config/systemd/user
cp systemd/zoombot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now zoombot
journalctl --user -u zoombot -f
```

Dry-run a single meeting without waiting for the scheduler:

```bash
./.venv/bin/python -m bot.joiner <event_id>     # event_id from the DB
sqlite3 ~/.zoombot/attendance.db 'select subject,status,joined_at,left_at,exit_reason from attendance'
```

## Attendance-check (QR code) detection

Some meetings show an on-screen QR code or a text prompt ("attendance",
"scan code", ...) that only a human can act on. While in a meeting, the bot
checks the screen every ~30s for a QR code (`pyzbar`) or a keyword match via
OCR (`pytesseract`, keywords in `ATTENDANCE_KEYWORDS`). On a match it saves a
screenshot to `~/.zoombot/screenshots/` and sends an ntfy push titled
**"Attendance check necessary"** with the screenshot attached directly to the
notification, so you can view it from your phone without any extra login.
Repeat alerts for the same prompt are suppressed for `ATTENDANCE_ALERT_COOLDOWN_MINUTES`
(default 10). Disable with `ATTENDANCE_CHECK_ENABLED=false`.

## When a join breaks

Zoom rewrites its web client every few months and the DOM selectors go stale.
Screenshots land in `~/.zoombot/screenshots/` — an `error` or `jointimeout`
shot shows exactly what the page looked like. The selector lists at the top of
`bot/joiner.py` are the only thing you should need to touch.

## Known limits

- **One Zoom session at a time.** If the bot is in a meeting as you and you
  join something else from your laptop, one of the two may get bumped. Test
  this deliberately with two throwaway meetings before you rely on it.
- **Pi 4 is the floor.** Chromium decoding a call uses ~1 core and ~1.2GB.
  Use a heatsink; a bare Pi 4 throttles partway through a long session.
- **Home internet is the single point of failure.** Verify the ntfy alerts
  actually reach your phone before trusting this, and spot-check early
  sessions yourself — a bot that fails silently is worse than no bot.
- **Registration-required meetings are a real gap.** There's no mail search
  anymore (no Graph, no `Mail.Read`), so the bot can't go fetch your
  per-registrant `tk=` confirmation link. It only ever uses whatever join
  link is sitting in the calendar invite body. That's usually the plain,
  non-personalized link, which commonly still works -- but if an organizer
  requires registration and only puts the personalized link in a
  confirmation *email* rather than the invite itself, the bot has nothing to
  join with and will skip the meeting as "no zoom link found."
- Recurring events are expanded client-side from each series' `RRULE` (the
  published ICS feed typically carries one `VEVENT` per series, not one per
  occurrence, unlike Graph's `calendarView`). Exception/override instances
  (`RECURRENCE-ID`) and `EXDATE` are honored; more exotic recurrence edge
  cases haven't been battle-tested.
- If an event moves after the bot queued it, the row is updated on the next
  poll only while its status is still `scheduled`.
