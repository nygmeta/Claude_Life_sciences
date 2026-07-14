# Lab Agent

Turn a scientist's spoken request into a **validated, robot-agnostic protocol** — and
route it to the instrument that can actually run it.

```
Voice pipeline  →  Lab Agent API  →  Workflow IR  →  Robot adapter  →  Simulation
                   (this repo)
```

Your teammate owns speech (STT → Claude → TTS). This repo is the middle: it takes a
transcript, resolves intent with Claude, compiles a platform-independent workflow,
validates it against deterministic safety rules and each platform's declared capabilities,
and simulates the run — with a human confirmation gate before anything moves.

The core principle: **free-form model output never touches hardware.** Claude proposes a
*Plan*; deterministic code compiles, validates, and simulates it. Claude resolves
**intent**; the systems of record resolve **fact**.

> **Status:** backend, API, web console, and both demo scenarios are complete and tested.

---

## Two-layer IR (the key design choice)

| Layer | Produced by | Nature | File |
|-------|-------------|--------|------|
| **Plan** | Claude (tool-forced) | semantic, carries `assumptions` + `missing_fields` | `app/models/plan.py` |
| **Workflow (Ops)** | deterministic compiler | fully-specified, platform-independent primitives | `app/models/workflow.py` |

Adapters consume **only** the Workflow. Each adapter *declares its capabilities*, and the
validator reconciles the workflow against them — that declaration is what makes the system
genuinely robot-agnostic rather than robot-agnostic in name.

## Pipeline

```
transcript
  │  planner (Claude → Plan)              app/agent/planner.py
  ▼
Plan ──► clarification loop (missing?)    app/agent/clarify.py
  │  compiler (Plan + SOP → Ops)          app/compiler/plan_to_ops.py
  ▼
Workflow ──► validation                   app/validation/
  │    • safety + resource rules (model-free)
  │    • platform capability reconciliation
  ▼
confirmation gate (assumptions read back, hazards named)
  │  adapter.simulate()                   app/adapters/
  ▼
executed  +  audit trail (transcript → plan → ops → validation → run)
```

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # fastapi, pydantic, anthropic

# Runs fully offline — no ANTHROPIC_API_KEY needed (planner uses a deterministic mock):
python -m demo.demo_script               # full spoken conversation, both scenarios
python -m tests.test_pipeline            # 8 assertions on the core guarantees

# Serve the API the voice layer calls:
uvicorn app.main:app --reload

# Web console (separate terminal) — runs with NO backend in demo mode:
python -m http.server 8090 --directory web    # -> http://localhost:8090
```

With a real key, set `ANTHROPIC_API_KEY` (see `.env.example`) and the planner calls
`claude-sonnet-5` with a **forced tool call**, so its output always conforms to the Plan schema.

## The one endpoint

```
POST /session/message?adapter=opentrons
{ "transcript": "Run an ELISA on today's plasma samples", "session_id": "abc" }
```

Returns `reply` (the string for TTS to speak), plus `state`, `plan`, `workflow`,
`validation`, `clarification_questions`, and `simulation_log`. Pass the same `session_id`
back each turn, since clarification and confirmation are multi-turn. `GET /health` lists
adapters; `POST /session/reset?session_id=…` clears state.

State machine: `idle → gathering → awaiting_confirmation → ready → executed`, with a
`validation_failed` branch that loops back for a fix.

---

## Two SOPs, two scales (the universality proof)

| SOP | Intent | Scale | Native platform |
|-----|--------|-------|-----------------|
| `SOP-ELISA-04` | `elisa` | µL, tip-based | Opentrons / Hamilton-class |
| `SOP-DIL-01` | `serial_dilution` | nL, tipless direct dilution | Echo / acoustic |

Same IR, one validator. The ELISA routes to Opentrons and is **refused by Echo** (an assay
with incubations isn't an acoustic job); the nanoliter dilution routes to Echo and is
**refused by Opentrons** (sub-microliter is below a tip-based floor). The refusals are
what prove the abstraction is load-bearing.

## Extending

- **New SOP:** add a `*_sop.json` in `app/sop/` with `required_parameters` + step templates; the registry picks it up. Add a compiler branch in `plan_to_ops.py`.
- **New platform (Hamilton, MANTIS, real Echo):** subclass `Adapter` in `app/adapters/`, declare its `Capabilities`, implement `compile()` + `simulate()`. Nothing above the adapter layer changes.
- **New safety rule:** add a function to `app/validation/safety_rules.py` and append it to `ALL_RULES`.
- **Real inventory/LIMS:** replace `app/inventory/store.py` internals; keep the interface.

## What's mocked (and honest about it)

- **Inventory/deck state** is a JSON file behind a swappable interface — wire a real LIMS in later.
- **Incubations / plate reads** are `delay()` / `manual_step`, not modeled instruments.
- **Planner** falls back to a deterministic mock with no API key, so the demo never depends on the network.
- **Opentrons** emits runnable OT-2 Python (software sim); **Echo** emits a real picklist CSV. Both enforce their true volume/op limits.

## Web console

`web/` is a zero-build console (plain HTML + JS, no npm) showing the conversation, the
pipeline lighting up stage by stage, a live 96-well plate, the validation verdict with
every rule that fired, the **routing dashboard explaining why each platform accepts or
refuses**, and a timestamped audit trail.

It has two modes: **Demo** runs entirely from fixtures generated by real backend runs —
no backend, no API key, no network, so a bad conference connection can't break it — and
**Live** calls the API and reads `GET /adapters` for real capability data.

See `web/README.md`. It's self-contained, so it can be iframed into an existing voice
front end; or skip the UI entirely and POST straight to `/session/message`.

## Voice interface

`voice/` is the speech half: a self-hosted, real-time voice pipeline that turns a spoken
request into the transcript this API consumes, and speaks the `reply` back.

```
mic → browser VAD → Fun-ASR-Nano (STT) → Claude → gepard-1.0 (TTS) → speaker
                                            │
                                            └─ POST /session/message   (this repo)
