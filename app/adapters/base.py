"""
Adapter capability contract.

The robot-agnostic trick isn't the IR alone — it's that each adapter DECLARES its
capabilities, and the validator reconciles the workflow against that declaration.
A `transfer` means nanoliters-no-tips on an Echo and microliters-with-tips on a
Hamilton; some ops are simply unexecutable on some platforms. Declaring
capabilities lets the validator reject an infeasible workflow *before* compilation,
with a real reason.

To add a platform (Hamilton, Echo, MANTIS), subclass Adapter, declare its
Capabilities, and implement compile() + simulate(). Nothing above this layer changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.models.session import ValidationIssue
from app.models.workflow import OpType, Workflow


@dataclass
class Capabilities:
    name: str
    supported_ops: set[OpType]
    min_volume_uL: float
    max_volume_uL: float
    needs_tips: bool
    labware_types: set[str] = field(default_factory=set)


class Adapter(ABC):
    capabilities: Capabilities

    def check_capabilities(self, wf: Workflow) -> list[ValidationIssue]:
        """Reconcile a workflow against this platform's declared capabilities.

        Aggregated: one issue per distinct unsupported op / over-max volume,
        rather than one per operation.
        """
        cap = self.capabilities
        unsupported: set[str] = set()
        over_max: set[float] = set()
        under_min: set[float] = set()
        for op in wf.operations:
            if op.op not in cap.supported_ops:
                unsupported.add(op.op.value)
            if op.volume:
                v = op.volume.to_uL()
                if v > cap.max_volume_uL:
                    over_max.add(v)
                if v < cap.min_volume_uL:
                    under_min.add(v)

        issues: list[ValidationIssue] = []
        for op_name in sorted(unsupported):
            issues.append(ValidationIssue(
                severity="error", rule="unsupported_op",
                message=(f"{cap.name} cannot perform '{op_name}'. "
                         f"Route this step to another platform or a manual step."),
                fixable_by_user=False,
            ))
        for v in sorted(over_max):
            issues.append(ValidationIssue(
                severity="error", rule="volume_over_platform_max",
                message=f"{v:.3g} uL exceeds {cap.name}'s max transfer of {cap.max_volume_uL} uL.",
            ))
        for v in sorted(under_min):
            issues.append(ValidationIssue(
                severity="error", rule="volume_under_platform_min",
                message=(f"{v * 1000:.1f} nL is below {cap.name}'s minimum transfer of "
                         f"{cap.min_volume_uL * 1000:.1f} nL. This job needs a lower-volume platform."),
                fixable_by_user=False,
            ))
        return issues

    @abstractmethod
    def compile(self, wf: Workflow) -> str:
        """Return platform-specific artifact (script, picklist, etc.) as text."""

    @abstractmethod
    def simulate(self, wf: Workflow) -> tuple[bool, str]:
        """Dry-run the workflow. Returns (ok, log)."""
