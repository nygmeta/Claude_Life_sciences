"""
Workflow / Ops IR — the EXECUTABLE layer.

This is the common, robot-agnostic representation that every adapter consumes.
It is produced deterministically by the compiler from a Plan + SOP (no model in
that step, so it is reproducible and testable) and validated before any adapter
touches it.

Keep the primitive vocabulary SMALL. Six or seven primitives map cleanly onto
every platform's SDK. `manual_step` is the human-in-the-loop escape hatch for
anything a given robot cannot do.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.plan import Volume


class OpType(str, Enum):
    transfer = "transfer"        # move liquid A -> B
    mix = "mix"                  # aspirate/dispense in place
    wash = "wash"                # plate-wash cycle (aspirate + dispense buffer)
    incubate = "incubate"        # hold at temp for a duration
    wait = "wait"                # timed pause (no temp control)
    move_labware = "move_labware"  # relocate labware on/off deck
    manual_step = "manual_step"  # human action; robot cannot perform


class WellRef(BaseModel):
    labware: str        # references a Labware.id
    well: str           # e.g. "A1"


class Labware(BaseModel):
    id: str                      # logical id used across ops, e.g. "sample_plate"
    type: str                    # platform-neutral type token, e.g. "corning_96_wellplate_360ul"
    role: Optional[str] = None   # "samples" | "reagent" | "assay" | "tips" ...
    slot: Optional[str] = None   # deck position; assigned at compile time


class Reagent(BaseModel):
    id: str                      # inventory id, e.g. "RG-IL6-DET"
    name: str
    location: Optional[WellRef] = None
    required_volume: Optional[Volume] = None


class Operation(BaseModel):
    op: OpType
    # transfer / mix / wash
    source: Optional[WellRef] = None
    dest: Optional[WellRef] = None
    volume: Optional[Volume] = None
    reagent_id: Optional[str] = None
    mix_after: Optional[dict[str, Any]] = None   # {"reps": 3, "volume": 40}
    new_tip: bool = True                          # tip discipline; drives contamination checks
    # incubate / wait
    duration_s: Optional[int] = None
    temperature_c: Optional[float] = None
    # move_labware / manual_step
    labware_id: Optional[str] = None
    note: Optional[str] = None                     # human-readable; used by manual_step + TTS

    def describe(self) -> str:
        """One-line natural-language description, for TTS read-back and logs."""
        if self.op == OpType.transfer and self.volume and self.source and self.dest:
            return (f"Transfer {self.volume.value} {self.volume.unit.value} from "
                    f"{self.source.labware}:{self.source.well} to "
                    f"{self.dest.labware}:{self.dest.well}")
        if self.op == OpType.wash and self.dest:
            return f"Wash {self.dest.labware} (plate wash cycle)"
        if self.op == OpType.incubate:
            mins = (self.duration_s or 0) // 60
            temp = f" at {self.temperature_c}C" if self.temperature_c is not None else ""
            return f"Incubate {mins} min{temp}"
        if self.op == OpType.wait:
            return f"Wait {(self.duration_s or 0) // 60} min"
        if self.op == OpType.manual_step:
            return f"MANUAL: {self.note or 'operator action required'}"
        if self.op == OpType.move_labware:
            return f"Move labware {self.labware_id}"
        return self.op.value


class Workflow(BaseModel):
    """The common robot-agnostic workflow. Adapters consume this and nothing above it."""

    workflow_id: str
    source_sop: Optional[str] = None
    labware: list[Labware] = Field(default_factory=list)
    reagents: list[Reagent] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def labware_by_id(self, lid: str) -> Optional[Labware]:
        return next((lw for lw in self.labware if lw.id == lid), None)
