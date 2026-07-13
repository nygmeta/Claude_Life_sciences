"""
Inventory / deck-state access.

This is the *system of record* the agent queries live. Resolution of FACT
(what is loaded, what is in date, what fits) belongs here, never to Claude.
Backed by a JSON file for the MVP; the interface is deliberately small so a real
LIMS / deck-state service can drop in behind it unchanged.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from typing import Any, Optional

from app.config import INVENTORY_PATH


class InventoryStore:
    def __init__(self, path=INVENTORY_PATH):
        with open(path) as f:
            self._data: dict[str, Any] = json.load(f)

    # --- reagents ---
    def reagents(self) -> list[dict]:
        return self._data.get("reagents", [])

    def get_reagent(self, reagent_id: str) -> Optional[dict]:
        return next((r for r in self.reagents() if r["id"] == reagent_id), None)

    def find_reagent_by_name(self, query: str) -> Optional[dict]:
        q = query.lower()
        return next((r for r in self.reagents() if q in r["name"].lower()), None)

    def has_reagent(self, reagent_id: str, min_uL: float = 0.0) -> bool:
        r = self.get_reagent(reagent_id)
        return bool(r) and r.get("volume_uL", 0) >= min_uL

    def is_expired(self, reagent_id: str, on: Optional[date] = None) -> Optional[bool]:
        r = self.get_reagent(reagent_id)
        if not r or "expiry" not in r:
            return None
        on = on or date.today()
        return date.fromisoformat(r["expiry"]) < on

    # --- samples ---
    def samples(self) -> list[dict]:
        return self._data.get("samples", [])

    def samples_collected_on(self, day: str) -> list[dict]:
        return [s for s in self.samples() if s.get("collected") == day]

    # --- labware / deck ---
    def labware_loaded(self) -> list[dict]:
        return self._data.get("labware_loaded", [])

    def get_labware(self, labware_id: str) -> Optional[dict]:
        return next((l for l in self.labware_loaded() if l["id"] == labware_id), None)

    def pipettes(self) -> list[dict]:
        return self._data.get("pipettes", [])

    def get_pipette(self, pipette_id: str) -> Optional[dict]:
        return next((p for p in self.pipettes() if p["id"] == pipette_id), None)

    def context_summary(self) -> dict:
        """Compact live-state snapshot handed to Claude as grounding context."""
        return {
            "reagents_available": [
                {"id": r["id"], "name": r["name"], "volume_uL": r.get("volume_uL")}
                for r in self.reagents()
            ],
            "samples_today": self.samples_collected_on(date.today().isoformat()),
            "labware_loaded": [
                {"id": l["id"], "type": l["type"], "slot": l.get("slot")}
                for l in self.labware_loaded()
            ],
            "pipettes": self.pipettes(),
        }


@lru_cache
def get_store() -> InventoryStore:
    return InventoryStore()
