#!/usr/bin/env bash
# Install the INTEGRATED stack (console + Lab Agent) as per-user LaunchAgents, so it
# survives the thing that actually kills demos: not an attacker, a closed laptop lid.
#
#   ai.lab-auto.api   the Lab Agent API   (127.0.0.1:8000, loopback only)
#   ai.lab-auto.web   the orchestrator    (127.0.0.1:8766, console + voice WS)
#
# NOT installed here, because they already exist and are shared with the standalone stack:
#   ai.lab-assistant.forward   the SSH forward to the GPU host (ASR :8030, TTS :8040)
#   ai.lab-assistant.tunnel    the cloudflared connector
#
# RunAtLoad + KeepAlive on both: they come back after a crash, a logout, and a reboot. A
# process started with nohup does none of that, and a demo that dies when the machine
# sleeps was never deployed, it was merely running.
#
# This is the integrated (console) stack. deploy/install-mac-agents.sh is the older
# standalone voice page: different ports, different label prefix, they do not collide.
#
# Usage:  bash deploy/install-integrated-agents.sh
#         bash deploy/install-integrated-agents.sh --uninstall
set -uo pipefail

VOICE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$VOICE/.." && pwd)"
VENV="${LA_VENV:-$VOICE/venv-web}"
AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

WS_PORT="${LA_WS_PORT:-8766}"
BACKEND_PORT="${LA_LAB_BACKEND_PORT:-8000}"
ASR_PORT="${LA_ASR_PORT:-8030}"
TTS_PORT="${LA_TTS_PORT:-8040}"

API_LABEL="ai.lab-auto.api"
WEB_LABEL="ai.lab-auto.web"

if [ "${1:-}" = "--uninstall" ]; then
  for label in "$WEB_LABEL" "$API_LABEL"; do
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null && echo "booted out $label"
    # The plist is MOVED, not deleted: nothing in this tree is removed in place.
    if [ -f "$AGENTS/$label.plist" ]; then
      mkdir -p "$VOICE/deprecated"
      mv "$AGENTS/$label.plist" "$VOICE/deprecated/$label.plist.$(date +%s)"
    fi
  done
  echo "==> uninstalled. The GPU forward and the tunnel are untouched."
  exit 0
fi

# --- preflight: fail here, not at the first spoken word ----------------------
[ -x "$VENV/bin/python" ] || { echo "!! no venv at $VENV. Create it first:"; \
  echo "   python3 -m venv $VENV"; \
  echo "   $VENV/bin/pip install -r $VOICE/web/requirements.txt -r $ROOT/requirements.txt"; exit 1; }

KEY="${LA_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
if [ -z "$KEY" ] && [ -s "$VOICE/credentials/anthropic_key.txt" ]; then
  KEY="$(tr -d '[:space:]' < "$VOICE/credentials/anthropic_key.txt")"
fi
[ -n "$KEY" ] || { echo "!! no Anthropic key (LA_ANTHROPIC_API_KEY or credentials/anthropic_key.txt)"; exit 1; }

curl -s -m 5 -o /dev/null "http://127.0.0.1:$ASR_PORT/v1/models" || {
  echo "!! ASR not reachable on :$ASR_PORT. Start the forward first: bash deploy/dev-forward.sh --bg"; exit 1; }
curl -s -m 5 -o /dev/null "http://127.0.0.1:$TTS_PORT/health" || {
  echo "!! TTS not reachable on :$TTS_PORT. Start the forward first: bash deploy/dev-forward.sh --bg"; exit 1; }
echo "==> GPU services reachable (:$ASR_PORT, :$TTS_PORT)"

mkdir -p "$AGENTS" "$VOICE/logs"

render() {   # render <template> <label> <dest>
  sed -e "s|@LABEL@|$2|g" \
      -e "s|@VOICE@|$VOICE|g" \
      -e "s|@ROOT@|$ROOT|g" \
      -e "s|@VENV@|$VENV|g" \
      -e "s|@WS_PORT@|$WS_PORT|g" \
      -e "s|@BACKEND_PORT@|$BACKEND_PORT|g" \
      -e "s|@ASR_PORT@|$ASR_PORT|g" \
      -e "s|@TTS_PORT@|$TTS_PORT|g" \
      -e "s|@ANTHROPIC_KEY@|$KEY|g" \
      "$1" > "$3"
  plutil -lint "$3" >/dev/null || { echo "!! bad plist: $3"; exit 1; }
  chmod 600 "$3"      # it carries the API key
}

# Stop whatever currently holds the ports (a nohup run from a terminal, or an older agent).
launchctl bootout "gui/$UID_NUM/$WEB_LABEL" 2>/dev/null
launchctl bootout "gui/$UID_NUM/$API_LABEL" 2>/dev/null
pkill -f "$VOICE/web/server.py" 2>/dev/null
pkill -f "uvicorn app.main:app" 2>/dev/null
sleep 1

render "$VOICE/deploy/units/lab-auto-api.plist.in" "$API_LABEL" "$AGENTS/$API_LABEL.plist"
render "$VOICE/deploy/units/lab-auto-web.plist.in" "$WEB_LABEL" "$AGENTS/$WEB_LABEL.plist"

launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$API_LABEL.plist" || { echo "!! failed to load $API_LABEL"; exit 1; }
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$WEB_LABEL.plist" || { echo "!! failed to load $WEB_LABEL"; exit 1; }

# --- health gate: do not claim UP until it actually answers -------------------
ok=0
for _ in $(seq 1 40); do
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:$BACKEND_PORT/health" \
  && curl -s -m 3 -o /dev/null "http://127.0.0.1:$WS_PORT/console.html"; then ok=1; break; fi
  sleep 1
done
[ "$ok" = "1" ] || { echo "!! agents loaded but did not come up. See $VOICE/logs/"; exit 1; }

echo "==> $API_LABEL  UP  (127.0.0.1:$BACKEND_PORT, loopback only)"
echo "==> $WEB_LABEL  UP  (127.0.0.1:$WS_PORT)"
echo
echo "    local:  http://127.0.0.1:$WS_PORT/console.html"
echo "    logs:   tail -f $VOICE/logs/web.log $VOICE/logs/lab-agent.log"
echo "    stop:   bash deploy/install-integrated-agents.sh --uninstall"
echo
echo "    Both survive a crash, a logout, and a reboot (RunAtLoad + KeepAlive)."
echo "    They do NOT survive the Mac going to sleep: disable sleep for the demo window."
