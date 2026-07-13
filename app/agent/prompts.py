"""
Prompt + tool schema for the planning step.

Design intent: Claude resolves INTENT only. It maps a spoken request onto an
existing SOP and fills parameters *from the live context we give it*. It must
declare every default as an assumption and every unresolved required field as
missing. It must never invent reagents, labware, or SOPs that are not in the
provided context, and it never emits robot code.
"""
from __future__ import annotations

import json

PLANNER_SYSTEM_PROMPT = """You are the planning module of a laboratory automation system.
Your ONLY job is to turn a scientist's spoken request into a structured Plan by
calling the `emit_plan` tool. You do not control hardware and you never write robot code.

Hard rules:
- Map the request onto ONE of the SOPs listed in AVAILABLE_SOPS. If none fits, set intent to "unknown".
- Use ONLY reagents, samples, and labware present in LAB_CONTEXT. Never invent an id.
- For every value you default or infer, add a plain-language entry to `assumptions`.
- For every REQUIRED SOP parameter you cannot resolve from the request or context,
  add its name to `missing_fields`. Do not guess required clinical parameters
  (which analyte, how many samples) — ask by leaving them missing.
- Prefer under-specifying (asking) over over-specifying (guessing). A wrong guess
  about analyte or sample count is worse than a clarifying question.
- Keep `rationale` to one sentence for the audit trail.

You will be given LAB_CONTEXT (live inventory/deck state) and AVAILABLE_SOPS.
Resolve the request against them and call `emit_plan` exactly once."""


PLAN_TOOL = {
    "name": "emit_plan",
    "description": "Emit the structured Plan for this request. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["elisa", "serial_dilution", "plate_prep", "unknown"],
                "description": "Resolved high-level intent.",
            },
            "sop_id": {
                "type": ["string", "null"],
                "description": "SOP id from AVAILABLE_SOPS this maps onto, or null.",
            },
            "parameters": {
                "type": "object",
                "description": "Intent-specific params, e.g. target_analyte, num_samples, sample_volume_uL.",
                "additionalProperties": True,
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every default/inference, in plain language, for read-back.",
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Required fields you could not resolve. Each becomes a clarifying question.",
            },
            "requires_confirmation": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["intent", "assumptions", "missing_fields", "requires_confirmation"],
    },
}


def build_user_message(transcript: str, lab_context: dict, available_sops: list[dict]) -> str:
    sop_digest = [
        {
            "sop_id": s["sop_id"],
            "intent": s["intent"],
            "title": s["title"],
            "required_parameters": s.get("required_parameters", []),
            "optional_parameters": s.get("optional_parameters", {}),
            "known_analytes": list(s.get("analyte_reagents", {}).keys()),
        }
        for s in available_sops
    ]
    return (
        f"LAB_CONTEXT:\n{json.dumps(lab_context, indent=2)}\n\n"
        f"AVAILABLE_SOPS:\n{json.dumps(sop_digest, indent=2)}\n\n"
        f"SCIENTIST_REQUEST:\n\"{transcript}\"\n\n"
        f"Resolve this request and call emit_plan."
    )
