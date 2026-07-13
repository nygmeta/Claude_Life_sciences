"""
Echo adapter (stub): Workflow -> acoustic-dispense picklist CSV.

Bonus adapter that proves the architecture. An Echo picklist is just rows of
source well / dest well / transfer volume (nL) — declarative, tipless,
nanoliter-scale. The SAME Workflow IR that becomes imperative Opentrons Python
becomes this CSV. That contrast is the strongest "universal" evidence you can show.

It also *correctly rejects* the ELISA workflow at the capability layer — 100 uL
transfers are far above Echo's droplet range — which is exactly the kind of
honest platform mismatch the validator is supposed to catch.
"""
from __future__ import annotations

import csv
import io

from app.adapters.base import Adapter, Capabilities
from app.models.workflow import OpType, Workflow


class EchoAdapter(Adapter):
    capabilities = Capabilities(
        name="Labcyte Echo 525",
        supported_ops={OpType.transfer},         # acoustic ejection only
        min_volume_uL=0.0025,                     # 2.5 nL droplet
        max_volume_uL=5.0,                        # practical per-well ceiling
        needs_tips=False,
        labware_types={"corning_96_wellplate_360ul"},
    )

    DROPLET_NL = 2.5  # acoustic transfers must be whole multiples of the droplet

    def check_capabilities(self, wf: Workflow):
        issues = super().check_capabilities(wf)
        offenders: set[float] = set()
        for op in wf.operations:
            if op.op == OpType.transfer and op.volume:
                nl = op.volume.to_uL() * 1000
                if abs(round(nl / self.DROPLET_NL) * self.DROPLET_NL - nl) > 1e-6:
                    offenders.add(round(nl, 4))
        from app.models.session import ValidationIssue
        for nl in sorted(offenders):
            issues.append(ValidationIssue(
                severity="error", rule="echo_droplet_resolution",
                message=(f"{nl:g} nL is not a multiple of the {self.DROPLET_NL} nL droplet. "
                         f"Adjust volumes or point count."),
            ))
        return issues

    def compile(self, wf: Workflow) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Source Plate", "Source Well", "Destination Plate",
                         "Destination Well", "Transfer Volume (nL)"])
        for op in wf.operations:
            if op.op == OpType.transfer and op.source and op.dest and op.volume:
                nl = op.volume.to_uL() * 1000  # uL -> nL, on the droplet grid
                writer.writerow([
                    op.source.labware, op.source.well,
                    op.dest.labware, op.dest.well,
                    f"{nl:g}",
                ])
        return buf.getvalue()

    def simulate(self, wf: Workflow) -> tuple[bool, str]:
        rows = self.compile(wf).strip().splitlines()
        return True, f"[echo picklist] {len(rows) - 1} droplet transfers generated."
