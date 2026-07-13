# Integration Notes: the LLM Seam

*Created: 2026-07-09 +08.*

This repo is the voice half (VAD, ASR, LLM, TTS) of a two-team hackathon build. A separate
lab-automation project builds the robot-control half. The two meet at exactly one seam: the
LLM step inside `web/server.py`, user text in, reply text out. This note documents that
seam so both sides can build toward it without a mid-hackathon rewrite.

## Where the seam lives today

As of this session, the LLM call lives as `stream_llm(messages, out=None)` in
`web/server.py`, already landed exactly as the WS-agnostic seam described below: it takes
no WebSocket, no session object, and no WebSocket message types. Callers wire it up
however they need (the assistant path pulls sentences out of it for per-sentence TTS; a
smoke test can just collect the chunks).

## The contract: `stream_llm(messages, out=None)`

- **In:** `messages`, the Anthropic-format list of `{"role": ..., "content": ...}` turns
  (what the session already accumulates today via `Session.add`).
- **Out:** `stream_llm` is an async generator; it `yield`s the reply as text chunks while
  Claude streams, so a caller iterates it directly (`async for chunk in stream_llm(...)`)
  rather than registering a callback. The optional `out` argument is a plain dict: after
  the stream ends, `stream_llm` best-effort fills in `out["in_tokens"]` /
  `out["out_tokens"]` from the final message's usage, for latency logging. A caller that
  does not need token counts can leave `out` as `None`.
- Nothing inside the function needs to know it is being called from a browser session: no
  `ws`, no session object, and no WebSocket message types cross this boundary. That is what
  "WS-agnostic" means here, and it is why the sentence-streaming TTS feature could be built
  without changing this seam's signature.

## Why one function, and why it stays one function

Keeping the entire Claude call behind a single, narrow function means there is exactly one
place that needs to change when the two halves merge, instead of a WebSocket handler that
both teams would otherwise need to edit. Mid-hackathon, the merge is expected to grow the
seam, not move it: Claude gains tool-use so it can call out to robot-control functions
defined by the lab-automation side, the caller runs those tools and feeds the results back
to Claude, and Claude's continuation still comes out as plain reply text that flows into
TTS unchanged. From the orchestrator's point of view (transcript display, TTS synthesis,
latency logging), nothing else changes; only what happens inside `stream_llm` grows.

## Merge shape

- **Lanes are directory-scoped** (`web/`, `asr/`, `tts/`, `deploy/` here; the
  lab-automation side keeps its own directories), so each team's code ports into the
  merged repo unchanged.
- **The seam function is a contract file**, the same way the WebSocket message schema is:
  single writer (web-core), and any signature or behavior change is announced explicitly
  before another lane builds on it, the same discipline used for a WS-contract delta.
- Expect a robot-side lane, added by the teammate (human or agent), that plugs into
  `stream_llm` through its tool-use hooks rather than a rewrite of the orchestrator.
- `CLAUDE_USAGE.md` continues in the merged repo unchanged; if judges need the two halves
  distinguished, entries can add a `Workspace:` line.

## Session addition: assistant-path TTS parameters (WS contract)

Unrelated to the LLM seam above, but recorded in this file since it is where WebSocket
contract deltas get written down. The header's config panels are **STT** (ASR hotwords +
replacements, renamed from "Hints"), **VAD** (speech/turn thresholds), and **TTS**
(assistant voice + params, with a trial synthesizer). The TTS panel merges what used to
be a separate "Voice" panel and TTS playground into one: a shared set of controls (voice
choice, temperature, cfg_scale, top_k, max_frames) drives both a preview trial and the
assistant's own params, instead of two panels with independent controls.

- **`{type: "set_tts_params", voice?, temperature?, cfg_scale?, top_k?, max_frames?}`**
  (client to server): sets the session's assistant-path TTS params. A field omitted from
  the message is left unchanged; a field sent explicitly as `null` resets it to the
  server default (the backend then applies its own default). The server replies with a
  `tts_params` message. The message itself is unchanged, but the client's trigger changed:
  it used to send this on every control edit; it now stages edits in the TTS panel and
  sends this only when "Confirm change" is clicked. That button is dirty-gated, disabled
  until the panel's controls differ from the server's last-applied config, with the
  baseline coming from the `tts_params` message received on connect.
- **`{type: "tts_params", params: {voice, temperature, cfg_scale, top_k, max_frames},
  defaults: {temperature, cfg_scale, top_k, max_frames}}`** (server to client): the
  session's current params (`null` for anything unset) plus the server's generation
  defaults (`LA_TTS_TEMP=0.3`, `LA_TTS_CFG=1.0`, `LA_TTS_TOPK=0`, `LA_TTS_MAXFRAMES=1075`).
  Sent on connect and again after every `set_tts_params`. `defaults` has no `voice` key;
  the voice default is resolved by the backend, not the orchestrator.
- **`{type: "list_voices", model?, tag?}` -> `{type: "voices", voices, model, tag}`**:
  `list_voices` still takes an optional `tag` (`"assistant"` or `"playground"`, default
  `"playground"`), echoed back on the `voices` reply. The client now sends `tag:
  "assistant"` on every call, since the merged TTS panel has one shared voice dropdown
  instead of a separate one per panel.
- **`{type: "tts_test", text, model?, voice?, temperature?, cfg_scale?, top_k?,
  max_frames?}`** (client to server, schema unchanged): the merged panel's "Synthesize"
  trial now also sends `max_frames`, a field the server already accepted but the client
  did not previously send.
- These params are threaded into the sentence-streaming TTS consumer described in
  README's "Sentence-streaming TTS" note, so every streamed sentence in a reply is
  synthesized with the session's chosen voice and params, not a fixed default.

## Session addition: session history (WS contract)

