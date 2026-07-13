"""
Validation pipeline (runs on the Ops Workflow, in order).

    schema  -> already guaranteed by Pydantic at construction
    resource/safety rules -> safety_rules.ALL_RULES
    platform capability   -> adapter.check_capabilities(workflow)
    simulation dry-run    -> adapter.simulate(workflow)  [called separately by the API]

The first stages loop back to the voice layer (fixable issues). The capability
and simulation stages gate execution. This module returns a single report; the
API decides how to surface it.
"""
from __future__ import annotations

from app.adapters.base import Adapter
from app.inventory.store import get_store
from app.models.session import ValidationReport
from app.models.workflow import Workflow
from app.validation.safety_rules import ALL_RULES


def validate(workflow: Workflow, adapter: Adapter) -> ValidationReport:
    inv = get_store()
    issues = []

    # 1. Deterministic safety + resource rules (model-free).
    for rule in ALL_RULES:
        issues.extend(rule(workflow, inv))

    # 2. Platform capability reconciliation — the robot-agnostic guardrail.
    issues.extend(adapter.check_capabilities(workflow))

    passed = not any(i.severity == "error" for i in issues)
    return ValidationReport(passed=passed, issues=issues)
