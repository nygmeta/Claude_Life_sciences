#!/usr/bin/env bash
# Runs ON the GPU host. Stops the GPU services and frees their VRAM.
#
# This is the whole "on-demand" story: stop before a training run, start after.
# Nothing needs to be automated until a training job actually contends for the
# card, and even then, stopping is simpler than capping per-process memory.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

for name in tts asr; do
  pid="$LA_APP_DIR/logs/$name.pid"
  if [ -f "$pid" ] && kill "$(cat "$pid")" 2>/dev/null; then
    echo "==> $name stopped (pid $(cat "$pid"))"
  else
    echo "==> $name not running"
  fi
done

sleep 2
echo
echo -n "VRAM now: "
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null || true
