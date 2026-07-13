# Speculative LLM Start: Implementation Spec (Stage 1)

Fire the Claude call at the segment boundary (~600 ms of silence) instead of the turn
boundary (~1.5 s), generate silently, and release only when the turn commits. Expected
win: 150-350 ms lower perceived reply latency, and commit-to-first-audio collapsing to
roughly one TTS synth call because sentences completed during the turn-pause window are
already queued. Design grounded in a prior research pass on preemptive generation in
voice agents (LiveKit `preemptive_generation`, Deepgram Flux `EagerEndOfTurn`);
orchestration context in `doc/ORCHESTRATION.md`.

*Created: 2026-07-10 01:13 +08. Status: IMPLEMENTED and deployed on 2026-07-10. Local
gate was green pre-deploy (six smoke suites, pytest 19 passed). A live turn against the
deployed stack recorded `fire_to_commit_ms` 1001.6, `commit_to_first_audio_ms` 737.7,
`spec.committed` true, `tts.voice` `en_oak`. The spec body below is the design record
this session implemented against; see `doc/ORCHESTRATION.md`'s speculative-flow
subsection for the before/after summary and `doc/STATUS.md` for the full deploy log.*

## Principles (pinned; do not relitigate at implementation time)

1. **Server-side trigger, zero client changes.** The fire point is the completion of
   `handle_segment`'s ASR, not a new client message. The WS contract does not change:
   the client still sees `reply_start` only after its `end_turn`, exactly as today,
   just sooner. `replyInFlight`, barge-in, and `reply_cancelled` semantics are
   untouched.
2. **Gate everything, not just TTS.** Until commit, the speculative turn sends NOTHING
   to the client (no `reply_start`, no `reply_delta`) and synthesizes nothing. Tokens
   accumulate server-side; complete sentences accumulate in the TTS queue unbatched.
   A discarded speculation must be invisible to the user.
3. **Append-only transcript equality is the commit test.** `sess.pending` is
   append-only within a turn, so a speculation fired from segments [0..n) is valid at
   commit iff `len(sess.pending) == n` (and the joined text matches the snapshot,
   belt-and-braces). No fuzzy reconciliation.
4. **History stays clean until commit.** The speculative producer reads
   `sess.history + [ad-hoc user message]` without mutating the session. `sess.add()`
   for both the user text and the assistant reply happens only on/after commit.
5. **A discarded speculation is silent.** No `reply_cancelled`, no history commit, no
   per-turn latency record; just a counter. `reply_cancelled` remains reserved for
   turns the client knows about.

## State machine

Per session, at most one speculative task, reusing the existing single-task invariant:

- **IDLE** -> segment transcribed (`handle_segment` appended to `sess.pending`) and
  guards pass -> **SPECULATING**: snapshot `(n_segments, user_text)`, spawn the
  speculative task.
- **SPECULATING** -> another segment transcribed -> silently abort the task
  (`_abort_reply_task`-style, no client sends), refire from the new snapshot.
  This is Flux's `TurnResumed`.
- **SPECULATING** -> `end_turn` arrives:
  - snapshot still matches `sess.pending` -> **COMMIT**: run the normal end_turn
    bookkeeping (`sess.add("user", ...)`, asr record), set the task's commit event,
    promote it to the committed reply (it already occupies `sess.reply_task`).
  - snapshot stale (should not happen given refire-on-segment, but guard anyway) ->
    silently abort, fall through to a normal non-speculative turn.
- **SPECULATING** -> `cancel_turn`, `new_session`, or socket close -> silently abort
  (client never saw this turn; nothing to send). Note the client cannot send
  `cancel_turn` here in practice (its `replyInFlight` is false), so this is teardown
  hygiene, not a hot path.
- **COMMITTED** -> existing behavior: barge-in `cancel_turn` -> partial commit +
  `reply_cancelled`; supersede on new `end_turn`; teardown aborts.

## Code changes (`web/server.py`)

- **Session**: add `__slots__` entries `spec_snapshot` (dict: `n_segments`,
  `user_text`, `fired_at`, `fire_count`) or fold into the existing `reply_ctx` with a
  `committed: bool` flag. The task handle reuses `sess.reply_task` so the
  one-in-flight invariant and all teardown paths keep working; `_cancel_reply_task`
  gains an uncommitted branch that skips history/`reply_cancelled`/log.
