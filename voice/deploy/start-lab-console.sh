#!/usr/bin/env bash
# Run the console on the machine that sits on the robot's LAN.
#
# This is the "host it yourself" path. It starts the two things that MUST be local, and
# borrows the two things that must not be:
#
#   local  (this script starts them)          remote (already running, over wss://)
#   ------------------------------------      ------------------------------------
#   Lab Agent API   127.0.0.1:8000            ASR   (Fun-ASR-Nano, GPU)
#   console page    127.0.0.1:8090            TTS   (gepard-1.0, GPU)
#                                             safety gates, "did you mean X?" checks
#
# No GPU is needed here, and no speech model is downloaded here. Audio crosses the
# network; the protocol never does. Whatever eventually drives the robot is the machine
# standing beside it.
#
# Usage, first time (the voice host is remembered afterwards):
#
#   bash voice/deploy/start-lab-console.sh --voice <voice-host>
#
# and from then on just:
#
#   bash voice/deploy/start-lab-console.sh
#
# Stop with: bash voice/deploy/stop-lab-console.sh
#
# NOTE ON HARDWARE: this brings the console up to a SIMULATED run. The adapter can
# compile and simulate an Opentrons protocol, but there is no execute() and nothing in
# this repo opens a socket to a robot yet. See doc/HARDWARE_EXECUTION.md. Running this
# script on the robot's LAN is the necessary first half of closing that gap, not the
# whole of it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"      # .../voice
ROOT="$(cd "$HERE/.." && pwd)"                               # repo root (holds app/)
CONF="$HERE/credentials/console.env"                          # gitignored
VENV="$HERE/venv-console"

API_PORT="${LA_API_PORT:-8000}"
CONSOLE_PORT="${LA_CONSOLE_PORT:-8090}"

# --- the voice host: take it from the flag, else from the remembered config -----
VOICE_HOST="${LA_VOICE_HOST:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --voice) VOICE_HOST="${2:-}"; shift 2 ;;
    --voice=*) VOICE_HOST="${1#*=}"; shift ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done
if [ -z "$VOICE_HOST" ] && [ -f "$CONF" ]; then
  # shellcheck source=/dev/null
  source "$CONF"
  VOICE_HOST="${LA_VOICE_HOST:-}"
fi
if [ -z "$VOICE_HOST" ]; then
  echo "!! no speech host set. Pass it once and it will be remembered:"
  echo "     bash voice/deploy/start-lab-console.sh --voice <voice-host>"
  echo
  echo "   <voice-host> is the hostname of the speech service (the box with the GPU)."
  echo "   Without it the console still loads, but it cannot hear or speak."
  exit 1
fi
VOICE_HOST="${VOICE_HOST#wss://}"; VOICE_HOST="${VOICE_HOST#https://}"
VOICE_HOST="${VOICE_HOST%/}"
mkdir -p "$HERE/credentials" "$HERE/logs"
printf 'LA_VOICE_HOST=%s\n' "$VOICE_HOST" > "$CONF"

# --- python + deps -------------------------------------------------------------
PY="${LA_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "!! no python3 on PATH"; exit 1; }

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating a virtualenv at voice/venv-console (one time)"
  "$PY" -m venv "$VENV" || { echo "!! could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
fi
VPY="$VENV/bin/python"
echo "==> installing the Lab Agent's dependencies (fastapi, uvicorn, pydantic, anthropic)"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$VPY" -m pip install --quiet -r "$ROOT/requirements.txt" || {
  echo "!! dependency install failed"; exit 1; }

# --- the Anthropic key: the PLANNER runs here, so this machine needs one --------
# The speech service holds its own key for the "did you mean X?" checks. That one is not
# shared, and is not this one.
KEY="${ANTHROPIC_API_KEY:-}"
if [ -z "$KEY" ] && [ -s "$HERE/credentials/anthropic_key.txt" ]; then
  KEY="$(tr -d '[:space:]' < "$HERE/credentials/anthropic_key.txt")"
fi
if [ -z "$KEY" ]; then
  echo "!! no Anthropic API key. The planner runs on THIS machine, so it needs one."
  echo "   Either:  export ANTHROPIC_API_KEY=sk-ant-..."
  echo "   or put the key in:  voice/credentials/anthropic_key.txt   (gitignored)"
  exit 1
fi

# --- is the remote speech service actually up? ---------------------------------
# Warn, do not abort: the console is still usable in Demo mode (pre-rendered audio) and
# by typing, and saying so beats a page that silently never finds its voice.
if curl -sS -m 8 -o /dev/null "https://$VOICE_HOST/"; then
  echo "==> speech service reachable at $VOICE_HOST"
else
  echo "!!  speech service at $VOICE_HOST did not answer."
  echo "    The console will still load, Demo mode still speaks (pre-rendered audio),"
  echo "    and you can still type. Live voice will not work until it is back."
fi

# --- the Lab Agent, on loopback only -------------------------------------------
# It has no authentication of its own. Nothing but this machine should reach it.
#
# The `exec` is load-bearing. Without it, `( cd .. && cmd ) &` records the PID of the
# WRAPPER SUBSHELL, not of uvicorn, so the stop script kills the wrapper and leaves
# uvicorn holding the port. The next start then fails to bind, or worse, keeps serving
# stale code and looks like the edit did not take. `exec` replaces the subshell with
# uvicorn, so $! is uvicorn's own PID.
( cd "$ROOT" && exec env ANTHROPIC_API_KEY="$KEY" "$VPY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$API_PORT" --log-level warning ) \
    >"$HERE/logs/lab-agent.log" 2>&1 &
echo $! >"$HERE/logs/lab-agent.pid"
for _ in $(seq 1 40); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$API_PORT/health" && break
  sleep 0.5
done
curl -s -m 3 -o /dev/null "http://127.0.0.1:$API_PORT/health" || {
  echo "!! the Lab Agent did not start. See voice/logs/lab-agent.log"; exit 1; }
echo "==> Lab Agent up on 127.0.0.1:$API_PORT (planner, validator, adapters)"

# --- the console page ----------------------------------------------------------
"$VPY" "$HERE/deploy/serve_console.py" --port "$CONSOLE_PORT" \
    >"$HERE/logs/console.log" 2>&1 &
echo $! >"$HERE/logs/console.pid"
sleep 1
curl -s -m 3 -o /dev/null "http://127.0.0.1:$CONSOLE_PORT/console.html" || {
  echo "!! the console server did not start. See voice/logs/console.log"; exit 1; }

URL="http://localhost:$CONSOLE_PORT/console.html?voice=$VOICE_HOST&api=http://localhost:$API_PORT"
echo "==> console up on 127.0.0.1:$CONSOLE_PORT"
echo
echo "    OPEN:  $URL"
echo
echo "    Open it as localhost, NOT as an IP address. localhost is a secure context, so"
echo "    the microphone works with no HTTPS certificate on this machine."
echo
echo "    Try speaking:"
echo "      \"Run an ELISA on today's plasma samples.\""
echo "      \"IL-6, 24 samples, 400 microliters per well.\"   (400 is deliberately unsafe)"
echo "      \"Make it 100 microliters per well.\""
echo "      \"Yes, go ahead.\""
echo
echo "    stop:  bash voice/deploy/stop-lab-console.sh"
echo "    logs:  tail -f voice/logs/lab-agent.log voice/logs/console.log"

# Open a browser if there is one. Never fatal: on a headless box there is not.
if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
fi
