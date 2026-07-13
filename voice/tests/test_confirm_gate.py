"""Unit tests for the pending-confirmation resolution: the execution floors (F2 +
the round-3 no-confidence rule), intent binding (F1), and pending expiry with
passthrough (F4 + round-3).

All live in _handle_pending_action_turn. The design ruling under test: execution
requires a CLEAR, INTENT-BOUND confirm, but no filter ever eats the word (a
low-confidence or unbound confirm re-prompts and keeps the pending; it is never
dropped or auto-cancelled). Cancel is deliberately never confidence-gated. An
expired pending never fires: a stale confirm/cancel gets a notice, anything else
passes through to a normal turn.

These drive the real handler with a mocked TTS (synthesize), a temp sessions dir,
and (for passthrough) a stubbed handle_end_turn, so no network and no clock sleep:
expiry is exercised by planting created_ts in the past.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402
from web import lab_gate  # noqa: E402


class DummyWS:
    def __init__(self):
        self.messages = []

    async def send(self, payload):
        self.messages.append(json.loads(payload))


def _wav():
    return server.pcm16_to_wav(b"\x00\x00" * 200).getvalue()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No network TTS, no real disk writes: keep the tests hermetic."""
    async def fake_synth(text, model=None, **params):
        return _wav()
    monkeypatch.setattr(server, "synthesize", fake_synth)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "lat.jsonl")
    monkeypatch.setattr(server, "CONFIRM_FLOOR", 0.40)
    monkeypatch.setattr(server, "PENDING_TTL_S", 120)


def _conf(prob_mean, prob_min=None):
    return {"prob_mean": prob_mean, "prob_min": prob_min if prob_min is not None else prob_mean,
            "tokens": 1}


def _arm(text, *, created_ts=None, conf=0.97, intent="dispense", args=None):
    """A session with a pending action and one accumulated confirm/cancel segment.
    conf is the segment's prob_mean (or None for a missing confidence block). The
    pending's bound/keyword/confirm_phrase are derived from the intent exactly as the
    server arms them, so the resolution logic sees a real pending shape."""
    args = args or {"volume_ul": 50, "well": "A3"}
    severity = lab_gate.severity_of(intent)
    bound = severity in (lab_gate.IRREVERSIBLE, lab_gate.HAZARDOUS)
    sess = server.Session()
    sess.pending = [text]
    sess.asr_ms = [10.0]
    sess.asr_conf = [None if conf is None else _conf(conf)]
    sess.turn_t0 = time.perf_counter()
    sess.pending_action = {
        "intent": intent, "args": args, "readback": lab_gate.readback(intent, args),
        "strict": bound, "bound": bound, "keyword": lab_gate.keyword_of(intent),
        "confirm_phrase": lab_gate.confirm_phrase(intent) if bound else None,
        "severity": severity, "prob_min": 0.9,
        "created_ts": created_ts if created_ts is not None else time.time()}
    return sess


def _run(sess):
    ws = DummyWS()
    asyncio.run(server._handle_pending_action_turn(ws, sess))
    return ws


def _types(ws):
    return [m.get("type") for m in ws.messages]


def _last(ws, t):
    return next((m for m in reversed(ws.messages) if m.get("type") == t), None)


def _decision():
    """The decision of the last logged action record (pins the exact decision name,
    which the WS messages do not always distinguish, e.g. noconf vs lowconf)."""
    if not server.LOG_FILE.exists():
        return None
    recs = [json.loads(x) for x in server.LOG_FILE.read_text().splitlines() if x.strip()]
    return (recs[-1].get("action") or {}).get("decision") if recs else None


# --------------------------------------------------------------- the execution floors
def test_bound_confirm_at_the_floor_executes():
    # exactly at the floor is not below it: execute.
    sess = _arm("confirm dispense", conf=0.40)
    ws = _run(sess)
    assert "action_executed" in _types(ws)
    assert sess.pending_action is None
    assert _decision() == "confirmed"


