"""Unit tests for web/addressed.py: the addressed-speech classifier.

Pure and offline. The Claude call is INJECTED (`llm_call`), so every test either
stubs it or asserts it was never reached: the fast paths must decide the
high-stakes utterances (confirm, cancel, stop, wake form) with no model in the
loop, and every fault must fail OPEN so the user's speech is never swallowed.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import addressed  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Stub:
    """A stub llm_call that records its calls and returns a canned verdict."""

    def __init__(self, result=None, raises=None, delay=0.0):
        self.result = result
        self.raises = raises
        self.delay = delay
        self.calls = []

    async def __call__(self, system, messages, tools, tool_choice):
        self.calls.append({"system": system, "messages": messages, "tools": tools,
                           "tool_choice": tool_choice})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.result


def never_called(*a, **k):
    raise AssertionError("the LLM must not be called on a fast path")


# ---------------------------------------------------------------- fast paths
def test_confirm_under_pending_action_is_addressed_without_llm():
    for text in ("confirm", "yes", "go ahead", "do it"):
        v = run(addressed.classify_addressed(text, [], True, never_called))
        assert v["addressed"] is True, text
        assert v["confidence"] == 1.0


def test_cancel_under_pending_action_is_addressed_without_llm():
    for text in ("cancel", "no", "never mind", "abort"):
        v = run(addressed.classify_addressed(text, [], True, never_called))
        assert v["addressed"] is True, text


def test_stop_is_addressed_without_llm_even_with_nothing_pending():
    for text in ("stop", "halt", "emergency stop", "please stop now"):
        v = run(addressed.classify_addressed(text, [], False, never_called))
        assert v["addressed"] is True, text
        assert v["confidence"] == 1.0


def test_wake_forms_are_addressed_without_llm():
    for text in ("assistant, dispense 50 microliters", "hey assistant",
                 "hello assistant what is the pH", "ok assistant next step",
                 "lab assistant, stop the stirrer"):
        v = run(addressed.classify_addressed(text, [], False, never_called))
        assert v["addressed"] is True, text


def test_third_person_mention_is_not_a_wake_form():
    """'the assistant' mid-sentence is talk ABOUT it, not TO it: no fast path, so
    it must reach the classifier rather than being waved through as a wake form."""
    stub = Stub(result={"addressed": False, "confidence": 0.9, "reason": "third person"})
    v = run(addressed.classify_addressed("I think the assistant is broken", [], False, stub))
    assert len(stub.calls) == 1
    assert v["addressed"] is False


def test_standalone_filler_is_not_addressed_without_llm():
    for text in ("mm", "uh", "yeah", "right", "uh huh", "hmm"):
        v = run(addressed.classify_addressed(text, [], False, never_called))
        assert v["addressed"] is False, text


def test_filler_with_pending_action_is_a_confirmation_not_filler():
    """'yeah' is backchannel noise when nothing is pending, but a confirmation when
    an action is armed. The pending fast path must win."""
    assert run(addressed.classify_addressed("yeah", [], False, never_called))["addressed"] is False
    assert run(addressed.classify_addressed("yeah", [], True, never_called))["addressed"] is True


def test_filler_prefix_of_a_real_command_is_not_filler():
    """'yeah dispense 50 into A3' is a command, not backchannel: it must not be
    dropped by the filler fast path, so it reaches the classifier."""
    stub = Stub(result={"addressed": True, "confidence": 0.95, "reason": "lab command"})
    v = run(addressed.classify_addressed("uh dispense 50 microliters into A3", [], False, stub))
    assert len(stub.calls) == 1
    assert v["addressed"] is True


def test_empty_text_is_not_addressed_and_costs_no_call():
    v = run(addressed.classify_addressed("   ", [], False, never_called))
    assert v["addressed"] is False


# ------------------------------------------------------------------ fail open
def test_fails_open_when_llm_raises():
    stub = Stub(raises=RuntimeError("api down"))
    v = run(addressed.classify_addressed("did you see the game last night", [], False, stub))
    assert v["addressed"] is True          # never lose speech to a classifier fault
    assert "failing open" in v["reason"]


def test_fails_open_when_llm_times_out(monkeypatch):
    monkeypatch.setenv("LA_ADDRESSED_TIMEOUT_S", "0.05")
    stub = Stub(result={"addressed": False, "confidence": 0.9, "reason": "chatter"}, delay=1.0)
    v = run(addressed.classify_addressed("some ambiguous chatter", [], False, stub))
    assert v["addressed"] is True
    assert "timed out" in v["reason"]


def test_fails_open_when_llm_returns_nothing_usable():
    for bad in (None, {}, {"confidence": 0.9}, "not a dict"):
        v = run(addressed.classify_addressed("ambiguous chatter", [], False, Stub(result=bad)))
        assert v["addressed"] is True, bad


def test_fails_open_when_no_llm_call_is_available():
    v = run(addressed.classify_addressed("ambiguous chatter", [], False, None))
    assert v["addressed"] is True


# --------------------------------------------------------------- the LLM path
def test_injected_stub_addressed_true():
    stub = Stub(result={"addressed": True, "confidence": 0.93, "reason": "lab command"})
    v = run(addressed.classify_addressed("set the temperature to 37 degrees", [], False, stub))
    assert v == {"addressed": True, "confidence": 0.93, "reason": "lab command"}
    assert len(stub.calls) == 1


def test_injected_stub_addressed_false():
    stub = Stub(result={"addressed": False, "confidence": 0.88, "reason": "human small talk"})
    v = run(addressed.classify_addressed("did you see the game last night", [], False, stub))
    assert v["addressed"] is False
    assert v["confidence"] == 0.88


def test_confidence_is_clamped_and_reason_trimmed():
    stub = Stub(result={"addressed": True, "confidence": 4.2, "reason": "x" * 400})
    v = run(addressed.classify_addressed("ambiguous chatter", [], False, stub))
    assert v["confidence"] == 1.0
    assert len(v["reason"]) <= 120

    stub = Stub(result={"addressed": False, "confidence": -3, "reason": ""})
    v = run(addressed.classify_addressed("ambiguous chatter", [], False, stub))
    assert v["confidence"] == 0.0
    assert v["reason"] == "no reason given"

    stub = Stub(result={"addressed": True, "confidence": "not a number", "reason": "eh"})
    v = run(addressed.classify_addressed("ambiguous chatter", [], False, stub))
    assert v["confidence"] == 0.5      # unparseable confidence falls back, verdict stands


def test_call_carries_forced_tool_context_and_recent_turns():
    stub = Stub(result={"addressed": True, "confidence": 0.9, "reason": "answers assistant"})
    turns = [{"role": "user", "content": "start the protocol"},
             {"role": "assistant", "content": "Step 1 of 6. Resuspend the pellet."},
             {"role": "user", "content": "ok"},
             {"role": "assistant", "content": "Ready when you are."}]
    run(addressed.classify_addressed("next step", turns, False, stub))
    call = stub.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "report_addressed"}
    assert call["tools"][0]["name"] == "report_addressed"
    schema = call["tools"][0]["input_schema"]
    assert schema["required"] == ["addressed", "confidence", "reason"]
    assert schema["additionalProperties"] is False
    body = call["messages"][0]["content"]
    assert "next step" in body                       # the candidate utterance
    assert "Resuspend the pellet." in body           # prior turns as context
    assert "start the protocol" not in body          # only the last MAX_CONTEXT_TURNS
    assert "waiting for the user to say confirm or cancel: no" in body


def test_call_announces_a_pending_action_to_the_model():
    stub = Stub(result={"addressed": True, "confidence": 0.9, "reason": "answer"})
    run(addressed.classify_addressed("that sounds about right", [], True, stub))
    assert "confirm or cancel: YES" in stub.calls[0]["messages"][0]["content"]
