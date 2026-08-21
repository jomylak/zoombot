# zoombot

Joins Zoom meetings tagged in your Outlook calendar, sits in them, leaves, and
logs what happened. Built for a Raspberry Pi 4 (64-bit, 4GB+).

```
Outlook (Graph)  ->  filter by category  ->  extract Zoom link
                                              |
                        prefer per-registrant tk= link from your inbox
                                              |
                     headless Chromium joins  ->  SQLite log + ntfy push
```

## 1. Register a Microsoft app (5 min, one time)

1. <https://portal.azure.com> -> **Microsoft Entra ID** -> **App registrations** -> **New registration**
2. Name it anything. Under supported account types choose
   **"Personal Microsoft accounts only"**.
3. Leave the redirect URI blank. Register.
4. **Authentication** -> **Allow public client flows: Yes**. Save.
   (Device-code auth fails with an obscure error without this.)
5. **API permissions** -> Add -> Microsoft Graph -> **Delegated** ->
   `Calendars.Read`, `Mail.Read`. No admin consent needed on a personal account.
6. Copy the **Application (client) ID** into `.env` as `MS_CLIENT_ID`.

## 2. Install on the Pi

```bash
git clone <your-repo> ~/zoombot && cd ~/zoombot
./scripts/setup_pi.sh
cp .env.example .env && nano .env
```

`ZOOM_DISPLAY_NAME` must match your Zoom account name exactly — it's what a
host's attendance report matches against.

## 3. Authenticate

```bash
./.venv/bin/python -m bot.graph login    # prints a code, open the URL anywhere
./.venv/bin/python -m bot.zoom_login     # needs a screen -- see note below
```

`zoom_login` opens a real browser window. On a headless Pi, either run it once
on your laptop and copy `~/.zoombot/zoom_state.json` over, or install
`realvnc-vnc-server` and do it over VNC. Cookies last weeks; when they expire
the bot detects the login wall, fails loudly, and pushes you a notification.

## 4. Tag a meeting

In Outlook, right-click the event -> **Categorize** -> create/apply a category
named to match `AUTOJOIN_CATEGORY` (default `AutoJoin`). You apply this
yourself; it doesn't depend on the organizer.

Verify it's visible:

```bash
./.venv/bin/python -m bot.graph
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
- Recurring events are treated as independent occurrences (Graph's
  `calendarView` expands them), so each instance is tracked separately.
- If an event moves after the bot queued it, the row is updated on the next
  poll only while its status is still `scheduled`.