Also unrelated to the LLM seam, recorded here for the same reason as the TTS-params
addition above. Every session (the live one and every one that came before it on this
server process) is now persisted to `data/sessions/<id>.json`, one file per session,
written as its messages arrive (gitignored, same directory pattern as
`data/latency.jsonl` and `data/asr_hints.json`), so history survives a server restart.
The persisted transcript (`Session.messages`) is the full, never-truncated conversation,
kept separate from `Session.history`, the window actually sent to the LLM (capped to
`LA_HISTORY_TURNS` user+assistant pairs). The header's `New session` button (renamed from
`Reset`) and the new "History" panel are the client side of this; see README's "Session
history" note for the UI description.

- **`{type: "session_started", id, number, name, started_at}`** (server to client): sent
  once on connect, seeding the client with its live session's identity, and again every
  time that same connection starts a fresh session via `new_session`. `id` is the
  session's internal id (its file's basename, sans `.json`); `number` is a small
  sequential integer assigned at creation, one past the highest `number` found among the
  session files already on disk (starts at 1 with none yet); `name` defaults to
  `"Session {number}"`; `started_at` is an SGT ISO 8601 timestamp (`timespec="seconds"`).
- **`{type: "list_sessions"}`** (client to server) results in **`{type: "sessions",
  sessions: [{id, number, name, started_at}, ...]}`** (server to client): every session
  found on disk, identity fields only, never the full transcript (so the list stays cheap
  regardless of how many past sessions have piled up), sorted by `number` descending
  (newest first).
- **`{type: "get_session", id}`** (client to server) results in **`{type: "session_data",
  id, number, name, started_at, messages: [{role, content}, ...]}`** (server to client):
  the full transcript of one session, live or past. Replies `{type: "error", text:
  "get_session needs an id."}` for a missing or empty `id`, or `{type: "error", text:
  "Session not found."}` if no file matches.
- **`{type: "rename_session", id, name}`** (client to server) results in **`{type:
  "session_renamed", id, name}`** (server to client): renames any session, live or past.
  Renaming the caller's own live session (`id` matches the connection's current session)
  updates it in memory and re-persists; renaming a past session patches its file directly
  (`name` and `updated_at`). Replies `{type: "error", text: "Rename needs an id and a
  non-empty name."}` if either field is missing or empty, or `{type: "error", text:
  "Session not found."}` if `id` is not the live session and no file exists for it.
- **`{type: "delete_session", id}`** (client to server) results in **`{type:
  "session_deleted", id}`** (server to client): deletes a past session's file. Deleting
  the caller's own live session is refused server-side, `{type: "error", text: "Can't
  delete the live session."}`; the client also never renders a delete control for the live
  row. Deleting a file that is already gone still replies `session_deleted` (treated as
  success); any other filesystem error replies `{type: "error", text: "Delete failed."}`
  instead.
- **`{type: "new_session"}`** (client to server) results in **`{type: "session_started",
  id, number, name, started_at}`** (server to client): finalizes the connection's current
  session (its file on disk is left as-is, now a past session) and starts a brand new one,
  reassigning the connection's in-memory session in place. This replaces the old `reset`
  message type entirely: `reset` used to soft-clear `Session.history`/`Session.pending` in
  place and reply with a `status` message; `reset` no longer exists as a message type at
  all, there is no alias, deprecation window, or fallback path for it.

## Session addition: barge-in cancellation (WS contract)

Also recorded here for the same reason as the additions above. Reply generation (the
LLM producer and TTS consumer described in the README's "Sentence-streaming TTS" note)
now runs as a background asyncio task per turn, so the WS receive loop stays responsive
while a reply is in flight: a new speech segment can transcribe while an old reply is
still unwinding. At most one reply task runs per session at a time.

- **`{type: "cancel_turn"}`** (client to server): sent on barge-in (VAD speech start)
  while a reply is in flight, gated client-side by a `replyInFlight` flag. Safe to send
  at any time; with no reply in flight the server no-ops. On receipt, the server cancels
  the in-flight reply task, commits the partial reply text (what the user already
  saw/heard) to the session history as the assistant message if it is non-empty, logs a
  status `"cancelled"` latency record, and sends `reply_cancelled`.
- **`{type: "reply_cancelled", text}`** (server to client): the terminal message of a
  cancelled turn; `text` is the partial reply text streamed so far. `reply_audio_end` is
  not sent for a cancelled turn unless the TTS consumer already sent it before the
  cancel landed, so the client tolerates either order, including `reply_cancelled`
  arriving after `reply_done`.
- A new `end_turn` arriving while a reply task is still running supersedes it: the
  running task is cancelled and cleaned up exactly as above, then the new turn starts.
- Known residual: cancelling only aborts the HTTP request to the TTS backend; a gepard
  generation already running on GPU still finishes that one sentence server-side.

## Session addition: speculative LLM start (timing note, WS contract unchanged)

Landed and deployed 2026-07-10; see `doc/ORCHESTRATION.md`'s "Speculative LLM start"
subsection and `doc/SPECULATIVE_START.md` for the design record. The WS contract itself
does not change: no new message types, no changed fields, and the client still only sees
`reply_start` after its own `end_turn`, exactly as before. What changed is timing only:
`reply_start`, `reply_delta`, and `reply_audio` may now arrive much sooner after
`end_turn`, since the LLM call frequently already started (and produced tokens) during
the segment-boundary pause before the turn even committed. A client should treat this as
pure latency improvement, not a contract change; no client code needs to handle anything
differently.

Also from this session: the gepard-1.0 TTS defaults changed. The
`tts_params` message's `defaults` object now includes `voice` (`"en_oak"`) in addition to
`temperature`, which is now `0.15` (was `0.3`). Set via new envs `LA_TTS_VOICE` and
`LA_TTS_TEMP`.

## Session addition: ASR per-token confidence (additive)

Landed 2026-07-12. The `asr/` service now scores its own output instead of returning a
bare transcript, wrapping the Fun-ASR-Nano decoder's `generate()` once at load to force
`output_scores`/`return_dict_in_generate` and hand back `.sequences`, so `funasr`'s own
`batch_decode` path is untouched. This is additive end to end: a caller that ignores the
new field sees exactly the old contract.

- **`POST /v1/audio/transcriptions`** (`json` and `verbose_json` response formats): both
  gain a nullable `"confidence"` field, `{logprob_mean, logprob_min, prob_mean, prob_min,
  tokens}`. `prob_mean`/`prob_min` are `exp()` of the corresponding logprob, rounded to 4
  decimal places; `tokens` is the count of scored decode steps. `confidence` is `null` when
  `FUNASR_CONFIDENCE=0` (the collection env, default on), when the wrap could not attach to
  this decode path, or when the decoder produced no scored tokens. A wrap or stats failure
  degrades to `null`, never to a broken transcription.
- **`transcript`** (server to client, WS): gains the same additive nullable `confidence`
  field, the block above for that segment. `web/server.py` reads it off the ASR response
  (attribute or `model_extra`) and attaches it unchanged.
- **`data/latency.jsonl`**: the `asr` block's per-turn record gains a nullable
  `confidence` object, `{prob_min, prob_mean, segments: [...]}`: `prob_min` is the minimum
  `prob_min` across the turn's segments (the weakest word anywhere in the turn, what the
  lab-command gate below keys on), `prob_mean` is the mean of the segments' `prob_mean`
  values, and `segments` carries each segment's confidence block (or `null`) verbatim.
  `null` when no segment in the turn carried a confidence block.

