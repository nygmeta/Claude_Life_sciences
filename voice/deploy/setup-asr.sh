#!/usr/bin/env bash
# Runs ON the GPU host. Builds the FunASR-Nano environment.
#
# Torch is installed EXPLICITLY from the CUDA index before anything else, and the
# GPU preflight runs twice: once before the dependency install (so a bad wheel
# fails in seconds rather than after a long download), and once after (because
# funasr declares an unpinned torch dependency and could replace it).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

VENV="$LA_ENV_DIR/asr"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if [ ! -x "$PY" ]; then
  echo "==> [1/5] creating conda env at $VENV (python $LA_PY_VER)"
  "$LA_CONDA" create -y -p "$VENV" "python=$LA_PY_VER" -c conda-forge --override-channels
else
  echo "==> [1/5] env already exists at $VENV, reusing"
fi

echo "==> [2/5] pip tooling"
"$PIP" install --quiet --upgrade pip wheel setuptools

echo "==> [3/5] torch from $LA_TORCH_INDEX  ($LA_TORCH_SPEC)"
# pip caches wheels, so the gepard env's torch install later is nearly instant.
# shellcheck disable=SC2086  intentional word splitting of the spec
"$PIP" install $LA_TORCH_SPEC --index-url "$LA_TORCH_INDEX"

echo "==> [4/5] GPU preflight BEFORE the expensive install"
"$PY" "$LA_APP_DIR/scripts/preflight_gpu.py"

echo "==> [5/5] funasr and service deps"
"$PIP" install -r "$LA_APP_DIR/asr/requirements.txt"

echo "==> re-running preflight (funasr declares an unpinned torch; check it did not clobber)"
"$PY" "$LA_APP_DIR/scripts/preflight_gpu.py"

echo "$PY" > "$LA_APP_DIR/.asr_python"
echo "==> asr env ready: $PY"
