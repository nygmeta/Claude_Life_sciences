"""
Lab Agent API.

One endpoint the voice layer drives turn by turn:

    POST /session/message   {transcript, session_id?}  ->  MessageResponse

The handler is a small state machine:

    idle ── plan ──► gathering ── answers ──► awaiting_confirmation
                          │                          │
                    (still missing)            "yes/confirm"
                                                     ▼
                                     validate ─► ready ─► (simulate) ─► executed
                                        │
                                   errors found
                                        ▼
                                 validation_failed ── fix ──► (re-validate)

Voice pipeline → Lab Agent API → Workflow IR → adapter → simulation.
"""
from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.opentrons_adapter import OpentronsAdapter
from app.adapters.echo_adapter import EchoAdapter
from app.agent import clarify, planner
from app.compiler.plan_to_ops import compile_plan
from app.hardware import direct_intents
from app.hardware.ot2_client import OT2Error
from app.models.plan import Intent
from app.models.session import (
    MessageRequest, MessageResponse, Session, SessionState,
)
from app.validation.validator import validate

app = FastAPI(title="Lab Agent", version="0.1.0")

# Open CORS: the voice front end (and the web console) run on a different origin.
# Fine for a hackathon / local demo; tighten allow_origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSIONS: dict[str, Session] = {}
ADAPTERS = {"opentrons": OpentronsAdapter(), "echo": EchoAdapter()}

# Word-anchored on purpose: a plain substring test here once let "yesterday" (contains
# "yes") execute a pending plan, and "going" / "that's not correct" had the same failure
# mode. Never loosen these back to `word in t`.
_CONFIRM_RE = re.compile(r"\b(yes|confirm|confirmed|go ahead|proceed|do it|run it)\b", re.I)
_CANCEL_RE = re.compile(r"\b(no|cancel|stop|abort|wait|never mind|nevermind)\b", re.I)

# A confirmation may not carry NEW CONTENT: "run it" is sign-off, "run it on yesterday's
# samples" is a fresh instruction and must reach clarification instead of the robot.
# So an affirmative also has to be *bare*: everything left over once the affirmative
# phrase is removed must be filler, or a word from the plan the scientist is confirming.
# Anything unrecognised counts as content, which fails safe toward re-asking.
_CONFIRM_FILLERS = {
    "please", "now", "just", "right", "ok", "okay", "alright", "then", "and", "so",
    "the", "a", "an", "that", "this", "it", "all", "lets", "let's", "we", "i", "you",
    "thanks", "run",
}
_WORD_RE = re.compile(r"[a-z']+")


def _plan_words(plan) -> set[str]:
    """Words naming the plan under confirmation, so "confirm ELISA" and "yes, run the
    ELISA" stay bare sign-offs rather than reading as new instructions."""
    if plan is None:
        return set()
    src = f"{plan.intent.value if plan.intent else ''} {plan.sop_id or ''}"
    return set(_WORD_RE.findall(src.lower()))


def _is_bare_affirmative(t: str, plan) -> bool:
    """True only for a confirmation that adds nothing to the plan."""
    if not _CONFIRM_RE.search(t):
        return False
    residue = _CONFIRM_RE.sub(" ", t)
    allowed = _CONFIRM_FILLERS | _plan_words(plan)
    return not [w for w in _WORD_RE.findall(residue) if w not in allowed]


def _label(wf) -> str:
    """Human phrase describing a workflow, per intent — for TTS read-back."""
    md = wf.metadata
    intent = md.get("intent")
    if intent == "elisa":
        return (f"{md.get('analyte')} ELISA, {md.get('num_samples')} samples in "
                f"{md.get('replicates')} replicates ({md.get('total_wells')} wells)")
    if intent == "serial_dilution":
        return (f"{md.get('num_points')}-point {md.get('dilution_factor'):g}-fold serial "
                f"dilution of {md.get('compound')} ({md.get('final_volume_nL'):g} nL final volume)")
    return intent or "workflow"


def _get_session(session_id: str | None) -> Session:
    if session_id and session_id in SESSIONS:
        return SESSIONS[session_id]
    sid = session_id or f"sess_{uuid.uuid4().hex[:8]}"
    SESSIONS[sid] = Session(session_id=sid)
    return SESSIONS[sid]


@app.get("/health")
def health():
    return {"status": "ok", "adapters": list(ADAPTERS)}


@app.get("/adapters")
def adapters():
    """Declared capabilities per platform. This is what the routing dashboard
    reads to explain WHY a platform is selected or refused — the reasons come
    from the contract itself, not from hardcoded copy."""
    out = []
    for key, ad in ADAPTERS.items():
        c = ad.capabilities
        out.append({
            "key": key,
            "name": c.name,
            "supported_ops": sorted(o.value for o in c.supported_ops),
            "min_volume_uL": c.min_volume_uL,
            "max_volume_uL": c.max_volume_uL,
            "needs_tips": c.needs_tips,
            "labware_types": sorted(c.labware_types),
        })
    return {"adapters": out}


@app.get("/session/{session_id}/audit")
def audit(session_id: str):
    """Full audit trail: request -> plan -> compile -> validation -> run."""
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "No such session")
    return {
        "session_id": s.session_id,
        "transcript_log": s.transcript_log,
        "audit_trail": s.audit_trail,
    }


@app.post("/session/reset")
def reset(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"reset": session_id}