```

One orchestrator process serves the page and a WebSocket; ASR and TTS run as GPU services
alongside it. A lab is not a chat room: the mic hears the whole room, and a word can move a
robot. So beyond a basic STT → LLM → TTS loop:

- **Confidence, built not read.** The STT model exposes none, so we wrap its decoder and
  capture a score for every token. Every segment carries its own confidence.
- **Noise gate.** A segment below a calibrated floor never becomes a turn. The threshold came
  from 29 hand-labelled clips (noise 0.04–0.37, speech 0.50–0.96), not from a guess.
- **Confirmation floor.** When the Lab Agent is awaiting confirmation, its next affirmative
  starts a machine, so an unclear "yes" is refused out loud rather than obeyed, and missing
  confidence fails closed. A "cancel" always passes at any confidence: refusing one buys no
  safety and strands the user.
- **Intent verification.** A *confident* mishear ("I am six" for IL-6) is read back as "Did you
  mean: IL-6?" before it can reach the planner. Confidence alone cannot catch this class.
- **Transcript normalisation.** Spoken forms the planner cannot parse ("I L six", "per whale")
  are rewritten at the seam. Every rule comes from a failure measured on the real stack.
- **Barge-in and streaming TTS.** It listens while it speaks, so it can be cut off mid-sentence,
  and the reply is synthesised sentence by sentence.
- **Turn segmentation.** Several spoken fragments become one turn, instead of a reply to every
  pause.
- **Addressed-speech detection** (opt-in, `LA_ADDRESSED=1`). A bounded classifier decides
  whether an utterance was aimed at the assistant or at a colleague.

Scope: the above is what the integrated console runs, where the Lab Agent owns planning and
confirmation. The standalone voice app (`voice/web/index.html`) additionally carries a
severity × confidence command gate with digit read-back, intent-bound confirmation, proactive
lab-event announcements, and a hands-busy protocol walkthrough. See `voice/doc/FEATURES.md`.

### Running it

The integrated console (`voice/web/console.html`) is this API's web console **plus** voice.
One command brings it up:

```bash
bash voice/deploy/start-lab-console.sh --voice <voice-host>
```

It starts the Lab Agent on loopback, serves the console, and borrows ASR, TTS, the safety
gates and the "did you mean X?" check from a remote speech service over `wss://`. It
installs four pure-Python packages and **needs no GPU**, because the speech half does not
have to live where the console lives:

```
?api=http://localhost:8000     the Lab Agent, which must sit next to the robot
?voice=<voice-host>            the speech service, wherever the GPU is
```

That split is not a preference. A robot lives on a lab LAN and the GPUs cannot move there,
so **audio crosses the network and the protocol does not**: the machine that talks to the
robot is the one standing beside it. Step by step in
[voice/doc/HOST_THE_CONSOLE.md](voice/doc/HOST_THE_CONSOLE.md).

**This does not drive a robot yet.** The Opentrons adapter compiles and *simulates*; there
is no `execute()`, and no code here opens a socket to a robot. Every safety gate in the
system today therefore guards a subprocess. What real execution would require, and which
guarantees must be proven to still hold when it lands, is in
[voice/doc/HARDWARE_EXECUTION.md](voice/doc/HARDWARE_EXECUTION.md).

Start at `voice/README.md`. The seam where the two halves meet is `voice/doc/INTEGRATION.md`.

Voice subsystem by Junchen Lu (@RanaCM).

## Project layout

```
app/
  models/        Plan + Workflow (Ops) IR, session/API contracts
  agent/         planner (Claude), prompts, clarification loop
  sop/           SOP registry + ELISA and serial-dilution templates
  compiler/      Plan + SOP → Workflow (deterministic, testable)
  validation/    deterministic safety rules + capability reconciliation
  adapters/      capability contract + Opentrons and Echo adapters
  inventory/     mock LIMS / deck-state store
  main.py        FastAPI app + session state machine
demo/            end-to-end scripted conversation (both scenarios)
web/             zero-build console (demo + live modes)
tests/           pipeline guarantees
voice/           real-time voice interface (VAD → STT → Claude → TTS)
```
