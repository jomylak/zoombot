#!/usr/bin/env bash
# Pi 4 (64-bit Raspberry Pi OS Bookworm) setup. Run from the repo root.
set -euo pipefail

echo ">> System packages"
sudo apt update
sudo apt install -y python3-venv python3-pip chromium chromium-common \
     pulseaudio pulseaudio-utils fonts-liberation libnss3 libatk-bridge2.0-0 \
     libgtk-3-0 libasound2 sqlite3

echo ">> Virtualenv"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
# NOTE: deliberately NOT running `playwright install chromium` -- there is no
# arm64 build. We point Playwright at the system chromium instead.

echo ">> Virtual audio sink (Zoom's web client refuses to start without one)"
mkdir -p ~/.config/pulse
grep -q 'module-null-sink' ~/.config/pulse/default.pa 2>/dev/null || {
  echo ".include /etc/pulse/default.pa"                     >> ~/.config/pulse/default.pa
  echo "load-module module-null-sink sink_name=virtual"     >> ~/.config/pulse/default.pa
  echo "load-module module-null-source source_name=virtmic" >> ~/.config/pulse/default.pa
}
systemctl --user enable pulseaudio.service pulseaudio.socket || true
systemctl --user restart pulseaudio.service || true

echo ">> Let the service keep running when you're not logged in"
sudo loginctl enable-linger "$USER"

echo
echo "Next:"
echo "  1. cp .env.example .env  &&  edit it"
echo "  2. ./.venv/bin/python -m bot.graph login       # Microsoft device code"
echo "  3. ./.venv/bin/python -m bot.zoom_login        # needs a screen, once"
echo "  4. ./.venv/bin/python -m bot.graph             # verify tagged events show up"
