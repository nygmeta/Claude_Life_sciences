"""
Deterministic safety + resource rules.

These NEVER involve the model. Each rule inspects the compiled Workflow against
the live inventory/deck state and returns zero or more issues. Claude may explain
a flag to the scientist, but it is not the thing standing between them and a
hazard — these functions are.

Add rules here as new failure modes are discovered. Every rule is independently
unit-testable.
"""
from __future__ import annotations

from datetime import date

from app.inventory.store import InventoryStore
from app.models.session import ValidationIssue
from app.models.workflow import OpType, Workflow

# Reagent pairs that must never be dispensed into the same wells / handled together.
INCOMPATIBLE_PAIRS = {
    frozenset({"bleach", "acid"}),      # -> chlorine gas
    frozenset({"bleach", "ammonia"}),   # -> chloramine gas
}
HAZARD_KEYWORDS = {"corrosive_acid", "corrosive_base", "oxidizer", "toxic"}


def rule_well_capacity(wf: Workflow, inv: InventoryStore) -> list[ValidationIssue]:
    """No single transfer may exceed the destination well's working volume.

    Aggregates: reports each distinct (labware, volume, limit) violation once,
    with a count of affected transfers — TTS-friendly, not one line per well.
    """
    seen: dict[tuple, int] = {}
    for op in wf.operations:
        if op.op == OpType.transfer and op.volume and op.dest:
            lw = inv.get_labware(op.dest.labware)
            if not lw:
                continue
            working = lw.get("well_working_uL")
            if working and op.volume.to_uL() > working:
                key = (op.dest.labware, op.volume.to_uL(), working)
                seen[key] = seen.get(key, 0) + 1
    issues = []
    for (labware, vol, working), count in seen.items():
        issues.append(ValidationIssue(
            severity="error", rule="well_capacity",
            message=(f"{count} transfer(s) request {vol:.0f} uL into {labware}, but the "
                     f"well working volume is {working:.0f} uL. Reduce the volume."),
        ))
    return issues


def rule_reagent_present_and_sufficient(wf: Workflow, inv: InventoryStore) -> list[ValidationIssue]:
    """Every referenced reagent must exist, be in date, and have enough volume."""
    issues = []
    for rg in wf.reagents:
        info = inv.get_reagent(rg.id)
        if info is None:
            issues.append(ValidationIssue(
                severity="error", rule="reagent_missing",
                message=f"Reagent {rg.id} is referenced but not loaded in inventory.",
            ))
            continue
        if inv.is_expired(rg.id):
            issues.append(ValidationIssue(
                severity="error", rule="reagent_expired",
                message=f"Reagent {info['name']} ({rg.id}) is past its expiry date.",
            ))
        need = rg.required_volume.to_uL() if rg.required_volume else 0.0
        have = info.get("volume_uL", 0)
        if need > have:
            issues.append(ValidationIssue(
                severity="error", rule="reagent_insufficient",
                message=(f"Need {need:.0f} uL of {info['name']} but only {have:.0f} uL "
                         f"is available. Restock or reduce scope."),
            ))
    return issues


def rule_reagent_compatibility(wf: Workflow, inv: InventoryStore) -> list[ValidationIssue]:
    """Flag incompatible reagents dispensed into the same wells without a wash between."""
    issues = []
    # Track, per destination well, the reagents seen since the last wash.
    seen: dict[str, set[str]] = {}
    for op in wf.operations:
        if op.op == OpType.wash:
            seen.clear()
            continue
        if op.op == OpType.transfer and op.dest and op.reagent_id:
            info = inv.get_reagent(op.reagent_id) or {}
            tokens = _hazard_tokens(info)
            key = f"{op.dest.labware}:{op.dest.well}"
            prior = seen.setdefault(key, set())
            for pair in INCOMPATIBLE_PAIRS:
                if pair & tokens and pair & prior and not (pair & tokens) == (pair & prior):
                    issues.append(ValidationIssue(
                        severity="error", rule="reagent_incompatible",
                        message=(f"Incompatible reagents would meet in {key}: {sorted(pair)}. "
                                 f"Insert a wash step or separate them."),
                    ))
            prior |= tokens
    return issues


def rule_hazard_confirmation(wf: Workflow, inv: InventoryStore) -> list[ValidationIssue]:
    """Hazardous reagents get a soft flag so the confirmation gate names them explicitly."""
    issues = []
    for rg in wf.reagents:
        info = inv.get_reagent(rg.id) or {}
        if info.get("hazard") in HAZARD_KEYWORDS:
            issues.append(ValidationIssue(
                severity="warning", rule="hazardous_reagent",
                message=(f"{info['name']} is {info['hazard'].replace('_', ' ')}. "
                         f"This will be named at the confirmation gate."),
                fixable_by_user=False,
            ))
    return issues


def _hazard_tokens(info: dict) -> set[str]:
    tokens = set()
    name = info.get("name", "").lower()
    if "bleach" in name or "hypochlorite" in name:
        tokens.add("bleach")
    if "ammonia" in name:
        tokens.add("ammonia")
    if info.get("hazard") == "corrosive_acid" or "h2so4" in name or "acid" in name:
        tokens.add("acid")
    return tokens


# Note: transfer-volume bounds (min/max) are enforced per-platform by each
# adapter's capability check, not here — a tipless acoustic dispenser and a
# tip-based handler have entirely different limits. These rules are the
# platform-independent ones.
ALL_RULES = [
    rule_well_capacity,
    rule_reagent_present_and_sufficient,
    rule_reagent_compatibility,
    rule_hazard_confirmation,
]
