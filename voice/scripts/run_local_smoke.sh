#!/usr/bin/env bash
# Local (no-GPU) end-to-end smoke: starts two mock TTS instances (+ mock ASR) +
# the real orchestrator (which calls the REAL Claude Haiku API), drives one
# turn, then tears everything down. Uses the Mac's system python3.
set -uo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
LOGDIR="$APP/data"; mkdir -p "$LOGDIR"
# 8765 is the orchestrator's default port and may already be serving; use a free port here.
export LA_WS_PORT="${LA_WS_PORT:-8799}"
# two mock TTS instances, matching the model-ids the model-routing suite expects.
export MOCK_TTS_INSTANCES="${MOCK_TTS_INSTANCES:-9002:gepard-1.0,9003:gepard-1.0-alt}"
export LA_TTS_MODELS="${LA_TTS_MODELS:-gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003}"
# Operator email for the multi-client suite: the orchestrator treats this address
# as an operator (see-all view), and smoke_multi.py reads the SAME env var to know
# which simulated Access email to connect as for the operator cases. Exported, so
# both the server and the smoke child inherit it.
export LA_OPERATOR_EMAILS="${LA_OPERATOR_EMAILS:-operator.smoke@example.com}"
# The main orchestrator runs with lab mode OFF so the pre-existing suites stay
# byte-identical (lab mode is inert when off). The lab suite starts its OWN
# LA_LAB_MODE=1 orchestrator on a free port (see smoke_lab.py).
export LA_LAB_MODE="${LA_LAB_MODE:-0}"

cleanup() {
  [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> starting mock ASR/TTS ($MOCK_TTS_INSTANCES)"
"$PY" "$APP/web/mock_asr_tts.py" >"$LOGDIR/mock.log" 2>&1 &
MOCK_PID=$!

echo "==> starting orchestrator on :$LA_WS_PORT (real Claude Haiku, TTS_MODELS=$LA_TTS_MODELS)"
LA_FUNASR_URL="http://localhost:9001/v1" LA_TTS_URL="http://localhost:9002" \
  "$PY" "$APP/web/server.py" >"$LOGDIR/web.log" 2>&1 &
WEB_PID=$!

# wait for the WS port to accept connections
for i in $(seq 1 20); do
  if nc -z localhost "$LA_WS_PORT" 2>/dev/null; then break; fi
  sleep 0.5
done

echo "==> batching turn"
"$PY" "$APP/scripts/smoke_ws.py"; RC1=$?
echo "==> ASR hints (hotwords + replacements)"
"$PY" "$APP/scripts/smoke_hints.py"; RC2=$?
echo "==> TTS model routing + switch"
"$PY" "$APP/scripts/smoke_models.py"; RC3=$?
echo "==> session history (list/get/rename/delete + malicious-id rejection)"
"$PY" "$APP/scripts/smoke_sessions.py"; RC4=$?
# smoke_multi is parity-neutral: it persists sessions by renaming (no ASR
# segments, no LLM), so it never shifts the mock ASR counter and its position is
# free. Grouped with the other session/scope suites.
echo "==> multi-client scope isolation + operator see-all view"
"$PY" "$APP/scripts/smoke_multi.py"; RC7=$?
# smoke_cancel runs LAST among the parity-sensitive suites: its ASR segments
# would otherwise shift the mock ASR's process-global canned-segment counter that
# earlier scripts rely on for parity.
echo "==> barge-in cancellation (cancel_turn / reply_cancelled)"
"$PY" "$APP/scripts/smoke_cancel.py"; RC5=$?
# smoke_spec runs after cancel: it sends segments and starts a throwaway second
# orchestrator (LA_SPEC_START=0), so nothing after it depends on shared state.
echo "==> speculative LLM start (invisible / refire / committed / disabled)"
"$PY" "$APP/scripts/smoke_spec.py"; RC6=$?
# smoke_lab starts its OWN LA_LAB_MODE=1 orchestrator (free port) and drives
# utterances via the Part A filename override, so it never touches the mock ASR
# canned counter and does not perturb the suites above.
echo "==> lab-command gate (proceed / confirm+execute / reject / strict / chat / cancel)"
"$PY" "$APP/scripts/smoke_lab.py"; RC8=$?
# smoke_events also starts its OWN LA_EVENTS=1 / LA_LAB_MODE=1 orchestrator (free
# port) and drives operator injects + a stub timed completion via the Part A
# filename override, so it never touches the mock ASR canned counter.
echo "==> proactive event channel (inject broadcast / alert preempt / info defer / auth / stub timer)"
"$PY" "$APP/scripts/smoke_events.py"; RC9=$?
# smoke_addressed also starts its OWN LA_ADDRESSED=1 / LA_LAB_MODE=1 orchestrator
# (free port) and drives utterances via the Part A filename override, so it never
# touches the mock ASR canned counter.
echo "==> addressed-speech detection (lab command / side speech / confirm fast path / talk about it)"
"$PY" "$APP/scripts/smoke_addressed.py"; RC10=$?
# smoke_capture starts its OWN LA_CAPTURE=1 orchestrator (free port) writing to a
# THROWAWAY captures dir it moves aside afterwards, and drives utterances via the
# Part A filename override, so it never touches the mock ASR canned counter.
echo "==> segment capture (capture_state / wav + jsonl record / label fold / bad label)"
"$PY" "$APP/scripts/smoke_capture.py"; RC11=$?
# smoke_allowlist starts its OWN LA_ALLOWLIST orchestrator (free port) and only does
# the connect handshake (no ASR/TTS), so it never touches the mock ASR canned counter.
echo "==> email allowlist (allowlisted admitted, non-listed + missing rejected, operator)"
"$PY" "$APP/scripts/smoke_allowlist.py"; RC12=$?

echo "==> orchestrator log tail:"; tail -n 5 "$LOGDIR/web.log" 2>/dev/null || true
[ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && [ "$RC3" -eq 0 ] && [ "$RC4" -eq 0 ] \
  && [ "$RC5" -eq 0 ] && [ "$RC6" -eq 0 ] && [ "$RC7" -eq 0 ] && [ "$RC8" -eq 0 ] \
  && [ "$RC9" -eq 0 ] && [ "$RC10" -eq 0 ] && [ "$RC11" -eq 0 ] && [ "$RC12" -eq 0 ]
RC=$?
[ "$RC" -eq 0 ] && echo "SMOKE: PASS" || echo "SMOKE: FAIL: one or more checks failed"
exit "$RC"
