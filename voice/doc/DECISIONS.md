# Decisions Log

Dated record of decisions made autonomously during agent-team sessions, so the reasoning
is on record and not just in a diff. Append-only, newest entries at the bottom.

## 2026-07-09 SGT

**Decision**: sentence-streaming TTS sits behind `LA_TTS_STREAM` (default on), not a
hard replacement of the old single-synth path.
**Why**: keeps a same-client-contract A/B fallback for the demo; if streaming misbehaves
live, flipping the flag restores the previously working behavior without a code change.

**Decision**: a terminal `reply_audio_end` event marks the end of a turn's audio, instead
of a "final" flag on the last `reply_audio` chunk.
**Why**: simpler producer/consumer split, the TTS consumer does not need to know a
sentence is the last one before sending it, it just sends chunks as they're ready and
signals completion separately once the queue is drained.

**Decision**: the LLM call is a WS-agnostic async generator, `stream_llm(messages,
out=None)`, rather than a function that takes the WebSocket and sends its own messages.
**Why**: it is the single seam where the voice pipeline meets the separate lab-automation
half; keeping it one narrow, WS-unaware function means only that function needs to change
when the merge adds tool-use, not the orchestrator's WebSocket handling.

**Decision**: the sentence splitter flushes a sentence only when a terminator run
(`.`/`!`/`?`/`…`) is followed by whitespace, plus a small abbreviation guard (Mr, Dr,
e.g., etc).
**Why**: without the trailing-whitespace check, a decimal or a bare URL with an internal
dot and no following space (`weather.com`, `3.14`) would split mid-token. Verified
against real model output.

**Decision**: the TTS model switcher (the "Model" selector inside the TTS panel) is
hidden when only one model is configured, and only appears once 2 or more are available.
**Why**: keeps a single-model deploy visually uncluttered; the UI reflects what is
actually configured rather than always showing a control with nothing to switch.

**Decision**: no further visual UI redesign this session.
**Why**: the mono/brutalist redesign from an earlier session was already shipped and
browser-verified; this session added one feature-aligned element (a playback-driven
speaking indicator for the ordered audio queue) rather than revisiting the design.

**Decision**: the GPU pod was stopped (not deleted) at session close.
**Why**: preserves the container disk (venvs, warm model cache) and the `/workspace`
volume for a fast restart next session, per the owner's cost-saving instruction; a
deleted pod would force re-downloading model weights and rebuilding venvs.

**Decision**: this session's work was committed locally only.
**Why**: this repo has no configured git remote, so there is nowhere to push to.

**Decision**: the old `reset` WS message type was removed outright when session history
landed, rather than deprecated or kept alongside `new_session` as an alias.
**Why**: there is exactly one first-party client (`web/index.html`), so a breaking rename
costs nothing today, and it avoids two message names doing almost the same thing under
different semantics (`reset` used to soft-clear the current session in place; `new_session`
finalizes it into history and starts a numbered successor). If a second client is ever
built, migrating one message name is a small, mechanical change.

**Decision**: a session's persisted transcript (`Session.messages`, written to
`data/sessions/<id>.json`) is the full, never-truncated conversation, kept separate from
`Session.history`, the bounded window the LLM call actually sees (capped to
`LA_HISTORY_TURNS` pairs).
**Why**: the two arrays serve different consumers with different needs. The LLM only
needs recent context to keep token usage and latency bounded; the History panel, and a
future resumed session, need the complete record, not a truncated tail. Trimming the
persisted copy to match the LLM's window would silently lose part of every past session.

## 2026-07-12 SGT

**Decision**: ASR confidence is captured by wrapping the Fun-ASR-Nano decoder's
`generate()` in place (forcing `output_scores`/`return_dict_in_generate` and handing back
`.sequences`), not by forking `funasr` or adding a second forward pass.
**Why**: verified in the installed package that both Fun-ASR-Nano decode paths already
call `self.llm.generate(...)` then `batch_decode` immediately, so the wrap adds
effectively zero latency and never touches `funasr`'s own code. Every failure mode
degrades to `confidence: null` rather than a broken transcription, keeping the response
contract additive.

