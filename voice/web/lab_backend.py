"""The integration seam: the voice half talking to the Lab Agent API.

The Lab Agent (app/, this repo) owns the BRAIN of a lab turn: it takes a
transcript, resolves intent with Claude into a Plan, compiles the Plan to a
platform-independent Workflow, validates it against deterministic safety rules
and the target platform's declared capabilities, and simulates the run. It holds
its own per-session state machine (idle -> gathering -> awaiting_confirmation ->
executed), because clarification and confirmation are multi-turn.

So the voice half does NOT re-plan, re-confirm, or re-execute. It sends the
transcript and speaks the `reply` that comes back. There is exactly one planner
and one confirmation gate in the system, and they live on the Lab Agent side.

What the voice half DOES own, and why this module is not a thin HTTP wrapper:

  A microphone is not a keyboard. The Lab Agent implicitly trusts its transcript,
  which is the right assumption for a typed console and the wrong one for a room
  with a centrifuge running in it. Speech-side safety therefore stays here:

  - Confidence floor on a CONFIRMATION. When the backend is awaiting_confirmation,
    the next utterance can start a machine. If ASR is not sure what it heard, that
    utterance is NOT forwarded: it is re-prompted. A misheard "yes" must never be
    the thing that fires a pump. (The confidence-gate and addressed-speech checks
    upstream in server.py already drop noise and room chatter before we get here;
    this is the last, strictest one, and it is the reason a low-confidence
    affirmative cannot reach the backend at all.)

  - Session mapping. One WS session pins one backend session_id, so a multi-turn
    clarification survives and two browser clients cannot stomp each other's plan.

Enable by setting LA_LAB_BACKEND_URL. When it is unset the orchestrator keeps its
previous self-contained behavior (the local lab_gate + AutomationStub), so every
existing test and the no-backend demo path are unchanged.
"""
from __future__ import annotations

import os

import httpx

# Where the Lab Agent API lives, e.g. "http://127.0.0.1:8000". Unset -> seam off.
BACKEND_URL = os.environ.get("LA_LAB_BACKEND_URL", "").strip().rstrip("/")

# Which robot the Lab Agent should target. Its /adapters endpoint declares the
# capabilities of each; the validator reconciles the workflow against them, which
# is what makes a refusal ("an ELISA is not an acoustic job") meaningful.
ADAPTER = os.environ.get("LA_LAB_ADAPTER", "opentrons").strip() or "opentrons"

# Seconds. A planner turn can call Claude, so this is generous relative to the
# rest of the pipeline; it still bounds the turn rather than hanging the session.
TIMEOUT_S = float(os.environ.get("LA_LAB_BACKEND_TIMEOUT_S", "30"))

# The backend states in which the NEXT utterance can cause something to happen.
# An utterance arriving in one of these states is confirmation-critical.
_ARMED_STATES = frozenset({"awaiting_confirmation"})

# ASR confidence floor for a confirmation-critical utterance. Reuses the same env
# var as the local gate's execution floor so an operator tunes ONE number, and
# defaults to the same 0.40. Missing confidence (a degraded ASR, or the mock)
# fails OPEN, matching the local gate: it must not lock out a legitimate confirm.
CONFIRM_FLOOR = float(os.environ.get("LA_CONFIRM_FLOOR", "0.40"))


def enabled() -> bool:
    return bool(BACKEND_URL)


def armed(state: str | None) -> bool:
    """True when the backend's NEXT affirmative utterance would execute something.
    The whole reason the voice half treats the next turn as safety-critical."""
    return state in _ARMED_STATES


def blocks_confirmation(state: str | None, prob_mean: float | None) -> bool:
    """True when this utterance must NOT reach the backend.

    The backend is armed (its next affirmative executes), and ASR is not confident
    enough about what was said. Returning True means: do not forward, re-prompt.

    Keys on prob_mean, not prob_min, matching the local gate's execution floor:
    the calibration showed prob_min overlaps between speech and noise, so a single
    weak token in an otherwise clear "confirm" should not veto it.

    prob_mean None means the ASR supplied no confidence at all. That fails OPEN
    (returns False), deliberately: the mock and a degraded-but-working ASR both
    report no confidence, and locking them out would break the demo and every
    existing smoke. The floor only bites when we HAVE a number and it is low.
    """
    if not armed(state):
        return False
    if prob_mean is None or CONFIRM_FLOOR <= 0:
        return False
    return prob_mean < CONFIRM_FLOOR


REPROMPT = ("I did not catch that clearly enough to act on it. "
            "Please say confirm, or say cancel.")


class LabBackendError(RuntimeError):
    """The Lab Agent could not be reached or returned an error. The caller turns
    this into a spoken apology rather than silence, and never into an action."""


class LabBackend:
    """One backend conversation, pinned to one voice session."""

    def __init__(self, adapter: str | None = None):
        self.adapter = adapter or ADAPTER
        self.session_id: str | None = None   # assigned by the backend on turn 1
        self.state: str | None = None        # its state machine, as of the last reply
        self.last: dict | None = None        # the last full MessageResponse

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{BACKEND_URL}/health")
            r.raise_for_status()
            return r.json()

    async def send(self, transcript: str) -> dict:
        """One turn: POST the transcript, remember the state, return the response.

        The response's `reply` is what TTS speaks. `state`, `plan`, `workflow`,
        `validation` and `simulation_log` are what the UI renders.
        """
        payload = {"transcript": transcript}
        if self.session_id:
            payload["session_id"] = self.session_id
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
                r = await c.post(f"{BACKEND_URL}/session/message",
                                 params={"adapter": self.adapter}, json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            raise LabBackendError(str(e)) from e

        self.session_id = data.get("session_id") or self.session_id
        self.state = data.get("state")
        self.last = data
        return data

    async def reset(self) -> None:
        if not self.session_id:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{BACKEND_URL}/session/reset",
                             params={"session_id": self.session_id})
        except Exception:  # noqa: BLE001
            pass   # a failed reset must never break the voice session
        self.session_id = None
        self.state = None
        self.last = None


def summary(data: dict) -> dict:
    """The parts of a backend response the UI cares about, flattened for the WS.
    Kept small on purpose: the full plan/workflow can be large, and the browser
    only renders the headline (what will run, on what, and what the gate said)."""
    plan = data.get("plan") or {}
    wf = data.get("workflow") or {}
    val = data.get("validation") or {}
    return {
        "state": data.get("state"),
        "intent": plan.get("intent"),
        "sop_id": plan.get("sop_id"),
        "assumptions": plan.get("assumptions") or [],
        "operations": len((wf.get("operations") or [])),
        "validation_passed": val.get("passed"),
        "issues": [{"severity": i.get("severity"), "message": i.get("message")}
                   for i in (val.get("issues") or [])],
        "questions": data.get("clarification_questions") or [],
        "audit_id": data.get("audit_id"),
    }
