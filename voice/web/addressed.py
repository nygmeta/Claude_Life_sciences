"""Addressed-speech detection: is THIS utterance aimed at the assistant, or is it
overheard human-to-human side conversation?

An open mic on a lab bench hears colleagues talking to each other, a phone call,
a passing question to a labmate. A voice assistant that answers all of it is
worse than useless, so every transcribed segment is classified before it becomes
part of a turn.

Design rules, in priority order:

  1. DETERMINISTIC FAST PATHS FIRST. A confirmation, a cancellation, an emergency
     stop and a wake form are decided by the same pure matchers the gate uses
     (lab_gate.is_confirm / is_cancel / is_stop), with no model in the loop. These
     are the utterances where a wrong answer is most expensive (dropping "confirm"
     while an irreversible action is armed, or dropping "stop" while the
     centrifuge spins), and they are also the ones most easily matched by a
     regex. A standalone filler ("mm", "uh") with nothing pending is dropped just
     as cheaply.
  2. ONE Haiku call for the genuinely ambiguous rest, forced through a tool with a
     strict schema so the answer is a typed {addressed, confidence, reason} and
     never prose.
  3. FAIL OPEN. Any error, any timeout, any malformed answer returns
     addressed=True. A classifier hiccup must never swallow the user's speech: a
     false "addressed" costs one unnecessary reply, a false "not addressed" costs
     the user their turn and their trust.

This module is import-free of the WebSocket / orchestrator (the Claude call is
INJECTED as `llm_call`), so it is unit testable with a stub and the server keeps
sole ownership of the Anthropic client.

No em dashes anywhere (house rule).
"""
import asyncio
import os
import re

try:                       # imported as the `web.addressed` module (tests)
    from web import lab_gate
except ImportError:        # run directly from web/ (local smoke)
    import lab_gate

# Seconds to wait for the classifier call before failing open. It sits in front of
# the user's turn, so it is deliberately short: a slow classifier is a broken
# classifier as far as the pipeline is concerned. 2.0 is measured, not guessed:
# the call below runs about 1.05 s median / 1.13 s max against Haiku from a laptop
# (scripts/_probe_addr_tune.py), so 2.0 s absorbs network jitter while still
# bounding what the feature can add to a turn. Too TIGHT is the worse failure:
# a timeout fails open, so it pays the latency AND lets the side speech through.
def _timeout_s():
    return float(os.environ.get("LA_ADDRESSED_TIMEOUT_S", "2.0"))


# How many prior turns of context the model sees (a side conversation is often
# only recognizable against what came before).
MAX_CONTEXT_TURNS = 3

# --------------------------------------------------------------------- fast paths
# A wake form: the assistant addressed by name at the START of the utterance,
# with or without a greeting particle. "I think the assistant is broken" does NOT
# match (the name is not in vocative position), which is the whole point.
_WAKE_RE = re.compile(
    r"^\s*(?:hey|hi|hello|ok|okay|yo|excuse me)?[\s,]*(?:lab\s+)?assistant\b", re.I)

# Standalone filler / backchannel: what a person says to ANOTHER person while
# listening ("mm", "uh huh", "right"). Only treated as filler when the whole
# utterance is fillers and nothing is pending; with a pending action, "yeah" is a
# confirmation and is caught by the fast path above this one.
_FILLERS = {"mm", "mmm", "mhm", "hmm", "hm", "uh", "um", "er", "erm", "ah", "oh",
            "huh", "yeah", "yep", "yup", "ok", "okay", "right", "sure", "well",
            "so", "like", "you", "know"}
_WORD_RE = re.compile(r"[a-z']+")


def _is_filler(text):
    """True when the utterance is nothing but backchannel noise (at most 3 words,
    every word a filler). "yeah" alone is filler; "yeah, dispense it" is not."""
    words = _WORD_RE.findall(text.lower())
    if not words or len(words) > 3:
        return False
    return all(w in _FILLERS for w in words)


def _verdict(addressed, confidence, reason):
    return {"addressed": bool(addressed), "confidence": float(confidence),
            "reason": str(reason)}


# ------------------------------------------------------------------- the LLM path
# Forced structured output: tool_choice pins the model to THIS tool, and the schema
# is the only shape it can answer in (typed fields, all required, no extras), so
# the classifier can never answer in prose.
#
# NOT strict: true, deliberately. Strict tool use would have the API validate the
# arguments server-side, but measured on Haiku it costs 250-400 ms per call
# (1307 ms vs 1046 ms median, scripts/_probe_addr_tune.py) for a guarantee _coerce
# below already provides: it type-checks, clamps and defaults every field, and a
# malformed answer fails open rather than propagating. In a call that sits in front
# of the user's turn, that is not a good trade.
TOOL = {
    "name": "report_addressed",
    "description": ("Report whether the candidate utterance was addressed to the "
                    "assistant. You MUST call this tool exactly once and answer "
                    "with nothing else."),
    "input_schema": {
        "type": "object",
        "properties": {
            "addressed": {
                "type": "boolean",
                "description": ("true if the speaker is talking TO the assistant "
                                "(a request, a command, a question for it, an "
                                "answer to its question); false if this is people "
                                "talking to each other and the mic merely "
                                "overheard it"),
            },
            "confidence": {
                "type": "number",
                "description": "how sure you are, from 0.0 to 1.0",
            },
            "reason": {
                "type": "string",
                "description": "why, at most 6 words",
            },
        },
        "required": ["addressed", "confidence", "reason"],
        "additionalProperties": False,
    },
}