**Decision**: tool dispatch inside the lab-command gate parks on the turn's commit event
before doing anything.
**Why**: a speculative LLM call may parse a lab command before the turn commits, but only
the commit event may authorize a dispatch. A discarded speculation then dies
side-effect-free by task cancellation while parked, which keeps the latency win from
speculative start for both ordinary chat turns and the parsing step of command turns.

**Decision**: confirm/cancel turns are resolved lexically, with no LLM call at all; a
HAZARDOUS pending action requires the literal word "confirm", not a loose affirmation.
**Why**: matching lexically is deterministic, faster, and immune to the LLM paraphrasing a
safety-critical acknowledgement. Any unrelated utterance supersedes (clears) the pending
action instead of risking a misread as a confirmation.

**Decision**: the demo command catalog's severity tiers are stubbed server-side
(`read_sensor`/`stop_stirrer` SAFE, `set_temperature`/`start_stirrer` REVERSIBLE,
`dispense`/`add_reagent` IRREVERSIBLE, `start_centrifuge` HAZARDOUS), with env-tunable
thresholds `LA_CONF_LOW` (default `0.75`) and `LA_CONF_VERYLOW` (default `0.50`), and a
missing confidence reading treated as fully confident (`1.0`), logged as such.
**Why**: the real lab-automation command set and its severity classification belong to the
teammate's project; a stub lets the gate ship and be exercised end to end tonight. Every
decision is logged per turn (intent, decision, severity, `prob_min`) so the thresholds can
be retuned once real confidence distributions accumulate, rather than left as a guess.

**Decision**: the fast-path emergency stop is matched lexically before the LLM, on a
short standalone stop-like utterance at the segment boundary, and is framed as a
supervisory stop layered on a hardware e-stop, never the sole one.
**Why**: per the 2026-07-11 research pass, no voice channel may be a robot's safety-rated
stop function under IEC 61508 / ISO 13850; a pre-LLM lexical match is also the fastest
possible halt available in this pipeline, regardless of that framing.

**Decision**: the GPU courtesy protocol checks `nvidia-smi` before any host restart or
GPU-loading step and aborts only when free GPU memory is below 8 GB or a foreign process
holds more than 20 GB, refined from an initial gate of any foreign process over 24 GB
total or over 50% utilization.
**Why**: a neighboring session shares the same box overnight. First contact showed the
neighbor runs short, back-to-back ~4.3 GB jobs in a loop, so waiting for genuinely idle
utilization could stall all night; utilization sharing is harmless (CUDA time-slices a
restart costs the neighbor only seconds), and memory pressure is the real hazard the gate
needs to guard against. The first halt under the original gate was correct behavior at the
time and was released once the refined gate landed.

**Decision**: voice-ui was dispatched concurrently with web-core against frozen WS
contracts (`action_pending`/`action_executed`/`action_rejected`/`action_cancelled`/
`action_halted`), rather than serialized after web-core landed first.
**Why**: both contracts were pinned in each worker's brief ahead of time, which makes the
parallel safe. This session's confirmation is voice-only by design: no clickable confirm
button and no WS message type for one yet.

**Decision**: pre-existing failures in `tests/test_session_history.py` (around
`is_operator`) were fixed first, before any new lab-gate work started.
**Why**: the failures predated tonight's session, and gates like pytest and the local
smoke suite are meaningless evidence against a baseline that is already red. The fix was
assigned as step 0 ahead of the new feature work for that reason.

**Decision**: a broadcast `inject_event` gives every live connection its own `event_id`
and its own TTS synth, rather than one shared `event_id` and one shared audio clip
fanned out to all of them.
**Why**: each connection's session carries its own TTS model and params, so a single
shared synth would have to pick one connection's voice for everyone else, or synthesize
once per connection anyway; keying routing per connection (and per `event_id`) up front
keeps audio delivery correct for every listener with no special-casing.

**Decision**: announcements are not persisted to `Session.messages` or `Session.history`
this pass.
**Why**: the fastest path to a working announce channel tonight; the LLM stays entirely
unaware of what it has already announced, which is a real gap (an assistant reply could
contradict or repeat an announcement the user just heard). Recorded as an open item in
`doc/STATUS.md` rather than solved under time pressure, since persisting it correctly
means deciding whether it belongs in the truncated LLM window, the full transcript, or
both.

