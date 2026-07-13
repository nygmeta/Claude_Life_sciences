#!/usr/bin/env bash
# Runs LOCALLY, in a NORMAL Terminal window (not from an agent/background session:
# launchctl refuses domain operations from those with "141: Reentrancy avoided",
# and this script checks for that and stops).
#
# Installs the demo stack as three per-user LaunchAgents so it runs in the login
# session, managed by launchd, independent of any agent session's health:
#
#   ai.lab-assistant.forward   SSH port-forward to the GPU host (:ASR/:TTS)
#   ai.lab-assistant.web       orchestrator, web/server.py on loopback
#   ai.lab-assistant.tunnel    cloudflared connector for the public hostname
#
# All three: RunAtLoad + KeepAlive, so they start at login, restart on crash or
# dropped connection, and survive sleep and reboot.
#
# Values come from the gitignored credentials/host.env (LA_SSH_TARGET, ports,
# LA_OPERATOR_EMAILS). Rendered plists land in ~/Library/LaunchAgents/; nothing
# with personal values is written inside the repo.
#
# Manage after install:
#   launchctl kickstart -k gui/$UID/ai.lab-assistant.web      # restart one
#   launchctl bootout gui/$UID/ai.lab-assistant.tunnel        # stop one (until next login)
# Uninstall completely:
#   for s in forward web tunnel; do
#     launchctl bootout gui/$UID/ai.lab-assistant.$s
#     launchctl disable gui/$UID/ai.lab-assistant.$s
#   done
#   then move ~/Library/LaunchAgents/ai.lab-assistant.*.plist aside.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$HERE/credentials/host.env"
UNITS="$HERE/deploy/units"
DEST="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"

[ -f "$CONF" ] || { echo "missing $CONF"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
: "${LA_SSH_TARGET:?set LA_SSH_TARGET in credentials/host.env}"
: "${LA_OPERATOR_EMAILS:?set LA_OPERATOR_EMAILS in credentials/host.env}"
ASR_PORT="${LA_ASR_PORT:-8030}"
TTS_PORT="${LA_TTS_PORT:-8040}"
WS_PORT="${LA_WS_PORT:-8765}"

# Refuse to run from a degraded context; everything below would fail anyway.
if ! launchctl print "gui/$UID_N" >/dev/null 2>&1; then
  echo "!!  launchctl cannot reach your login session from this shell."
  echo "    Run this script from a normal Terminal window."
  exit 1
fi
[ -x "$HERE/venv-web/bin/python" ] || { echo "!! venv-web missing; run deploy/run-web-local.sh once first"; exit 1; }
[ -s "$HERE/credentials/cloudflared_token.txt" ] || { echo "!! missing credentials/cloudflared_token.txt"; exit 1; }

mkdir -p "$DEST" "$HERE/logs"

render() {  # $1 = unit short name
  sed -e "s|@REPO@|$HERE|g" \
      -e "s|@SSH_TARGET@|$LA_SSH_TARGET|g" \
      -e "s|@ASR_PORT@|$ASR_PORT|g" \
      -e "s|@TTS_PORT@|$TTS_PORT|g" \
      -e "s|@WS_PORT@|$WS_PORT|g" \
      -e "s|@OPERATOR_EMAILS@|$LA_OPERATOR_EMAILS|g" \
      "$UNITS/mac-$1.plist.in" > "$DEST/ai.lab-assistant.$1.plist"
  plutil -lint -s "$DEST/ai.lab-assistant.$1.plist"
  echo "==> rendered $DEST/ai.lab-assistant.$1.plist"
}

echo "==> stopping any hand-started instances (they would fight the agents for ports)"
pkill -f "$HERE/web/server.py" 2>/dev/null || true
pkill -f 'cloudflared tunnel run' 2>/dev/null || true
pkill -f "ssh.*-L $ASR_PORT:127.0.0.1:$ASR_PORT" 2>/dev/null || true
sleep 1

for s in forward web tunnel; do
  render "$s"
  launchctl bootout "gui/$UID_N/ai.lab-assistant.$s" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$DEST/ai.lab-assistant.$s.plist"
  echo "==> started ai.lab-assistant.$s"
done

echo
echo "==> waiting for the stack to come up"
ok=0
for _ in $(seq 1 30); do
  sleep 2
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:$ASR_PORT/v1/models" \
     && curl -s -m 3 -o /dev/null "http://127.0.0.1:$WS_PORT/"; then ok=1; break; fi
done
if [ "$ok" = 1 ]; then
  echo "==> UP: forward + orchestrator healthy."
  grep -c "Registered tunnel connection" "$HERE/logs/agent-tunnel.log" 2>/dev/null \
    | xargs -I{} echo "==> tunnel registered connections: {}"
  echo "==> public URL is live (behind Cloudflare Access)."
else
  echo "!!  not healthy after 60s. Check:"
  echo "      tail -20 $HERE/logs/agent-forward.log"
  echo "      tail -20 $HERE/logs/agent-web.log"
  echo "      tail -20 $HERE/logs/agent-tunnel.log"
  exit 1
fi