TOOL_CHOICE = {"type": "tool", "name": "report_addressed"}

SYSTEM = (
    "You are the gate on a lab voice assistant's open microphone. The mic is "
    "always on at a lab bench, so it hears BOTH speech aimed at the assistant AND "
    "colleagues talking to each other, phone calls, and passing remarks. Your only "
    "job is to decide, for ONE candidate utterance, whether the speaker was "
    "talking to the assistant.\n\n"
    "Answer addressed=true for: commands or requests to the lab equipment "
    "(dispense, set the temperature, start or stop the stirrer, run the "
    "centrifuge, read a sensor), protocol navigation (start the protocol, next "
    "step, repeat that, where am I), questions the assistant is meant to answer, "
    "confirmations or cancellations of something it asked about, and anything "
    "using its name in address.\n"
    "Answer addressed=false for: two humans talking to each other, small talk and "
    "greetings between people, third-person remarks ABOUT the assistant ('the "
    "assistant is broken', 'it misheard me'), and unrelated chatter the mic "
    "happened to catch.\n\n"
    "When you are genuinely unsure: if the utterance contains a lab command, "
    "answer true (dropping a real command is the costly error). Otherwise, if it "
    "reads as human-to-human, answer false. Call the report_addressed tool once."
)


def build_messages(text, recent_turns, has_pending_action):
    """The single user message for the classifier call: prior turns for context,
    whether a confirmation is outstanding, and the candidate utterance last so it
    is unmistakably the thing being judged."""
    lines = []
    turns = [t for t in (recent_turns or []) if (t or {}).get("content")][-MAX_CONTEXT_TURNS:]
    if turns:
        lines.append("Recent conversation (most recent last):")
        for t in turns:
            role = "assistant" if t.get("role") == "assistant" else "person"
            lines.append(f"  {role}: {t['content']}")
    else:
        lines.append("Recent conversation: none, this is the first utterance.")
    lines.append(
        "A lab action is armed and waiting for the user to say confirm or cancel: "
        + ("YES" if has_pending_action else "no"))
    lines.append("")
    lines.append(f"Candidate utterance: {text!r}")
    lines.append("Was this said TO the assistant?")
    return [{"role": "user", "content": "\n".join(lines)}]


def _coerce(raw):
    """Turn the tool's arguments into a verdict, or None when unusable (the caller
    then fails open). Confidence is clamped to [0, 1]; a missing one is treated as
    0.5; the reason is trimmed to a display-safe length."""
    if not isinstance(raw, dict) or "addressed" not in raw:
        return None
    try:
        conf = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = min(1.0, max(0.0, conf))
    reason = str(raw.get("reason") or "").strip()[:120] or "no reason given"
    return _verdict(bool(raw["addressed"]), round(conf, 3), reason)


async def classify_addressed(text, recent_turns, has_pending_action, llm_call):
    """Decide whether `text` was addressed to the assistant.

    Returns {"addressed": bool, "confidence": float, "reason": str}.

    `llm_call` is injected and is awaited as
        await llm_call(system, messages, tools, tool_choice)
    returning the tool's input dict (the server owns the Anthropic client and the
    tool_use extraction; tests pass a stub). It is called AT MOST ONCE, and only
    when no deterministic fast path decides the utterance.

    Fails open: a raising, timing-out, or malformed llm_call yields addressed=True.
    """
    text = (text or "").strip()
    if not text:
        return _verdict(False, 1.0, "empty utterance")

    # 1. A pending action makes confirm/cancel the highest-value words in the room:
    #    never let a classifier drop one.
    if has_pending_action and (lab_gate.is_confirm(text, strict=False) or lab_gate.is_cancel(text)):
        return _verdict(True, 1.0, "confirmation response to a pending action")
    # 2. An emergency stop is never side speech.
    if lab_gate.is_stop(text):
        return _verdict(True, 1.0, "emergency stop utterance")
    # 3. The assistant addressed by name, in vocative position.
    if _WAKE_RE.match(text):
        return _verdict(True, 1.0, "wake form addresses the assistant")
    # 4. Backchannel noise with nothing pending: not a turn, and not worth a call.
    if not has_pending_action and _is_filler(text):
        return _verdict(False, 0.9, "standalone filler")

    if llm_call is None:
        return _verdict(True, 0.5, "no classifier available")
    try:
        raw = await asyncio.wait_for(
            llm_call(SYSTEM, build_messages(text, recent_turns, has_pending_action),
                     [TOOL], TOOL_CHOICE),
            timeout=_timeout_s())
    except asyncio.TimeoutError:
        return _verdict(True, 0.5, "classifier timed out, failing open")
    except Exception as e:  # noqa: BLE001  # never lose speech to a classifier fault
        return _verdict(True, 0.5, f"classifier error {type(e).__name__}, failing open")
    verdict = _coerce(raw)
    if verdict is None:
        return _verdict(True, 0.5, "classifier returned no usable answer")
    return verdict
