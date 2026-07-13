#!/usr/bin/env bash
# Runs ON the GPU host. On-GPU validation of the gepard TTS stack: loads the
# model + codec, synthesizes one sentence, writes $LA_APP_DIR/out.wav. Run it
# after every tts env (re)build, BEFORE starting the service. First run also
# populates the HF cache with the model weights, so the service's first start
# is fast.
#
#   bash deploy/detached.sh start smoke-tts   # detached, log: logs/smoke-tts.log
#   bash deploy/smoke-tts.sh                  # or directly, in the foreground
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

PYFILE="$LA_APP_DIR/.tts_python"
[ -f "$PYFILE" ] || { echo "missing $PYFILE: run setup-tts.sh first"; exit 1; }
PY="$(cat "$PYFILE")"
[ -x "$PY" ] || { echo "tts interpreter $PY not executable: rebuild the env"; exit 1; }

exec "$PY" "$LA_APP_DIR/scripts/smoke_tts.py"
