#!/usr/bin/env bash
# Set up a Raspberry Pi 5 as a Smart Trolley terminal.
#
# After this the Pi only does two things:
#   1. pi_agent.py  - reads the HX711 load cell + camera, talks to the cloud
#   2. Chromium     - shows the cloud UI fullscreen
#
# Usage:
#   ./scripts/install-pi.sh https://trolley.example.com <DEVICE_TOKEN>
#
set -euo pipefail

CLOUD_URL="${1:-}"
DEVICE_TOKEN="${2:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$CLOUD_URL" ]]; then
  echo "Usage: $0 <cloud-url> [device-token]"
  echo "   e.g. $0 https://trolley.example.com 3f9a...c1"
  exit 1
fi

echo "==> Smart Trolley Pi setup"
echo "    project : $PROJECT_DIR"
echo "    cloud   : $CLOUD_URL"
echo

# ---------------------------------------------------------------------------
echo "==> System packages"
sudo apt-get update
# libzbar0  : barcode/QR decoding for the camera scanner
# python3-venv, python3-dev : venv + build deps for the lgpio wheel
# unclutter : hides the mouse pointer on the kiosk screen
sudo apt-get install -y python3-venv python3-dev libzbar0 unclutter curl swig liblgpio-dev

# The Chromium package is named differently across Raspberry Pi OS releases
# ("chromium" on Bookworm+, "chromium-browser" on older images). Installing a
# name that does not exist aborts apt, and `set -e` would kill the whole script,
# so try each and stop at the first that works.
if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  for pkg in chromium chromium-browser; do
    if sudo apt-get install -y "$pkg" 2>/dev/null; then
      echo "    installed $pkg"
      break
    fi
  done
fi
if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  echo "    !! No Chromium installed. The agent still works; install it later with:"
  echo "       sudo apt install -y chromium"
fi

# ---------------------------------------------------------------------------
echo "==> Python environment"
cd "$PROJECT_DIR"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -r requirements-hardware.txt

# ---------------------------------------------------------------------------
echo "==> GPIO permissions"
# lgpio talks to /dev/gpiochip0; the gpio group grants access without root.
sudo usermod -aG gpio,video "$USER" || true

# ---------------------------------------------------------------------------
echo "==> .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
fi
set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    # value may contain / and &, so use a different delimiter and escape
    python3 - "$key" "$value" <<'PY'
import pathlib, re, sys
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
lines = p.read_text().splitlines()
for i, line in enumerate(lines):
    if line.split("=", 1)[0].strip() == key:
        lines[i] = f"{key}={value}"
        break
p.write_text("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
set_env HARDWARE_MODE cloud
set_env CLOUD_URL "$CLOUD_URL"
set_env KIOSK_URL "$CLOUD_URL"
set_env KIOSK_MODE true
if [[ -n "$DEVICE_TOKEN" ]]; then
  set_env DEVICE_TOKEN "$DEVICE_TOKEN"
else
  echo "    !! No device token given. Add DEVICE_TOKEN=... to .env manually;"
  echo "       it must match the value on the cloud side."
fi
chmod 600 .env

# ---------------------------------------------------------------------------
echo "==> systemd services"
sudo tee /etc/systemd/system/trolley-agent.service >/dev/null <<EOF
[Unit]
Description=Smart Trolley device agent (HX711 load cell + camera)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/pi_agent.py
Restart=always
RestartSec=5
# The agent only needs GPIO and the network.
SupplementaryGroups=gpio video

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trolley-agent.service

# ---------------------------------------------------------------------------
echo "==> Kiosk autostart"
# Raspberry Pi OS Bookworm runs Wayland (labwc or wayfire) by default, so we
# cover both, plus X11/LXDE for older images.
install -Dm755 "$PROJECT_DIR/scripts/kiosk.sh" "$HOME/.local/bin/trolley-kiosk"

# wayfire
if [[ -f "$HOME/.config/wayfire.ini" ]] || command -v wayfire >/dev/null 2>&1; then
  mkdir -p "$HOME/.config"
  touch "$HOME/.config/wayfire.ini"
  if ! grep -q "trolley-kiosk" "$HOME/.config/wayfire.ini"; then
    printf '\n[autostart]\ntrolley = %s/.local/bin/trolley-kiosk\nscreensaver = false\ndpms = false\n' \
      "$HOME" >> "$HOME/.config/wayfire.ini"
  fi
fi

# labwc (default on newer Bookworm images)
if command -v labwc >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/labwc"
  cat > "$HOME/.config/labwc/autostart" <<EOF
$HOME/.local/bin/trolley-kiosk &
EOF
  chmod +x "$HOME/.config/labwc/autostart"
fi

# X11 / LXDE fallback
if [[ -d "$HOME/.config/lxsession/LXDE-pi" ]]; then
  AUTOSTART="$HOME/.config/lxsession/LXDE-pi/autostart"
  grep -q "trolley-kiosk" "$AUTOSTART" 2>/dev/null || \
    printf '@%s/.local/bin/trolley-kiosk\n@xset s off\n@xset -dpms\n' "$HOME" >> "$AUTOSTART"
fi

# ---------------------------------------------------------------------------
cat <<EOF

==> Done.

Next steps
  1. Wire the load cell (see the table in README.md):
       HX711 VCC -> Pi 3V3 pin 1     GND -> pin 6
       HX711 DT  -> GPIO5  pin 29    SCK -> GPIO6 pin 31

  2. Log out and back in (the gpio group needs a fresh session), then check the
     wiring:
       ./.venv/bin/python calibrate_scale.py diagnose

  3. Calibrate with a known weight (1 kg is ideal):
       ./.venv/bin/python calibrate_scale.py calibrate

  4. Start the agent and watch it connect:
       sudo systemctl start trolley-agent
       journalctl -u trolley-agent -f

  5. Reboot to bring up the kiosk browser:
       sudo reboot

  The USB barcode scanner needs no setup: it acts as a keyboard and types
  straight into the kiosk browser.
EOF
