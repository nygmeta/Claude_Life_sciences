"""
Session + API contract models.

The voice layer sends one transcript per turn and receives a reply plus
(optionally) a workflow and validation report. Because clarification and
confirmation are multi-turn, the Lab Agent keeps per-session state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.plan import Plan
from app.models.workflow import Workflow


def _now() -> str:
    """UTC ISO-8601 timestamp for audit records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionState(str, Enum):
    idle = "idle"
    gathering = "gathering"              # missing required info, asking questions
    awaiting_confirmation = "awaiting_confirmation"  # plan complete, need human sign-off
    validation_failed = "validation_failed"          # caught a problem, awaiting fix
    ready = "ready"                      # validated, compiled, safe to run
    executed = "executed"                # simulation / run complete


class ValidationIssue(BaseModel):
    severity: str          # "error" | "warning"
    rule: str              # which check fired
    message: str           # human-readable, TTS-ready
    fixable_by_user: bool = True


class ValidationReport(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]


class MessageRequest(BaseModel):
    """What the voice layer POSTs each turn."""
    transcript: str
    session_id: Optional[str] = None


class MessageResponse(BaseModel):
    """What the voice layer gets back. `reply` is the string TTS should speak."""
    session_id: str
    state: SessionState
    reply: str
    clarification_questions: list[str] = Field(default_factory=list)
    plan: Optional[Plan] = None
    workflow: Optional[Workflow] = None
    validation: Optional[ValidationReport] = None
    simulation_log: Optional[str] = None
    audit_id: Optional[str] = None


class Session(BaseModel):
    session_id: str
    state: SessionState = SessionState.idle
    plan: Optional[Plan] = None
    workflow: Optional[Workflow] = None
    transcript_log: list[dict[str, str]] = Field(default_factory=list)
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)

    def log_turn(self, role: str, text: str) -> None:
        self.transcript_log.append({
            "role": role, "text": text, "ts": _now(),
        })

    def audit(self, stage: str, payload: Any) -> None:
        """Append an audit record. Every stage of the pipeline lands here, so the
        chain transcript -> plan -> ops -> validation -> run is reconstructible."""
        self.audit_trail.append({
            "stage": stage, "ts": _now(), "payload": payload,
        })
