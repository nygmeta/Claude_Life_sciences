"""Unit tests for intent verification (LA_VERIFY_INTENT, speech mode).

The recognizer does not only fail quietly, it fails CONFIDENTLY: "Run an ELISA on
today's plasma samples" comes back as "Ron Eliza.", "IL-6" as "i am six", "per well"
as "Her will", each of them a high-confidence transcription. No confidence floor can
catch that, because the model was sure. So the assistant proposes the reading it
believes was intended, SAYS it, and hands the console nothing until the human agrees.

The contract under test:
  transcript_verify {raw, proposed}   we think we misheard; NO transcript_final is
                                      sent, so the console has nothing to POST
  transcript_final {text, verified}   the user confirmed: the PROPOSED text finally
                                      reaches the backend

And the boundary the whole design rests on, which is what most of this file is about:
a control utterance (confirm / cancel / stop) and an armed backend are NEVER verified.
A model that can "helpfully" repair a garbled command could repair a garbled CANCEL,
and that would put a language model between a scientist saying stop and a centrifuge.

The LLM is stubbed throughout. No network.
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
    """A WS stub for handler(): the connect path (with its query), a scripted list of
    client messages, then the receive loop ends."""

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
    """No network TTS, no real LLM, no disk writes. VERIFY_INTENT is pinned ON: the
    env default is on, but a test must not depend on the shell it runs in."""
    async def fake_synth(text, model=None, **params):
        return _wav()
    monkeypatch.setattr(server, "synthesize", fake_synth)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "lat.jsonl")
    monkeypatch.setattr(server, "ALLOWLIST", frozenset())
    monkeypatch.setattr(server, "VERIFY_INTENT", True)
    monkeypatch.setattr(lab_backend, "CONFIRM_FLOOR", 0.40)


@pytest.fixture
def verifier(monkeypatch):
    """Stub the ONE Anthropic call the verifier makes, at the same seam the real one
    uses (_verify_llm_call), so the prompt building, the JSON parsing, the materiality
    test and the fail-open wrapper are all still under test.

    `verifier.proposes(x)` scripts the model's answer; `verifier.fails(exc)` makes the
    call raise. `verifier.calls` records the (raw, questions, reply) it was asked
    about, which is how the exclusion tests prove the model was never consulted.
    """
    class Stub:
        def __init__(self):
            self.calls = []
            self._answer = None
            self._exc = None

        def proposes(self, text):
            self._answer = json.dumps({"text": text})

        def answers_raw(self, blob):      # a malformed / unfenced answer
            self._answer = blob

        def fails(self, exc):
            self._exc = exc

        async def __call__(self, raw, questions, reply):
            self.calls.append((raw, questions, reply))
            if self._exc is not None:
                raise self._exc
            return self._answer if self._answer is not None else json.dumps({"text": raw})

    stub = Stub()
    monkeypatch.setattr(server, "_verify_llm_call", stub)
    # llm_client is only ever checked for None (the real client is never touched:
    # _verify_llm_call above is the seam), but None means "no key" and skips the layer.
    if server.llm_client is None:
        monkeypatch.setattr(server, "llm_client", object())
    return stub


def _conf(prob_mean, prob_min=None):
    return {"prob_mean": prob_mean,
            "prob_min": prob_min if prob_min is not None else prob_mean, "tokens": 4}


def _sess(text, *, conf=0.95, lab_state="gathering", speech=True, questions=None,
          reply=None, pending_verify=None):
    """A speech-mode session with one accumulated segment, as handle_segment would have
    left it. conf is deliberately HIGH by default: the whole point of this layer is the
    confident mishearing, so these turns sail through every confidence floor."""
    sess = server.Session()
    sess.speech_mode = speech
    sess.lab_state = lab_state
    sess.lab_questions = questions
    sess.lab_reply = reply
    sess.pending_verify = pending_verify
    sess.pending = [text]
    sess.asr_ms = [10.0]
    sess.asr_conf = [None if conf is None else _conf(conf)]
    sess.turn_t0 = time.perf_counter()
    return sess


def _run(sess):
    """Commit the turn, then drain any audio it started, so an assertion can see the
    spoken question and not just the message that announced it."""
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


def _spoken(ws):
    """The line that was actually synthesized, reassembled from the audio chunks."""
    return " ".join(m["text"] for m in ws.messages if m.get("type") == "reply_audio")


# ------------------------------------------- (a) a material proposal asks, and only asks
def test_a_confident_mishearing_is_questioned_and_never_handed_over(verifier):
    # The real one, from a live session: the recognizer was SURE it heard "Ron Eliza."
    verifier.proposes("run an ELISA on today's plasma samples")
    sess = _sess("ron eliza", conf=0.93)
    ws = _run(sess)

    tv = _last(ws, "transcript_verify")
    assert tv is not None
    assert tv["raw"] == "ron eliza"
    assert tv["proposed"] == "run an ELISA on today's plasma samples"
    # THE invariant: the console is given nothing it could POST. A confident mishearing
    # reaching the backend is exactly what this layer exists to stop.
    assert "transcript_final" not in _types(ws)
    assert sess.pending_verify == {"raw": "ron eliza",
                                   "proposed": "run an ELISA on today's plasma samples"}


def test_the_proposal_is_spoken_as_a_question(verifier):
    # Spoken, because the user is not necessarily looking at the screen. And it asks
    # rather than reading the mishearing back: the raw transcript is already displayed,
    # so "I heard Ron Eliza" would spend the turn on something they can see.
    verifier.proposes("run an ELISA")
    ws = _run(_sess("ron eliza", conf=0.93))
    assert _spoken(ws) == "Did you mean: run an ELISA?"
    assert "reply_audio_end" in _types(ws)


def test_a_mishearing_the_normalizer_already_fixes_costs_no_verification_turn(verifier):
    """Materiality is judged against the text we would OTHERWISE HAND OVER, not against
    the raw transcript. normalize_transcript already turns "i am six" into "IL-6" for
    free, so the model agreeing with it is not news: stopping to ask would cost the user
    a confirmation round trip on an utterance that already worked."""
    verifier.proposes("IL-6")
    sess = _sess("i am six", conf=0.93,
                 questions=["Which analyte should I run the ELISA for? (e.g. IL-6)"])
    ws = _run(sess)

    assert "transcript_verify" not in _types(ws)
    assert _last(ws, "transcript_final")["text"] == "IL-6"
    assert sess.pending_verify is None


def test_the_assistants_last_question_is_the_context_the_model_gets(verifier):
    # Knowing the backend just asked "which analyte?" is what makes "i am six" -> "IL-6"
    # inferable at all. It reaches the model, or the layer is guessing blind.
    verifier.proposes("IL-6")
    q = ["Which analyte should I run the ELISA for? (e.g. IL-6)"]
    _run(_sess("i am six", questions=q, reply="Sure. Which analyte?"))
    assert verifier.calls == [("i am six", q, "Sure. Which analyte?")]
    prompt = server.build_verify_messages("i am six", q, "Sure. Which analyte?")[0]["content"]
    assert "Which analyte should I run the ELISA for?" in prompt
    assert "i am six" in prompt


# ------------------------------------------------- (b) confirming sends the PROPOSED text
def test_confirming_the_proposal_hands_over_the_proposed_text(verifier):
    sess = _sess("yes", conf=0.95,
                 pending_verify={"raw": "i am six", "proposed": "IL-6"})
    ws = _run(sess)

    tf = _last(ws, "transcript_final")
    assert tf is not None
    assert tf["text"] == "IL-6"          # the corrected utterance, not the raw one
    assert tf["verified"] is True
    assert sess.pending_verify is None   # answered: never reused
    # the confirm itself is never forwarded: "yes" is not what the scientist wanted run.
    assert "transcript_verify" not in _types(ws)


def test_a_confirmed_proposal_is_normalized_like_every_other_transcript_final(verifier):
    # The console's backend parses slots with regexes written for TYPED text. A verified
    # transcript is still a transcript, so it goes through the same normalizer: skipping
    # it here would deadlock the backend on exactly the turn we worked hardest to fix.
    sess = _sess("go ahead", pending_verify={"raw": "her will",
                                             "proposed": "one hundred microliters per well"})
    tf = _last(_run(sess), "transcript_final")
    assert tf["text"] == "100 microliters per well"
    assert tf["verified"] is True


# ------------------------------------------------------ (c) rejecting sends nothing at all
def test_rejecting_the_proposal_hands_over_nothing(verifier):
    sess = _sess("no", conf=0.95, pending_verify={"raw": "ron eliza", "proposed": "run an ELISA"})
    ws = _run(sess)

    # Not the proposed text (they said it was wrong), and NOT the raw text either: the
    # raw text is the string we already knew was a mishearing. A rejected reading must
    # not quietly degrade into the reading it replaced.
    assert "transcript_final" not in _types(ws)
    assert "transcript_verify" not in _types(ws)
    assert sess.pending_verify is None
    assert _spoken(ws) == server.VERIFY_RETRY == "Sorry, please say that again."


def test_an_unrelated_utterance_is_a_fresh_attempt_not_an_answer(verifier):
    # The user just says it again, louder. That is not a yes and not a no: drop the
    # proposal and process the new utterance from the top.
    verifier.proposes("run an ELISA on today's plasma samples")
    sess = _sess("run an ELISA on todays plasma samples", conf=0.95,
                 pending_verify={"raw": "ron eliza", "proposed": "run an ELISA"})
    ws = _run(sess)

    tv = _last(ws, "transcript_verify")
    assert tv is not None and tv["raw"] == "run an ELISA on todays plasma samples"
    assert sess.pending_verify["proposed"] == "run an ELISA on today's plasma samples"
    assert "transcript_final" not in _types(ws)


# ------------------------------------- (d) a control utterance is NEVER sent to a model
@pytest.mark.parametrize("text", [
    "confirm", "yes", "go ahead", "proceed",        # confirms
    "cancel", "no", "never mind", "abort",          # cancels
    "stop", "halt", "emergency stop",               # emergency stops
])
def test_control_utterances_are_never_verified(verifier, text):
    """THE line the whole design rests on. These words are matched by regex, acted on
    literally, and never rewritten. A model that can repair a garbled command can repair
    a garbled CANCEL, and that is a language model standing between a scientist saying
    stop and a centrifuge."""
    verifier.proposes("dispense 100 microliters")   # the repair that must never happen
    ws = _run(_sess(text, conf=0.95))

    assert verifier.calls == []                     # the model was not consulted, at all
    assert "transcript_verify" not in _types(ws)
    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == text    # handed over verbatim
    assert "verified" not in tf


def test_a_stop_answering_a_pending_verify_is_forwarded_not_swallowed(verifier):
    """An emergency stop is a stop first and an answer second. is_cancel() also matches
    "stop", so without an explicit ordering here a scientist's stop would be consumed as
    "no, that is not what I said" and answered with an apology instead of reaching the
    console. Nothing in this layer may swallow a stop."""
    sess = _sess("stop", conf=0.95,
                 pending_verify={"raw": "ron eliza", "proposed": "run an ELISA"})
    ws = _run(sess)

    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == "stop"   # it reaches the console
    assert sess.pending_verify is None               # and the proposal is dropped
    assert verifier.calls == []
    assert _spoken(ws) != server.VERIFY_RETRY


def test_verify_excluded_names_the_two_exclusions():
    control = _sess("", lab_state="gathering")
    assert server._verify_excluded(control, "confirm") == "control"
    assert server._verify_excluded(control, "cancel") == "control"
    assert server._verify_excluded(control, "stop") == "control"
    assert server._verify_excluded(control, "run an ELISA") is None
    armed = _sess("", lab_state="awaiting_confirmation")
    assert server._verify_excluded(armed, "run an ELISA") == "armed"


# --------------------------------------------- (e) an armed backend is NEVER verified
def test_an_armed_backend_is_never_verified(verifier):
    """While the backend is armed, the next affirmative fires a physical protocol, and
    that turn belongs to the confirmation floor. A misheard "yes" there must be REFUSED,
    never "corrected": correcting it is precisely how a machine starts by accident."""
    verifier.proposes("confirm")
    sess = _sess("yes indeed", conf=0.95, lab_state="awaiting_confirmation")
    ws = _run(sess)

    assert verifier.calls == []
    assert "transcript_verify" not in _types(ws)
    assert sess.pending_verify is None


def test_a_low_confidence_armed_turn_is_still_refused_by_the_floor(verifier):
    # The floor runs FIRST and is untouched: verification cannot reinterpret a turn it
    # has refused, and an outstanding proposal does not survive that refusal.
    verifier.proposes("confirm")
    sess = _sess("yes", conf=0.20, lab_state="awaiting_confirmation",
                 pending_verify={"raw": "i am six", "proposed": "IL-6"})
    ws = _run(sess)

    tr = _last(ws, "transcript_refused")
    assert tr is not None and tr["reason"] == "low_confidence_confirmation"
    assert "transcript_final" not in _types(ws)
    assert "transcript_verify" not in _types(ws)
    assert sess.pending_verify is None
    assert verifier.calls == []


# ----------------------------------------------------------- (f) every failure fails OPEN
def test_an_llm_error_fails_open_with_the_raw_transcript(verifier):
    verifier.fails(RuntimeError("api is down"))
    ws = _run(_sess("prepare a serial dilution", conf=0.9))
    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == "prepare a serial dilution"
    assert "transcript_verify" not in _types(ws)


def test_a_timeout_fails_open(monkeypatch):
    async def slow(raw, questions, reply):
        await asyncio.sleep(5)
    monkeypatch.setattr(server, "_verify_llm_call", slow)
    monkeypatch.setattr(server, "VERIFY_TIMEOUT_S", 0.05)
    if server.llm_client is None:
        monkeypatch.setattr(server, "llm_client", object())
    ws = _run(_sess("prepare a serial dilution", conf=0.9))
    assert _last(ws, "transcript_final")["text"] == "prepare a serial dilution"
    assert "transcript_verify" not in _types(ws)


def test_an_unparseable_answer_fails_open(verifier):
    verifier.answers_raw("I think they meant an ELISA, probably.")   # prose, not JSON
    ws = _run(_sess("ron eliza", conf=0.9))
    assert _last(ws, "transcript_final")["text"] == "ron eliza"
    assert "transcript_verify" not in _types(ws)
    assert server.parse_proposal("I think they meant an ELISA.") is None


def test_json_wrapped_in_a_fence_still_parses(verifier):
    # The model was told to answer with JSON and nothing else. A small model sometimes
    # fences it anyway, and that is not a reason to drop a good proposal.
    verifier.answers_raw('```json\n{"text": "run an ELISA"}\n```')
    assert server.parse_proposal('```json\n{"text": "IL-6"}\n```') == "IL-6"
    ws = _run(_sess("ron eliza", conf=0.9))
    assert _last(ws, "transcript_verify")["proposed"] == "run an ELISA"


def test_no_api_key_skips_the_layer_entirely(monkeypatch):
    monkeypatch.setattr(server, "llm_client", None)
    ws = _run(_sess("ron eliza", conf=0.9))
    assert _last(ws, "transcript_final")["text"] == "ron eliza"
    assert "transcript_verify" not in _types(ws)


def test_a_failed_verification_still_gets_the_deterministic_normalizer(verifier):
    """Fail-open means falling back to the path that already existed, which is not the
    same as falling back to the raw string: lab_backend.normalize_transcript still runs,
    and it already fixes this exact mishearing for free. The verifier is a second line
    of defense, never a replacement for the first."""
    verifier.fails(RuntimeError("api is down"))
    ws = _run(_sess("i am six", conf=0.9))
    assert _last(ws, "transcript_final")["text"] == "IL-6"
    assert "transcript_verify" not in _types(ws)


# -------------------------------------------- (g) an identical proposal is not a turn
def test_an_unchanged_proposal_does_not_interrupt_the_turn(verifier):
    # The default stub echoes the transcript back, which is what the model is told to do
    # when the transcript already looks right. That must be invisible.
    sess = _sess("add 50 microliters to well A3", conf=0.94)
    ws = _run(sess)
    tf = _last(ws, "transcript_final")
    assert tf is not None and tf["text"] == "add 50 microliters to well A3"
    assert "verified" not in tf                      # untouched shape for the console
    assert "transcript_verify" not in _types(ws)
    assert sess.pending_verify is None
    assert verifier.calls                            # it WAS asked; it just had nothing to fix


@pytest.mark.parametrize("proposed", [
    "Run an ELISA",            # casing only
    "run an elisa!",           # punctuation only
    "run   an   elisa",        # whitespace only
])
def test_a_cosmetic_difference_is_not_material(verifier, proposed):
    # Casing and punctuation belong to the recognizer, not the speaker. Asking "did you
    # mean run an elisa?" about a comma would teach the user to stop listening.
    verifier.proposes(proposed)
    ws = _run(_sess("run an elisa", conf=0.94))
    assert "transcript_verify" not in _types(ws)
    assert "transcript_final" in _types(ws)


def test_materially_same_is_case_punctuation_and_whitespace_insensitive():
    assert server.materially_same("Run an ELISA.", "run an elisa") is True
    assert server.materially_same("per well", "her will") is False
    # "IL-6" vs "IL 6" is a punctuation difference, so it is NOT material, and it does
    # not need to be: normalize_transcript restores the hyphen on the way out either way.
    assert server.materially_same("IL-6", "IL 6") is True


# ------------------------------------------------------------------- purely additive
def test_the_layer_is_inert_when_switched_off(monkeypatch, verifier):
    monkeypatch.setattr(server, "VERIFY_INTENT", False)
    verifier.proposes("run an ELISA")
    sess = _sess("ron eliza", conf=0.9)
    ws = _run(sess)
    assert verifier.calls == []                      # no call, so no cost and no latency
    assert _types(ws) == ["transcript_final"]        # and the exact message stream of before
    assert _last(ws, "transcript_final")["text"] == "ron eliza"
    assert sess.pending_verify is None


def test_set_lab_state_context_is_optional_and_does_not_clobber():
    """A console that sends neither field behaves exactly as before: it neither sets the
    context nor wipes what an earlier message set."""
    async def main():
        ws = DummyWS()
        sess = server.Session()
        sess.speech_mode = True
        await server.handle_set_lab_state(ws, sess, {"state": "gathering"})
        assert sess.lab_questions is None and sess.lab_reply is None

        await server.handle_set_lab_state(ws, sess, {
            "state": "gathering",
            "questions": ["Which analyte?", "  "],           # blanks dropped
            "reply": "  Sure. Which analyte?  ",             # stripped
        })
        assert sess.lab_questions == ["Which analyte?"]
        assert sess.lab_reply == "Sure. Which analyte?"

        await server.handle_set_lab_state(ws, sess, {"state": "executed"})
        assert sess.lab_questions == ["Which analyte?"]      # absent key: left alone
        assert sess.lab_reply == "Sure. Which analyte?"

        await server.handle_set_lab_state(ws, sess, {"state": "gathering",
                                                     "questions": [], "reply": ""})
        assert sess.lab_questions is None and sess.lab_reply is None   # present + empty: cleared
        return ws
    ws = asyncio.run(main())
    assert set(_types(ws)) == {"lab_state"}   # the message's own shape is unchanged


def test_a_default_connection_never_verifies(monkeypatch, verifier):
    """Not speech mode, so end_turn takes the reply pipeline and this layer does not
    exist. lab_state cannot even be set from there."""
    async def fake_transcribe(pcm, language, hints):
        return "i am six", _conf(0.93)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)
    verifier.proposes("IL-6")

    pcm = base64.b64encode(b"\x00\x00" * 100).decode("ascii")
    ws = ScriptWS("/", [{"type": "audio_segment", "audio_b64": pcm}])
    asyncio.run(server.handler(ws))
    assert verifier.calls == []
    assert "transcript_verify" not in [m.get("type") for m in ws.sent]


# --------------------------------------------------------------------- end to end
def test_end_to_end_mishear_question_confirm(monkeypatch, verifier):
    """The whole loop through the real handler(): the console reports the question its
    backend just asked, the recognizer confidently mishears the answer, we ask, the user
    says yes, and the CORRECTED utterance is what finally reaches the console."""
    heard = ["ron eliza", "yes"]

    async def fake_transcribe(pcm, language, hints):
        return heard.pop(0), _conf(0.93)
    monkeypatch.setattr(server, "transcribe", fake_transcribe)
    verifier.proposes("run an ELISA on today's plasma samples")

    pcm = base64.b64encode(b"\x00\x00" * 100).decode("ascii")
    ws = ScriptWS("/?mode=speech", [
        {"type": "set_lab_state", "state": "gathering",
         "questions": ["What would you like to run?"],
         "reply": "Ready. What would you like to run?"},
        {"type": "audio_segment", "audio_b64": pcm},
        {"type": "end_turn"},
        {"type": "audio_segment", "audio_b64": pcm},
        {"type": "end_turn"},
    ])
    asyncio.run(server.handler(ws))
    types = [m.get("type") for m in ws.sent]

    assert types.index("transcript_verify") < types.index("transcript_final")
    verify = next(m for m in ws.sent if m.get("type") == "transcript_verify")
    assert verify["raw"] == "ron eliza"
    assert verify["proposed"] == "run an ELISA on today's plasma samples"
    final = next(m for m in ws.sent if m.get("type") == "transcript_final")
    assert final["text"] == "run an ELISA on today's plasma samples"
    assert final["verified"] is True
    # exactly one transcript_final in the whole exchange: the misheard turn handed the
    # console nothing, and the confirming turn handed it the correction.
    assert types.count("transcript_final") == 1
    assert not {"reply_start", "reply_delta", "reply_done"} & set(types)
