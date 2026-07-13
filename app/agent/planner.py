"""
Planner: transcript -> Plan.

Uses Claude with a forced tool call so the output always conforms to the Plan
schema. If ANTHROPIC_API_KEY is unset, falls back to a deterministic mock so the
whole pipeline (and the demo) runs offline. The mock mimics what a well-prompted
Claude returns for the two demo intents.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS
from app.agent.prompts import (
    PLANNER_SYSTEM_PROMPT,
    PLAN_TOOL,
    build_user_message,
)
from app.models.plan import Intent, Plan
from app.inventory.store import get_store
from app.sop.registry import get_registry


def plan_from_transcript(transcript: str) -> Plan:
    context = get_store().context_summary()
    sops = get_registry().all_sops()
    if ANTHROPIC_API_KEY:
        try:
            return _plan_with_claude(transcript, context, sops)
        except Exception as e:  # never crash the turn on an API hiccup
            print(f"[planner] Claude call failed ({e}); using mock fallback.")
    return _plan_mock(transcript)


def merge_clarification(plan: Plan, answers: dict) -> Plan:
    """Fold clarification answers into an existing plan and recompute missing fields."""
    updated = plan.model_copy(deep=True)
    updated.parameters.update({k: v for k, v in answers.items() if v is not None})

    required = get_registry().required_parameters(updated.sop_id) if updated.sop_id else []
    updated.missing_fields = [f for f in required if f not in updated.parameters]

    # Resolve detection reagent once analyte is known (so the audit trail is honest).
    sop = get_registry().get(updated.sop_id) if updated.sop_id else None
    analyte = updated.parameters.get("target_analyte")
    if sop and analyte and analyte in sop.get("analyte_reagents", {}):
        updated.parameters.setdefault(
            "detection_reagent", sop["analyte_reagents"][analyte]["detection"]
        )
        updated.parameters.setdefault(
            "capture_reagent", sop["analyte_reagents"][analyte]["capture"]
        )
    return updated


# --------------------------------------------------------------------------- #
# Claude-backed planning
# --------------------------------------------------------------------------- #
def _plan_with_claude(transcript: str, context: dict, sops: list[dict]) -> Plan:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=PLANNER_SYSTEM_PROMPT,
        tools=[PLAN_TOOL],
        tool_choice={"type": "tool", "name": "emit_plan"},
        messages=[{"role": "user", "content": build_user_message(transcript, context, sops)}],
    )
    tool_use = next(b for b in resp.content if getattr(b, "type", None) == "tool_use")
    return Plan.model_validate(tool_use.input)


# --------------------------------------------------------------------------- #
# Offline deterministic mock (keeps the demo runnable with no API key)
# --------------------------------------------------------------------------- #
def _plan_mock(transcript: str) -> Plan:
    t = transcript.lower()
    reg = get_registry()

    if "elisa" in t:
        sop = reg.default_for_intent("elisa")
        params: dict = {}
        assumptions = [
            "Diluent/wash buffer defaulted to PBST per SOP-ELISA-04",
            "Replicates defaulted to 2 (duplicate wells)",
            "1 assay plate assumed",
        ]
        # 'today's plasma samples' -> resolve against inventory, but analyte is unknown.
        todays = get_store().samples_collected_on(date.today().isoformat())
        if "today" in t and todays:
            assumptions.append(
                f"'today's plasma samples' resolved to {len(todays)} samples collected {date.today().isoformat()}"
            )
        missing = ["target_analyte"]  # never guess the analyte
        if "sample" not in t or not any(c.isdigit() for c in t):
            missing.append("num_samples")
        return Plan(
            intent=Intent.elisa,
            sop_id=sop["sop_id"] if sop else None,
            parameters=params,
            assumptions=assumptions,
            missing_fields=missing,
            requires_confirmation=True,
            rationale="Mapped 'run an ELISA' to SOP-ELISA-04; analyte and sample count unresolved.",
        )

    if "dilution" in t or "dilute" in t:
        sop = reg.default_for_intent("serial_dilution")
        required = sop.get("required_parameters", ["compound", "num_points"]) if sop else ["compound", "num_points"]
        # Try to fill what's already in the utterance so a fully-specified request
        # can go straight to confirmation.
        from app.agent import clarify as _clarify
        stub = Plan(intent=Intent.serial_dilution, sop_id=sop["sop_id"] if sop else None,
                    missing_fields=required, assumptions=[], requires_confirmation=True)
        prefilled = _clarify.parse_answers(stub, transcript)
        missing = [f for f in required if f not in prefilled]
        return Plan(
            intent=Intent.serial_dilution,
            sop_id=sop["sop_id"] if sop else None,
            parameters=prefilled,
            assumptions=[
                "Dilution factor defaulted to 2-fold",
                "Diluent defaulted to DMSO backfill",
                "Final volume defaulted to 1000 nL per well",
                "Direct (non-contact) dilution chosen — suits acoustic dispense",
            ],
            missing_fields=missing,
            requires_confirmation=True,
            rationale="Mapped request to SOP-DIL-01 (nanoliter direct dilution).",
        )

    return Plan(
        intent=Intent.unknown,
        assumptions=[],
        missing_fields=[],
        requires_confirmation=True,
        rationale="Could not map request to a known SOP.",
    )
