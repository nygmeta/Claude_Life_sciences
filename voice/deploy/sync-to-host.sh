#!/usr/bin/env bash
# Runs LOCALLY (on the dev machine). Pushes code to the GPU host.
#
# Host coordinates are NOT committed. They live in the gitignored
# credentials/host.env:
#
#     LA_SSH_TARGET=<ssh alias or user@host>
#     LA_APP_DIR=<absolute path on the GPU host>
#
# What is deliberately NOT sent:
#   credentials/  the GPU host runs only ASR and TTS, which need no secrets.
#                 The API key stays on the machine running the orchestrator.
#   web/          the orchestrator runs locally, not on the GPU host.
#   data/         session transcripts stay on the dev machine.
#   .claude/ deprecated/  unmerged worktrees and backups, never useful remotely.
#   scripts/      except preflight_gpu.py and smoke_tts.py, the two the GPU host runs.
#
# tts/voices/*.pt IS sent (gitignored, but the service needs it, and shipping the
# exact files avoids re-downloading and avoids re-rolling default.pt).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$HERE/credentials/host.env"

[ -f "$CONF" ] || { echo "missing $CONF (see the header of this script)"; exit 1; }
# shellcheck source=/dev/null
source "$CONF"
: "${LA_SSH_TARGET:?set LA_SSH_TARGET in credentials/host.env}"
: "${LA_APP_DIR:?set LA_APP_DIR in credentials/host.env}"

echo "==> ensuring destination exists"
# The remote login shell may be fish, so wrap any payload in bash explicitly.
ssh "$LA_SSH_TARGET" "bash -lc 'mkdir -p \"$LA_APP_DIR/logs\" \"$LA_APP_DIR/scripts\"'"

echo "==> syncing asr/ tts/ deploy/  (includes tts/voices/*.pt)"
rsync -rlt --stats \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '*.log' --exclude '*.pid' --exclude 'out.wav' \
  "$HERE/asr" "$HERE/tts" "$HERE/deploy" \
  "$LA_SSH_TARGET:$LA_APP_DIR/"

echo "==> syncing the two scripts the GPU host needs (not the whole scripts/ dir)"
rsync -rlt --stats \
  "$HERE/scripts/preflight_gpu.py" "$HERE/scripts/smoke_tts.py" \
  "$LA_SSH_TARGET:$LA_APP_DIR/scripts/"

echo
echo "==> synced. On the GPU host:"
echo "    bash $LA_APP_DIR/deploy/setup-asr.sh"
echo "    bash $LA_APP_DIR/deploy/setup-tts.sh"
echo "    bash $LA_APP_DIR/deploy/run-services.sh"
echo "    bash $LA_APP_DIR/deploy/health.sh"
