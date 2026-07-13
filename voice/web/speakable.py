"""Turn text that READS well into text that SPEAKS well.

Two different audiences, two different spellings of the same sentence. The reader wants

    Ready: IL-6 ELISA, 24 samples (e.g. 400 uL into assay_plate).

and the speaker needs

    Ready: I L 6 ELISA, 24 samples, for example 400 microliters into assay plate.

gepard is a text-to-speech model, not a lab technician: hand it "IL-6" and it says
something between "ill six" and "eels", hand it "e.g." and it says "ee-gee", hand it
"uL" and it says "you-ell", and hand it "assay_plate" and it reads the underscore. None
of that is a bug in the model. It is what happens when a TTS front end is missing.

This is that front end. It is applied at ONE place, `server.synthesize()`, which every
synth call funnels through (the streaming reply consumer, the playground, the
announcements, the console's `speak`). Applying it there and only there is what gives us
the property we actually want:

    what is SPOKEN is normalized;  what is DISPLAYED is untouched.

The transcript panel keeps showing "IL-6" and "(e.g. IL-6)", because that is what a
scientist wants to READ. Only the audio changes.

Note the symmetry with `lab_backend.normalize_transcript()`, which does the exact
opposite on the way in: the recognizer writes a spoken "IL-6" as "I L 6", and the
planner's regexes want "IL-6". One converts speech into text conventions on the way in,
this one converts text conventions into speech on the way out. Neither is optional once
a microphone and a speaker are attached to a system that was written for a keyboard.
"""
from __future__ import annotations

import re

# Order matters: the acronym rules run before the unit rules so "IL-6" is spelled out
# before anything else can chew on the hyphen.
_RULES: list[tuple[re.Pattern, str]] = [
    # --- lab acronyms that must be SPELLED, not pronounced --------------------
    # A person says "eye ell six", never "ill-six". The recognizer already taught us
    # this is how the letters travel; say them back the same way.
    (re.compile(r"\bIL[-\s]?(\d)\b", re.I), r"I L \1"),
    (re.compile(r"\bTNF[-\s]?alpha\b", re.I), "T N F alpha"),
    (re.compile(r"\bTNF\b", re.I), "T N F"),
    (re.compile(r"\bCRP\b", re.I), "C R P"),
    # SOP-ELISA-04 -> "S O P, ELISA, 04". ELISA stays a word: people SAY "elisa".
    (re.compile(r"\bSOP[-\s]?([A-Z]+)[-\s]?(\d+)\b", re.I), r"S O P \1 \2"),
    (re.compile(r"\bSOP\b", re.I), "S O P"),
    (re.compile(r"\bRPM\b"), "R P M"),
    (re.compile(r"\bOT-?2\b", re.I), "O T 2"),

    # --- latin abbreviations: nobody says "ee-gee" out loud -------------------
    (re.compile(r"\be\.g\.\s*", re.I), "for example "),
    (re.compile(r"\bi\.e\.\s*", re.I), "that is "),
    (re.compile(r"\betc\.", re.I), "et cetera"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
    (re.compile(r"\bapprox\.", re.I), "approximately"),

    # --- units: spoken in full, and only when they are actually a unit --------
    # The lookbehind keeps "uL" attached to a number, so a stray "ul" in a word is
    # never rewritten.
    (re.compile(r"(?<=\d)\s*(?:µ|u)L\b"), " microliters"),
    (re.compile(r"(?<=\d)\s*nL\b"), " nanoliters"),
    (re.compile(r"(?<=\d)\s*mL\b"), " milliliters"),
    (re.compile(r"(?<=\d)\s*L\b"), " liters"),
    (re.compile(r"(?<=\d)\s*(?:µ|u)g\b"), " micrograms"),
    (re.compile(r"(?<=\d)\s*mg\b"), " milligrams"),
    (re.compile(r"(?<=\d)\s*°?C\b"), " degrees celsius"),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"(?<=\d)\s*x\b", re.I), " times"),

    # --- machine-readable punctuation a human would never pronounce ------------
    (re.compile(r"(\w)_(\w)"), r"\1 \2"),          # assay_plate -> assay plate
    (re.compile(r"\s*\(\s*"), ", "),               # parentheses become a spoken pause
    (re.compile(r"\s*\)\s*"), ", "),
    (re.compile(r"\s*/\s*"), " or "),              # "wash/diluent" -> "wash or diluent"
]

# After the substitutions above, tidy the seams so the TTS is not handed doubled commas
# or floating spaces before punctuation.
_CLEANUP = [
    (re.compile(r"\s+"), " "),
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
    (re.compile(r",\s*,+"), ","),
    (re.compile(r",\s*([.!?])"), r"\1"),
    # A parenthesis turned into a comma can land right after a sentence end
    # ("...for? (e.g. ...)" -> "...for?, for example..."). The comma is noise there: the
    # question mark already tells the model to pause.
    (re.compile(r"([.!?;:]),"), r"\1"),
]


def to_speakable(text: str) -> str:
    """Rewrite `text` into something a TTS model can pronounce.

    Never call this on text that will be DISPLAYED. Its whole purpose is to diverge
    from the written form.
    """
    if not text:
        return text
    out = text
    for rx, repl in _RULES:
        out = rx.sub(repl, out)
    for rx, repl in _CLEANUP:
        out = rx.sub(repl, out)
    return out.strip(" ,")
