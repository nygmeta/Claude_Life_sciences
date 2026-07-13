#!/usr/bin/env bash
# Runs ON the GPU host. Runs a long deploy/ step detached from the SSH session,
# and reports its status and exit code.
#
#   bash deploy/detached.sh start  setup-asr     # runs deploy/setup-asr.sh detached
#   bash deploy/detached.sh status setup-asr     # active / exited(code) / failed(code)
#   bash deploy/detached.sh stop   setup-asr
#
# WHY NOT `setsid nohup`: it is not enough. If the invoking user has no other
# login session and lingering is off, systemd stops the user manager when the SSH
# connection closes and tears down the whole user slice, killing the job with
# NOTHING written to its log. That looks exactly like a silent crash. A transient
# systemd user unit survives it, and records the exit code.
#
# Requires lingering, which the user can enable without sudo:
#     loginctl enable-linger
# Reverse it with `loginctl disable-linger` when handing the machine back.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/env.sh
source "$HERE/env.sh"

# Note: no braces in these :? messages. A '}' inside ${x:?...} ends the expansion
# early and the rest leaks into the variable's value.
ACTION="${1:?usage: detached.sh start|status|stop|log STEP}"
STEP="${2:?step name, for example setup-asr}"
UNIT="la-$STEP"
LOG="$LA_APP_DIR/logs/$STEP.log"

case "$ACTION" in
  start)
    if [ "$(loginctl show-user "$USER" 2>/dev/null | sed -n 's/^Linger=//p')" != "yes" ]; then
      echo "!!  lingering is off for $USER. Run 'loginctl enable-linger' first, or this"
      echo "    job dies silently when the SSH session closes."; exit 1
    fi
    systemctl --user stop "$UNIT" 2>/dev/null || true
    systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    # bash -lc so the login profile is sourced (conda lives on the login PATH).
    systemd-run --user --unit="$UNIT" --quiet \
      bash -lc "cd '$LA_APP_DIR' && bash '$HERE/$STEP.sh' > '$LOG' 2>&1"
    echo "==> started $UNIT   log: $LOG"
    ;;
  status)
    state="$(systemctl --user show "$UNIT" -p ActiveState --value 2>/dev/null || echo unknown)"
    code="$(systemctl --user show "$UNIT" -p ExecMainStatus --value 2>/dev/null || echo '?')"
    echo "unit=$UNIT state=$state exit_code=$code"
    [ "$state" = "active" ] && exit 2   # still running
    [ "$code" = "0" ] || exit 1
    ;;
  stop)
    systemctl --user stop "$UNIT" 2>/dev/null || true
    systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    echo "==> stopped $UNIT"
    ;;
  wait)
    # Block ON THE GPU HOST until the unit leaves 'active'. Blocking here rather
    # than in a local poll loop means a dropped SSH connection costs nothing.
    maxs="${3:-480}"
    end=$((SECONDS + maxs))
    while [ "$SECONDS" -lt "$end" ]; do
      [ "$(systemctl --user show "$UNIT" -p ActiveState --value)" != "active" ] && break
      sleep 5
    done
    echo "=== milestones ==="
    grep -E '^==>|PREFLIGHT|^!!' "$LOG" 2>/dev/null || true
    echo
    state="$(systemctl --user show "$UNIT" -p ActiveState --value)"
    code="$(systemctl --user show "$UNIT" -p ExecMainStatus --value)"
    echo "=== unit=$UNIT state=$state exit_code=$code ==="
    if [ "$state" = "active" ]; then echo "(still running after ${maxs}s)"; exit 2; fi
    if [ "$code" != "0" ]; then echo "--- last 25 lines ---"; tail -n 25 "$LOG"; exit 1; fi
    ;;
  log)
    tail -n "${3:-40}" "$LOG"
    ;;
  *)
    echo "unknown action: $ACTION"; exit 64 ;;
esac
