"""The seam's transcript normalizer and confirmation floor.

The normalizer exists because of a MEASURED failure, not a hypothetical one: on the
real stack, Fun-ASR-Nano transcribes the spoken "IL-6" as "IL 6" and the spoken "100"
as "hundred", while the Lab Agent's slot regexes want "IL-6" and digits. The backend
therefore never filled the analyte slot, re-asked the same question every turn, and the
conversation deadlocked before it could reach a confirmation at all.

It sits on the safety path (every lab utterance passes through it on the way to the
planner), so the tests below care as much about what it must NOT do as what it must.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import lab_backend as lb  # noqa: E402


# --- the two failures actually observed on the real stack ------------------- #

def test_spoken_analyte_gets_its_hyphen_back():
    # Fun-ASR-Nano's real output for a spoken "IL-6".
    assert lb.normalize_transcript(
        "Let's do IL 6 with 24 samples, 400 microliters per well"
    ) == "Let's do IL-6 with 24 samples, 400 microliters per well"


def test_spoken_number_becomes_digits():
    # Real output for a spoken "100 microliters".
    assert lb.normalize_transcript(
        "My mistake make it hundred microliters per well"
    ) == "My mistake make it 100 microliters per well"


@pytest.mark.parametrize("said,want", [
    # A scientist SPELLS the acronym out loud, and the recognizer writes the letters
    # apart. All of these were seen on the real stack for the same spoken "IL-6".
    ("I L 6. 5 samples", "IL-6. 5 samples"),
    ("I L 6.", "IL-6."),
    ("I.L. 6 with 24 samples", "IL-6 with 24 samples"),
    # ... and the DIGIT arrives as a word, and the letters get mangled. Verbatim from a
    # live session where the operator said "IL-6" three times and got three spellings.
    ("I L SIX", "IL-6"),
    ("i l six", "IL-6"),
    ("i am six", "IL-6"),
    ("I'll six", "IL-6"),
    ("aisle six with 24 samples", "IL-6 with 24 samples"),
    ("I L eight", "IL-8"),
    # The form a human can actually say and a recognizer can actually get right.
    ("interleukin six", "IL-6"),
    ("interleukin 6, 24 samples", "IL-6, 24 samples"),
    ("run a C R P assay", "run a CRP assay"),
    ("T N F alpha please", "TNF-alpha please"),
    ("four hundred microliters", "400 microliters"),
    # "per well" is load-bearing: the backend's volume override only fires when that exact
    # phrase is present. These are verbatim from a live session, all meaning "per well",
    # and each one silently dropped the correction.
    ("my mistake make it 100 microliters Her will.", "my mistake make it 100 microliters per well."),
    ("IL-6 with 24 samples. 400 microliters Her well.", "IL-6 with 24 samples. 400 microliters per well."),
    ("make it 100 microliters per whale", "make it 100 microliters per well"),
    ("50 microliters per wheel", "50 microliters per well"),
    ("fifty microliters per well", "50 microliters per well"),
    ("twenty four samples", "24 samples"),
    ("five points", "5 points"),
    ("IL6 assay", "IL-6 assay"),           # canonicalized (the backend accepts both)
    ("IL-6 assay", "IL-6 assay"),          # already canonical, unchanged
    ("TNF alpha", "TNF-alpha"),
])
def test_normalizations(said, want):
    assert lb.normalize_transcript(said) == want


# --- what it must NOT do ---------------------------------------------------- #

def test_the_il_mishears_only_fire_when_a_6_or_8_follows():
    """"I am six" is only rewritten because a 6 follows it and the question on the table
    was "which analyte?". Ordinary prose containing those words must survive untouched, or
    the normalizer becomes a source of nonsense instead of a fix for it."""
    for t in ["I am going to the aisle", "he is ill", "I'll do it", "I am running late",
              "aisle three"]:
        assert lb.normalize_transcript(t) == t
    # "six samples" DOES become "6 samples", but that is the quantity rule doing its job
    # (the backend's count regex wants digits), not the analyte rule misfiring: no IL
    # appears.
    assert lb.normalize_transcript("six samples") == "6 samples"
    assert "IL" not in lb.normalize_transcript("six samples")


def test_per_well_repair_only_fires_after_a_volume_unit():
    """Anchored to a unit, so it repairs the phrase where it is load-bearing and leaves
    ordinary speech alone. "her will" in a sentence about a will is not a pipetting
    instruction."""
    for t in ["her will was read", "the well is deep", "he knew her well",
              "par for the course"]:
        assert lb.normalize_transcript(t) == t


def test_number_words_outside_a_quantity_are_left_alone():
    """"one more time" must not become "1 more time". The rewrite only fires when the
    utterance actually quantifies something the backend parses."""
    assert lb.normalize_transcript("say that one more time") == "say that one more time"


def test_confirmations_and_cancels_pass_through_unchanged():
    """The normalizer sits in front of a machine that moves liquid. It may fix how a
    number is spelled; it may never change what an utterance MEANS."""
    for t in ["yes, go ahead", "confirm", "confirm centrifuge", "cancel", "no, stop",
              "abort", "wait"]:
        assert lb.normalize_transcript(t) == t


# --- the confirmation floor ------------------------------------------------- #

def test_floor_only_bites_when_the_backend_is_armed():
    # Not armed: a low-confidence utterance is ordinary speech, not a trigger.
    assert lb.blocks_confirmation("gathering", 0.10) is False
    assert lb.blocks_confirmation(None, 0.10) is False
    # Armed: the next affirmative starts a machine, so a mumble must not get through.
    assert lb.blocks_confirmation("awaiting_confirmation", 0.10) is True


def test_floor_fails_open_when_the_asr_reports_no_confidence():
    """A degraded ASR (and the mock) supply no confidence at all. Locking the user out
    of confirming in that case would be a worse failure than the one being prevented."""
    assert lb.blocks_confirmation("awaiting_confirmation", None) is False


def test_real_speech_clears_the_floor():
    """Regression guard on calibration. The lowest prob_mean measured on the real stack
    for a clear spoken confirmation was 0.641 (synthesized speech, so an optimistic
    bound). The floor must sit below that or a good confirmation gets refused."""
    assert lb.CONFIRM_FLOOR < 0.641
    assert lb.blocks_confirmation("awaiting_confirmation", 0.641) is False


def test_armed_states():
    assert lb.armed("awaiting_confirmation") is True
    assert lb.armed("gathering") is False
    assert lb.armed("executed") is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
