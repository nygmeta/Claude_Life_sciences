#!/usr/bin/env bash
# Runs ON the GPU host. Builds the gepard-1.0 TTS environment.
#
# INSTALL ORDER IS LOAD-BEARING. nemo-toolkit pins transformers <= 4.52, but
# gepard needs the Qwen3.5 backbone that only exists in transformers 5.x, so
# transformers is force-reinstalled AFTER nemo, and gepard_inference goes last so
# it imports against the correct version. pip does not re-resolve across separate
# invocations: whichever install lands last wins. Do not reorder these steps.
#
# Torch is installed explicitly first, from the CUDA index. Preflight runs before
# the long dependency install and again afterwards.
#
# Voices: this script does NOT generate default.pt. The served default voice is a
# preset (en_oak), and make_gepard_voice.py both requires a GPU and re-rolls the
# speaker every time it runs. If tts/voices/ already holds the .pt files (they
# rsync with the tree), nothing is fetched. Otherwise the presets are pulled from
# the public HuggingFace Space, which needs no token.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

VENV="$LA_ENV_DIR/tts"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if [ ! -x "$PY" ]; then
  echo "==> [1/7] creating conda env at $VENV (python $LA_PY_VER)"
  "$LA_CONDA" create -y -p "$VENV" "python=$LA_PY_VER" -c conda-forge --override-channels
else
  echo "==> [1/7] env already exists at $VENV, reusing"
fi

echo "==> [2/7] pip tooling"
"$PIP" install --quiet --upgrade pip wheel setuptools

echo "==> [3/7] torch from $LA_TORCH_INDEX  ($LA_TORCH_SPEC)"
# Reuses the pip wheel cache populated by the asr env, so this is nearly instant.
# shellcheck disable=SC2086  intentional word splitting of the spec
"$PIP" install $LA_TORCH_SPEC --index-url "$LA_TORCH_INDEX"

echo "==> [4/7] GPU preflight BEFORE the expensive install"
"$PY" "$LA_APP_DIR/scripts/preflight_gpu.py"

echo "==> [5/7] base deps (nemo pulls an OLD transformers here, expected)"
"$PIP" install -r "$LA_APP_DIR/tts/requirements.txt"

echo "==> [6/7] force transformers 5.3.0 + numpy<2.0 (gepard needs the Qwen3.5 backbone)"
"$PIP" install --force-reinstall "transformers==5.3.0" "numpy<2.0"

echo "==> [7/7] gepard_inference (last, so it imports against transformers 5.3.0)"
# Pinned to a fixed commit for reproducibility: the whole gepard-inference repo is a
# single inference commit, so this is code-identical to HEAD today, but pinning stops a
# future upstream edit to generate()'s defaults (stop_threshold, max_frames) from
# silently changing synthesis on the next env rebuild.
"$PIP" install "git+https://github.com/nineninesix-ai/gepard-inference@fa19f5793db3f9f8413da6da69bd32659bd4ac4d"

echo "==> re-running preflight (nemo declares an unpinned torch; check it did not clobber)"
"$PY" "$LA_APP_DIR/scripts/preflight_gpu.py"

echo "$PY" > "$LA_APP_DIR/.tts_python"
echo "==> tts env ready: $PY"

VOICES="$LA_APP_DIR/tts/voices"
n=$(find "$VOICES" -maxdepth 1 -name '*.pt' 2>/dev/null | wc -l | tr -d ' ')
if [ "$n" -gt 0 ]; then
  echo "==> voices: $n .pt files already present, not fetching"
else
  echo "==> voices: none present, fetching the presets from the public HF Space"
  "$PY" "$LA_APP_DIR/tts/fetch_voices.py" "$VOICES"
fi

if [ ! -f "$VOICES/en_oak.pt" ]; then
  echo "==> WARNING: en_oak.pt is missing. It is the served default voice, and a"
  echo "    missing voice file makes gepard fall back to an unconditioned speaker"
  echo "    that drifts between sentences, with NO error reported. Fix before use."
fi

echo "==> NEXT: validate on GPU before starting the service:"
echo "    LA_APP_DIR=$LA_APP_DIR \$(cat $LA_APP_DIR/.tts_python) $LA_APP_DIR/scripts/smoke_tts.py"
