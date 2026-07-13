"""
Clarification loop.

Turns a Plan's `missing_fields` into natural questions the TTS layer speaks, and
parses free-text answers back into typed parameters. Kept deliberately simple and
rule-based for the MVP; a production version would let Claude phrase questions and
extract slots, but the *set* of required fields stays authority-driven by the SOP.
"""
from __future__ import annotations

import re
from typing import Any

from app.models.plan import Plan

QUESTION_TEMPLATES = {
    "target_analyte": "Which analyte should I run the ELISA for? (e.g. IL-6)",
    "num_samples": "How many samples are we running?",
    "sample_volume_uL": "What sample volume per well, in microliters?",
    "compound": "Which compound should I dilute?",
    "num_points": "How many dilution points?",
    "dilution_factor": "What dilution factor?",
}

_ANALYTE_RE = re.compile(r"\b(IL-?6|IL-?8|TNF-?alpha|TNF|CRP)\b", re.IGNORECASE)
_VOLUME_RE = re.compile(r"(\d{1,4})\s*(?:microliters?|microlitres?|\u00b5l|ul)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d{1,3})\s*[-\s]?(samples?|points?|wells?)\b", re.IGNORECASE)


def questions_for(plan: Plan) -> list[str]:
    return [QUESTION_TEMPLATES.get(f, f"Please specify: {f}") for f in plan.missing_fields]


def _norm_analyte(raw: str) -> str:
    s = raw.upper().replace(" ", "")
    if s in ("IL6", "IL-6"):
        return "IL-6"
    if s in ("IL8", "IL-8"):
        return "IL-8"
    return s


def parse_answers(plan: Plan, transcript: str) -> dict[str, Any]:
    """Best-effort slot extraction. Handles the fields currently missing plus
    common explicit overrides (e.g. 'make it 100 microliters per well')."""
    answers: dict[str, Any] = {}
    text = transcript.strip()

    # Analyte
    if "target_analyte" in plan.missing_fields:
        m = _ANALYTE_RE.search(text)
        if m:
            answers["target_analyte"] = _norm_analyte(m.group(1))

    # Counts: prefer a number explicitly attached to 'samples'/'points'/'wells'
    # so we never mistake the '6' in 'IL-6' for a sample count.
    counts = {kind.rstrip("s").lower(): int(n) for n, kind in _COUNT_RE.findall(text)}
    if "num_samples" in plan.missing_fields:
        n = counts.get("sample") or counts.get("well")
        if n is not None:
            answers["num_samples"] = n
    if "num_points" in plan.missing_fields:
        if counts.get("point") is not None:
            answers["num_points"] = counts["point"]

    # Volume override (per well). Applies whether or not it was 'missing'.
    vm = _VOLUME_RE.search(text)
    if vm and ("per well" in text.lower() or "sample" in text.lower()
               or "sample_volume_uL" in plan.missing_fields):
        answers["sample_volume_uL"] = int(vm.group(1))

    # Dilution factor
    if "dilution_factor" in plan.missing_fields:
        dm = re.search(r"(\d{1,3})\s*[- ]?fold", text, re.IGNORECASE)
        if dm:
            answers["dilution_factor"] = int(dm.group(1))

    # Compound
    if "compound" in plan.missing_fields:
        cm = re.search(r"\bcompound[_ ]?(\w+)\b", text, re.IGNORECASE)
        if cm:
            answers["compound"] = f"compound_{cm.group(1)}"

    return answers
