#!/usr/bin/env bash
# Stop what start-lab-console.sh started. Leaves the venv and the remembered config
# alone, so starting again is instant.
#
# It does not trust the PID file. A stop that PRINTS "stopped" while the process is in
# fact still holding the port is worse than no stop at all: the next start either fails
# to bind or, more confusingly, keeps serving the old code and makes it look like your
# edit did nothing. So the ports are checked afterwards, and the truth is reported.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"      # .../voice

API_PORT="${LA_API_PORT:-8000}"
CONSOLE_PORT="${LA_CONSOLE_PORT:-8090}"

for name in console lab-agent; do
  PIDFILE="$HERE/logs/$name.pid"
  [ -f "$PIDFILE" ] || continue
  PID="$(cat "$PIDFILE" 2>/dev/null)"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
    for _ in $(seq 1 20); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$PID" 2>/dev/null || true
    echo "==> stopped $name (pid $PID)"
  else
    echo "==> $name was not running"
  fi
  mv -f "$PIDFILE" "$PIDFILE.last" 2>/dev/null || true
done

# --- did the ports actually come free? -----------------------------------------
if command -v lsof >/dev/null 2>&1; then
  LEAKED=0
  for port in "$API_PORT" "$CONSOLE_PORT"; do
    HOLDER="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)"
    if [ -n "$HOLDER" ]; then
      LEAKED=1
      echo "!!  port $port is STILL held by pid $HOLDER after the stop."
      echo "    Kill it before starting again, or the next start will bind-fail:"
      echo "      kill -9 $HOLDER"
    fi
  done
  [ "$LEAKED" -eq 0 ] && echo "==> ports $API_PORT and $CONSOLE_PORT are free"
fi
