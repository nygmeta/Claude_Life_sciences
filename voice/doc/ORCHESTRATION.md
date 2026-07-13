# Orchestration: VAD, Turn Detection, and the Reply Pipeline

How the system decides when the user has finished speaking, when to call the LLM,
and how reply audio streams back. Covers the browser (web/index.html) and the
orchestrator (web/server.py). The WS message shapes themselves live in
doc/INTEGRATION.md; this doc explains the timing and control flow around them.

*Created: 2026-07-09*

## 2026-07-09 22:56:01 +08 - Session

### The core principle

The server never decides when a user message is complete. The browser does, using
two nested silence timers, and the server is a passive accumulator until the client
sends `end_turn`. "Streaming TTS" is likewise not a model capability: gepard is a
blocking whole-utterance synthesizer, and the streaming feel comes from the
orchestrator pipelining per-sentence synth calls against the still-running LLM
token stream.

### Input side: two-tier silence detection (all client, web/index.html)

Mic audio is captured at 16 kHz and fed to OmniVAD in 10 ms frames (FRAME = 160
samples). Two different silence durations mean two different things:

- **Tier 1, segment boundary (600 ms default, `vadSegMs`).** OmniVAD fires
  `isSpeechEnd` after `vadMinSilenceFrames` (60 frames) of silence. The collected
  audio is sent as one `audio_segment`. This exists to feed ASR early; it does NOT
  mean the user is done. Details:
  - On `isSpeechStart`, collection begins with a ~300 ms pre-roll ring buffer
    prepended (PREROLL_FRAMES = 30) so the onset the VAD needed for detection is
    not chopped off the ASR input.
  - Segments shorter than 0.3 s are discarded client-side (MIN_SEG_SAMPLES):
    clicks and coughs never reach ASR.
  - `isSpeechStart` also stops assistant playback (barge-in) and, once the
    cancel_turn work lands, sends `cancel_turn` when a reply is in flight.
- **Tier 2, turn boundary (900 ms more, `vadTurnMs`).** When a segment ends, a JS
  timer arms: `setTimeout(endTurn, turnSilenceMs)`. Speech resuming inside the
  window cancels it, and the next segment joins the SAME turn; if the timer
  survives, the client sends `end_turn`, which is the trigger for the LLM call. A
  natural mid-thought pause therefore produces two ASR segments but one reply.
  Total silence before a reply starts is segment pause + turn pause, about 1.5 s
  at defaults.

The two timers decouple "chunk early for ASR efficiency" from "commit the turn".
`vadTurnMs` applies hot (read fresh each time the timer arms); threshold and
segment pause rebuild the OmniVAD instance. All three are live-tunable in the VAD
panel.

### Server side: accumulate, then fire (web/server.py)

- **`handle_segment`**: transcribes each segment immediately on arrival, applies
  post-ASR replacements, appends the text to `sess.pending`, echoes a `transcript`
  message. It does NOT call the LLM. Because ASR runs while the user is still
  mid-turn or during the turn-pause window, most ASR latency is hidden from the
  reply clock. Empty transcriptions are dropped silently.
- **`handle_end_turn`**: joins `sess.pending` with spaces into one user message,
  appends it to history, and launches the reply pipeline. Empty `pending` is a
  no-op, which makes duplicate or spurious `end_turn`s harmless (Stop always
  flushes one; a turn whose only segment transcribed empty produces no reply).
- **Ordering**: the WS receive loop processes messages sequentially, so even if
  the last segment's ASR outlasts the turn pause, the `end_turn` behind it in the
  socket cannot overtake it.

### Reply side: sentence pipelining over the LLM token stream

Two coroutines connected by an asyncio.Queue run concurrently per turn:

- **Producer** (`_llm_producer`): iterates Claude's streamed text deltas. Each
  delta goes to the client as `reply_delta` AND accumulates in a tail buffer;
  `split_sentences(tail)` pops complete sentences onto the queue as they form.
  At stream end, a leftover tail of 2+ chars is queued, then a None sentinel
  (always, even on error, so the consumer never hangs).
- **Consumer** (`_tts_consumer`): pulls sentences and calls the TTS `/synthesize`
  once per sentence, sequentially (avoids GPU contention), sending each WAV as
  `reply_audio` with a 0-based seq. After the sentinel: `reply_audio_end`.

The overlap is the whole trick: while sentence 1 synthesizes, the LLM is still
generating sentences 2 and 3. Time-to-first-audio is roughly (LLM time to finish
sentence 1) + (one synth), logged as `first_ms`. `LA_TTS_STREAM=0` collapses this
to one big synth after `reply_done`.

Sentence splitting (`split_sentences`): a run of `.!?…` immediately followed by
whitespace (lookahead, not consumed). The whitespace requirement prevents
mid-token splits on decimals and URLs. Suppressions: known abbreviations (Mr, Dr,
e.g, a.m, U.S, ...), candidates under 2 stripped chars, lone list markers ("1.",
"a."). Known gap: the terminator set is ASCII plus ellipsis only; CJK terminators
(。！？) are absent and CJK text has no trailing spaces, so a fully Chinese reply
does not split and synthesizes as one chunk at the end. Accepted for now: testing
is English-only.

Error asymmetry by design: if a synth fails mid-reply, the consumer stops
synthesizing but drains the queue and still sends `reply_audio_end`; the user
already has the text, and the turn logs `ok_partial:<err>` or `tts_error:<err>`
instead of "ok".

### Inside one gepard synth call (tts/server.py)

Each `/synthesize` is atomic; no partial audio leaves gepard:

