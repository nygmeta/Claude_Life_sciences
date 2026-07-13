"""Unit tests for speech-service mode (`?mode=speech`): the orchestrator acting as a
microphone and a speaker for a console that owns the conversation.

The contract under test:
  end_turn commits a turn but NEVER replies to it (no LLM, no lab backend), and
  answers with exactly one of
    transcript_final    the console may POST this (text NORMALIZED for its parsers)
    transcript_refused  the console must NOT: we are not sure what we heard, and its
                        backend is armed, so the next affirmative would start a machine
and `speak` / `cancel_speak` make this server the console's speaker.

The load-bearing case is the confirmation floor. A misheard "yes" must never be able
to fire a protocol, and it must FAIL AUDIBLY: the refusal is spoken, because silence
after saying "yes" is the failure mode this mode exists to prevent. Being HEARD and
being OBEYED are different things, which is why a low-confidence confirm survives the
noise gate (it reaches end_turn) and is still refused there.

These drive the real handlers with a fake ws and a mocked TTS (synthesize), plus one
end-to-end pass through handler() itself with a scripted client, so the query-param
switch, the gate exemption and the floor are exercised together. No network.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import lab_backend  # noqa: E402
from web import server  # noqa: E402


class DummyWS:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


class _Req:
    def __init__(self, path):
        self.path = path


class ScriptWS:
    """A WS stub for handler(): carries the connect path (with its query), replays a
    scripted list of client messages, then ends the receive loop."""

    def __init__(self, path, script=()):
        self.request = _Req(path)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []
        self.closed = None
        self._script = list(script)

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._script:
            raise StopAsyncIteration
        return json.dumps(self._script.pop(0))


def _wav():
    return server.pcm16_to_wav(b"\x00\x00" * 200).getvalue()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No network TTS, no LLM, no real disk writes: keep the tests hermetic. The
    confirmation floor lives in lab_backend (that is the module the speech path asks),
    so it is the one that has to be pinned."""
    async def fake_synth(text, model=None, **params):
        return _wav()
    monkeypatch.setattr(server, "synthesize", fake_synth)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "lat.jsonl")
    monkeypatch.setattr(server, "ALLOWLIST", frozenset())
    monkeypatch.setattr(lab_backend, "CONFIRM_FLOOR", 0.40)


@pytest.fixture
def no_llm(monkeypatch):
    """Any reply-pipeline LLM call is a BUG in speech mode: make one fail loudly, and
    hand the test a flag it can assert on."""
    seen = {"called": False}

    async def fake_stream(messages, out=None, tools_ctx=None):
        seen["called"] = True
        yield "this should never be generated"
    monkeypatch.setattr(server, "stream_llm", fake_stream)
    return seen


def _conf(prob_mean, prob_min=None):
    return {"prob_mean": prob_mean,
            "prob_min": prob_min if prob_min is not None else prob_mean, "tokens": 4}


def _sess(text, *, conf=0.95, lab_state=None, speech=True):
    """A speech-mode session with one accumulated segment, as handle_segment would
    have left it. conf is the segment's prob_mean (None = the ASR gave no confidence
    block at all)."""
    sess = server.Session()
    sess.speech_mode = speech
    sess.lab_state = lab_state
    sess.pending = [text]
    sess.asr_ms = [10.0]
    sess.asr_conf = [None if conf is None else _conf(conf)]
    sess.turn_t0 = time.perf_counter()
    return sess


def _run(sess):
    """Commit the turn, then drain any audio it started, so an assertion can see the
    spoken reprompt and not just the message that announced it."""
    ws = DummyWS()

    async def main():
        await server.handle_end_turn_speech(ws, sess)
        if sess.speak_task is not None:
            await sess.speak_task
    asyncio.run(main())
    return ws


def _types(ws):
    return [m.get("type") for m in ws.messages]


def _last(ws, t):
    return next((m for m in reversed(ws.messages) if m.get("type") == t), None)


# ------------------------------------------------------- (a) the accepted turn
def test_accepted_turn_emits_normalized_transcript_final_and_calls_no_llm(no_llm):
    # The normalization is not cosmetic: the console's backend parses slots with
    # regexes written for typed text, and a recognizer writes "IL 6" / "one hundred".
    # Handing it the raw string deadlocks its conversation (measured on the real stack).
    sess = _sess("run an IL 6 assay with one hundred microliters",
                 conf=0.91, lab_state="gathering")
    ws = _run(sess)
    tf = _last(ws, "transcript_final")
    assert tf is not None
    assert tf["text"] == "run an IL-6 assay with 100 microliters"
    assert tf["confidence"] == {"prob_mean": 0.91, "prob_min": 0.91}
    assert "transcript_refused" not in _types(ws)
    # no reply pipeline ran: this server is not part of that conversation.
    assert not no_llm["called"]
    assert not {"reply_start", "reply_delta", "reply_done"} & set(_types(ws))
    assert sess.reply_task is None