def test_bound_confirm_just_below_the_floor_reprompts_and_keeps_pending():
    sess = _arm("confirm dispense", conf=0.39)
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert "action_cancelled" not in _types(ws)   # not dropped, not cancelled
    assert sess.pending_action is not None          # pending KEPT
    assert _decision() == "reprompt_lowconf"
    rd = _last(ws, "reply_done")
    assert rd is not None and "not clearly" in rd["text"].lower()


def test_loose_confirm_low_conf_on_a_reversible_pending_is_gated():
    # a REVERSIBLE pending is NOT bound, so a loose "yes" is a full confirm; the floor
    # still applies to it.
    sess = _arm("yes", conf=0.20, intent="set_temperature", args={"celsius": 37.0})
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is not None
    assert _decision() == "reprompt_lowconf"


def test_hazardous_bound_confirm_gated_low_then_executes_clear():
    gated = _run(_arm("confirm centrifuge", intent="start_centrifuge",
                      args={"rpm": 3000, "minutes": 5}, conf=0.15))
    assert "action_executed" not in _types(gated)
    clear = _arm("confirm centrifuge", intent="start_centrifuge",
                 args={"rpm": 3000, "minutes": 5}, conf=0.97)
    ws = _run(clear)
    assert "action_executed" in _types(ws)


def test_missing_confidence_confirm_reprompts_not_executes():
    # ROUND-3 SPEC CHANGE (declared): missing confidence on a confirm no longer
    # fails open to execution; it re-prompts (decision reprompt_noconf), consistent
    # with the command gate's escalate-on-None. The word is still never dropped.
    sess = _arm("confirm dispense", conf=None)
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is not None          # pending KEPT
    assert _decision() == "reprompt_noconf"
    rd = _last(ws, "reply_done")
    assert rd is not None and "not clearly" in rd["text"].lower()


def test_floor_disabled_executes_any_confirm(monkeypatch):
    monkeypatch.setattr(server, "CONFIRM_FLOOR", 0.0)
    sess = _arm("confirm dispense", conf=0.01)
    ws = _run(sess)
    assert "action_executed" in _types(ws)


# ----------------------------------------------------------------------- intent binding
def test_bound_pending_bare_confirm_reprompts_unbound():
    # "confirm" alone on an irreversible pending must NOT fire: it re-prompts with
    # the exact bound phrase and keeps the pending.
    sess = _arm("confirm", conf=0.97, intent="dispense")
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is not None
    assert _decision() == "reprompt_unbound"
    rd = _last(ws, "reply_done")
    assert rd is not None and "confirm dispense" in rd["text"].lower()


def test_bound_pending_bare_yes_reprompts_unbound():
    sess = _arm("yes", conf=0.97, intent="dispense")
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert _decision() == "reprompt_unbound"


def test_keyword_without_the_word_confirm_does_not_execute():
    # "yes dispense" has the keyword but not the word "confirm": it is not a bound
    # confirm, so it re-prompts, never fires.
    sess = _arm("yes dispense", conf=0.97, intent="dispense")
    ws = _run(sess)
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is not None
    assert _decision() == "reprompt_unbound"


def test_bound_confirm_with_keyword_executes():
    sess = _arm("please confirm dispense now", conf=0.97, intent="dispense")
    ws = _run(sess)
    assert "action_executed" in _types(ws)
    assert sess.pending_action is None


