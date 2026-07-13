#!/usr/bin/env bash
# Runs ON the GPU host. Probes real service state, not "a process exists".
# status:loading is NOT a failure: models load on startup. Poll until ok.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

echo -n "ASR        :$LA_ASR_PORT/health -> "
curl -s -m 5 "http://$LA_BIND_HOST:$LA_ASR_PORT/health" || echo -n "FAIL"; echo
echo -n "TTS gepard :$LA_TTS_PORT/health -> "
curl -s -m 5 "http://$LA_BIND_HOST:$LA_TTS_PORT/health" || echo -n "FAIL"; echo

echo
echo -n "GPU: "
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null || echo "nvidia-smi unavailable"
echo "compute apps on the GPU (watch for a neighbouring training job):"
nvidia-smi --query-compute-apps=pid,used_memory --format=csv 2>/dev/null || true