def test_accepted_turn_speaks_nothing_on_its_own():
    # Audio in speech mode comes ONLY from an explicit `speak`. An accepted turn is
    # handed over silently: the console decides what (if anything) gets said.
    ws = _run(_sess("start the protocol", conf=0.9, lab_state="gathering"))
    assert "reply_audio" not in _types(ws)


# --------------------------------------------- (b) the confirmation floor refuses
def test_armed_low_confidence_turn_is_refused_and_never_handed_over():
    sess = _sess("yes", conf=0.20, lab_state="awaiting_confirmation")
    ws = _run(sess)
    tr = _last(ws, "transcript_refused")
    assert tr is not None
    assert tr["reason"] == "low_confidence_confirmation"
    assert tr["prob_mean"] == 0.20
    assert tr["reprompt"] == lab_backend.REPROMPT
    # THE invariant: the console never sees a transcript it could POST, so a misheard
    # "yes" cannot reach the backend, let alone start a machine.
    assert "transcript_final" not in _types(ws)


def test_a_refusal_is_spoken_out_loud():
    # Silence after saying "yes" is the failure being avoided: the user has to HEAR
    # why nothing happened. Sentence-split like any other utterance, so the audio
    # starts on sentence 1 rather than after the whole reprompt is synthesized.
    ws = _run(_sess("yes", conf=0.20, lab_state="awaiting_confirmation"))
    spoken = [m["text"] for m in ws.messages if m.get("type") == "reply_audio"]
    assert spoken and " ".join(spoken) == lab_backend.REPROMPT
    assert "reply_audio_end" in _types(ws)


# ------------------------------------------------- (c) armed, but heard clearly
def test_armed_clear_confirm_is_accepted():
    sess = _sess("confirm", conf=0.95, lab_state="awaiting_confirmation")
    ws = _run(sess)
    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == "confirm"
    assert "transcript_refused" not in _types(ws)


def test_armed_confirm_exactly_at_the_floor_is_accepted():
    # at the floor is not below it (mirrors the local gate's boundary).
    ws = _run(_sess("confirm", conf=0.40, lab_state="awaiting_confirmation"))
    assert "transcript_final" in _types(ws)
    assert "transcript_refused" not in _types(ws)


# --------------------------------------------- (d) missing confidence fails OPEN
def test_missing_confidence_fails_open_and_is_accepted():
    # The mock ASR and a degraded-but-working real one both supply no confidence.
    # Locking them out of confirming would break the demo, so the floor only bites
    # when we HAVE a number and it is low.
    sess = _sess("yes", conf=None, lab_state="awaiting_confirmation")
    ws = _run(sess)
    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == "yes"
    assert tf["confidence"] == {"prob_mean": None, "prob_min": None}
    assert "transcript_refused" not in _types(ws)


# ----------------------------------------------- (e) a non-armed state cannot bite
def test_non_armed_state_means_the_floor_does_not_bite():
    # Same weak confidence that is refused while armed: with the console merely
    # gathering, the next utterance executes nothing, so there is nothing to protect.
    ws = _run(_sess("about a hundred microliters", conf=0.05, lab_state="gathering"))
    assert "transcript_final" in _types(ws)
    assert "transcript_refused" not in _types(ws)


def test_unknown_and_absent_states_are_not_armed():
    for state in (None, "executed", "idle", "some_future_state"):
        ws = _run(_sess("yes", conf=0.05, lab_state=state))
        assert "transcript_final" in _types(ws), state
        assert "transcript_refused" not in _types(ws), state


def test_set_lab_state_stores_the_console_state():
    async def main():
        ws = DummyWS()
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_set_lab_state(ws, sess, {"state": "awaiting_confirmation"})
        assert sess.lab_state == "awaiting_confirmation"
        assert lab_backend.armed(sess.lab_state)
        await server.handle_set_lab_state(ws, sess, {"state": "executed"})
        assert sess.lab_state == "executed"
        assert not lab_backend.armed(sess.lab_state)
        await server.handle_set_lab_state(ws, sess, {})       # junk -> not armed
        assert sess.lab_state is None
        return ws
    ws = asyncio.run(main())
    assert _types(ws) == ["lab_state", "lab_state", "lab_state"]