def test_action_pending_carries_confirm_phrase_when_bound():
    # arm through the real tool path so the additive WS field is exercised: a bound
    # (dispense) pending advertises the exact phrase; a reversible one advertises null.
    async def arm(intent, args, prob_min):
        ws = DummyWS()
        sess = server.Session()
        commit = {"event": asyncio.Event()}
        commit["event"].set()
        ctx = server._LabToolsCtx(ws, sess, commit, server._asr_rec([10.0], [_conf(0.6)], "x"))
        ctx.prob_min = prob_min
        await ctx.handle("lab_command", {"intent": intent, "args": args})
        return ws
    bound = asyncio.run(arm("dispense", {"volume_ul": 50, "well": "A3"}, 0.6))
    ap = _last(bound, "action_pending")
    assert ap is not None and ap.get("confirm_phrase") == "confirm dispense"
    # a reversible pending (set_temperature at low confidence) advertises no phrase
    rev = asyncio.run(arm("set_temperature", {"celsius": 37.0}, 0.60))
    ap2 = _last(rev, "action_pending")
    assert ap2 is not None and ap2.get("confirm_phrase") is None


# --------------------------------------------------------------------------- cancel
def test_cancel_is_never_confidence_gated():
    sess = _arm("cancel", conf=0.05)
    ws = _run(sess)
    ac = _last(ws, "action_cancelled")
    assert ac is not None and ac["reason"] == "user"
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is None


# --------------------------------------------------------------------- expiry (TTL)
def test_pending_within_ttl_still_executes(monkeypatch):
    monkeypatch.setattr(server, "PENDING_TTL_S", 100)
    sess = _arm("confirm dispense", conf=0.97, created_ts=time.time() - 1)
    ws = _run(sess)
    assert "action_executed" in _types(ws)
    assert "action_cancelled" not in _types(ws)


def test_expired_confirm_is_noticed_not_executed(monkeypatch):
    monkeypatch.setattr(server, "PENDING_TTL_S", 100)
    sess = _arm("confirm dispense", conf=0.97, created_ts=time.time() - 200)
    ws = _run(sess)
    ac = _last(ws, "action_cancelled")
    assert ac is not None and ac["reason"] == "expired"
    assert "action_executed" not in _types(ws)
    assert sess.pending_action is None
    assert _decision() == "expired"
    rd = _last(ws, "reply_done")
    assert rd is not None and "expired" in rd["text"].lower()


def test_expired_unrelated_utterance_passes_through_to_a_normal_turn(monkeypatch):
    # ROUND-3: an expired pending no longer eats unrelated speech. It drops the
    # pending (action_cancelled expired, no canned notice) and runs the utterance as
    # a normal turn.
    monkeypatch.setattr(server, "PENDING_TTL_S", 100)
    seen = {}

    async def fake_end_turn(ws, sess):
        seen["pending"] = list(sess.pending)
        seen["pending_action"] = sess.pending_action
    monkeypatch.setattr(server, "handle_end_turn", fake_end_turn)

    sess = _arm("read the temperature sensor", conf=0.97, created_ts=time.time() - 200)
    ws = _run(sess)
    ac = _last(ws, "action_cancelled")
    assert ac is not None and ac["reason"] == "expired"
    assert "reply_done" not in _types(ws)              # no canned notice was spoken
    assert seen.get("pending") == ["read the temperature sensor"]
    assert seen.get("pending_action") is None          # pending cleared before the turn


def test_expiry_disabled_lets_an_old_pending_execute(monkeypatch):
    monkeypatch.setattr(server, "PENDING_TTL_S", 0)
    sess = _arm("confirm dispense", conf=0.97, created_ts=time.time() - 100000)
    ws = _run(sess)
    assert "action_executed" in _types(ws)


# ------------------------------------------------------------------------- supersede
def test_unrelated_utterance_supersedes_and_runs_a_normal_turn(monkeypatch):
    seen = {}

    async def fake_end_turn(ws, sess):
        seen["pending"] = list(sess.pending)
    monkeypatch.setattr(server, "handle_end_turn", fake_end_turn)

    sess = _arm("what is the weather", conf=0.97)   # not expired, not confirm/cancel
    ws = _run(sess)
    ac = _last(ws, "action_cancelled")
    assert ac is not None and ac["reason"] == "superseded"
    assert seen.get("pending") == ["what is the weather"]