1. Prefill: the voice's ref_codes ([1, T, 32]) are compressed by the RefCompressor
   Q-Former into 8 speaker query tokens prepended to the text embeddings, once per
   generate() call. This per-sentence re-prefill is exactly why the unconditioned
   "default" voice drifted per sentence before default.pt pinned it.
2. Autoregressive decode of 32-codebook NanoCodec frames (temperature, top-k,
   text-CFG per step) until EOS or max_frames. Internally incremental, never
   exposed.
3. One-shot codec decode to a complete 22050 Hz PCM16 WAV.

So three token streams exist and only Claude's is exposed as a stream; audio
delivery granularity equals sentence-splitting granularity. Gapless playback
depends on synth rtf: sentence N+1 must finish synthesizing before sentence N
finishes playing.

### Barge-in and cancel_turn (landed and deployed 2026-07-09)

Before: barge-in was client-only. `isSpeechStart` stopped playback, cleared the
queue, and dropped stale `reply_audio` via an `acceptingAudio` gate, but the
server kept synthesizing the abandoned reply to completion, and because
`handle_end_turn` ran inline in the sequential receive loop, the server was deaf
to ALL messages while a reply generated: a cancel could never even be read, and
the barged-in user's new speech queued behind the entire abandoned synth.

The fix (landed and deployed this session; local smoke and pytest both green, only
the web process was restarted):

- Server runs each turn's producer/consumer pair as a background asyncio.Task so
  the receive loop stays responsive; ASR of the next utterance now overlaps the
  old reply's unwinding.
- New client-to-server `cancel_turn` (sent on barge-in only when a reply is in
  flight, gated by a `replyInFlight` flag so idle VAD false triggers cancel
  nothing); server cancels the task, commits the partial reply text to history
  (context should match what the user heard), logs status "cancelled", and sends
  terminal `reply_cancelled` with the partial text.
- A new `end_turn` arriving while a task still runs supersedes it (cancel + await,
  then start fresh): at most one in-flight reply per session.
- Known residual: cancelling the httpx request does not abort a gepard generation
  already running on GPU; one in-flight sentence still completes server-side.

### Speculative LLM start (landed and deployed 2026-07-10)

Before: the LLM call fired only at the turn boundary. `handle_end_turn` joined
`sess.pending` and started the reply pipeline, so every reply waited out the full
segment pause plus turn pause (about 1.5 s at defaults) before Claude even saw the
first token of user text, even though the ASR text for the last segment was often
sitting ready well before `end_turn` arrived.

The fix (landed and deployed this session; local smoke and pytest both green, only
the web process was restarted):

- The reply pipeline now fires speculatively at the segment boundary, inside
  `handle_segment`, right after ASR appends to `sess.pending`, instead of waiting for
  `end_turn`. The producer and consumer run exactly as before, but everything they
  would send to the client (`reply_start`, `reply_delta`, `reply_audio`) is held behind
  a per-turn commit gate (an `asyncio.Event`) instead of going out immediately.
- A new segment arriving while a speculation is in flight silently aborts it and
  refires from the updated snapshot, same as `cancel_turn`'s abort path but with no
  client-visible signal, since the client never knew this turn existed yet.
- `end_turn` checks whether the in-flight task is an uncommitted speculation whose
  snapshot still matches `sess.pending`; if so it commits (sets the gate event, runs
  the normal `sess.add()` bookkeeping) and the already-running task's buffered output
  flushes to the client immediately, instead of starting a fresh LLM call from zero.
  A stale or missing snapshot falls through to today's non-speculative turn.
- The WS contract is byte-identical: the client still sees `reply_start` only after
  its own `end_turn`, exactly as before, just sooner. Only the timing moved.
- Gated by `LA_SPEC_START` (default on) and `LA_SPEC_MAX_TURN_S` (default 12 s, a
  dictation guard so a very long turn does not keep refiring speculative calls).
- A live turn against the deployed stack recorded `fire_to_commit_ms` 1001.6,
  `commit_to_first_audio_ms` 737.7, and `spec.committed` true, confirming the
  speculative call was already producing tokens well before the turn committed. Full
  design record, state machine, and metrics schema: `doc/SPECULATIVE_START.md`.

### Improvement directions (assessed 2026-07-09, ranked)

1. **Speculative LLM start**: landed and deployed on 2026-07-10. See
   `doc/SPECULATIVE_START.md` for the design spec and live numbers, and the
   "Speculative LLM start" subsection below for the before/after summary.
2. **Binary WS audio upload** (best effort-to-value): the server receive loop
   already accepts raw binary frames as segments, but the client sends base64 in
   JSON: +33% bytes plus encode/decode CPU on the latency-critical path. Switching
   sendSeg to ws.send(buffer) is small and contract-compatible.
3. **Semantic endpointing**: the fixed 900 ms turn pause treats "in Tokyo?" and
   "for the..." identically. ASR text is already available at segment end; a
   completeness heuristic (terminal punctuation / question shape shortens the
   wait, mid-phrase lengthens it) makes the assistant faster AND less
   interruptive. Needs tuning against real speech.
4. **CJK sentence splitting**: add 。！？ to the terminator set with a no-space
   boundary rule when non-English testing starts.
5. **GPU-side cancellation**: a cancellation check inside gepard's decode loop
   would free the GPU on barge-in instead of finishing one abandoned sentence.
   Leave unless barge-in becomes frequent.
6. **Echo-cancellation dependence**: barge-in relies on getUserMedia
   echoCancellation to stop the assistant triggering the VAD on itself. With
   cancel_turn live, self-triggering would kill replies, not just playback. If it
   shows up, duck VAD sensitivity while assistant audio is playing.