# ------------------------------------------------ heard vs obeyed: the noise gate
def test_low_confidence_confirm_survives_the_noise_gate_while_armed(monkeypatch):
    """The exemption that makes an audible refusal possible at all. Without it a
    mumbled "yes" is dropped as noise HERE, and end_turn never sees it: the user says
    yes, the room goes quiet, and they cannot tell whether the protocol is running."""
    async def fake_transcribe(pcm, language, hints):
        return "yes", _conf(0.20)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)

    async def main():
        armed = _sess("", conf=0.9, lab_state="awaiting_confirmation")
        armed.pending = []
        await server.handle_segment(DummyWS(), armed, b"\x00\x00" * 100)
        # heard: it accumulated into the turn (so the floor can refuse it out loud)
        assert armed.pending == ["yes"]

        # not armed: the same weak "yes" is ordinary speech, and the noise gate drops
        # it exactly as it always has.
        idle = _sess("", conf=0.9, lab_state="gathering")
        idle.pending = []
        ws = DummyWS()
        await server.handle_segment(ws, idle, b"\x00\x00" * 100)
        assert idle.pending == []
        assert _last(ws, "transcript")["discarded"] == "low_confidence"
    asyncio.run(main())


def test_gate_exemption_does_not_leak_to_a_normal_connection():
    # The default path must be untouched: lab_state is meaningless there (it can only
    # be set by a speech-mode client), so it must not be able to open the noise gate.
    sess = _sess("yes", conf=0.20, lab_state="awaiting_confirmation", speech=False)
    assert server._gate_exempt(sess, "yes") is False
    speech = _sess("yes", conf=0.20, lab_state="awaiting_confirmation", speech=True)
    assert server._gate_exempt(speech, "yes") is True


# --------------------------------------------------------------- no LLM, ever
def test_speech_mode_never_speculates(monkeypatch, no_llm):
    """Speculation fires at the SEGMENT boundary, ahead of end_turn: it is the one
    place a speech-mode segment could still reach an LLM."""
    async def fake_transcribe(pcm, language, hints):
        return "prepare a dilution series", _conf(0.9)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)

    async def main():
        speech = _sess("", lab_state="gathering")
        speech.pending = []
        await server.handle_segment(DummyWS(), speech, b"\x00\x00" * 100)
        assert speech.pending == ["prepare a dilution series"]   # heard
        assert speech.reply_task is None                          # but no LLM fired

        # control: the SAME segment on a normal connection does speculate, so the
        # assertion above is testing speech mode and not a broken fixture.
        normal = _sess("", speech=False)
        normal.pending = []
        await server.handle_segment(DummyWS(), normal, b"\x00\x00" * 100)
        assert normal.reply_task is not None
        server._abort_reply_task(normal)
    asyncio.run(main())


# ------------------------------------------------------------- speak / cancel_speak
def test_speak_streams_audio_sentence_by_sentence():
    async def main():
        ws = DummyWS()
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_speak(ws, sess, {"text": "The plate is loaded. Ready to run."})
        await sess.speak_task
        return ws
    ws = asyncio.run(main())
    audio = [m for m in ws.messages if m.get("type") == "reply_audio"]
    assert [m["text"] for m in audio] == ["The plate is loaded.", "Ready to run."]
    assert [m["seq"] for m in audio] == [0, 1]          # ordered, so playback is ordered
    assert _last(ws, "reply_audio_end")["chunks"] == 2
    # speech mode has no reply of its own: these are the console's words, not ours.
    assert not {"reply_start", "reply_delta", "reply_done"} & set(_types(ws))


def test_speak_honors_a_per_call_voice(monkeypatch):
    seen = {}

    async def fake_synth(text, model=None, **params):
        seen["voice"] = params.get("voice")
        return _wav()
    monkeypatch.setattr(server, "synthesize", fake_synth)

    async def main():
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_speak(DummyWS(), sess, {"text": "Done.", "voice": "en_fig"})
        await sess.speak_task
    asyncio.run(main())
    assert seen["voice"] == "en_fig"


def test_cancel_speak_is_safe_when_nothing_is_speaking():
    async def main():
        sess = server.Session()
        sess.speech_mode = True
        assert await server._cancel_speak_task(sess) is False
        assert sess.speak_task is None
    asyncio.run(main())


