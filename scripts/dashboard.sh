#!/usr/bin/env bash
# One-shot status view for SSH sessions: `./scripts/dashboard.sh`.
# Reads the same .env/DB the bot uses; no extra deps beyond sqlite3
# (already required by setup_pi.sh) and standard coreutils.
set -uo pipefail
cd "$(dirname "$0")/.."

# Matches config.py's precedence: an explicitly-exported STATE_DIR wins,
# otherwise fall back to whatever's in .env, otherwise the default.
if [ -z "${STATE_DIR:-}" ] && [ -f .env ]; then
  STATE_DIR=$(grep -E '^STATE_DIR=' .env | tail -1 | cut -d= -f2-)
fi
STATE_DIR="${STATE_DIR:-$HOME/.zoombot}"
STATE_DIR="${STATE_DIR/#\~/$HOME}"
DB="$STATE_DIR/attendance.db"
BROWSER_LOCK="${BROWSER_LOCK_PATH:-$HOME/.applypilot/browser.lock}"
BROWSER_LOCK="${BROWSER_LOCK/#\~/$HOME}"

if [ -t 1 ] && command -v tput >/dev/null && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_HDR=$(tput bold)$(tput setaf 6)   # cyan bold
  C_GOOD=$(tput setaf 2)              # green
  C_WARN=$(tput setaf 3)              # yellow
  C_BAD=$(tput bold)$(tput setaf 1)   # red bold
  C_DIM=$(tput dim)
  C_OFF=$(tput sgr0)
else
  C_HDR=""; C_GOOD=""; C_WARN=""; C_BAD=""; C_DIM=""; C_OFF=""
fi

hdr() { printf '\n%s== %s ==%s\n' "$C_HDR" "$1" "$C_OFF"; }

hdr "zoombot service"
status_line=$(systemctl --user status zoombot --no-pager -n 0 2>/dev/null | grep -E "Active:|Main PID:")
if [ -z "$status_line" ]; then
  echo "  ${C_DIM}systemctl unavailable (not on the Pi, or not running as a user service)${C_OFF}"
elif echo "$status_line" | grep -q "running"; then
  echo "  ${C_GOOD}${status_line}${C_OFF}" | sed 's/^  *//'
else
  echo "  ${C_BAD}${status_line}${C_OFF}" | sed 's/^  *//'
fi

if [ ! -f "$DB" ]; then
  echo "No DB found at $DB -- has the bot run yet?"
  exit 0
fi

hdr "next up (scheduled)"
next_up=$(sqlite3 -header -column "$DB" "
  SELECT scheduled_start, subject,
         COALESCE(platform, 'no link yet') AS platform
  FROM attendance
  WHERE status = 'scheduled'
  ORDER BY scheduled_start LIMIT 8;")
if [ -z "$next_up" ]; then echo "  (nothing queued)"; else echo "$next_up"; fi

hdr "active right now"
active=$(sqlite3 -header -column "$DB" "
  SELECT scheduled_start, subject, status, joined_at
  FROM attendance WHERE status IN ('joining','in_meeting');")
if [ -z "$active" ]; then
  echo "  (nothing in progress)"
else
  echo "${C_GOOD}${active}${C_OFF}"
fi

hdr "needs your attention"
attn=$(sqlite3 -header -column "$DB" "
  SELECT scheduled_start, subject, status, COALESCE(error, 'no link found yet -- may need manual join') AS note
  FROM attendance
  WHERE status IN ('needs_manual_login','failed')
     OR (status = 'scheduled' AND join_url IS NULL)
  ORDER BY scheduled_start DESC LIMIT 8;")
if [ -z "$attn" ]; then
  echo "  ${C_GOOD}(none)${C_OFF}"
else
  echo "${C_BAD}${attn}${C_OFF}"
fi

hdr "recent history"
sqlite3 -header -column "$DB" "
  SELECT scheduled_start, subject, status, exit_reason
  FROM attendance WHERE status = 'done'
  ORDER BY scheduled_start DESC LIMIT 5;"

hdr "learned join variants"
variants=$(sqlite3 -header -column "$DB" "
  SELECT platform, status, COUNT(*) AS n
  FROM join_variants GROUP BY platform, status ORDER BY platform;")
if [ -z "$variants" ]; then echo "  (none learned yet)"; else echo "$variants"; fi

hdr "shared browser lock (vs. job-discovery agent)"
if [ -f "$BROWSER_LOCK" ]; then
  if command -v flock >/dev/null && flock -n "$BROWSER_LOCK" -c true 2>/dev/null; then
    echo "  ${C_GOOD}free${C_OFF}"
  else
    echo "  ${C_WARN}HELD (something is using Chromium right now)${C_OFF}"
  fi
else
  echo "  ${C_DIM}no lock file yet ($BROWSER_LOCK) -- nothing has taken it${C_OFF}"
fi

hdr "system"
free -h 2>/dev/null | grep -E "Mem|Swap" || vm_stat 2>/dev/null | head -4
uptime
echo "screenshots: $(find "$STATE_DIR/screenshots" -type f 2>/dev/null | wc -l | tr -d ' ') files, $(du -sh "$STATE_DIR/screenshots" 2>/dev/null | cut -f1)"
df -h "$STATE_DIR" 2>/dev/null | tail -1
echo
