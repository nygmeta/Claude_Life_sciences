"""The confirmation gate must fire BEFORE the assistant speaks.

The demo makes a hard claim on camera: nothing happens until the scientist says yes.
That claim is undermined if the assistant can be heard narrating an action while the
gate has not yet decided whether the action is even allowed. A viewer cannot tell the
difference between "it is talking about doing it" and "it is doing it".

Two separate properties, tested separately:

  1. EXECUTION ordering, which is structural: a command whose severity requires
     confirmation arms a pending action and returns CONFIRMATION REQUIRED to the
     model. It never reaches the execute path. (test_confirm_gate.py owns the
     details; the check here is the ordering invariant.)

  2. AUDIO ordering, which used to be only a prompt-level guarantee: the tool-use
     loop streamed text out of EVERY pass, and the gate only ran after a pass ended,
     so any pre-tool narration would have been synthesized before the gate returned.
     LA_STRICT_GATE_AUDIO buffers a pass's text and drops it if that pass turns out
     to be a tool call, which makes the ordering structural. This test pins it.

The test drives stream_llm directly against a fake Anthropic stream, so it needs no
API key, no network, and no GPU: it can assert the exact ordering of "text was
yielded" against "the gate ran", which is the thing that actually matters.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Final:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = _Block("usage", input_tokens=0, output_tokens=0)


class _FakeStream:
    """One pass of the model: some text, then maybe a tool call."""

    def __init__(self, text, stop_reason, content):
        self._text = text
        self._final = _Final(stop_reason, content)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for ch in self._text:
                yield ch
        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, passes):
        self._passes = list(passes)

    def stream(self, **kw):
        return self._passes.pop(0)


class _FakeClient:
    def __init__(self, passes):
        self.messages = _FakeMessages(passes)


class _ToolsCtx:
    """Records WHEN the gate ran, relative to the text the model produced."""

    def __init__(self, events):
        self.events = events
        self.tools = [{"name": "lab_command"}]
        self.system = "sys"
        self.action = None

    async def handle(self, name, tool_input):
        self.events.append("GATE")      # the gate runs here
        return "CONFIRMATION REQUIRED. Do not claim any action happened."


async def _run(passes, events, strict):
    ctx = _ToolsCtx(events)
    old_client, old_strict = server.llm_client, server.STRICT_GATE_AUDIO
    server.llm_client = _FakeClient(passes)
    server.STRICT_GATE_AUDIO = strict
    try:
        async for chunk in server.stream_llm([{"role": "user", "content": "x"}],
                                             tools_ctx=ctx):
            events.append(f"TEXT:{chunk}")
    finally:
        server.llm_client, server.STRICT_GATE_AUDIO = old_client, old_strict


def _passes_with_preamble():
    """The dangerous shape: the model narrates BEFORE calling the tool."""
    return [
        _FakeStream("Okay, starting that now. ", "tool_use",
                    [_Block("tool_use", id="t1", name="lab_command", input={})]),
        _FakeStream("Say confirm centrifuge to proceed, or cancel.", "end_turn", []),
    ]


def test_pre_gate_narration_is_never_spoken():
    """STRICT (the shipped default): no text may be emitted before the gate runs.

    Any text emitted before GATE would be handed to the sentence splitter and
    synthesized, i.e. the assistant would be heard describing an action the gate had
    not yet allowed.
    """
    events = []
    asyncio.run(_run(_passes_with_preamble(), events, strict=True))

    gate_at = events.index("GATE")
    text_before = [e for e in events[:gate_at] if e.startswith("TEXT:")]
    assert not text_before, f"text was emitted before the gate ran: {text_before}"

    # and the post-gate readback IS still spoken: the guarantee must not be bought
    # by muting the assistant.
    spoken = "".join(e[5:] for e in events if e.startswith("TEXT:"))
    assert "confirm centrifuge" in spoken
    assert "starting that now" not in spoken.lower()


def test_post_gate_reply_still_streams():
    """The buffer must not swallow an ordinary reply that calls no tool at all."""
    events = []
    passes = [_FakeStream("The incubator is at 37 degrees.", "end_turn", [])]
    asyncio.run(_run(passes, events, strict=True))
    spoken = "".join(e[5:] for e in events if e.startswith("TEXT:"))
    assert spoken == "The incubator is at 37 degrees."


def test_without_strict_mode_the_hazard_is_real():
    """Documents WHY the flag exists: with it off, pre-tool narration reaches TTS.

    This is not a wish, it is the old behavior, and it is why the guarantee needed to
    become structural rather than resting on the model choosing not to narrate.
    """
    events = []
    asyncio.run(_run(_passes_with_preamble(), events, strict=False))
    gate_at = events.index("GATE")
    text_before = [e for e in events[:gate_at] if e.startswith("TEXT:")]
    assert text_before, "expected the unguarded loop to leak pre-gate text"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