## Session addition: the lab-command gate (LA_LAB_MODE, server -> client WS)

Landed 2026-07-12. When `LA_LAB_MODE` is on (default `1`), assistant turns gain a single
Claude tool, `lab_command` (`{intent, args}`, `intent` restricted to a demo command
catalog), inside `stream_llm`'s existing streaming loop, extended for this mode to run up
to 4 tool-use round trips per turn. This is the lab-automation seam described earlier in
this file: `web/lab_gate.py` is pure and WS-free, holding the command catalog, the gate
decision, the spoken readback, and an `AutomationStub` (an in-memory deterministic lab)
that the real lab-automation driver replaces at integration time, keeping the same
async execute/halt/busy surface.

- **The gate decision**: `decision = f(severity tier, turn ASR prob_min)`. Every command
  in the catalog is tagged SAFE, REVERSIBLE, IRREVERSIBLE, or HAZARDOUS. SAFE and
  REVERSIBLE commands proceed by default and only escalate to a confirmation at low
  confidence; IRREVERSIBLE and HAZARDOUS commands always require confirmation (both are
  now **intent-bound**, see below, requiring the exact phrase "confirm `<keyword>`", not
  a bare "confirm" or a loose affirmation), and reject outright below the very-low
  threshold rather than execute on a guess. Thresholds are env-tunable:
  `LA_CONF_LOW` (default `0.75`) and `LA_CONF_VERYLOW` (default `0.50`). **Missing
  confidence escalates, and never rejects** (revised 2026-07-12, review fix F3, replacing
  an earlier "treated as fully confident" rule): with no confidence reading at all, SAFE
  still proceeds (it is read-only), REVERSIBLE and IRREVERSIBLE now ask for confirmation
  (previously REVERSIBLE proceeded), and HAZARDOUS stays `confirm_strict`. The reasoning
  is that a missing reading means the command's wording cannot be verified, so a physical
  command should ask rather than assume it was heard correctly, but the gate must never
  lock out a degraded-ASR user by rejecting outright for a reading it never had.
