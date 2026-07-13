#!/usr/bin/env bash
# Bring up the FULL integrated stack on localhost, with every module real, and leave it
# running so a human can put on a headset and talk to it.
#
#   Lab Agent API (real Claude planner)   127.0.0.1:8000   <- app/, this repo
#   orchestrator  (voice half + seam)     127.0.0.1:8766   <- serves the page + WS
#   ASR  Fun-ASR-Nano   (real, GPU)       127.0.0.1:8030   <- via deploy/dev-forward.sh
#   TTS  gepard-1.0     (real, GPU)       127.0.0.1:8040   <- via deploy/dev-forward.sh
#
# Then open  http://127.0.0.1:8766  and speak. localhost is a secure context, so the
# microphone works with no HTTPS and no tunnel.
#
# The port is 8766, NOT 8765, so this never collides with a separately running
# standalone orchestrator.
#
# Prereqs: the GPU services must already be forwarded (bash deploy/dev-forward.sh --bg),
# and the Anthropic key must be readable (credentials/anthropic_key.txt or the env).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"      # .../voice
ROOT="$(cd "$HERE/.." && pwd)"                                # repo root (holds app/)
PY="${LA_PYTHON:-python3}"

PORT="${LA_WS_PORT:-8766}"
BACKEND_PORT="${LA_LAB_BACKEND_PORT:-8000}"
ASR_PORT="${LA_ASR_PORT:-8030}"
TTS_PORT="${LA_TTS_PORT:-8040}"

# --- the GPU services have to be up: fail fast, not at the first spoken word ---
curl -s -m 5 -o /dev/null "http://127.0.0.1:$ASR_PORT/v1/models" || {
  echo "!! ASR not reachable on :$ASR_PORT. Start the forward first:"
  echo "   bash deploy/dev-forward.sh --bg"; exit 1; }
curl -s -m 5 -o /dev/null "http://127.0.0.1:$TTS_PORT/health" || {
  echo "!! TTS not reachable on :$TTS_PORT. Start the forward first:"
  echo "   bash deploy/dev-forward.sh --bg"; exit 1; }
echo "==> real ASR (:$ASR_PORT) and real TTS (:$TTS_PORT) are up"

KEY="${LA_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
if [ -z "$KEY" ] && [ -s "$HERE/credentials/anthropic_key.txt" ]; then
  KEY="$(tr -d '[:space:]' < "$HERE/credentials/anthropic_key.txt")"
fi
[ -n "$KEY" ] || { echo "!! no Anthropic key (LA_ANTHROPIC_API_KEY or credentials/anthropic_key.txt)"; exit 1; }

mkdir -p "$HERE/logs"

# --- the Lab Agent API, on loopback ONLY ---------------------------------------
# It has no authentication of its own, so it must never be exposed. The orchestrator
# is the only thing that talks to it.
( cd "$ROOT" && ANTHROPIC_API_KEY="$KEY" "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" --log-level warning \
    >"$HERE/logs/lab-agent.log" 2>&1 & echo $! >"$HERE/logs/lab-agent.pid" )
for _ in $(seq 1 40); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$BACKEND_PORT/health" && break
  sleep 0.5
done
curl -s -m 3 -o /dev/null "http://127.0.0.1:$BACKEND_PORT/health" || {
  echo "!! Lab Agent API failed to start; see logs/lab-agent.log"; exit 1; }
echo "==> Lab Agent API up on :$BACKEND_PORT (real Claude planner, loopback only)"

# --- the orchestrator, with the seam pointed at it -----------------------------
cd "$HERE"
LA_WS_PORT="$PORT" \
LA_WS_HOST="${LA_WS_HOST:-127.0.0.1}" \
LA_LAB_MODE=1 \
LA_LAB_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" \
LA_FUNASR_URL="http://127.0.0.1:$ASR_PORT/v1" \
LA_TTS_MODELS="gepard-1.0=http://127.0.0.1:$TTS_PORT" \
LA_TTS_URL="http://127.0.0.1:$TTS_PORT" \
LA_ANTHROPIC_API_KEY="$KEY" \
  "$PY" web/server.py >"$HERE/logs/web.log" 2>&1 &
echo $! >"$HERE/logs/web.pid"
sleep 4
curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/" || {
  echo "!! orchestrator failed to start; see logs/web.log"; exit 1; }

echo "==> orchestrator up on :$PORT (seam -> :$BACKEND_PORT)"
echo
echo "    OPEN:  http://127.0.0.1:$PORT"
echo "    Click Start, allow the mic, and speak. Try:"
echo "      \"Run an ELISA on today's plasma samples.\""
echo "      \"CRP, 24 samples, 400 microliters per well.\"   (400 is deliberately unsafe)"
echo "      \"Make it 100 microliters per well.\""
echo "      \"Yes, go ahead.\""
echo
echo "    stop:  bash deploy/stop-integration-local.sh"
echo "    logs:  tail -f logs/web.log logs/lab-agent.log"