**Decision**: the demo lab's automation-stub state (`Session.lab_stub`) is carried over
to a fresh session on the same connection by `new_session_preserving`, exactly like the
existing TTS-params carryover, rather than reset with the rest of the conversation.
**Why**: the stub models physical lab state (temperature, stirrer, centrifuge, wells),
not conversation state; a `new_session` (renamed from the old `Reset`) starts a fresh
transcript, but the physical lab a `new_session` user is talking to has not actually
reset, so its in-memory model should not either.

**Decision**: the live orchestrator was restarted on `b6590cf`, a clean commit taken
before any Phase 4 edits began, rather than left running on whatever the working tree
held mid-edit.
**Why**: the public demo host must never serve a half-edited working tree; restarting on
a clean, tagged commit before starting Phase 4's edits guarantees the live stack always
reflects a known, fully-landed state, even if Phase 4 runs long or is interrupted.

**Decision**: `LA_ADDRESSED` (the addressed-speech classifier) defaults to off and stays
opt-in rather than shipping on by default alongside the rest of Phase 4.
**Why**: it is the only feature in this repo that can discard real user speech. Every
other gate in the lab-command pipeline can, at worst, ask for an unnecessary
confirmation or refuse a command; a false "not addressed" verdict here silently costs
the user their turn. That asymmetry means it needs tuning against real lab audio,
including real background chatter, before it is trusted on by default; see the open
item in `doc/STATUS.md`.

**Decision**: the addressed-speech classifier fails OPEN (`addressed: true`) on any
error, timeout, or malformed answer from the model, rather than failing closed or
retrying.
**Why**: the two possible wrong answers are not symmetric in cost. A false "addressed"
costs one unnecessary reply; a false "not addressed" costs the user their turn and their
trust that the assistant is actually listening. Failing open means a classifier hiccup
degrades the feature back to "no addressed-speech gate at all" rather than to
"sometimes ignores the user," which is the strictly worse failure mode.

**Decision**: `LA_PROTOCOL_TIMER_SCALE` compresses only the actual wait
(`lab_gate.step_timer_s`), never the spoken duration a step's text or completion
announcement states.
**Why**: the operator hears "5 minutes" and later hears "5 minutes elapsed" regardless
of the scale; only the demo/smoke's wall-clock wait shrinks. A scale that also
compressed the spoken numbers would make the assistant state a duration it did not
actually wait, which is a form of misleading readback, the same category of problem
the lab-command gate's grounded digit-by-digit readback exists to avoid elsewhere.

**Decision**: protocol navigation (`protocol_start`/`next`/`back`/`repeat`/`status`) is
classed SAFE, the same tier as `read_sensor`, rather than a new tier of its own.
**Why**: navigating a written protocol moves no hardware and has no physical
consequence, so it fits the SAFE definition exactly; it still confirms at very low ASR
confidence like any SAFE command, since a misheard "next" or "back" could desynchronize
the operator's spoken understanding of the protocol from the tracked step even though
nothing physical happens.

