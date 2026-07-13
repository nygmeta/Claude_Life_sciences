# Confirmation Gate vs Streamed TTS: Ordering

*Created: 2026-07-13 18:13 +08 (SGT).*

Can the assistant start SPEAKING before the confirmation gate has returned? Separate question from whether it can ACT before the gate, and the two have different answers. This doc records the question, what the code actually does, how it was probed, and the measured result.

## 1. The question

From the lab-automation side of the integration, ahead of recording the demo:

> Does the streaming TTS ever start speaking before the confirmation gate returns? The
> gate is a hard stop, nothing executes until the scientist says yes, and if TTS can
> stream past it, we cannot show that on video.

Two distinct properties are bundled in there, and they need to be answered separately:

- **Safety**: can a physical action execute before the user confirms?
- **Presentation**: can the assistant's VOICE run ahead of the gate, so that on camera it sounds like it acted (or is acting) before the scientist confirmed?

A system can be perfectly safe and still look unsafe on video. The demo needs both.

## 2. Design and expected behavior

### The gate is structural, not advisory

`web/lab_gate.py:gate()` maps (intent severity x ASR confidence) onto one of four actions. The severity table (`web/lab_gate.py:184-187`):

```
SAFE          proceed         (confirm if prob_min < VERYLOW)
REVERSIBLE    proceed         (confirm if prob_min < LOW)
IRREVERSIBLE  confirm         (reject  if prob_min < VERYLOW)
HAZARDOUS     confirm_strict  (reject  if prob_min < VERYLOW)
```

The gate runs inside the LLM turn, as a TOOL the model calls. In the tool handler (`web/server.py:2037-2060`), a `confirm` / `confirm_strict` decision:

1. arms `sess.pending_action` (the command is parked, not run),
2. sends `action_pending` to the client (carrying the readback and the exact `confirm_phrase`),
3. returns to Claude the literal string `"CONFIRMATION REQUIRED. Do not claim any action happened. ..."`.

The execute path (`web/server.py:2032`, `stub.execute(...)`) is in the *other* branch and is never reached. Execution can only happen on a LATER turn, and only when the user says the intent-bound phrase (`confirm centrifuge`, not a bare "yes"), so a stray affirmative in a noisy room cannot fire the wrong machine.

**So the safety property is enforced by control flow.** It does not depend on the model behaving well.

### Where the presentation property gets subtle

The tool-use loop (`web/server.py:1245-1265`) is a standard streaming loop:

```python
for _ in range(LAB_TOOL_MAX_ITERS):
    async with llm_client.messages.stream(...) as stream:
        async for chunk in stream.text_stream:   # 1250: text is yielded HERE
            yield chunk
        final = await stream.get_final_message()
    if final.stop_reason != "tool_use":          # 1258
        break
    for b in tool_uses:
        content = await tools_ctx.handle(...)    # 1264: the GATE runs HERE
```

Text is yielded from EVERY pass, and the gate only runs AFTER a pass completes. So if Claude were to narrate before calling the tool ("Okay, starting that now..."), that sentence would be handed to the sentence splitter and synthesized BEFORE the gate had returned. Nothing in the control flow prevents it.

What prevents it today is the system prompt (`web/lab_gate.py:LAB_SYSTEM_SUFFIX`), which instructs the model never to claim an action happened unless a tool result confirms it.

**So the presentation property is, as implemented, a prompt-level guarantee, not a structural one.** That distinction is the whole reason this was worth probing rather than asserting.

## 3. How it was probed

Reasoning about the code gives a "should not happen". The demo needs a "does not happen". So: measure the wire.

`scripts/_probe_gate_order.py` starts its own `LA_LAB_MODE=1` orchestrator on a free port against the mock ASR/TTS, injects one utterance via the mock's filename-override token (so the transcript and its confidence are exact and reproducible), commits the turn, then records EVERY WebSocket message in arrival order with a millisecond stamp relative to `end_turn`.

It then answers one question mechanically, rather than by eyeballing a log:

- index of `action_pending` (the gate armed) in the message sequence,
- indices of any `reply_audio` (synthesized speech) and `reply_delta` (streamed text) before it,
- indices of any `action_executed` before it (a safety bug, if ever non-empty).

Run against two commands of different severity:

```
PROBE_CMD="start the centrifuge at 3000 rpm for 5 minutes"   # IRREVERSIBLE
PROBE_CMD="dispense 50 microliters into well A3"             # IRREVERSIBLE, bound phrase
```

Driver: `scripts/_run_probe.sh` (brings up the mock, runs both probes). Both hit the REAL Claude API, so this exercises the true model behavior, not a stub.

## 4. Results

Both commands: the gate fires FIRST, and no audio or text precedes it.

