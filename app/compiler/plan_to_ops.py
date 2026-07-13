"""
Compiler: Plan (+ SOP) -> Workflow (Ops IR).

Deterministic template expansion. No model here, so it is reproducible and unit
testable. It lays out wells, resolves reagent references, computes required
reagent volumes, and emits primitive operations. The output is still fully
platform-independent — adapters specialize it afterwards.
"""
from __future__ import annotations

import string
import uuid

from app.models.plan import Plan, Volume, Unit
from app.models.workflow import (
    Labware, Operation, OpType, Reagent, WellRef, Workflow,
)
from app.sop.registry import get_registry
from app.inventory.store import get_store

_ROWS = "ABCDEFGH"
_COLS = list(range(1, 13))


def _well_sequence(n: int) -> list[str]:
    """Column-major well order (A1,B1,...,H1,A2,...), the standard fill order."""
    wells = []
    for col in _COLS:
        for row in _ROWS:
            wells.append(f"{row}{col}")
            if len(wells) == n:
                return wells
    return wells


ECHO_DROPLET_NL = 2.5  # acoustic droplet resolution


def compile_plan(plan: Plan) -> Workflow:
    if plan.intent.value == "elisa":
        return _compile_elisa(plan)
    if plan.intent.value == "serial_dilution":
        return _compile_serial_dilution(plan)
    raise NotImplementedError(f"No compiler for intent '{plan.intent.value}' yet.")


def _round_droplet(nl: float, res: float = ECHO_DROPLET_NL) -> float:
    """Round a nanoliter volume to the acoustic droplet resolution."""
    return round(round(nl / res) * res, 4)


def _compile_serial_dilution(plan: Plan) -> Workflow:
    """Direct dilution series: for point i, dispense compound = V / DF**i and
    backfill diluent to a constant final volume V. No well-to-well chaining and
    no mixing, so error doesn't compound and the whole thing is a set of
    independent transfers — exactly what an acoustic dispenser wants.
    """
    sop = get_registry().get(plan.sop_id) or {}
    p = plan.parameters
    opt = sop.get("optional_parameters", {})
    src = sop.get("sources", {})

    DF = float(p.get("dilution_factor", opt.get("dilution_factor", 2)))
    V = float(p.get("final_volume_nL", opt.get("final_volume_nL", 1000)))
    n = int(p.get("num_points", 0))
    compound = p.get("compound", "compound")
    diluent_reagent = src.get("diluent_reagent", "RG-DMSO")
    compound_well = src.get("compound_well", "A1")
    diluent_well = src.get("diluent_well", "A2")

    store = get_store()
    layout = sop.get("layout", {}).get("labware", [
        {"id": "compound_source", "type": "corning_96_wellplate_360ul", "role": "samples"},
        {"id": "dilution_plate", "type": "corning_96_wellplate_360ul", "role": "assay"},
    ])
    labware = []
    for lw in layout:
        loaded = store.get_labware(lw["id"])
        labware.append(Labware(id=lw["id"], type=lw["type"], role=lw.get("role"),
                               slot=(loaded or {}).get("slot")))

    dest_wells = [f"A{i + 1}" for i in range(min(n, 12))]  # single row for the demo
    operations: list[Operation] = []
    diluent_total = 0.0

    for i, well in enumerate(dest_wells):
        compound_vol = _round_droplet(V / (DF ** i))
        diluent_vol = _round_droplet(V - compound_vol)

        operations.append(Operation(
            op=OpType.transfer,
            source=WellRef(labware="compound_source", well=compound_well),
            dest=WellRef(labware="dilution_plate", well=well),
            volume=Volume(value=compound_vol, unit=Unit.nL),
            new_tip=False,  # acoustic: tipless
            note=f"{compound} dilution point {i + 1} (1:{DF ** i:g})",
        ))
        if diluent_vol > 0:
            operations.append(Operation(
                op=OpType.transfer,
                reagent_id=diluent_reagent,
                source=WellRef(labware="compound_source", well=diluent_well),
                dest=WellRef(labware="dilution_plate", well=well),
                volume=Volume(value=diluent_vol, unit=Unit.nL),
                new_tip=False,
                note="diluent backfill",
            ))
            diluent_total += diluent_vol

    info = store.get_reagent(diluent_reagent) or {}
    reagents = [Reagent(
        id=diluent_reagent, name=info.get("name", diluent_reagent),
        location=WellRef(labware="compound_source", well=diluent_well),
        required_volume=Volume(value=diluent_total, unit=Unit.nL),
    )]

    return Workflow(
        workflow_id=f"wf_{uuid.uuid4().hex[:8]}",
        source_sop=plan.sop_id,
        labware=labware,
        reagents=reagents,
        operations=operations,
        metadata={
            "intent": plan.intent.value,
            "compound": compound,
            "num_points": n,
            "dilution_factor": DF,
            "final_volume_nL": V,
        },
    )