- **New WS messages (server to client)**:
  - **`action_executed`** `{intent, args, result: {ok, detail, state}, confirmed}`: a
    command ran (either proceeding straight through, or after a spoken confirmation;
    `confirmed` distinguishes the two).
  - **`action_pending`** `{intent, args, readback, severity, confidence, confirm_phrase}`:
    a command needs a spoken confirmation before it runs. `readback` is the grounded,
    digit-by-digit spoken form Claude is instructed to read back verbatim (for example,
    `dispense(50, "A3")` reads back as "dispense five zero, that is 50, microliters into
    well A three"). `confirm_phrase` is additive (landed 2026-07-12, round-3 rework): the
    exact spoken phrase, `"confirm <keyword>"` (for example `"confirm dispense"`), that an
    IRREVERSIBLE/HAZARDOUS pending requires; `null` for a REVERSIBLE/SAFE pending, which
    still accepts a loose confirmation. The client shows this phrase directly in the
    pending strip and event row instead of a generic "say confirm" hint.
  - **`action_rejected`** `{intent, reason}`: the gate refused the command outright
    (confidence too low for its severity tier, or an unrecognized intent).
  - **`action_cancelled`** `{reason: "user" | "superseded" | "expired"}`: a pending
    confirmation was cleared: by the user saying a cancel word, by an unrelated utterance
    superseding it, or (see the pending-expiry note below) by aging past
    `LA_PENDING_TTL_S` unconfirmed.
  - **`action_halted`** `{halted: <string>, state}`: the fast-path emergency stop fired.
    `halted` is a human-readable summary of what was stopped (for example "stirrer at 400
    rpm"); `state` is the automation stub's post-halt snapshot.
- **Intent-bound confirmation** (review fix F1, landed 2026-07-12, round-3): an
  IRREVERSIBLE or HAZARDOUS pending is **bound** and executes ONLY on the exact phrase
  `"confirm <keyword>"` (each command in the catalog carries a spoken `keyword`, for
  example `dispense`, `reagent`, `centrifuge`; matched on a word boundary, case- and
  punctuation-insensitive, so "confirm dispense", "confirm the dispense", and "please
  confirm dispense now" all fire, but "confirm dispenser" does not). A bare "confirm", a
  loose "yes", or the wrong keyword entirely (for example "yes dispense", which contains
  no confirm word at all) does not execute: it re-prompts with the exact required
  phrase (decision `reprompt_unbound`), and the pending action stays armed, never
  executed and never dropped. IRREVERSIBLE and HAZARDOUS now behave identically: the
  earlier HAZARDOUS-only "strict" distinction is gone (the internal `strict` field is
  kept on the pending dict only for backward compatibility and is no longer read; `bound`,
  true for IRREVERSIBLE and HAZARDOUS, is the single source of truth). A REVERSIBLE or
  SAFE (low-confidence) pending is not bound and keeps accepting a loose confirmation
  ("yes", "confirm", "go ahead", ...), since a wrong reversible action costs far less
  than a wrong irreversible one. This is explicitly **not authentication**: it binds a
  confirmation to *which command* it applies to, not to *who* is speaking; anyone
  physically near the microphone who knows the keyword can still confirm, which is why
  this alone only mitigates, and does not solve, an overheard `other_speaker`
  confirmation (see the noise-gate section's open item on the future speaker-gate layer).
- **Turn resolution while a confirmation is pending**: the next turn is resolved without
  an LLM call at all, checked in this order: expiry first (below), then a cancel word
  (which clears the pending action unconditionally), then a full confirmation attempt
  (bound phrase for IRREVERSIBLE/HAZARDOUS, a loose confirm otherwise, additionally
  gated on the confirmation-execution floor below), then, for a bound pending only, a
  bare/loose affirmation without the keyword (intent-bound re-prompt above), and
  anything else supersedes the pending action into a normal LLM turn. This is
  deterministic and cannot be talked out of by an LLM paraphrase of a safety-critical
  acknowledgement.
- **The confirmation-execution floor and no-confidence re-prompt** (`LA_CONFIRM_FLOOR`,
  default `0.40`; `"0"` disables it): landed 2026-07-12 (review fix F2, revised in
  round-3). A spoken confirmation is exempt from the noise gate (see the noise gate
  section below), so it is always heard and never silently dropped; but *execution* is
  gated on how clearly it was heard, since a spurious confirm fires an irreversible
  action. Two distinct re-prompt paths, both keeping the pending armed and never
  executing or dropping the word: **no confidence block at all** now re-prompts (decision
  `reprompt_noconf`, revised 2026-07-12, round-3, reversing the original fail-open
  behavior below) rather than executing, so this is consistent with the command gate's
  own escalate-on-missing-confidence (F3) instead of contradicting it; and a `prob_mean`
  below the floor re-prompts (decision `reprompt_lowconf`), with boundary semantics
  matching the noise gate (a `prob_mean` exactly at the floor executes, the check is a
  strict `<`, not `<=`), keyed on `prob_mean` rather than `prob_min` because the labeled
  capture data shows `prob_min` overlapping between classes. Both re-prompts share the
  same canned reply text ("I heard confirm, but not clearly. Please say it again, or say
  cancel."); only the logged `decision` differs. **Cancel and the fast-path stop remain
  fail-open, unaffected by either check**: the asymmetry is the whole point of this
  design, since a spurious low-confidence *cancel* only costs re-issuing the command
  (reversible), while a spurious low-confidence *confirm* would fire an irreversible
  action (not reversible), so only the confirm path is tightened.
- **Pending-confirmation expiry, with passthrough** (`LA_PENDING_TTL_S`, default `120`
  seconds; `"0"` disables it): landed 2026-07-12 (review fix F4), revised the same day
  in round-3 to add passthrough. A pending action older than the TTL (measured from its
  `created_ts`, checked strictly `>`, so an age exactly at the TTL still resolves
  normally) never fires. What happens next depends on what the turn actually said,
  reversing the original "always consume the turn" behavior: a stale confirm or cancel
  attempt gets the expiry notice ("That request expired. Please repeat the command."),
  `reason: "expired"`, decision `expired`, and consumes the turn, since a "confirm" heard
  long after the original readback must never fire an action the user has moved on from.
  Any OTHER utterance, one that was never trying to confirm or cancel this pending,
  instead drops the pending silently (`action_cancelled` `reason: "expired"`, no canned
  notice) and is processed as a completely normal LLM turn, so an unrelated request is
  answered rather than swallowed by an expiry notice it never asked about.
- **Fast-path stop**: a short, standalone stop-like utterance ("stop", "halt", "abort",
  "emergency stop") is matched lexically before the LLM, at the segment boundary, and
  cancels any in-flight reply and halts the automation stub immediately. A command that
  merely contains a stop word alongside other content, like "stop the stirrer", is not a
  bare stop: it routes through the LLM as the `stop_stirrer` intent, gated like any other
  command. This is a supervisory stop layered on top of whatever hardware e-stop the real
  lab-automation system has, never a substitute for one.
- **Confidence-gated speculation**: speculative LLM start (the segment-boundary firing
  described above) no-ops while a confirmation is pending, since that turn's resolution
  never calls the LLM.
- **`data/latency.jsonl`**: an additive `action` block, `{intent, decision, severity,
  prob_min}` (or `prob_mean` for the confirmation-floor decisions), present on any turn
  where the lab_command tool fired (including a confirm/cancel/reprompt/expired turn),
  `null` otherwise.
- **Client**: `web/index.html` renders five new event rows (Pending, Confirmed/Done,
  Rejected, Cancelled, Halted) plus a strip pinned under the live transcript while a
  confirmation is outstanding, and voice-only: the user speaks the confirmation (the
  bound phrase when one is required, shown directly in the strip and row via
  `confirm_phrase`, or a loose "confirm"/"cancel" otherwise) rather than clicking a
  button. Unknown WS message types are ignored defensively rather than thrown on, so
  this stays forward-compatible with future message types.

## Session addition: proactive announcements (LA_EVENTS)

Landed 2026-07-12 (Phase 3). The assistant can now speak unprompted when the lab produces
an event, not only in reply to a turn. Delivery is per connection, serial, and
severity-arbitrated, so an announcement never garbles an in-flight reply.

- **New WS messages (server to client), always sent as a triple per event, in order**:
  - **`announce`** `{event_id, severity: "info" | "alert", text, source, ts}`: the event
    itself. `source` is `"operator"` (an injected event) or `"stub"` (a timed automation
    completion).
  - **`announce_audio`** `{event_id, audio_b64, sample_rate, format: "wav"}`: exactly one
    per event, synthesized with the receiving session's own TTS model and params. Skipped
    only if that synth call fails; the triple still completes with `announce_end`.
  - **`announce_end`** `{event_id}`: always sent, whether or not audio was.
  - These three are strictly ordered per connection (an `AnnounceManager` delivers one
    announcement at a time, FIFO within a severity), so a client never has to reconcile
    two events' messages interleaved.
- **New WS message (client to server)**: **`inject_event`** `{severity, text,
  broadcast}` (`broadcast` optional, default `true`). Operator-only: a non-operator
  connection (see `doc/MULTI_CLIENT.md`'s operator model) gets `{type: "error", text: "not
  authorized"}` and nothing else happens. `broadcast: true` delivers to every live
  connection, each getting its own `event_id` and its own TTS synth using that
  connection's current session's params; `broadcast: false` delivers only to the sender.
- **Arbitration**: an `alert` always preempts: it aborts an in-flight speculation that has
  not yet committed (invisible to the client, dropped silently), cancels a committed
  in-flight reply (the client sees the ordinary `reply_cancelled`, exactly as a manual
  `cancel_turn` would produce), and clears a pending lab-command confirmation (the client
  sees `action_cancelled` with `reason: "superseded"`), then announces; alerts also jump
  ahead of any queued `info` events. An `info` defers: it is held, event-driven (no
  polling), until no committed reply is in flight, then delivered; it never interrupts. A
  deferred `info` survives an `alert` that preempts ahead of it and is delivered after.
- **Event sources**: `AutomationStub` timed completions route to the connection that owns
  that stub (never broadcast): `start_centrifuge` fires an `info` after its actual run
  duration (`minutes` from the tool call), `set_temperature` fires an `info` about 2
  seconds after being set. These timers are cancelled, and so never fire, on `halt()` (the
  fast-path stop) or on disconnect. The other source is the operator's `inject_event`.
- **Client behavior** (`web/index.html`): `announce` renders an EVENT or ALERT row in the
  transcript (ALERT gets the same strong/inverse treatment as a hazardous pending action).
  Announcement audio is a separate playback path from reply audio: an `info` clip holds
  until reply playback is fully idle, then plays; an `alert` clip hushes any in-flight
  reply or announcement audio, plays a short two-tone WebAudio earcon, then plays the
  clip. Barge-in (the user starting to speak) stops announcement audio exactly as it stops
  reply audio.
- **Not persisted**: an announcement is never added to `Session.messages` (the persisted
  transcript) or to `Session.history` (the LLM's context window). The LLM has no memory of
  what it has already announced this session; see `doc/STATUS.md`'s open items.
- **`data/latency.jsonl`**: a new record `kind: "announce"`, `{event_id, severity, source,
  wait_ms, tts: {ms, error}, session}`. `wait_ms` is queueing time from enqueue to delivery
  start (nonzero mainly for a deferred `info`).
- **Envs**: `LA_EVENTS` (default `1`) gates the whole channel: when off, no `announce*`
  message is ever sent and `inject_event` no-ops. The `AutomationStub` completion timers
  additionally require `LA_LAB_MODE` (both must be on for a stub-sourced event).

## Session addition: protocol walkthrough (LA_LAB_MODE, extends the lab-command gate)

Landed 2026-07-12 (Phase 4b). The assistant can walk an operator through a written
protocol hands-free: reading each step back, tracking where the operator is, and
announcing when a timed step's incubation finishes.

- **No new tool.** Five new SAFE intents ride the EXISTING `lab_command` tool via its
  generated intent enum (built from the `COMMANDS` catalog keys): `protocol_start`
  (optional `name` argument), `protocol_next`, `protocol_back`, `protocol_repeat`,
  `protocol_status`. Everything about the gate, confirmation, and WS event types
  documented above for `lab_command` applies unchanged; navigating a written protocol
  moves no hardware, so these five proceed without confirmation by default, but still
  confirm at very low ASR confidence like any SAFE command.
- **Tool result is the step's verbatim text.** `web/lab_gate.py`'s hardcoded demo
  protocol (a six-step plasmid miniprep) supplies each step's spoken text as the tool
  result detail. The lab system prompt instructs Claude to read that text back to the
  user VERBATIM and forbids it from inventing, reordering, merging, or skipping a
  protocol step, or stating one it did not get from a tool result.
- **Protocol state lives on the automation stub** (`AutomationStub.protocol`, `{name,
  step, total, done}`), the same object the demo lab's other state lives on, and is
  visible to a client already reading `action_executed`'s `result.state`:
  `result.state.protocol.step` is the current 1-based step number.
- **Bounds behavior**: `protocol_back` at step 1 stays on step 1; `protocol_next` past
  the last step completes the protocol idempotently (repeating it just confirms it is
  already done); `protocol_status` before `protocol_start` answers that no protocol is
  running rather than erroring.
- **Timed steps announce through the Phase 3 event channel.** A step with a timer
  schedules an `info` announcement (see the proactive-announcements section above) for
  when its countdown ends, for example "Step 2 incubation complete: 5 minutes elapsed."
  The announcement always states the REAL duration the operator was told to wait, never
  a scaled one. Timers disarm on `protocol_start` / `protocol_next` / `protocol_back`
  (navigating away from a step cancels its timer, so an abandoned step never
  announces), on `halt()`, and on disconnect, exactly like the other automation-stub
  timers.
- **`LA_PROTOCOL_TIMER_SCALE`** (default `1.0`) compresses only the WAIT
  (`lab_gate.step_timer_s`), never the spoken duration: the step text and the
  completion announcement both always say "5 minutes" while the actual countdown can be
  scaled down so a demo or a smoke observes the announcement in about a second instead
  of sitting through a real incubation. A value around `0.01` is a reasonable choice for
  a live demo.

## Session addition: addressed-speech detection (LA_ADDRESSED, additive to `transcript`)

Landed 2026-07-12 (Phase 4a). The assistant runs with an open microphone on a lab
bench, where it also hears colleagues talking to each other. `LA_ADDRESSED` (default
`0`, off, opt-in) gates a classifier that decides, for every transcribed segment,
whether it was said TO the assistant before it is allowed to become part of a turn.

- **`transcript`** (server to client, WS) gains an additive boolean `addressed` when
  the feature is on. A segment classified NOT addressed is still shown to the client
  (`addressed: false`, rendered greyed so the user can see what was heard and ignored),
  but it does not accumulate into the turn, does not trigger speculative LLM start, and
  never earns a reply. With `LA_ADDRESSED` off, `transcript` is byte-identical to
  before, including the absence of the `addressed` field.
- **Deterministic fast paths decide most utterances with no model call**: a pending
  confirmation's confirm or cancel word, a stop utterance, a wake form (the assistant
  addressed by name in vocative position), and a standalone filler with nothing pending
  are all matched by pure regexes (reusing `lab_gate.is_confirm` / `is_cancel` /
  `is_stop` for the first two) before any Claude call is considered.
- **One bounded Claude Haiku call for the genuinely ambiguous rest**, forced through a
  tool (`report_addressed`, `{addressed, confidence, reason}`) with `tool_choice`
  pinning that exact tool, so the classifier can never answer in prose. Bounded by
  `LA_ADDRESSED_TIMEOUT_S` (default `2.0`).
- **Fails OPEN.** Any error, timeout, or malformed answer from the classifier returns
  `addressed: true`, so a classifier hiccup can never silently swallow the user's
  speech; the worst case is one unnecessary reply, not a dropped turn.
- **`data/latency.jsonl`**: dropped side speech logs a record `kind: "sidespeech"`,
  `{text, confidence, reason, ms}` (`ms` is the classifier call's wall time), in place
  of the usual assistant-turn record for that segment.

## Session addition: the iOS mic fix (client capture path, no WS contract change)

Landed 2026-07-12 (`web/index.html` only; no server-side change). The mic worked on
desktop Chrome but silently failed on iOS Safari/Chrome: no error, just no audio ever
reaching the VAD. Root cause: the client used to construct its `AudioContext` with an
explicit `{sampleRate: 16000}`, which WebKit refuses, pinning every context to the
hardware rate (48000 on iOS) regardless of what is requested. Three changes, together:

- **Capture at the hardware rate, resample in-client to the wire's fixed 16 kHz.** The
  `AudioContext` is now constructed with no `sampleRate` option, so it runs at whatever
  rate the platform gives it. A small streaming resampler (a box-filter lowpass plus
  linear interpolation, both with cross-block state so a long recording resamples
  identically to one cut into `onaudioprocess` blocks) downsamples to 16 kHz before the
  audio is sent. On native 16 kHz hardware the resampler is the identity function
  (zero cost); the WS wire format is unchanged either way; a server that never sees this
  commit is unaffected.
- **The `AudioContext` is created and `resume()`d synchronously inside the click
  handler, before the `getUserMedia` await.** iOS creates every context suspended and
  only honors `resume()` from inside a user gesture; awaiting the microphone permission
  first breaks that gesture chain, leaving a context that is technically running but
  never actually processes audio, with no visible error.
- **Permission failures and setup failures now report distinct, actionable text**
  instead of one generic "mic permission failed" string: a `NotAllowedError`/
  `SecurityError` names the iOS Settings path to check, `NotFoundError` says no
  microphone was found, an insecure origin (`getUserMedia` entirely absent) says the
  page needs HTTPS or localhost, and anything else during context/graph setup is
  reported as its own distinct error rather than folded into the permission message.
  The status line after a successful start also states the real hardware rate (for
  example "listening (48000 Hz -> 16000)") so a resampled session is visible, not silent.

## Session addition: capture-mode client metadata (`client_info`, additive)

Landed 2026-07-12, alongside segment capture (below). A new client-to-server message,
**`client_info`** `{ua, platform, hw_sample_rate, resampled, vad_threshold,
seg_pause_ms, turn_pause_ms, viewport}`, reports the environment that produced the
audio: browser, OS/platform, the real capture rate and whether the resampler above is
in the path, the live VAD settings, and whether the viewport is `"mobile"` or
`"desktop"`. Metadata only, never used for any decision. Sent on connect (rate
unknown yet), again right after a successful mic start (rate now known), and again
after every applied VAD-panel change, so the server's copy always describes the
settings that actually produced the most recent audio; the latest one sent always
wins, no history is kept. The server stores it on the session regardless of whether
capture mode is on, so flipping `LA_CAPTURE` on mid-session does not produce capture
records with a `null` `client_info`.

## Session addition: segment capture (LA_CAPTURE, LA_CAPTURE_DIR)

Landed 2026-07-12. An opt-in debug mode for internal testing (with the testers'
consent): on iOS, background noise sometimes gets transcribed into words (occasionally
even into a hotword), and no filter can be tuned without the offending audio. When
`LA_CAPTURE` is on (default off, and off means off: not one byte is written to disk,
`capture_state` reports `on: false`, and `label_segment` is a no-op), every uploaded
speech segment, including the ones that never reach the user (an ASR error, an empty
transcript), is saved as a 16 kHz mono WAV plus one append-only JSONL record, and a
tester can label a transcript as noise, another speaker, or real speech.

- **`capture_state`** `{on}` (server to client, sent on connect): tells the client
  whether capture mode is live, so it knows whether to show the per-segment labeling
  controls at all. Absent entirely when off leaves the client byte-identical to before
  this feature.
- **`label_segment`** `{id, label: "noise" | "other_speaker" | "speech"}` (client to
  server, capture mode only): labels one segment by its transcript id. Own-scope only:
  `id` must be a transcript id this connection's own live session actually captured
  (there is no cross-scope labeling, not even for an operator); an id the connection
  never captured is a silent no-op ack. Answered by **`segment_labeled`** `{id, label}`
  (server to client), which the client also uses to move a locally-optimistic tag from
  "pending" (sent, unconfirmed) to acknowledged.
- **Client rendering** (revised 2026-07-12): the live transcript aggregates a turn's
  addressed, accepted segments into one row, matching the persisted history, which is
  also one line per turn. Capture mode adds a numbered chip strip under that row, one
  chip per segment; tapping a chip opens the same three-choice label menu for that
  segment's own transcript id, so labels still map 1:1 to individual recordings even
  though the visible text is merged. A gate-discarded or side-speech segment (see the
  noise gate and addressed-speech sections) still renders as its own standalone muted
  row with its own flag-button label control, since it never joined a turn to begin
  with, and that control is deliberately never dimmed like the rest of the muted row:
  a wrongly dropped clip is exactly the one worth flagging, so it must stay
  full-strength.
- **Layout under `LA_CAPTURE_DIR`** (default `../data/captures`, gitignored):
  `<YYYY-MM-DD>/<sid>-<seq>.wav` (the exact 16 kHz PCM16 bytes sent to the ASR, wrapped
  as a WAV header, never re-encoded; `seq` is the transcript id, so the clip, the JSONL
  record, and the on-screen line all join), and one append-only `captures.jsonl` with
  two record kinds: `{kind: "segment", ts, date, sid, scope, seq, wav, dur_s, rms, peak,
  transcript, confidence, hotwords_active, addressed, accepted, reject_reason,
  client_info, label: null, error}` per captured segment, and `{kind: "label", ts, sid,
  seq, label}` per label action. The file is append-only by design: a label is never an
  in-place edit of its segment's line (that would race with concurrent appends from
  other segments), so a reader folds the **last** label record per `(sid, seq)`,
  exactly as `scripts/capture_report.py` does.
- **`scripts/capture_report.py`**: reads `captures.jsonl` back and answers the question
  capture exists for, whether an ASR-confidence threshold can separate noise from real
  speech. Prints per-label counts, how many segments the noise gate below already
  rejected (and why), and a `prob_mean`/`prob_min`/`dur_s`/`rms` stats table per label
  (min/mean/p50/p90/max); `--csv <path>` (or `-` for stdout) also writes one row per
  clip for spreadsheet analysis. This is exactly the report whose output (below)
  calibrated the noise gate.
- **Off the latency path**: `capture_segment()` returns immediately, handing the level
  math (RMS/peak over every sample) and both disk writes to a worker thread
  (`asyncio.to_thread`), so the speculative LLM call firing right behind it is never
  held up by disk I/O; every write is inside its own try/except, so a full disk or a
  permissions error costs a log line, never the user's turn.
- **Privacy note**: internal testing only, with the testers' consent, and there is a
  deliberate operator decision behind one omission: the page shows **no on-page
  recording indicator**. The capture-mode labeling controls (the flag button on each
  segment row) are the only visible cue that anything unusual is happening, and they
  only appear when capture is on. This is acceptable for a small internal test group
  who know capture is happening, and should be revisited (an explicit on-screen
  indicator added) before anyone outside that group is ever recorded this way.

## Session addition: per-scope ASR hints, new defaults (`get_hints`/`set_hints`, shape unchanged)

Landed 2026-07-12. Hints (hotwords + replacements) used to live in one process-global
dict backed by a single file, so any client's `set_hints` silently rewrote the ASR bias
*and* the transcripts of every other client sharing the server. They are now scoped
exactly like sessions: one file per scope under `data/hints/<scope>.json` (`"public"`
or the 16-hex `sha256(lower(email))[:16]` token), with an in-process cache keyed by
scope so the per-segment hot path (every VAD segment reads hints before calling the
ASR) stays a plain dict lookup, never a file read.

- **`get_hints`/`set_hints` keep their exact existing message shapes** (see the
  "assistant-path TTS parameters" section above for the general hints description);
  only their *values* are now the connection's own scope. **No cross-scope operator
  addressing exists for hints at all**: unlike `list_sessions`/`get_session` (which an
  operator can address into another scope), a client-supplied `scope` field on a hints
  message is ignored for every connection, operator included. Hints silently rewrite
  the transcript a user sees and hears the assistant respond to, so a cross-scope
  write would be a footgun with no demo value; the operator's existing session-level
  "see all" view is unaffected by this.
- **New defaults for any scope with no saved file**: hotwords `["Claude"]`,
  replacements `{"cloud code": "Claude Code"}`, handed out as a fresh copy per scope
  (never a shared reference, so one scope's later edit cannot leak into another
  scope's still-unsaved defaults). A scope that has explicitly saved an empty
  hotword list or replacement map keeps that empty value; it is a real user choice,
  not "unset", and does not fall back to the defaults.
- **The legacy global `data/asr_hints.json` is retired**, moved aside rather than
  migrated: every scope, public included, starts fresh from the new defaults above.

## Session addition: the incoming-segment noise gate (LA_CONF_FLOOR)

Landed 2026-07-12, calibrated directly against the segment-capture data above. On an
open mic (iOS especially), background noise sometimes gets transcribed into fluent-
looking words. This gate drops such a segment before it can enter a turn, spend a
speculative LLM call, or reach the addressed-speech classifier, using the ASR's own
confidence and a text-shape check, both keyed on 29 operator-labeled capture clips.

- **`transcript`** (server to client, WS) gains an additive nullable `discarded`
  field, `"low_confidence"` or `"degenerate"`, present only when the gate rejected the
  segment. A rejected segment still gets a transcript id and is shown to the user
  (rendered muted/greyed, same treatment as side speech), and is still captured (with
  a `reject_reason` matching `discarded`) when capture mode is on, but it does not
  accumulate into the turn, does not trigger speculation, and earns no reply. Absent
  entirely on an accepted segment.
- **Ordering**: the gate runs on the transcript text after replacements are applied,
  and before the segment can accumulate into the turn, before speculative LLM start,
  and before the addressed-speech classifier, so a rejected segment never costs a
  Haiku call at any later layer.
- **Two independent conditions, checked in this order**:
  1. **Degenerate text** (a runaway-repetition loop, for example "to the to the to
     the...") is dropped unconditionally, with no confidence check at all: a zlib
     compression-ratio test plus a token-dominance test (one token making up more
     than 40% of at least 8 tokens). This runs first because the calibration data
     contains a repetition-loop clip the ASR reported at `prob_mean` 0.9195, above any
     sane confidence floor; confidence alone cannot catch it, only the text shape can.
  2. **The `prob_mean` floor**, `LA_CONF_FLOOR` (default `0.40`; `"0"` disables the
     floor entirely, leaving only the degenerate check active). A segment whose ASR
     `prob_mean` is below the floor is rejected as `"low_confidence"`. A missing
     confidence block fails OPEN on this check (the floor cannot judge what it cannot
     see), so a degraded ASR response never costs the user a turn purely for lacking a
     confidence reading.
- **Calibration basis**: 29 operator-labeled clips from the segment-capture set (see
  above). `prob_mean` cleanly separates the two classes: noise clips ranged 0.043 to
  0.372, speech clips ranged 0.501 to 0.956, and `0.40` sits in that gap. `prob_min`
  does **not** separate them (the ranges overlap: a real command can have one weak
  worst-token even while its overall mean is high), which is why the floor keys on
  `prob_mean` and never on `prob_min` (the lab-command gate's severity thresholds,
  documented above, still key on `prob_min` for their own, unrelated, purpose). The
  degenerate check's 0.9195-confidence outlier is exactly what motivated adding a
  text-shape condition independent of confidence, rather than trying to lower the
  floor far enough to also catch it (which would have rejected real, quiet speech).
- **Safety/control exemptions**: the gate never rejects a segment that is a pending
  lab-command's spoken confirm or cancel word (a quiet "confirm" must still commit the
  action), or a bare utterance that would trigger the fast-path emergency stop (a
  quiet "stop" must still halt something in flight). These are checked before the gate
  runs, using the same session state the fast-path stop and the confirm/cancel turn
  resolution already use.
- **`data/latency.jsonl`**: a rejected segment logs a record `kind:
  "segment_rejected"`, `{session, reason, prob_mean, text_len}`, in place of the usual
  transcript-driven processing for that segment.

## Session addition: shared WebAudio playback for iOS (client only, no WS contract change)

Landed 2026-07-12. All assistant audio, both reply chunks and proactive announcements,
used to play through a fresh `<audio>` element per clip. That worked on desktop, but on
iOS the mic-permission gesture does not unlock audio playback: each `<audio>` element
needs its own user gesture, so the queue was silently draining, with the play() promise
rejection swallowed by a bare `.catch()` and no visible error at all.

- **Fix**: every clip is now decoded (`decodeAudioData`, with a fallback path for
  WebKit's older callback-only signature) and played through **one** shared
  `AudioContext`, created and `resume()`d synchronously inside the same click gesture
  that starts the microphone (`ensurePlayCtx()`, called from `start()`). A
  gesture-resumed `AudioContext` keeps playing freely afterward, so every later clip,
  including one that arrives seconds or minutes after the gesture, plays without needing
  its own tap. Reply audio and announcement audio share this one context, each with its
  own generation counter (bumped on barge-in / stop) so a decode that resolves after the
  clip it belonged to has already been superseded is discarded rather than starting a
  stale source.
- **Playback failures now surface in the status line** instead of failing silently: a
  clip that never played this turn despite arriving (`reply_audio` received but nothing
  ever started) reports "audio decode failed"; a suspended context that will not resume
  reports "audio blocked: tap Start again". Any successful play clears a standing
  warning, so the message does not linger once audio recovers.
- **The TTS panel's trial synthesizer is untouched**, still a native `<audio>` element:
  its "Synthesize" button is itself a direct tap, which is exactly the gesture iOS
  requires, so it was never affected by the bug this fix addresses and gets no benefit
  from changing it.
- **No WS contract change**: this is a client-only fix; `reply_audio`, `announce_audio`,
  and every other message shape are unchanged.
- **The alert earcon's delayed callback is generation-guarded** (landed 2026-07-12,
  round-3): the two-tone earcon (see the proactive-announcements section above) plays
  before its alert clip via a roughly 300ms `setTimeout`, and that callback now checks
  the announce generation counter it captured when scheduled against the current one
  before firing. Without the guard, a barge-in during the beep (which bumps the
  generation counter via `stopAnnounceAudio`) could not stop an earcon already in
  flight: the delayed callback would still fire afterward and start playing the alert
  clip over the user's own speech. With the guard, a stale callback silently no-ops
  instead.

## Session addition: server-side email allowlist replaces Cloudflare Access (connect contract)

Landed 2026-07-13. Cloudflare Access has been removed from in front of the app, so
the `Cf-Access-Authenticated-User-Email` header this repo used to trust on the WS
upgrade will never arrive again. Identity now comes from the client itself, gated by
a new server-side allowlist. This section documents the connect-time contract only;
see `doc/MULTI_CLIENT.md` for the full identity, scoping, and (materially weaker,
stated plainly there) trust model this change carries.

- **Client to server**: the WS connect URL carries the connection's identity as
  `?email=<addr>` (URL-encoded), sent over `wss`. No other message carries identity;
  it is resolved once, at connect, from the query string alone.
- **`LA_ALLOWLIST`** (comma-separated emails, lowercased and stripped, parsed the same
  way as `LA_OPERATOR_EMAILS`): when non-empty, a connection must supply an email on
  this list or it is rejected; when empty or unset (the default), there is no
  enforcement at all, matching every existing dev/smoke path.
- **Server to client (new)**: `{type: "auth_error", reason: "email_required" |
  "not_allowlisted"}`, sent only when the allowlist rejects a connection. It is
  immediately followed by the server closing the socket with code `4001`. This is the
  only message such a connection ever receives; no session is created, and there is
  no fallback to the public scope for a rejected connection.
- **Everything downstream of a successful connect is unchanged**: `_scope_for_email`,
  the per-user scope hash, the `LA_OPERATOR_EMAILS` operator concept, and every WS
  contract delta documented under "Identity + scope" in `web/server.py`'s own
  docstring all keep their existing shape. Only the source of the email changed,
  from a server-trusted edge header to a client-supplied, server-gated query
  parameter.
