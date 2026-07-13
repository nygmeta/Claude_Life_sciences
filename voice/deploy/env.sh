#!/usr/bin/env bash
# Sourced by every deploy/ script. Defines the whole host contract in one place.
# Nothing here names a host, a user, or an absolute home path: every value is an
# env override with a $HOME-relative default, so the same pack runs on any Linux
# box with an NVIDIA GPU and conda.
#
# Override any of these in the environment before invoking a script, e.g.
#   env LA_APP_DIR=/opt/lab-assistant bash deploy/setup-asr.sh

# Where the code lives on the GPU host.
export LA_APP_DIR="${LA_APP_DIR:-$HOME/lab-assistant}"

# Where the conda prefix environments live. Kept OUTSIDE the app dir so a code
# rsync never walks them, and so teardown is one directory.
export LA_ENV_DIR="${LA_ENV_DIR:-$HOME/lab-assistant-envs}"

# Single HuggingFace cache. Splitting the cache across two directories caused a
# real bug once, so there is exactly one cache now.
export LA_HF_HOME="${LA_HF_HOME:-$HOME/lab-assistant-hf}"
export HF_HOME="$LA_HF_HOME"

# Python provisioning. The system python on many Ubuntu boxes cannot create a
# venv (no ensurepip, and installing python3-venv needs root), so conda prefix
# environments are the default. 3.12 is a hard floor: gepard-inference declares
# requires-python >= 3.12.
LA_CONDA="${LA_CONDA:-conda}"
if ! command -v "$LA_CONDA" >/dev/null 2>&1; then
  # A systemd user unit does not get a login shell's PATH, so fall back to the
  # usual install locations rather than failing with "conda: command not found".
  for _c in "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda" \
            "$HOME/anaconda3/bin/conda" /opt/conda/bin/conda; do
    [ -x "$_c" ] && { LA_CONDA="$_c"; break; }
  done
fi
export LA_CONDA
export LA_PY_VER="${LA_PY_VER:-3.12}"

# Torch must be installed explicitly. There is no base image to inherit it from.
# cu128 is the channel that carries sm_120 (Blackwell) kernels; the PyPI default
# wheel does not. Install it BEFORE any requirements file.
export LA_TORCH_SPEC="${LA_TORCH_SPEC:-torch==2.11.0 torchaudio==2.11.0}"
export LA_TORCH_INDEX="${LA_TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"

# Expected compute capability, asserted by scripts/preflight_gpu.py.
export LA_EXPECT_CC="${LA_EXPECT_CC:-12.0}"

# Bind address for the GPU services. Loopback by default: these APIs are
# unauthenticated and only the orchestrator should ever reach them. The
# orchestrator connects over an SSH forward.
export LA_BIND_HOST="${LA_BIND_HOST:-127.0.0.1}"
export LA_ASR_PORT="${LA_ASR_PORT:-8030}"
export LA_TTS_PORT="${LA_TTS_PORT:-8040}"

# The box's uplink to download.pytorch.org is slow and occasionally resets a
# connection mid-wheel. Make pip patient rather than let one reset fail an env
# build, and keep pip's unpack scratch on the big disk under $HOME rather than in
# /tmp, out of the way of any /tmp policy.
export PIP_RETRIES="${PIP_RETRIES:-10}"
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-120}"
export TMPDIR="${TMPDIR:-$LA_APP_DIR/.tmp}"

mkdir -p "$LA_APP_DIR/logs" "$LA_ENV_DIR" "$LA_HF_HOME" "$TMPDIR"