def _compile_elisa(plan: Plan) -> Workflow:
    sop = get_registry().get(plan.sop_id)
    if sop is None:
        raise ValueError(f"SOP {plan.sop_id} not found")

    p = plan.parameters
    opt = sop.get("optional_parameters", {})
    replicates = int(p.get("replicates", opt.get("replicates", 2)))
    sample_vol = float(p.get("sample_volume_uL", opt.get("sample_volume_uL", 100)))
    num_samples = int(p.get("num_samples", 0))
    analyte = p.get("target_analyte")
    detection_reagent = p.get("detection_reagent")

    controls = sop["layout"]["controls"]
    total_wells = num_samples * replicates + controls["standards"] + controls["blanks"]

    # Labware from SOP layout, slots resolved from live deck state.
    store = get_store()
    labware: list[Labware] = []
    for lw in sop["layout"]["labware"]:
        loaded = store.get_labware(lw["id"])
        labware.append(Labware(
            id=lw["id"], type=lw["type"], role=lw.get("role"),
            slot=(loaded or {}).get("slot"),
        ))

    assay_wells = _well_sequence(total_wells)
    sample_wells = _well_sequence(num_samples)

    reagents: list[Reagent] = []
    reagent_totals: dict[str, float] = {}

    def _add_reagent_use(rid: str, per_well_uL: float, wells: list[str]):
        reagent_totals[rid] = reagent_totals.get(rid, 0.0) + per_well_uL * len(wells)

    operations: list[Operation] = []

    for step in sop["steps"]:
        op = step["op"]

        if op == "transfer":
            reagent_ref = step.get("reagent")
            # Resolve templated reagent (e.g. "{detection_reagent}").
            if reagent_ref == "{detection_reagent}":
                reagent_ref = detection_reagent
            vol_raw = step.get("volume_uL")
            vol = sample_vol if vol_raw == "{sample_volume_uL}" else float(vol_raw)

            if step.get("source") == "sample_plate":
                # Sample transfer: map each sample well -> its replicate assay wells.
                for i, sw in enumerate(sample_wells):
                    for r in range(replicates):
                        dest = assay_wells[i * replicates + r]
                        operations.append(Operation(
                            op=OpType.transfer,
                            source=WellRef(labware="sample_plate", well=sw),
                            dest=WellRef(labware="assay_plate", well=dest),
                            volume=Volume(value=vol, unit=Unit.uL),
                            new_tip=True,  # fresh tip per sample: contamination discipline
                            note=step.get("note"),
                        ))
            else:
                # Bulk reagent addition to every assay well in use.
                for w in assay_wells:
                    operations.append(Operation(
                        op=OpType.transfer,
                        reagent_id=reagent_ref,
                        source=WellRef(labware="reagent_reservoir", well="A1"),
                        dest=WellRef(labware="assay_plate", well=w),
                        volume=Volume(value=vol, unit=Unit.uL),
                        new_tip=False,  # same reagent, tip reuse acceptable
                        note=step.get("note"),
                    ))
                if reagent_ref:
                    _add_reagent_use(reagent_ref, vol, assay_wells)

        elif op == "wash":
            cycles = int(step.get("cycles", 3))
            operations.append(Operation(
                op=OpType.wash,
                reagent_id=step.get("reagent"),
                dest=WellRef(labware=step["dest"], well="ALL"),
                note=f"{cycles}x wash cycle",
            ))
            _add_reagent_use(step.get("reagent"), 300.0 * cycles, assay_wells)

        elif op == "incubate":
            operations.append(Operation(
                op=OpType.incubate,
                duration_s=int(step.get("duration_min", 0)) * 60,
                temperature_c=step.get("temperature_c"),
                note=step.get("note"),
            ))

        elif op == "wait":
            operations.append(Operation(
                op=OpType.wait,
                duration_s=int(step.get("duration_min", 0)) * 60,
                note=step.get("note"),
            ))

        elif op == "manual_step":
            operations.append(Operation(op=OpType.manual_step, note=step.get("note")))

    # Build reagent manifest with required volumes for downstream resource checks.
    for rid, total in reagent_totals.items():
        info = store.get_reagent(rid)
        reagents.append(Reagent(
            id=rid,
            name=(info or {}).get("name", rid),
            location=WellRef(labware="reagent_reservoir", well="A1"),
            required_volume=Volume(value=total, unit=Unit.uL),
        ))

    return Workflow(
        workflow_id=f"wf_{uuid.uuid4().hex[:8]}",
        source_sop=plan.sop_id,
        labware=labware,
        reagents=reagents,
        operations=operations,
        metadata={
            "intent": plan.intent.value,
            "analyte": analyte,
            "num_samples": num_samples,
            "replicates": replicates,
            "total_wells": total_wells,
            "sample_volume_uL": sample_vol,
        },
    )
