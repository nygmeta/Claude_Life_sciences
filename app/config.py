"""Configuration. All knobs in one place."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Claude ---
# Default to Sonnet 5: near-Opus reasoning at lower cost, good for real-time
# planning. Swap to claude-opus-4-8 for harder multi-step reasoning.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # if unset, planner uses mock mode
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2000"))

# --- Data sources (mocked for the MVP; interface stays swappable) ---
INVENTORY_PATH = BASE_DIR / "inventory" / "inventory.json"
SOP_DIR = BASE_DIR / "sop"

# --- Target platform for this MVP ---
DEFAULT_ADAPTER = os.environ.get("DEFAULT_ADAPTER", "opentrons")
