#!/usr/bin/env bash
# Runs ON the GPU host. (Re)launches the two GPU services, detached:
#   ASR        :8030  (env: asr)
#   gepard TTS :8040  (env: tts)
#
# Both bind to LA_BIND_HOST, which defaults to 127.0.0.1. These APIs have no
# authentication, so nothing but a local orchestrator (or an SSH forward) should
# be able to reach them. The orchestrator itself runs elsewhere.
#
# Idempotent: kills a prior instance by pidfile first. Unlike the pod recipe this
# VERIFIES the interpreter exists and that the process is still alive a moment
# after launch, rather than printing success unconditionally.
#
# NOTE: processes started here die on reboot and are not restarted on crash.
# For anything meant to stay up, use deploy/install-services.sh instead, which
# installs the same two services as persistent systemd --user units.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

launch() {  # $1=name  $2=marker-file  $3=script  $4..=args
  local name="$1" marker="$2" script="$3"; shift 3
  local log="$LA_APP_DIR/logs/$name.log" pid="$LA_APP_DIR/logs/$name.pid"

  if [ ! -f "$marker" ]; then
    echo "!!  $name: marker $marker missing, run the setup script first"; return 1
  fi
  local py; py="$(cat "$marker")"
  if [ ! -x "$py" ]; then
    echo "!!  $name: interpreter '$py' from $marker does not exist (stale marker)"; return 1
  fi

  # Match script AND args: tts/server.py can run as sibling instances.
  local pattern="$script"; [ "$#" -gt 0 ] && pattern="$script $*"
  [ -f "$pid" ] && kill "$(cat "$pid")" 2>/dev/null || true
  pkill -f "$pattern" 2>/dev/null || true
  sleep 1

  cd "$LA_APP_DIR"
  setsid nohup "$py" "$script" "$@" </dev/null >"$log" 2>&1 &
  local p=$!
  echo "$p" > "$pid"

  sleep 2
  if kill -0 "$p" 2>/dev/null; then
    echo "==> $name pid $p  log=$log"
  else
    echo "!!  $name died within 2s. Last lines of $log:"; tail -n 15 "$log"; return 1
  fi
}

rc=0
launch asr "$LA_APP_DIR/.asr_python" "$LA_APP_DIR/asr/server.py" \
  --host "$LA_BIND_HOST" --port "$LA_ASR_PORT" || rc=1

LA_TTS_BACKEND=gepard \
  launch tts "$LA_APP_DIR/.tts_python" "$LA_APP_DIR/tts/server.py" \
  --host "$LA_BIND_HOST" --port "$LA_TTS_PORT" || rc=1

echo
echo "==> launched. Models load on startup and take about a minute."
echo "    Poll with: bash $HERE/health.sh"
exit "$rc"