@app.post("/session/message", response_model=MessageResponse)
def message(req: MessageRequest, adapter: str = "opentrons") -> MessageResponse:
    if adapter not in ADAPTERS:
        raise HTTPException(400, f"Unknown adapter '{adapter}'")
    session = _get_session(req.session_id)
    session.log_turn("scientist", req.transcript)
    active = ADAPTERS[adapter]

    # Direct hardware intents (home the robot, lights, health) bypass the SOP
    # planner and hit the OT-2 HTTP API. Only intercept when no plan is under
    # negotiation: a "yes" during SOP awaiting_confirmation must NOT be re-read
    # as a direct command, and a mid-clarification transcript belongs to the
    # SOP flow.
    if session.state in (SessionState.idle, SessionState.executed):
        intent = direct_intents.match(req.transcript)
        if intent is not None:
            return _handle_direct(session, intent)

    # --- Route by current state -------------------------------------------- #
    if session.state == SessionState.awaiting_confirmation:
        return _handle_confirmation(session, req.transcript, active)

    if session.state in (SessionState.gathering, SessionState.validation_failed) and session.plan:
        return _handle_followup(session, req.transcript, active)

    # New request.
    return _handle_new_request(session, req.transcript, active)


# --------------------------------------------------------------------------- #
def _handle_direct(session: Session, intent: str) -> MessageResponse:
    """Direct-to-robot bypass. Fires immediately on a matched final transcript;
    trades the two-turn confirmation gate for latency. Read-only intents (lights,
    health) never had a gate; motion intents (home, rerun) rely on ASR finality
    and the operator not saying the phrase unless they mean it."""
    try:
        result = direct_intents.execute(intent)
    except OT2Error as e:
        session.audit("direct_hardware_error", {"intent": intent, "error": str(e)})
        return _respond(
            session,
            f"I could not reach the Opentrons: {e}. "
            f"Check that the robot is powered on and on the same network.",
        )
    session.audit("direct_hardware", {"action": result.action, "detail": result.detail})
    session.state = SessionState.idle
    return _respond(session, result.reply)


def _handle_new_request(session: Session, transcript: str, adapter) -> MessageResponse:
    plan = planner.plan_from_transcript(transcript)
    session.plan = plan
    session.audit("plan", plan.model_dump())

    if plan.intent == Intent.unknown:
        session.state = SessionState.idle
        return _respond(session, "I couldn't match that to a known protocol. "
                                 "Could you name the assay or procedure?")

    if not plan.is_complete:
        return _ask_clarification(session)

    return _to_confirmation(session, adapter)


def _handle_followup(session: Session, transcript: str, adapter) -> MessageResponse:
    answers = clarify.parse_answers(session.plan, transcript)
    session.plan = planner.merge_clarification(session.plan, answers)
    session.audit("clarification", {"answers": answers, "plan": session.plan.model_dump()})

    if not session.plan.is_complete:
        return _ask_clarification(session)

    return _to_confirmation(session, adapter)


def _handle_confirmation(session: Session, transcript: str, adapter) -> MessageResponse:
    t = transcript.strip().lower()
    if _CANCEL_RE.search(t):
        session.state = SessionState.idle
        return _respond(session, "Cancelled. Nothing was executed. What would you like to do?")

    if not _is_bare_affirmative(t, session.plan):
        # Treat as a late correction, e.g. "actually make it 100 uL per well".
        answers = clarify.parse_answers(session.plan, transcript)
        if answers:
            session.plan = planner.merge_clarification(session.plan, answers)
            return _to_confirmation(session, adapter)
        return _respond(session, "Please confirm to proceed, or tell me what to change.")

    # Confirmed -> execute (simulate).
    ok, log = adapter.simulate(session.workflow)
    session.audit("simulation", {"ok": ok, "adapter": adapter.capabilities.name})
    session.state = SessionState.executed
    verb = "completed in simulation" if ok else "failed in simulation"
    reply = (f"Confirmed. {_label(session.workflow)} {verb} on {adapter.capabilities.name}. "
             f"{len(session.workflow.operations)} operations. Full log attached.")
    return _respond(session, reply, simulation_log=log)


# --------------------------------------------------------------------------- #
def _ask_clarification(session: Session) -> MessageResponse:
    session.state = SessionState.gathering
    qs = clarify.questions_for(session.plan)
    lead = "Before I build the protocol, I need a couple of details. " if len(qs) > 1 else ""
    return _respond(session, lead + " ".join(qs), clarification_questions=qs)


def _to_confirmation(session: Session, adapter) -> MessageResponse:
    # Compile Plan -> Ops, then validate against safety rules + platform capability.
    try:
        workflow = compile_plan(session.plan)
    except NotImplementedError as e:
        session.state = SessionState.idle
        return _respond(session, str(e))
    session.workflow = workflow
    session.audit("compile", workflow.model_dump())

    report = validate(workflow, adapter)
    session.audit("validation", report.model_dump())

    if not report.passed:
        session.state = SessionState.validation_failed
        problems = " ".join(i.message for i in report.errors)
        return _respond(
            session,
            f"I found a problem before running anything: {problems}",
            validation=report,
        )

    # Passed. Present assumptions + any warnings and ask for sign-off.
    session.state = SessionState.awaiting_confirmation
    a = session.plan.assumptions
    warnings = [i.message for i in report.issues if i.severity == "warning"]
    summary = f"Ready: {_label(workflow)}, using {session.plan.sop_id}."
    assume = (" Assumptions: " + "; ".join(a) + ".") if a else ""
    warn = (" Note: " + " ".join(warnings)) if warnings else ""
    return _respond(session, summary + assume + warn + " Shall I proceed?",
                    validation=report)


def _respond(session: Session, reply: str, **extra) -> MessageResponse:
    session.log_turn("agent", reply)
    return MessageResponse(
        session_id=session.session_id,
        state=session.state,
        reply=reply,
        plan=session.plan,
        workflow=session.workflow,
        audit_id=session.session_id,
        clarification_questions=extra.get("clarification_questions", []),
        validation=extra.get("validation"),
        simulation_log=extra.get("simulation_log"),
    )