def test_cancel_speak_stops_audio_in_flight(monkeypatch):
    async def slow_synth(text, model=None, **params):
        await asyncio.sleep(5)
        return _wav()
    monkeypatch.setattr(server, "synthesize", slow_synth)

    async def main():
        ws = DummyWS()
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_speak(ws, sess, {"text": "This is a long announcement."})
        await asyncio.sleep(0)          # let the synth start
        assert await server._cancel_speak_task(sess) is True
        assert sess.speak_task is None
        return ws
    ws = asyncio.run(main())
    assert "reply_audio" not in _types(ws)   # barge-in: the audio never went out


def test_a_new_speak_supersedes_the_one_in_flight(monkeypatch):
    async def slow_synth(text, model=None, **params):
        await asyncio.sleep(5)
        return _wav()
    monkeypatch.setattr(server, "synthesize", slow_synth)

    async def main():
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_speak(DummyWS(), sess, {"text": "First."})
        first = sess.speak_task
        await asyncio.sleep(0)
        await server.handle_speak(DummyWS(), sess, {"text": "Second."})
        assert first.cancelled() or first.done()     # at most one utterance in flight
        assert sess.speak_task is not first
        await server._cancel_speak_task(sess)
    asyncio.run(main())


# ------------------------------------------------------ the connect-URL mode switch
def test_read_client_mode_parses_and_normalizes():
    assert server._read_client_mode(ScriptWS("/?mode=speech")) == "speech"
    assert server._read_client_mode(ScriptWS("/?mode=SPEECH")) == "speech"
    assert server._read_client_mode(ScriptWS("/?email=a%40b.com&mode=speech")) == "speech"
    assert server._read_client_mode(ScriptWS("/")) is None
    assert server._read_client_mode(ScriptWS("/?mode=")) is None
    assert server._read_client_mode(ScriptWS("/?other=x")) is None


def test_end_to_end_armed_low_confidence_confirm_is_refused(monkeypatch):
    """The whole chain, through the real handler(): the ?mode=speech switch, the
    console's set_lab_state arming the floor, the noise-gate exemption that lets a
    quiet "yes" through to end_turn, and the floor refusing to hand it over."""
    async def fake_transcribe(pcm, language, hints):
        return "yes", _conf(0.18)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)

    pcm = base64.b64encode(b"\x00\x00" * 100).decode("ascii")
    ws = ScriptWS("/?mode=speech", [
        {"type": "set_lab_state", "state": "awaiting_confirmation"},
        {"type": "audio_segment", "audio_b64": pcm},
        {"type": "end_turn"},
    ])
    asyncio.run(server.handler(ws))
    types = [m.get("type") for m in ws.sent]
    assert "transcript_refused" in types
    assert "transcript_final" not in types           # the console has nothing to POST
    assert not {"reply_start", "reply_delta", "reply_done"} & set(types)
    refused = next(m for m in ws.sent if m.get("type") == "transcript_refused")
    assert refused["reason"] == "low_confidence_confirmation"


def test_end_to_end_clear_turn_is_handed_over(monkeypatch):
    async def fake_transcribe(pcm, language, hints):
        return "add fifty microliters to well A3", _conf(0.93)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)

    pcm = base64.b64encode(b"\x00\x00" * 100).decode("ascii")
    ws = ScriptWS("/?mode=speech", [
        {"type": "set_lab_state", "state": "gathering"},
        {"type": "audio_segment", "audio_b64": pcm},
        {"type": "end_turn"},
    ])
    asyncio.run(server.handler(ws))
    final = next(m for m in ws.sent if m.get("type") == "transcript_final")
    assert final["text"] == "add 50 microliters to well A3"   # normalized for the console
    assert final["confidence"]["prob_mean"] == 0.93


def test_a_default_connection_ignores_the_speech_messages():
    """Purely additive: on a connection that did not ask for speech mode, the new
    message types stay what they were before this feature existed (unknown, ignored)."""
    ws = ScriptWS("/", [
        {"type": "set_lab_state", "state": "awaiting_confirmation"},
        {"type": "speak", "text": "this must not be spoken"},
        {"type": "cancel_speak"},
    ])
    asyncio.run(server.handler(ws))
    types = [m.get("type") for m in ws.sent]
    assert "lab_state" not in types
    assert "reply_audio" not in types
    # and the connect handshake is byte-for-byte the one every existing client gets
    assert types == ["status", "tts_params", "capture_state", "session_started"]
