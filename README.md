# zoombot

Joins meetings tagged in your Outlook calendar, sits in them, leaves, and
logs what happened. Built for a Raspberry Pi 4 (64-bit, 4GB+).

```
Outlook (published ICS feed)  ->  filter by [AutoJoin] in the title  ->  extract meeting link
                                                                          |
                                          headless Chromium joins  ->  SQLite log + ntfy push
```

Zoom, Microsoft Teams, Google Meet, and Webex links are all recognized;
anything else found next to "join the meeting"-style wording in the invite
is attempted too. See **Joining new platforms** below for how that works
and **Meetings the bot can't join** for the cases it deliberately skips.
Check schedule:

```bash
sqlite3 -header -column ~/.zoombot/attendance.db \
  'SELECT scheduled_start, subject, status, platform
   FROM attendance
   WHERE scheduled_start > datetime("now")
   ORDER BY scheduled_start;'
```

Check what the ICS feed currently offers (bypasses the DB, tag-filtered, next `LOOKAHEAD_MINUTES`):

```bash
./.venv/bin/python -m bot.ics_calendar
```

Check uptime:

```bash
systemctl --user status zoombot
```

Or get all of the above in one view:

```bash
./scripts/dashboard.sh
```

`LOOKAHEAD_MINUTES` controls how far ahead events get queued into the DB
(the joiner itself only launches `JOIN_LEAD_MINUTES` before start regardless
of this value) -- set it to `30240` (3 weeks) to see/queue everything that
far out.
 
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

Web clients get rewritten every few months and DOM selectors go stale.
Screenshots land in `~/.zoombot/screenshots/` — an `error` or `jointimeout`
shot shows exactly what the page looked like. Zoom's known-good selectors
live in `bot/platforms.py` (`BASELINE_SELECTORS`); if one of those goes
stale, the AI agent below self-heals it automatically the next time it
runs, rather than needing a manual fix.

## Joining new platforms

Zoom is the only platform with a hand-written selector list. Every other
platform (Teams, Meet, Webex, or anything else the bot finds a join link
for) is driven by an AI agent on first contact instead:

1. The first time the bot sees a **new shape of meeting link** (same
   platform + same URL shape, e.g. any `teams.microsoft.com/l/meetup-join/...`
   link), it hands the whole pre-join sequence to an OpenRouter model
   (`OPENROUTER_MODEL`, a cheap paid model by default) — it looks at the
   page's visible elements and a screenshot each step and decides what to
   click, fill, or wait for.
2. Every selector it successfully uses gets recorded to
   `~/.zoombot/attendance.db` (`learned_selectors` table), keyed by that
   URL shape.
3. On the next meeting of the same shape, the bot tries the recorded
   selectors directly — no AI call, no added latency or cost. The agent
   only gets invoked again for a single step if a recorded selector stops
   resolving (the platform's DOM changed), not for the whole flow.

So the first join of a new platform is slower and depends on the model
being available and fast enough; every join after that is the same
deterministic, selector-based flow Zoom already uses. Needs
`OPENROUTER_API_KEY` set — without it (`AGENT_ENABLED=false` or no key),
the bot only attempts platforms it already has selectors for (Zoom).

## Meetings the bot can't join

The bot will never attempt to sign in with your personal account —
if a join link leads to a personal-login wall instead of a guest/anonymous
join option, it stops immediately, screenshots it, sets the meeting's
status to `needs_manual_login`, and pushes you an alert instead of trying
anything. This is detected both by a static list of identity-provider
domains (`accounts.google.com`, `login.microsoftonline.com`, ...) and by
the AI agent's own judgment on platforms it hasn't seen before.

Concrete cases where that happens:

- A **Teams** meeting whose organizer's tenant has external/anonymous
  access disabled, or an internal channel meeting with no guest link at all.
- A **Zoom** meeting with the host setting "Only authenticated users can
  join," which forces Zoom account sign-in.
- A **Google Meet** meeting restricted to signed-in members of the
  organizer's Workspace domain.
- A **Webex** meeting some orgs restrict to accounts on their own Webex site.
- **Registration-required Zoom webinars** where the only valid join link is
  the personalized `tk=` link emailed after registering with your identity
  — related but not identical to a login wall (see Known limits below);
  the bot has no way to fetch that email.

## Sharing the Pi with another Chromium-based process

If something else on the same machine also drives headless Chromium (e.g. a
job-discovery/apply agent), point both at the same `BROWSER_LOCK_PATH` so
they never launch Chromium at once on a memory-constrained Pi. This bot
always takes the lock **blocking** -- joining on time matters more than a
clean handoff -- and touches `BROWSER_YIELD_PATH` right before it blocks, so
a cooperating process mid-batch can notice and wrap up its current item
early instead of making the bot wait. Both paths default under
`~/.applypilot/` and are no-ops if nothing else on the box uses them.

## Known limits

- **One meeting session at a time**, regardless of platform. If the bot is
  in a meeting as you and you join something else from your laptop on the
  same account, one of the two may get bumped. Test this deliberately with
  two throwaway meetings before you rely on it.
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
