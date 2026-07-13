"""
SOP registry.

Resolves a resolved Intent to a concrete SOP template. In production this is a
versioned SOP repository with approvals; for the MVP it loads JSON files from
the sop/ directory. Claude may *suggest* an sop_id, but the registry is the
authority on which SOPs exist and what they require.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

from app.config import SOP_DIR


class SOPRegistry:
    def __init__(self, sop_dir=SOP_DIR):
        self._by_id: dict[str, dict] = {}
        self._by_intent: dict[str, list[dict]] = {}
        for path in sop_dir.glob("*_sop.json"):
            with open(path) as f:
                sop = json.load(f)
            self._by_id[sop["sop_id"]] = sop
            self._by_intent.setdefault(sop["intent"], []).append(sop)

    def get(self, sop_id: str) -> Optional[dict]:
        return self._by_id.get(sop_id)

    def default_for_intent(self, intent: str) -> Optional[dict]:
        sops = self._by_intent.get(intent, [])
        return sops[0] if sops else None

    def required_parameters(self, sop_id: str) -> list[str]:
        sop = self.get(sop_id)
        return sop.get("required_parameters", []) if sop else []

    def all_sops(self) -> list[dict]:
        return list(self._by_id.values())


@lru_cache
def get_registry() -> SOPRegistry:
    return SOPRegistry()
