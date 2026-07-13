"""
Plan IR — the SEMANTIC layer.

This is what Claude produces from a scientist's spoken intent. It captures
*what the scientist means*, not *what the robot does*. It is deliberately
platform-independent and carries its own uncertainty (assumptions + missing
fields) so the voice layer can read assumptions back and ask clarifying
questions before anything is compiled toward hardware.

Claude proposes a Plan. Deterministic code (compiler + validator) disposes.
Claude never emits robot code.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Unit(str, Enum):
    nL = "nL"
    uL = "uL"
    mL = "mL"


class Volume(BaseModel):
    value: float
    unit: Unit = Unit.uL

    def to_uL(self) -> float:
        factor = {Unit.nL: 1e-3, Unit.uL: 1.0, Unit.mL: 1e3}[self.unit]
        return self.value * factor


class Intent(str, Enum):
    """Supported high-level intents. Extend as SOPs are added."""
    elisa = "elisa"
    serial_dilution = "serial_dilution"
    plate_prep = "plate_prep"
    unknown = "unknown"


class Plan(BaseModel):
    """Claude's structured interpretation of a spoken request."""

    intent: Intent = Field(description="Resolved high-level intent.")
    sop_id: Optional[str] = Field(
        default=None,
        description="SOP this plan maps onto, e.g. 'SOP-ELISA-04'. Resolved by the SOP registry.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Intent-specific parameters (target_analyte, num_samples, etc.).",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Defaults Claude applied. READ THESE BACK to the scientist before executing.",
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Required fields Claude could not resolve. Each triggers a clarification question.",
    )
    requires_confirmation: bool = Field(
        default=True,
        description="Whether a human confirmation gate is required before execution.",
    )
    rationale: Optional[str] = Field(
        default=None,
        description="One-line explanation of the mapping, for the audit trail.",
    )

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields) == 0 and self.intent != Intent.unknown
