#!/usr/bin/env bash
# Launch the Smart Trolley UI fullscreen on the Pi's touchscreen.
#
# Reads KIOSK_URL (or CLOUD_URL) from the project's .env, waits for the cloud
# app to answer, then opens Chromium with no browser chrome at all.
#
# Started automatically at login by scripts/install-pi.sh. Run it by hand to
# test:  ./scripts/kiosk.sh
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ---- work out which URL to open -------------------------------------------
get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- ; }
URL="$(get_env KIOSK_URL)"
[[ -z "$URL" ]] && URL="$(get_env CLOUD_URL)"
[[ -z "$URL" ]] && URL="http://localhost:8000"
URL="${URL%/}"
# ask the page to go fullscreen and hide the dev controls
case "$URL" in
  *\?*) URL="${URL}&kiosk=1" ;;
  *)    URL="${URL}?kiosk=1" ;;
esac

echo "[kiosk] target: $URL"

# ---- stop the screen blanking ---------------------------------------------
if [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
  xset s off      || true
  xset -dpms      || true
  xset s noblank  || true
  # hide the mouse pointer after 2s of inactivity
  command -v unclutter >/dev/null && (unclutter -idle 2 &) || true
fi
# Wayland (labwc/wayfire): blanking is disabled in wayfire.ini by install-pi.sh
command -v wlr-randr >/dev/null && wlr-randr >/dev/null 2>&1 || true

# ---- wait for the app to be reachable ------------------------------------
HEALTH="${URL%%\?*}/healthz"
echo "[kiosk] waiting for $HEALTH"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1; then
    echo "[kiosk] cloud app is up"
    break
  fi
  sleep 2
done

# ---- pick a browser -------------------------------------------------------
BROWSER=""
for candidate in chromium-browser chromium google-chrome chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then BROWSER="$candidate"; break; fi
done
if [[ -z "$BROWSER" ]]; then
  echo "[kiosk] No Chromium found. Install it:  sudo apt install -y chromium-browser"
  exit 1
fi

# A dedicated profile keeps "restore pages?" prompts and old state out of the way.
PROFILE="$HOME/.config/trolley-kiosk"
mkdir -p "$PROFILE"
# Clear the crash flags so no "Chrome didn't shut down correctly" bar appears.
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' \
  "$PROFILE/Default/Preferences" 2>/dev/null || true

echo "[kiosk] launching $BROWSER"
exec "$BROWSER" \
  --kiosk \
  --app="$URL" \
  --user-data-dir="$PROFILE" \
  --start-fullscreen \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI,Translate \
  --no-first-run \
  --fast --fast-start \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --no-sandbox \
  "$@"
