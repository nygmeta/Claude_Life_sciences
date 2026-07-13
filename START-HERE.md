# START HERE

## See the demo (10 seconds, nothing to install)

Open the folder `web/` and **double-click `index.html`**.

It opens in your browser. Click **1 · ELISA**, then press **▶ Next turn** four times.
Then click **2 · Dilution** and press **Next turn** twice.

No server. No install. No API key. No internet. It just works.

---

## What you're looking at

A scientist speaks a request. Claude turns it into a plan. Deterministic code compiles,
validates, and routes it to the instrument that can actually run it.

**Scenario 1 — ELISA.** The agent asks the two things it must never guess (which analyte,
how many samples). Then it **catches a 400 µL transfer into a 300 µL well and blocks it**
before anything moves. After the correction, it runs on Opentrons.

**Scenario 2 — Serial dilution.** A nanoliter dilution series compiles to an Echo picklist
and runs.

Watch the **Robot routing** panel in both. The ELISA is accepted by Opentrons and *refused*
by the Echo. The dilution is accepted by the Echo and *refused* by Opentrons. Same
intermediate representation, same validator, opposite verdicts — decided purely by each
platform's declared capabilities, with no special-casing. That's the whole idea.

---

## Run the backend (optional)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 -m demo.demo_script     # the same two scenarios, in the terminal
python3 -m tests.test_pipeline  # 8 tests
uvicorn app.main:app --reload   # the API the voice layer calls
```

Then in the console, click **Live** to drive the real backend instead of the fixtures.

---

## For the voice teammate

One endpoint. POST a transcript, get back what to say plus the validated workflow:

```
POST /session/message?adapter=opentrons
{ "transcript": "Run an ELISA on today's plasma samples", "session_id": "abc" }
```

Returns `reply` (speak this), plus `plan`, `workflow`, `validation`, `state`, and
`clarification_questions`. Pass the same `session_id` back each turn — clarification and
confirmation are multi-turn.

---

## Files

```
web/index.html    the console — double-click this
app/              the backend (IR, planner, compiler, validators, adapters, API)
demo/             terminal demo of both scenarios
tests/            8 tests covering the core guarantees
README.md         full architecture write-up
PUSH.md           how to get this onto GitHub
```
