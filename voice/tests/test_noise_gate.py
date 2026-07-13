"""Unit tests for the incoming-segment noise gate (LA_CONF_FLOOR).

The gate has two pure pieces and one session-aware exemption:
  - gate_segment(text, confidence, floor) -> reject reason or None. This is what
    decides, so the boundary (exactly 0.40), the degenerate check (the real
    "to the to the" loop vs a legitimate long sentence), and the fail-open on a
    missing confidence block are all pinned here against hand-picked inputs.
  - _gate_exempt(sess, text) -> bool. A safety/control utterance (a pending
    confirm/cancel, a would-halt stop) must never be gated, so those are pinned too.

Calibration these encode (see the gate_segment comment in web/server.py): the floor
keys on prob_MEAN because prob_min overlaps between noise and speech, and the
degenerate check is independent of confidence because the one high-confidence noise
clip in the set is a repetition loop the floor cannot catch.
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import server  # noqa: E402


def conf(prob_mean=None, prob_min=None):
    return {"prob_mean": prob_mean, "prob_min": prob_min}


# ------------------------------------------------------------------- the floor
def test_floor_boundary_is_inclusive_at_the_floor():
    # exactly at the floor is NOT below it: accept.
    assert server.gate_segment("start the centrifuge", conf(prob_mean=0.40), 0.40) is None
    # a hair under: reject.
    assert server.gate_segment("start the centrifuge", conf(prob_mean=0.3999), 0.40) == "low_confidence"


def test_floor_accepts_clear_speech_and_rejects_low_noise():
    assert server.gate_segment("read the temperature sensor", conf(prob_mean=0.95), 0.40) is None
    assert server.gate_segment("uh the the", conf(prob_mean=0.12), 0.40) == "low_confidence"


def test_floor_keys_on_prob_mean_not_prob_min():
    # a real command with a weak WORST token (prob_min low) but a strong MEAN must
    # pass: this is the overlap that made the floor key on prob_mean. (LAB-SMOKE c
    # rides exactly this: pmin 0.40, pmean 0.98 -> passes the floor, reaches the
    # lab gate, which is the layer that rejects on prob_min.)
    assert server.gate_segment("dispense 50 microliters into well A3",
                               conf(prob_mean=0.98, prob_min=0.40), 0.40) is None


def test_floor_disabled_accepts_anything_confidencewise():
    assert server.gate_segment("mumble mumble", conf(prob_mean=0.01), 0.0) is None


def test_missing_confidence_fails_open_on_the_floor():
    # no confidence block (a degraded ASR, or a mock without the override): the floor
    # cannot judge, so it accepts rather than dropping the user's speech.
    assert server.gate_segment("read the temperature sensor", None, 0.40) is None


# -------------------------------------------------------------- the degenerate check
def test_real_repetition_loop_is_degenerate_even_at_high_confidence():
    # the 0.9195-confidence noise clip in the calibration set is a loop like this;
    # the floor cannot catch it, the text shape must. High confidence, still dropped.
    loop = "to the to the to the to the to the to the to the to the"
    assert server._is_degenerate(loop) is True
    assert server.gate_segment(loop, conf(prob_mean=0.92), 0.40) == "degenerate"


def test_token_dominance_trips_degenerate():
    # >= 8 tokens, one token > 40% of them
    text = "no no no no no no no no maybe"
    assert server._is_degenerate(text) is True


def test_legitimate_long_sentence_is_not_degenerate():
    s = "Please start the centrifuge at three thousand rpm for five minutes and then stop."
    assert server._is_degenerate(s) is False
    assert server.gate_segment(s, conf(prob_mean=0.9), 0.40) is None


def test_short_text_is_never_degenerate():
    # under 24 chars: too short to judge repetition; falls through to the floor.
    assert server._is_degenerate("no no no") is False


def test_degenerate_beats_the_floor_and_applies_without_confidence():
    loop = "la la la la la la la la la la la la la la la la"
    # checked before the floor, so a degenerate clip reports "degenerate" not
    # "low_confidence" even when its confidence is also below the floor.
    assert server.gate_segment(loop, conf(prob_mean=0.05), 0.40) == "degenerate"
    # and it needs no confidence block at all.
    assert server.gate_segment(loop, None, 0.40) == "degenerate"


# ---------------------------------------------------------------------- exemptions
def _session(pending=None):
    sess = server.Session()
    sess.pending_action = pending
    return sess


class _FakeTask:
    """A reply task that is still running, for the would-halt-stop exemption."""
    def done(self):
        return False


def test_pending_confirm_low_conf_is_exempt():
    # a quiet "confirm" while an action is pending must still be honored.
    sess = _session(pending={"strict": False})
    assert server._gate_exempt(sess, "confirm") is True
    assert server._gate_exempt(sess, "yes") is True          # loose affirmation, loose pending
    assert server._gate_exempt(sess, "cancel") is True       # cancel too


def test_strict_pending_exempts_confirm_but_not_a_loose_yes():
    sess = _session(pending={"strict": True})
    assert server._gate_exempt(sess, "confirm") is True
    # under a strict (HAZARDOUS) gate a loose "yes" is not a confirmation and not a
    # cancel, so it is NOT exempt: it would re-prompt anyway, per the pending path.
    assert server._gate_exempt(sess, "yes") is False


def test_no_pending_means_confirm_words_are_not_exempt():
    sess = _session(pending=None)
    assert server._gate_exempt(sess, "yes") is False
    assert server._gate_exempt(sess, "confirm") is False


def test_would_halt_stop_is_exempt(monkeypatch):
    monkeypatch.setattr(server, "LAB_MODE", True)
    sess = _session(pending=None)
    sess.reply_task = _FakeTask()          # something is in flight to halt
    assert server._gate_exempt(sess, "stop") is True
    assert server._gate_exempt(sess, "halt") is True


def test_stop_with_nothing_to_halt_is_not_exempt(monkeypatch):
    monkeypatch.setattr(server, "LAB_MODE", True)
    sess = _session(pending=None)          # no reply, stub idle, no pending
    # a bare "stop" with nothing running is just a word: the gate may judge it like
    # any other segment (it is short, so never degenerate; the floor still applies).
    assert server._gate_exempt(sess, "stop") is False
