#!/usr/bin/env bash
# Runs LOCALLY. Starts the orchestrator and the web page on this machine, wired to
# the GPU services through the SSH forward opened by deploy/dev-forward.sh.
#
# This is the whole point of the split topology:
#   - the Anthropic API key never leaves this machine
#   - data/sessions/*.json (full transcripts) never leave this machine
#   - the GPU host holds code and model weights only, and is exposed to nothing
#   - the browser loads http://localhost:8765, a secure context, so the mic works
#     with no HTTPS, no tunnel, and no public URL for an app that has no auth
#
#   bash deploy/dev-forward.sh --bg     # first: open the tunnel
#   bash deploy/run-web-local.sh        # then: serve the page
#   open http://localhost:8765
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$HERE/credentials/host.env"
[ -f "$CONF" ] && { . "$CONF"; } || true
ASR_PORT="${LA_ASR_PORT:-8030}"
TTS_PORT="${LA_TTS_PORT:-8040}"

VENV="${LA_WEB_VENV:-$HERE/venv-web}"   # matches the venv-*/ gitignore rule
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating local web venv at $VENV (no torch, no GPU)"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip wheel setuptools
  "$VENV/bin/pip" install -r "$HERE/web/requirements.txt"
fi

# Refuse to start blind: the forward must already be up, or every turn fails
# at the first ASR call with a confusing connection error.
for p in "$ASR_PORT" "$TTS_PORT"; do
  if ! curl -s -m 3 -o /dev/null "http://127.0.0.1:$p/health"; then
    echo "!!  nothing healthy on 127.0.0.1:$p"
    echo "    Open the tunnel first:  bash deploy/dev-forward.sh --bg"
    echo "    And make sure the GPU services are up on the host:"
    echo "      ssh <host> 'bash -lc \"bash ~/lab-assistant/deploy/run-services.sh\"'"
    exit 1
  fi
done
echo "==> GPU services reachable through the forward"

export LA_FUNASR_URL="http://127.0.0.1:$ASR_PORT/v1"
export LA_TTS_MODELS="gepard-1.0=http://127.0.0.1:$TTS_PORT"
export LA_WS_HOST="${LA_WS_HOST:-127.0.0.1}"   # loopback: no LAN exposure
export LA_WS_PORT="${LA_WS_PORT:-8765}"

echo "==> orchestrator on http://localhost:$LA_WS_PORT  (Ctrl-C to stop)"
cd "$HERE"
exec "$VENV/bin/python" "$HERE/web/server.py"
