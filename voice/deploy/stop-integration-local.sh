#!/usr/bin/env bash
# Stop the local integrated stack started by run-integration-local.sh.
# Leaves the GPU services and the SSH forward alone: they are shared.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for name in web lab-agent; do
  pid_file="$HERE/logs/$name.pid"
  if [ -s "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    if kill "$pid" 2>/dev/null; then
      echo "stopped $name (pid $pid)"
    fi
    mv "$pid_file" "$pid_file.stopped" 2>/dev/null || true
  fi
done
echo "done. GPU services and the SSH forward are untouched."