**Decision**: the muddled commit history from the concurrent-writer collision (see
`doc/STATUS.md`'s incident entry) is left as is, not rewritten.
**Why**: a second worker was live and had already built on top of the affected commits
by the time the collision was discovered; rewriting shared history under a writer that
recently had commits in flight risks losing or duplicating work in a way a clean
`git commit --amend` or rebase cannot safely guarantee here. Recording the true shape of
what happened in the docs, honestly, is a better trade than a history rewrite whose
safety cannot be fully verified.

**Decision**: the incoming-segment noise gate keys its confidence floor on the ASR's
`prob_mean`, not `prob_min`.
**Why**: 29 operator-labeled capture clips showed `prob_mean` cleanly separating noise
(0.043-0.372) from speech (0.501-0.956), with `0.40` sitting in the gap, while
`prob_min` ranges overlap between the two classes (a real command can have one weak
worst token even while its overall mean confidence is high). Keying on the metric that
the labeled data actually separates on, rather than the one the lab-command gate
happens to already use for its own severity thresholds, is what makes the floor
trustworthy rather than a guess borrowed from an unrelated feature.

**Decision**: the confidence floor is set to `0.40` (env `LA_CONF_FLOOR`), derived
directly from the labeled capture data rather than picked by feel.
**Why**: `0.40` sits cleanly between the highest labeled noise clip (`prob_mean`
0.372) and the lowest labeled speech clip (0.501) in the 29-clip calibration set; a
floor chosen without that data would either be too loose (letting noise through) or
too tight (risking real, quiet speech) with no evidence either way.

**Decision**: a runaway-repetition ("degenerate") text check runs independently of,
and before, the confidence floor, rather than trying to lower the floor far enough to
also catch repetition loops.
**Why**: the calibration set contains a repetition-loop noise clip the ASR reported at
`prob_mean` 0.9195, comfortably above any confidence floor that would not also reject
real quiet speech. Confidence and text shape are different signals; only the text
shape can catch a decoder that is fluently, confidently wrong.

**Decision**: the noise gate exempts a pending lab command's confirm/cancel and any
utterance that would trigger the fast-path emergency stop, checked before the gate
runs.
**Why**: an early version of the gate did not carve out this exemption, and a worker
implementing it caught, before it shipped, that a quiet "confirm" or "stop" spoken
under genuine low confidence (exactly the situation where a user most needs to be
heard) would have been silently dropped as noise. Safety and control utterances are
not content to filter; they must always reach the layer that acts on them.

**Decision**: a segment the noise gate rejects is still shown to the user (as a muted
row, with a `discarded` reason) and remains fully labelable in capture mode, rather
than being silently dropped from the transcript entirely.
**Why**: a false drop (real speech the gate mistook for noise) is exactly the case the
calibration set most needs more examples of. Making rejected segments visible and
labelable turns every false drop the gate produces into new calibration data, instead
of losing the evidence needed to retune it.

**Decision**: segment capture (`LA_CAPTURE`) ships with no on-page recording
indicator; the labeling controls are the only visible cue.
**Why**: an explicit operator decision for an internal-testing-only mode used with the
testers' consent, prioritizing shipping the calibration tool fast over building a UI
affordance that only matters once someone outside that trusted group could be
recorded. This should be revisited, and an explicit on-screen indicator added, before
capture mode is ever used with anyone who has not separately consented to it.

**Decision**: ASR hints (hotwords + replacements) are scoped per user with no
cross-scope access at all, not even for the operator's otherwise-existing "see all"
capability.
**Why**: hints rewrite the transcript a user sees and the assistant responds to, so
letting one connection (even an operator's) silently edit another user's hints is a
footgun with no corresponding demo value; the operator's session-level see-all view
already serves the legitimate cross-scope need (reading, not rewriting another
user's experience) without extending it here.

**Decision**: capture labels can be applied any time after the fact from the
append-only `captures.jsonl` store, so a UI to label past (history-view) sessions was
deliberately not built this session.
**Why**: because labeling is a new append-only record keyed by `(sid, seq)`, not an
edit to the original segment record, `scripts/capture_report.py` (or any future
offline tool) can label backlog clips from the WAVs and transcripts already on disk
without needing a live connection to that segment's session at all. Building a
history-view labeling UI now would duplicate work an offline script already covers.

**Decision**: the confirmation-execution floor (`LA_CONFIRM_FLOOR`) gates whether a
spoken confirmation is allowed to *execute*, rather than folding a "confirm" into the
incoming noise gate's own confidence check.
**Why**: the first instinct under review was to gate the noise floor itself against a
spoken confirmation, but the noise gate's job is to keep genuine noise from ever
entering a turn, and a quiet, real "confirm" is not noise, it must always be heard.
The reviewer's asymmetry argument settled it: dropping a low-confidence confirm
outright (never-drop violated) is just as wrong as executing one on a guess
(never-execute-unclear violated), so a single confidence check cannot satisfy both
constraints; splitting the concern into two layers, the (unchanged) noise-gate
exemption for hearing it and a new, separate execution floor for acting on it,
preserves both without a filter ever eating the word.

**Decision**: both new confidence-boundary checks (`LA_CONFIRM_FLOOR` and
`LA_PENDING_TTL_S`) are inclusive of the boundary value itself: a `prob_mean` exactly
at the floor executes, and a pending age exactly at the TTL still resolves normally.
**Why**: consistency with the existing noise-gate floor, which is also a strict `<`
(not `<=`) comparison, so "at the threshold" reads the same way across every gate in
this codebase rather than each one picking its own convention.

**Decision**: cancel is never gated by the confirmation-execution floor, only confirm
is.
**Why**: the two wrong outcomes are not symmetric in cost. A spurious low-confidence
cancel, acted on when the user did not really say it, only costs re-issuing the
original command: reversible, cheap, and recoverable in the next turn. A spurious
low-confidence confirm, acted on the same way, fires an irreversible physical action.
Gating only the side that can cause irreversible harm is the whole point of the
design, not an oversight on the other side.

**Decision**: an expired pending action consumes the turn outright (`action_cancelled
reason: "expired"` plus a canned reply), even when the turn's actual content was an
unrelated request that would otherwise have superseded the pending action.
**Why**: re-running that same turn immediately afterward as a fresh, ordinary LLM turn
would ask the user to piece together why their request seemed to vanish into an
unrelated exchange about an action they had already moved on from. An explicit "that
request expired, please repeat" is a small one-turn cost for a much clearer signal,
traded deliberately against the marginally faster path of silently forwarding the
turn.

**Decision**: the TTS playground's trial synthesizer keeps its own native `<audio>`
element; it was not moved onto the new shared WebAudio playback context.
**Why**: the playground's "Synthesize" button click is itself the user gesture, so a
native `<audio>` element's `play()` already succeeds there on iOS without any of the
machinery the reply/announcement paths needed. Moving it onto the shared context would
add code with no bug to fix and no user-visible improvement, since native controls
(the audio scrubber, ability to replay) are also a legitimate reason to prefer the
plain element for a manual trial tool.

**Decision**: capture mode's per-turn chip strip labels each segment by its own
transcript id, even though the visible transcript row now merges every segment's text
into one line per turn.
**Why**: a label describes one recording (one WAV file), and a saved recording is a
segment, not a turn; merging a real sentence and a noise blip's labels into one
combined judgment would make either label a lie about half the audio. Keeping the
visible text merged (matching how a turn is persisted) while keeping every chip keyed
to its own segment id preserves the one-label-one-recording honesty the capture
mode exists for, without giving up the more readable, turn-aggregated transcript view.

**Decision**: an IRREVERSIBLE or HAZARDOUS confirmation is bound to the specific
command's spoken keyword ("confirm dispense", not a bare "confirm" or a loose "yes"),
and the earlier HAZARDOUS-only strict distinction is retired in favor of treating
IRREVERSIBLE and HAZARDOUS identically.
**Why**: the review finding this closes is an accidental-bystander scenario, someone
nearby says "yes" to something unrelated (or agrees with a different sentence
entirely) while a command happens to be pending, and that loose affirmation should
never be able to fire an irreversible or hazardous action it was never actually
about. Binding the confirmation to naming the command itself is a cheap, effective
guard against that accidental case. The full item (both IRREVERSIBLE and HAZARDOUS
bound, not just HAZARDOUS) was chosen over a strict-only variant because the
accidental-bystander risk does not care whether the pending command happens to be
merely irreversible rather than hazardous: a wrong irreversible action is still wrong.
This is explicitly **not authentication**: it verifies WHICH command is being
confirmed, not WHO is confirming it. Anyone who overhears the pending command and
knows its keyword can still say the bound phrase, which is exactly why this only
mitigates, and does not solve, an `other_speaker` confirmation; a real speaker-gate
layer is the only thing that closes that gap, and stays a documented open item.

**Decision**: an unbound (loose-affirmation) re-prompt and a bound (wrong-phrase)
re-prompt use two different messages, rather than one generic "please confirm again."
**Why**: they are different problems needing different corrective instructions. A
loose "yes"/"confirm" under a bound pending is missing information (which command),
so its re-prompt states the exact required phrase ("To proceed, say confirm
dispense, or say cancel."). A confirmation that was unclear or unconfident (below the
floor, or with no confidence reading at all) already had the right words, it just was
not heard clearly enough, so its re-prompt asks the user to simply repeat themselves
("I heard confirm, but not clearly. Please say it again, or say cancel.") rather than
re-stating information they already provided correctly.

**Decision**: a confirmation with no ASR confidence block at all now re-prompts
(`reprompt_noconf`) rather than executing, reversing the original fail-open behavior
from the F2 confirmation floor.
**Why**: the original fail-open choice made sense in isolation (never lock out a
degraded-ASR user), but it left the confirmation gate inconsistent with the command
gate's own F3 rule, which escalates rather than relaxes on missing confidence. Round-3
review flagged that inconsistency directly: the same "no confidence reading" fact was
being treated as a green light in one gate and a caution signal in the other for no
principled reason. Making both gates escalate on missing confidence removes that
asymmetry, and a re-prompt (not a reject) preserves the escalate-never-reject
discipline that already governs every other confidence gate in this codebase.

**Decision**: an expired pending action now passes an unrelated utterance through to a
normal turn instead of always consuming the turn with an expiry notice, superseding
the F4 decision from the previous review round.
**Why**: this is a documented reversal, not a silent one. The previous round's
reasoning, that re-running an expired pending's turn fresh would be more confusing
than an explicit notice, held only for an utterance that was actually trying to
confirm or cancel the stale pending; it did not hold for a completely unrelated
request, which that same reasoning was accidentally swallowing into a "please
repeat" notice about a command the user was not even asking about anymore. Splitting
the two cases (confirm/cancel attempts still get the expiry notice and consume the
turn; anything else passes through) keeps the original reasoning where it applies and
fixes it where it does not.

**Decision**: the alert earcon's delayed `setTimeout` callback checks the announce
generation counter before firing, rather than relying on `stopAnnounceAudio` alone to
prevent a stale earcon from starting its clip.
**Why**: `stopAnnounceAudio` already stops a currently-playing announcement source,
but the earcon's own scheduled callback lives outside that source, as a bare
`setTimeout` with no handle `stopAnnounceAudio` could cancel. Without capturing and
re-checking the generation at fire time, a barge-in during the roughly 300ms beep
would still let the delayed callback start the alert clip afterward, on top of the
user's own speech. Capturing the generation when the earcon is scheduled and
comparing it at fire time closes that gap with the same mechanism (a generation
counter) already used everywhere else in the announcement and reply audio paths,
rather than introducing a second, different cancellation mechanism just for the
earcon.

## 2026-07-13 SGT

**Decision**: Cloudflare Access is removed and replaced by a server-side email
allowlist (`LA_ALLOWLIST`), with identity now coming from a client-supplied `?email=`
query parameter instead of a verified edge header.
**Why**: the operator wanted the login experience simplified, specifically no OTP or
email-verification step for testers to click through before reaching the app. The
accepted tradeoff, stated plainly rather than glossed over, is that this is no longer
real authentication: the email is self-asserted, so anyone who knows or guesses an
allowlisted address can connect as that identity, and the operator address in
particular now functions as a shared secret rather than a login. A one-click
identity-provider option (reinstating a verified login with materially less user
friction than Access's OTP flow) was offered and explicitly declined in favor of the
simpler allowlist. Per-user scoping and the operator "see all" view are unaffected;
only the strength of the identity claim underneath them changed.

**Decision**: `set_temperature` and three other lab commands' argument-name drift
(`celsius` vs `temperature`/`temperature_celsius`, and similar for `start_stirrer`,
`start_centrifuge`, `add_reagent`) was fixed with an alias-tolerant argument reader,
not a per-argument tool schema.
**Why**: the alias reader is the fast, low-risk fix for what a live-speech
verification run had already caught as a real, silent bug (a command that looked
like it ran but never changed the state it claimed to). A per-argument JSON schema
on the `lab_command` tool, which would let the API itself reject or coerce an
out-of-vocabulary key rather than requiring the server to guess likely aliases after
the fact, is the durable fix and remains a known, un-done follow-up; it was not
pursued in this pass because the immediate priority was closing a live-verified bug
quickly, and a schema change touches every command's tool-call contract, not just
the four found drifting today.
