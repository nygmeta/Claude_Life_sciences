#!/usr/bin/env bash
# Runs LOCALLY (on the dev machine). Starts the Cloudflare Tunnel connector that
# publishes the demo's public hostname to the LOCAL orchestrator (:8765). The
# public-hostname ingress is configured in the Cloudflare Zero Trust dashboard;
# this only runs the connector.
#
#   bash deploy/run-tunnel.sh          start (background), logs/tunnel.log
#   bash deploy/run-tunnel.sh stop     stop the connector
#   bash deploy/run-tunnel.sh status   is it up, and is it registered
#
# SECURITY PRECONDITION, do not remove: this app has NO authentication of its own.
# It is safe to publish ONLY because a Cloudflare Access policy gates the hostname
# at the edge (email one-time PIN), and the orchestrator trusts the
# Cf-Access-Authenticated-User-Email header for client/operator identity. If Access
# is ever detached from the hostname, that header becomes attacker-settable and the
# operator boundary is void. Verify with:
#     curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://<hostname>
# An UNAUTHENTICATED request must answer 302 to a cloudflareaccess.com login. If it
# answers 200, or an origin error such as 1033, Access is NOT in front: stop the
# tunnel and fix that before letting anyone near the URL.
#
# The token is read from the gitignored credentials/cloudflared_token.txt and is
# never echoed. --config /dev/null keeps this connector from picking up an
# unrelated tunnel's ~/.cloudflared/config.yml on this machine.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HERE/logs/tunnel.log"
PIDF="$HERE/logs/tunnel.pid"
TOKEN_FILE="$HERE/credentials/cloudflared_token.txt"
ACTION="${1:-start}"

case "$ACTION" in
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
    pkill -f 'cloudflared tunnel run' 2>/dev/null
    rm -f "$PIDF" 2>/dev/null
    echo "==> tunnel stopped"
    exit 0
    ;;
  status)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      echo "==> connector running (pid $(cat "$PIDF"))"
      grep -c "Registered tunnel connection" "$LOG" 2>/dev/null \
        | xargs -I{} echo "    registered connections: {}"
    else
      echo "==> connector NOT running"
    fi
    exit 0
    ;;
esac

command -v cloudflared >/dev/null 2>&1 || { echo "!! cloudflared not installed"; exit 1; }
[ -s "$TOKEN_FILE" ] || { echo "!! missing $TOKEN_FILE"; exit 1; }

# Refuse to publish a dead origin: the orchestrator must already serve locally.
if ! curl -s -m 5 -o /dev/null "http://127.0.0.1:8765/"; then
  echo "!! nothing serving on 127.0.0.1:8765. Start the orchestrator first:"
  echo "     bash deploy/dev-forward.sh --bg && bash deploy/run-web-local.sh"
  exit 1
fi

mkdir -p "$HERE/logs"
pkill -f 'cloudflared tunnel run' 2>/dev/null; sleep 1

# GODEBUG=netdns=go: use Go's PURE-GO resolver (reads /etc/resolv.conf directly)
# instead of the cgo one, which on macOS goes through mDNSResponder. When
# mDNSResponder is wedged (a common macOS state after network changes or sleep),
# every getaddrinfo call fails, so cloudflared cannot resolve the edge host and
# dies with "Couldn't resolve SRV record ... no such host" even though DNS itself
# is fine (dig, which queries the nameserver directly, still works). The pure-Go
# resolver sidesteps the broken daemon entirely and needs no sudo.
GODEBUG=netdns=go cloudflared --config /dev/null --no-autoupdate tunnel run \
  --token "$(cat "$TOKEN_FILE")" </dev/null >"$LOG" 2>&1 &
echo $! > "$PIDF"
sleep 6

if ! kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "!! connector died immediately. Last lines:"; tail -n 15 "$LOG"; exit 1
fi
n=$(grep -c "Registered tunnel connection" "$LOG" 2>/dev/null || echo 0)
echo "==> connector pid $(cat "$PIDF")  registered connections: $n  log=$LOG"
[ "$n" -gt 0 ] || { echo "   (no registration yet; re-check with: bash deploy/run-tunnel.sh status)"; }
