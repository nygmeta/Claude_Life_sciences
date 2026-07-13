#!/usr/bin/env bash
# Runs LOCALLY. Opens an SSH forward so the local orchestrator can reach the GPU
# services, which bind to loopback on the GPU host and are exposed to nothing else.
#
# Nothing is published to the LAN or the internet. The only way in is this tunnel.
#
#   bash deploy/dev-forward.sh          foreground, Ctrl-C to stop
#   bash deploy/dev-forward.sh --bg     background, writes logs/dev-forward.pid
#
# Uses autossh when available so the forward survives a network blip.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$HERE/credentials/host.env"
[ -f "$CONF" ] || { echo "missing $CONF"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
: "${LA_SSH_TARGET:?}"
ASR_PORT="${LA_ASR_PORT:-8030}"
TTS_PORT="${LA_TTS_PORT:-8040}"

FWD=(-N
     -L "${ASR_PORT}:127.0.0.1:${ASR_PORT}"
     -L "${TTS_PORT}:127.0.0.1:${TTS_PORT}"
     -o ExitOnForwardFailure=yes
     -o ServerAliveInterval=30
     -o ServerAliveCountMax=3)

if command -v autossh >/dev/null 2>&1; then
  BIN=(autossh -M 0)
else
  BIN=(ssh)
  echo "note: autossh not found, using plain ssh (dies on a network blip)"
fi

if [ "${1:-}" = "--bg" ]; then
  mkdir -p "$HERE/logs"
  # setsid is Linux-only. On macOS, do NOT use nohup here: in a non-interactive
  # shell with no controlling terminal (an agent/CI runner), nohup itself fails
  # with "can't detach from console: Inappropriate ioctl for device" and nothing
  # starts. Backgrounding with all three streams redirected already detaches the
  # process from the terminal, and `disown` (where the shell supports it) drops
  # it from the job table so it survives the parent exiting.
  if command -v setsid >/dev/null 2>&1; then
    setsid "${BIN[@]}" "${FWD[@]}" "$LA_SSH_TARGET" \
      </dev/null >"$HERE/logs/dev-forward.log" 2>&1 &
  else
    "${BIN[@]}" "${FWD[@]}" "$LA_SSH_TARGET" \
      </dev/null >"$HERE/logs/dev-forward.log" 2>&1 &
  fi
  echo $! > "$HERE/logs/dev-forward.pid"
  disown 2>/dev/null || true
  sleep 2
  if kill -0 "$(cat "$HERE/logs/dev-forward.pid")" 2>/dev/null; then
    echo "==> forward up (pid $(cat "$HERE/logs/dev-forward.pid")): :$ASR_PORT and :$TTS_PORT"
  else
    echo "!!  forward died immediately:"; cat "$HERE/logs/dev-forward.log"; exit 1
  fi
else
  echo "==> forwarding :$ASR_PORT and :$TTS_PORT from the GPU host. Ctrl-C to stop."
  exec "${BIN[@]}" "${FWD[@]}" "$LA_SSH_TARGET"
fi
