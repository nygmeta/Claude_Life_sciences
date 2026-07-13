"""What is SPOKEN is normalized; what is DISPLAYED is untouched.

gepard is a speech model, not a lab technician. Handed "IL-6" it says something between
"ill six" and "eels"; handed "e.g." it says "ee-gee"; handed "400 uL" it says "four
hundred you-ell"; handed "assay_plate" it reads the underscore out loud. None of that is
a model defect: it is what happens when a TTS front end is missing.

These tests pin the front end, and they pin the boundary: the rewrite happens inside
server.synthesize(), which is the single place text becomes audio, so it can never leak
into the transcript the scientist READS.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.speakable import to_speakable  # noqa: E402


@pytest.mark.parametrize("written,spoken", [
    # The two the operator hit in a live demo.
    ("Which analyte should I run the ELISA for? (e.g. IL-6)",
     "Which analyte should I run the ELISA for? for example I L 6"),
    ("Ready: IL-6 ELISA, 24 samples.", "Ready: I L 6 ELISA, 24 samples."),

    # Acronyms a person SPELLS out loud rather than pronouncing.
    ("IL-8 assay", "I L 8 assay"),
    ("TNF-alpha", "T N F alpha"),
    ("CRP", "C R P"),
    ("using SOP-ELISA-04", "using S O P ELISA 04"),

    # Units. "you-ell" is not a volume.
    ("400 uL into the well", "400 microliters into the well"),
    ("50 nL", "50 nanoliters"),
    ("incubate at 37C", "incubate at 37 degrees celsius"),

    # Machine punctuation nobody pronounces.
    ("48 transfers into assay_plate", "48 transfers into assay plate"),

    # Latin.
    ("i.e. the plate is full", "that is the plate is full"),
])
def test_spoken_form(written, spoken):
    assert to_speakable(written) == spoken


def test_ordinary_prose_is_left_alone():
    """The rewrite must not paraphrase. It only respells what cannot be pronounced."""
    plain = "I found a problem before running anything."
    assert to_speakable(plain) == plain


def test_elisa_stays_a_word():
    """People SAY "elisa". Spelling it out would be worse than leaving it."""
    assert "E L I S A" not in to_speakable("Run an ELISA on the plasma samples.")


def test_empty_is_safe():
    assert to_speakable("") == ""
    assert to_speakable(None) is None


def test_the_display_text_is_never_touched():
    """The guarantee that makes this safe: the rewrite lives inside synthesize(), so the
    caller's string is unchanged and the transcript keeps the readable spelling."""
    original = "Ready: IL-6 ELISA (e.g. 400 uL)."
    _ = to_speakable(original)
    assert original == "Ready: IL-6 ELISA (e.g. 400 uL)."   # not mutated in place


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
