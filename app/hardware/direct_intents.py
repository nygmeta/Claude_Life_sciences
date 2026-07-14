"""Direct-to-robot intents.

Match a small set of utterances (initialize, lights, health) BEFORE the SOP planner,
and hand them straight to the OT-2 HTTP API. The SOP path stays untouched for
"run an ELISA" style requests.

Anything ambiguous falls through to the planner, which then either matches a real
SOP or asks for clarification. False negatives here are fine; false positives are
not, because a false positive on "home the robot" moves the arm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from . import ot2_client


@dataclass
class DirectResult:
    reply: str
    action: str
    detail: dict


# Which direct intents move the arm. Motion intents must not fire on a
# speculative / partial transcript — they enter a one-turn confirmation gate.
MOTION_INTENTS = frozenset({"home", "rerun"})


def is_motion(intent_name: str) -> bool:
    return intent_name in MOTION_INTENTS


# Word-anchored patterns. Kept narrow so a phrase like "run the initialization
# on tomorrow's samples" does NOT trigger the home command — that has content
# beyond the direct intent and belongs in the SOP planner.
_HOME_RE = re.compile(
    r"\b(initialize|initialise|home|reset)\b.*\b(robot|opentron|opentrons|ot[- ]?2|arm)\b",
    re.I,
)
_HOME_ALT_RE = re.compile(
    r"\b(robot|opentron|opentrons|ot[- ]?2|arm)\b.*\b(home|initialize|initialise|reset)\b",
    re.I,
)

_LIGHTS_ON_RE = re.compile(
    r"\b(lights?|deck lights?)\b.*\b(on)\b|\bturn on\b.*\blights?\b",
    re.I,
)
_LIGHTS_OFF_RE = re.compile(
    r"\b(lights?|deck lights?)\b.*\b(off)\b|\bturn off\b.*\blights?\b",
    re.I,
)

_HEALTH_RE = re.compile(
    r"\b(robot|opentron|opentrons|ot[- ]?2)\b.*\b(status|healthy?|ok|okay|connected|reachable)\b",
    re.I,
)
_HEALTH_ALT_RE = re.compile(
    r"\b(status of|check on|check|is)\b.*\b(robot|opentron|opentrons|ot[- ]?2)\b",
    re.I,
)
# "connect to the OT-2", "hello robot", "talk to the Opentrons" — all safe,
# read-only. We treat them as a health/hello ping so a scientist can verify the
# link before asking for motion. Deliberately fuzzy: an ASR mishearing "connect
# to the OT-2" as "connect to the opener trunk" still forwards a corrected
# transcript containing "opentrons" after the speech-side verification turn.
_CONNECT_RE = re.compile(
    r"\b(connect|talk|hello|hi|hey)\b.*\b(robot|opentron|opentrons|ot[- ]?2)\b",
    re.I,
)

# "Rerun the transfer", "run the transfer again", "do it again", "run the protocol
# again". Deliberately strict: plain "run the protocol" is NOT a rerun (too easy
# to fire from an ASR partial that repeats). Fires only when there's a runnable
# protocol already on the robot. This is a demo convenience, not a real
# re-execute: HARDWARE_EXECUTION.md still governs the adapter-native path.
_RERUN_RE = re.compile(
    r"\bre[- ]?run(s|ning|ned)?\b|"
    r"\brun\b.*\b(again|once more)\b|"
    r"\bdo\b.*\b(again|once more)\b|"
    r"\brepeat\b.*\b(transfer|protocol|run|it)\b",
    re.I,
)


def _fmt_pipette(p: str | None) -> str:
    if not p:
        return "none"
    # "p1000_single_gen2" -> "p1000 single"
    parts = p.replace("_gen2", "").split("_")
    return " ".join(parts)


def _do_home() -> DirectResult:
    ot2_client.home_robot()
    return DirectResult(
        reply="Homed the Opentrons OT-2. All axes returned to home position.",
        action="home_robot",
        detail={},
    )


def _do_lights_on() -> DirectResult:
    ot2_client.set_lights(True)
    return DirectResult(reply="Deck lights on.", action="lights_on", detail={})


def _do_lights_off() -> DirectResult:
    ot2_client.set_lights(False)
    return DirectResult(reply="Deck lights off.", action="lights_off", detail={})


def _do_rerun() -> DirectResult:
    p = ot2_client.latest_protocol()
    if p is None:
        return DirectResult(
            reply="I don't see any protocol on the robot to rerun. Upload one first.",
            action="rerun_none",
            detail={},
        )
    name = (p.get("metadata") or {}).get("protocolName") or p.get("id")
    run = ot2_client.run_and_wait(p["id"])
    status = run.get("status", "unknown")
    errs = run.get("errors") or []
    if status == "succeeded" and not errs:
        reply = f"Rerun of {name} completed. Zero errors."
    else:
        first = (errs[0].get("detail") if errs else "no error detail")
        reply = f"Rerun of {name} finished as {status}. {first}"
    return DirectResult(
        reply=reply,
        action="rerun_last",
        detail={"protocol_id": p["id"], "run_id": run.get("id"), "status": status},
    )


def _do_health() -> DirectResult:
    s = ot2_client.health_summary()
    reply = (
        f"Robot is reachable at {s['wifi_ip']} on wifi. "
        f"Left mount: {_fmt_pipette(s['left_pipette'])}. "
        f"Right mount: {_fmt_pipette(s['right_pipette'])}. "
        f"API {s['api_version']}."
    )
    return DirectResult(reply=reply, action="health", detail=s)


# Order matters: motion patterns are checked ahead of the read-only ones so
# a health match can't swallow a "home the robot" utterance. Ambiguity favors
# the more specific direct intent.
_MATCHERS: list[tuple[re.Pattern, str]] = [
    (_HOME_RE, "home"),
    (_HOME_ALT_RE, "home"),
    (_RERUN_RE, "rerun"),
    (_LIGHTS_ON_RE, "lights_on"),
    (_LIGHTS_OFF_RE, "lights_off"),
    (_HEALTH_RE, "health"),
    (_HEALTH_ALT_RE, "health"),
    (_CONNECT_RE, "health"),
]

EXECUTORS: dict[str, Callable[[], DirectResult]] = {
    "home": _do_home,
    "rerun": _do_rerun,
    "lights_on": _do_lights_on,
    "lights_off": _do_lights_off,
    "health": _do_health,
}

# Spoken read-back for the confirmation gate. Kept short so TTS is snappy;
# includes the specific arm-visible action so the scientist can catch a
# misheard intent before it moves.
CONFIRM_PROMPTS: dict[str, str] = {
    "home": "About to home the OT-2 — the gantry will return to its home position. "
            "Say 'yes' to continue, or 'cancel'.",
    "rerun": "About to rerun the last protocol on the OT-2. "
             "Say 'yes' to continue, or 'cancel'.",
}


def match(transcript: str) -> str | None:
    """Return the intent name for a direct-robot utterance, or None to fall through."""
    for pat, name in _MATCHERS:
        if pat.search(transcript):
            return name
    return None


def execute(intent_name: str) -> DirectResult:
    return EXECUTORS[intent_name]()
