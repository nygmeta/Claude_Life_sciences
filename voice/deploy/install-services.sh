#!/usr/bin/env bash
# Runs ON the GPU host. Installs the two GPU services as PERSISTENT systemd
# --user units and starts them. This is the durable alternative to
# run-services.sh's detached processes: an enabled unit survives SSH
# disconnect, logout, and unattended reboots, and restarts the service if it
# crashes (Restart=on-failure). Use run-services.sh only for a quick throwaway
# demo; use this for anything meant to stay up.
#
# Requires lingering, which the user can enable without sudo:
#     loginctl enable-linger
#
# After installing, manage with:
#     systemctl --user status lab-assistant-asr lab-assistant-tts
#     journalctl --user -u lab-assistant-tts -n 50 --no-pager -o cat
#     systemctl --user restart lab-assistant-asr
#     systemctl --user disable --now lab-assistant-asr lab-assistant-tts  # off
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

if [ "$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')" != "yes" ]; then
  echo "!!  lingering is off for $USER. Run 'loginctl enable-linger' first, or the"
  echo "    services die when the last login session closes."; exit 1
fi

# The interpreter markers are written by the setup scripts; installing units
# that point at a missing interpreter would just crash-loop.
for m in .asr_python .tts_python; do
  [ -f "$LA_APP_DIR/$m" ] || { echo "!!  missing $LA_APP_DIR/$m: run the setup scripts first"; exit 1; }
done
ASR_PY="$(cat "$LA_APP_DIR/.asr_python")"
TTS_PY="$(cat "$LA_APP_DIR/.tts_python")"
[ -x "$ASR_PY" ] || { echo "!!  asr interpreter '$ASR_PY' not executable (stale marker)"; exit 1; }
[ -x "$TTS_PY" ] || { echo "!!  tts interpreter '$TTS_PY' not executable (stale marker)"; exit 1; }

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

render() {  # $1=template  $2=destination
  sed -e "s|@LA_APP_DIR@|$LA_APP_DIR|g" \
      -e "s|@LA_HF_HOME@|$LA_HF_HOME|g" \
      -e "s|@LA_BIND_HOST@|$LA_BIND_HOST|g" \
      -e "s|@LA_ASR_PORT@|$LA_ASR_PORT|g" \
      -e "s|@LA_TTS_PORT@|$LA_TTS_PORT|g" \
      -e "s|@ASR_PY@|$ASR_PY|g" \
      -e "s|@TTS_PY@|$TTS_PY|g" \
      "$1" > "$2"
  echo "==> wrote $2"
}

render "$HERE/units/lab-assistant-asr.service.in" "$UNIT_DIR/lab-assistant-asr.service"
render "$HERE/units/lab-assistant-tts.service.in" "$UNIT_DIR/lab-assistant-tts.service"

# A leftover run-services.sh instance holds the port and would make the unit
# flap. Stop by pidfile (and pattern, matching run-services.sh's own cleanup).
for name in asr tts; do
  pidf="$LA_APP_DIR/logs/$name.pid"
  if [ -f "$pidf" ]; then
    kill "$(cat "$pidf")" 2>/dev/null || true
    mv "$pidf" "$pidf.replaced-by-systemd"
  fi
done
pkill -f "$LA_APP_DIR/asr/server.py" 2>/dev/null || true
pkill -f "$LA_APP_DIR/tts/server.py" 2>/dev/null || true
sleep 1

systemctl --user daemon-reload
systemctl --user enable --now lab-assistant-asr.service lab-assistant-tts.service

echo
echo "==> units enabled and started. Models load on startup and take about a"
echo "    minute; poll with: bash $HERE/health.sh"
systemctl --user --no-pager status lab-assistant-asr lab-assistant-tts 2>/dev/null | grep -E "service|Active:" || true