| | centrifuge | dispense |
|---|---|---|
| `action_pending` (gate armed) | 1243 ms | 2011 ms |
| `reply_start` (first text) | 1959 ms | 2998 ms |
| `reply_audio` (first SPOKEN audio) | 2348 ms | 3390 ms |
| audio before gate | none (0 chunks) | none (0 chunks) |
| text before gate | none (0 deltas) | none (0 deltas) |
| executed before gate | no | no |

Margin between the gate arming and the first audio byte: **1105 ms** (centrifuge) and **1379 ms** (dispense).

What the assistant actually says is only the readback, for example:

> "Starting the centrifuge at three zero zero zero, that is 3000, r p m for five, that is
> 5, minutes. Say confirm centrifuge to proceed, or cancel."

It never voices a claim that the action happened. That is exactly the shot the demo wants: command -> readback -> silence -> spoken confirm -> action.

The reason nothing is spoken early is that Claude goes straight to the tool call and emits zero preamble text. That held on both probes, and the system prompt forbids claiming an action happened. But per section 2, it remains a model-behavior guarantee rather than a control-flow one.

### Hardening: LANDED

The presentation property is now structural, not prompt-level.

`LA_STRICT_GATE_AUDIO` (default ON) buffers the text of a tool-use pass in `stream_llm` and releases it only once `stop_reason` is known. If the pass turns out to be a tool call, its text was pre-gate narration: it is DROPPED, never shown and never synthesized. The gate then runs, and the next pass (which carries the tool result, so it knows the action is merely pending) streams normally.

Cost, stated honestly: on a tool-calling turn it costs nothing, because the model emits no text before the tool call anyway, which is exactly what the probes measured, so the buffer usually holds nothing. On a turn that calls NO tool (ordinary chat while lab mode is on) the reply is released when generation completes rather than as it is written, so first audio waits for the last token. `LA_STRICT_GATE_AUDIO=0` trades the guarantee back for the stream.

Pinned by `tests/test_gate_audio_order.py`, which drives `stream_llm` against a fake model that DOES narrate before calling the tool (the dangerous shape the real model never produced) and asserts three things:

1. no text is emitted before the gate runs,
2. the post-gate readback is still spoken, so the guarantee is not bought by muting the assistant,
3. with the flag off, the pre-gate text does leak, which documents why the flag has to exist instead of leaving the old behavior as an implicit promise.

The claim on camera now rests on code.

## 6. The other half of the gate: the integration seam

Once the Lab Agent API is wired in (`LA_LAB_BACKEND_URL`, see `web/lab_backend.py`), the backend owns planning, validation, confirmation, and execution, and the local `lab_gate` stands down: there is exactly one planner and one confirmation gate.

The same question then reappears in a new place, and gets the same treatment. The backend trusts its transcript, which is right for a typed console and wrong for a room with a centrifuge in it. So the voice half keeps the last, strictest check: while the backend is `awaiting_confirmation`, an utterance whose ASR confidence is below `LA_CONFIRM_FLOOR` is NOT forwarded at all. The backend never sees it, so its state machine cannot move, so a misheard "yes" cannot execute anything.

Two failure modes were found by testing this rather than assuming it:

- The utterance was being dropped by the noise gate BEFORE it could be re-prompted, so a mumbled "yes" produced total silence: the scientist had said yes, the room said nothing back, and they could not tell whether the protocol was running. Being HEARD and being OBEYED are different things. `_gate_exempt` now lets a confirm/cancel through while the backend is armed, so the confirmation floor can refuse it OUT LOUD.
- Speculative LLM start had to be disabled against the backend. Speculation fires the reply at the segment boundary, before the turn is finished, and discards it if the user keeps talking. That is harmless for a stateless LLM call and unsafe against a state machine with an audit trail: a discarded speculation would still have advanced the backend's state and, at worst, taken a half-heard "yes" as sign-off.

Both are covered by `scripts/smoke_integration.py`, which stands up both halves for real and speaks to them.

## 5. Why this is also a Claude-usage exhibit

The interesting part is not the answer, it is the method, and it is a compact example of how Claude was used throughout this project:

- **The question was refused at face value.** "Can TTS stream past the gate" was decomposed into two properties (safety vs presentation) that have different answers. Answering only the easy one would have been technically true and practically misleading.
- **The code was read before it was trusted.** The tool loop was traced to the exact line (`server.py:1250` vs `server.py:1264`) where the ordering hazard actually lives, which is what turned a vague worry into a precise, falsifiable claim.
- **A "should not happen" was upgraded to a measured "does not happen."** Rather than reason from the prompt, a probe was written to timestamp the real wire against the real Claude API, and to compute the verdict mechanically instead of by reading a log.
- **The residual risk was reported, not buried.** The probes passed; the honest finding is still that the guarantee is prompt-level, with a concrete fix proposed. Reporting a passing result as if it were airtight would have been the easy path.

Logged in [CLAUDE_USAGE.md](../CLAUDE_USAGE.md), entry `2026-07-13 18:13 +08 : Confirmation-gate vs streamed-TTS ordering`, which links back to this doc.