- **Commit gate**: an `asyncio.Event` created per speculative task, carried in
  `reply_ctx`.
  - `_llm_producer`: before the first client send, and for every send thereafter,
    require the event. Concretely: buffer until `event.is_set()`; on the first
    post-commit iteration flush `reply_start` + one catch-up `reply_delta` with the
    accumulated text, then stream live. `reply_done` is likewise gated (a producer
    that finishes early buffers its `reply_done` until commit).
  - `_tts_consumer`: `await event.wait()` once before its first dequeue. The queue
    itself buffers freely pre-commit.
  - Non-speculative turns pass an already-set event so both coroutines behave exactly
    as today (single code path, no fork of `_run_turn`).
- **Fire point**: tail of `handle_segment`, after `sess.pending.append(text)`:
  abort-and-refire per the state machine. Guards, all env-tunable:
  - `LA_SPEC_START` (default `1`; `0` disables the whole feature, restoring current
    behavior exactly).
  - `LA_SPEC_MAX_TURN_S` (default `12`): skip when `perf_counter() - sess.turn_t0`
    exceeds it (dictation guard; the research is unanimous this is not optional).
  - Skip when `llm_client is None` (mock-less local runs).
- **Commit point**: top of `handle_end_turn`, before the existing supersede call:
  if the in-flight task is an uncommitted speculation with a matching snapshot,
  commit it (bookkeeping + set event) and return; else fall through (the existing
  `_cancel_reply_task` supersede will silently abort a stale speculation via the
  uncommitted branch).
- **LLM timing note**: the producer's ttft clock currently starts at `t_reply`
  (end_turn). For speculative turns, record both `fired_at -> first token` (true LLM
  ttft) and `end_turn -> first audio` (perceived, unchanged definition) so the win is
  measurable rather than defined away.

## Metrics (extend the existing `latency.jsonl` record; same file, same writer)

Add a `spec` block to the per-turn assistant record:
`{"enabled": bool, "fired": <count this turn>, "committed": bool,
"discarded": <count>, "fire_to_commit_ms": ..., "commit_to_first_audio_ms": ...}`.
The three axes to watch, from the research report:

- perceived TTFT (end_turn -> first `reply_audio`): should drop 150-350 ms.
- speculative discard rate: if > ~5-10% of turns, require more accumulated ASR or a
  semantic likely-done signal before firing (Stage 2 hook).
- wasted-token cost: with Haiku this is near-free; if it ever matters, draft with a
  cheaper model and re-call on commit (Deepgram's guidance). Not worth building now.

## Smoke additions (extend `scripts/run_local_smoke.sh` + `scripts/smoke_spec.py`)

1. Segment sent, NO end_turn, wait > LLM mock latency: assert the client received no
   `reply_start` (speculation is invisible).
2. Segment -> second segment -> end_turn: assert exactly one reply, whose user turn is
   both segments joined (refire correctness).
3. Segment -> end_turn: assert reply arrives, and the turn's latency record has
   `spec.committed == true` and a sane `commit_to_first_audio_ms`.
4. `LA_SPEC_START=0`: assert records show `spec.enabled == false` and behavior is
   byte-identical to today's flow.
5. Existing cancellation suite must stay green unchanged (barge-in on a COMMITTED
   speculative turn behaves exactly like today's `cancel_turn`).

## Sequencing

1. Wait for the UI session to finish and commit (this spec exists so nothing runs in
   the shared tree until then).
2. web-core implements per this spec; local smoke gate (all suites incl. the five
   above) + pytest.
3. pod-ops deploys, web-only restart, verifies with a live turn and a
   `spec.committed` record in the deployed system's latency log.
4. docs updates `doc/ORCHESTRATION.md` (improvement direction 1 -> landed) and
   `doc/INTEGRATION.md` (a note that the contract is unchanged but reply timing
   moved); usage-log entry.

## Out of scope (later stages, see the research report)

- Stage 2: dynamic commit window from a text-completeness heuristic on segment
  transcripts (server piggybacks an `endpoint` hint on the `transcript` message;
  client stretches/shrinks `turnSilenceMs`). Keep the fire trigger a pluggable
  predicate so this slots in.
- Stage 3: Smart Turn v3 (BSD-2, 8 MB int8 ONNX, CPU) as the audio commit gate and,
  eventually, the low/high two-threshold scheme. Licensing note: LiveKit's
  turn-detector models are forbidden for standalone orchestrators; do not use.
