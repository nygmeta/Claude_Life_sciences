# Findings in the Lab Agent (backend half)

*Created: 2026-07-13 19:39 +08 (SGT).*

Issues found in the Lab Agent backend (`app/`, the Lab Agent API) while wiring and testing the integration seam from the voice half.

**Status: NOTHING HERE HAS BEEN CHANGED.** These are files owned by the backend side, so nothing has been modified without agreement. This doc exists so the findings are precise, evidenced, and ready for the backend's owner to accept, reject, or fix. No patch will be applied without agreement from that side.

Each item states what was measured (not what was assumed), why it matters for a VOICE front end specifically, and a proposed fix the backend side can take or leave.

---

## P0. The slot regexes were written for typed text, and real ASR does not write like that

*Found 2026-07-13 evening, running the full real stack (real Fun-ASR-Nano, real gepard TTS, the backend's real Claude planner) on localhost. The mock-based integration test could never have found this: it handed the backend the exact string the test had typed.*

**Where:** `app/agent/clarify.py`.

```python
_ANALYTE_RE = re.compile(r"\b(IL-?6|IL-?8|TNF-?alpha|TNF|CRP)\b", re.IGNORECASE)
_VOLUME_RE  = re.compile(r"(\d{1,4})\s*(?:microliters?|...)\b", re.IGNORECASE)
```

`IL-?6` matches `IL6` and `IL-6`. It does **not** match `IL 6`, with a space. And `_VOLUME_RE` requires **digits**.

**What the real recognizer actually produced**, speaking the exact demo lines:

| spoken | Fun-ASR-Nano transcribed |
|---|---|
| "IL-6 with 24 samples" | "IL **6** with 24 samples" (space, no hyphen) |
| "make it **100** microliters" | "make it **hundred** microliters" (a word, not digits) |

**Consequence:** the analyte slot never filled. The backend re-asked *"Which analyte should I run the ELISA for?"* on every single turn and the conversation **deadlocked**: it never reached `awaiting_confirmation`, so it could never be confirmed, so nothing could ever run. The demo does not merely misbehave, it cannot complete.

**Mitigated (on the voice side, already landed, no action needed from the backend side).** Producing a canonical transcript is the speech layer's job: a planner should not have to know how an acoustic model spells things. `voice/web/lab_backend.py:normalize_transcript()` now rewrites `IL 6` to `IL-6` and spoken numbers to digits before the transcript is POSTed, and it is unit tested (including that it can never turn a cancel into a confirm). With that in place, the real stack gets through: the planner compiles 348 operations, the validator refuses the deliberately-unsafe 400 uL, and the assistant speaks the refusal.

**The residual risk remains on the backend side, and closing it is worth about 10 minutes of work.** The normalizer patches the two cases we measured. It cannot patch the general problem, which is that **slot-filling a domain acronym out of speech by regex is brittle**. Observed on the real stack, all for the same spoken word:

    "IL-6"  ->  "IL 6"      (fixed by the normalizer)
    "IL-6"  ->  "IELTS 6"   (not fixable by any normalizer)
    "IL-6"  ->  "Stoyl 6"   (not fixable by any normalizer)

(Those last two are partly an artefact of our test rig: the "speaker" was gepard TTS, which pronounces the acronym badly. A human will do better. But it shows how narrow the regex's target is, and a scientist in a noisy room is not a clean signal either.)

**Proposed fix:** the Lab Agent already has Claude in the loop. `_handle_new_request` uses the planner (Claude, tool-forced), but `_handle_followup` parses the clarification answer with **regex** (`clarify.parse_answers`). Letting Claude merge the clarification too, with the plan and the missing fields as context, would resolve "IELTS 6" to IL-6 the way a human colleague would, because the question just asked was "which analyte?". That is a small change to one function and it removes an entire class of demo-breaking failure.

---

### Update, 2026-07-14 01:10 +08: the whack-a-mole is now measured, and it does not end

Four distinct spellings of ONE spoken "IL-6" have now been observed from the real recognizer, three of them in a single live session where the operator repeated himself:

    "IL 6"      (space, no hyphen)
    "IELTS 6"   (the letters heard as a word)
    "I L SIX"   (letters apart, and the DIGIT as a word)
    "i am six"  (the letters heard as different words entirely)

Each one needed a new patch to `voice/web/lab_backend.py:normalize_transcript()`. The normalizer now also accepts "interleukin six", which is worth knowing operationally: one real word beats two spelled letters, for the speaker AND for the recognizer, so on stage that is the phrase to say. But the pattern is clear. Two spelled letters have no lexical anchor, so the acoustic model reaches for the nearest English words, and there is no finite list of the words it might reach for. **A regex cannot win this.**

**Why an LLM is the right tool here, and why a NEW one is not needed.** The model is already in the system, one function away from the bug. Claude plans the opening request; a regex then handles the answer to the question that Claude itself just asked. That asymmetry IS the bug. Claude, holding the plan, the `missing_fields`, and the question "which analyte should I run the ELISA for? (e.g. IL-6)", resolves "i am six" the way any colleague would. That is not clever prompting, it is context a regex structurally cannot have.

Cost is not the objection: the planner call already measured at **5.9 s** (Sonnet) on the opening turn. A clarification merge is a smaller call, happens once or twice per conversation, and is nowhere near the confirmation path, so it adds nothing to the latency of the moment that matters.

**Where the LLM must NOT go, and this is a hard line.** Not into the voice half's transcript path. That normalizer sits in front of the confirmation gate: EVERY lab utterance passes through it, including "confirm" and "cancel". It carries a test asserting it can never turn a cancel into a confirm, and that guarantee exists only because the thing is deterministic. A model that can helpfully repair "i am six" into "IL-6" is a model that can helpfully repair a garbled "cancel" into something else, and then a language model sits between a scientist saying stop and a centrifuge spinning. No amount of prompting makes that testable.

The line to hold is the one the Lab Agent's own README already draws ("Claude resolves intent; the systems of record resolve fact"):

    deterministic code owns CONTROL utterances (confirm, cancel, stop)
    the LLM owns SLOT FILLING (which analyte, how many samples, what volume)

The follow-up merge is on the intent side of that line. It belongs to Claude.

**Deliberately NOT built (2026-07-14).** A bounded LLM slot-repair on the VOICE side was considered as insurance and rejected for now: fire a small model only while the backend is `gathering`, only to fill the slot being asked about, and never on a turn that could be a confirm or a cancel. It would be safe and it would work, but it is a compensating layer in the wrong architectural place, and it would let the real fix rot. Recorded here so the option is on the table if the change is declined, not so that someone builds it by default.

If the preference is to keep it deterministic, the cheaper version is to widen the regexes: `IL[-\s]?6`, and accept number words as well as digits.

---

## P1. The confirm/cancel matcher does substring containment, so "yes, go ahead now" CANCELS

**Where:** `app/main.py`, `_AFFIRMATIVE` / `_NEGATIVE` and `_handle_confirmation`.

```python
_AFFIRMATIVE = {"yes", "confirm", "confirmed", "go", "proceed", "do it", "run it", "correct"}
_NEGATIVE    = {"no", "cancel", "stop", "abort", "wait"}

if any(w in t for w in _NEGATIVE):        # checked FIRST
    ... cancel
if not any(w in t for w in _AFFIRMATIVE):
    ... treat as a correction
... execute
```

The membership test is `w in t` on the raw string, so it matches **inside words**. `"no"` is inside `"now"`. `"go"` is inside `"good"`.

**Measured**, by running the backend's own two sets against utterances a scientist would plausibly speak (negatives are evaluated first, so they win):

| utterance | classified as | correct? |
|---|---|---|
| `yes` | EXECUTE | yes |
| `yes, go ahead` | EXECUTE | yes |
| **`yes, go ahead now`** | **CANCEL** | **NO** |
| **`yep, do it now`** | **CANCEL** | **NO** |
| **`yeah that's right, run it now`** | **CANCEL** | **NO** |
| **`go ahead now`** | **CANCEL** | **NO** |
| `confirmed, run it` | EXECUTE | yes |
| `sounds good` | EXECUTE | (only because "go" is inside "good") |
| `no, cancel that` | CANCEL | yes |
| `cancel` | CANCEL | yes |

**4 of 12** natural confirmations are misclassified. Every failure is caused by the word **"now"**, which is close to the most natural thing a person says when confirming out loud. Typed into a console, nobody writes "now"; spoken, everybody says it.

**Why it matters here and not before:** this is a *text-matching* bug, not a mishearing one, so nothing on the voice side protects against it. The ASR confidence floor cannot help: the transcript is perfectly correct, and it is the matcher that misreads it.

**Two directions of failure:**
- **False CANCEL** (the demo-breaker): the scientist confirms, the system abandons the
  protocol. It fails safe, but on camera it looks broken.
- **False EXECUTE** (the safety one): any utterance containing the substring `"go"`
  reads as an affirmative. `"sounds good"`, `"let me go grab it"`, `"good, that's what
  I forgot"` all contain it. In a room where the mic hears everything, an offhand
  remark can look like sign-off. (The voice half's addressed-speech gate and confidence
  floor reduce the exposure, but they are not designed to be the last line of defense
  against a matcher that says "good" means "go".)

**Proposed fix:** match on whole words, not substrings, and check the affirmative intent explicitly rather than by exclusion. The voice half already has exactly this, tested, in `voice/web/lab_gate.py`:

```python
_CONFIRM_LOOSE_RE = re.compile(
    r"\b(confirm|confirmed|yes|yeah|yep|yup|sure|affirmative|go ahead|do it|proceed)\b", re.I)
```

`is_confirm()` / `is_cancel()` there are word-boundary matchers and are already unit tested. The backend side can import them, copy them, or write an independent version: the point is the word boundary. A one-line version of the fix is to change `w in t` to a regex with `\b` around each keyword.

**Nice-to-have on top:** the voice half binds an irreversible confirmation to the *intent* (`"confirm centrifuge"`, not a bare `"yes"`), so a stray affirmative cannot fire the wrong machine. The backend's gate accepts any loose affirmative. That is a reasonable choice for a typed console and a weaker one for a microphone in a shared room. Worth a conversation, not a unilateral change: see P2.

---

## P2. A bare "yes" is enough to execute, with no binding to what is being confirmed

**Where:** `app/main.py`, `_handle_confirmation`.

Any affirmative in `_AFFIRMATIVE` confirms whatever plan happens to be armed. There is no check that the user was answering *this* question.

**Why it matters for voice:** the microphone hears the whole room. A "yes" spoken to a colleague, to a phone call, or to a different question can arrive while a protocol is armed. The voice half already mitigates this (addressed-speech classification, a confidence floor on the confirming utterance, and it refuses to forward a confirmation it did not hear clearly), but those reduce the probability rather than remove the class.

**Proposed fix (backend's call):** for a high-severity plan, require an intent-bound phrase, the way the voice half does: read back `"say confirm ELISA to proceed, or cancel"` and accept only that. This is a UX change to the backend's state machine, so the decision belongs entirely to that side. Flagging it, not pushing it.

---

## P3. The API contract has nowhere to put ASR confidence

**Where:** `app/models/session.py`, `MessageRequest`.

```python
class MessageRequest(BaseModel):
    transcript: str
    session_id: Optional[str] = None
```

The backend implicitly trusts the transcript, which is correct for a typed console and is the assumption the voice half has to compensate for. Today the voice half enforces the confidence floor on its own side and simply does not forward an utterance it did not hear well enough (see `voice/web/lab_backend.py:blocks_confirmation`), so the backend never sees a misheard confirmation. That works and needs nothing from the backend side.

**Optional enhancement, only if the backend is meant to reason about it:** add an optional field, and the voice half will populate it.

```python
class MessageRequest(BaseModel):
    transcript: str
    session_id: Optional[str] = None
    confidence: Optional[float] = None   # ASR mean confidence, 0..1, None if unknown
```

Then the validator could, for example, treat a low-confidence transcript as a reason to re-ask rather than to plan. Purely additive, and the seam works fine without it. Not a bug, listed so the option is on the table before the contract hardens.

---

## P4. Pre-deployment items (not demo blockers)

Neither of these affects the demo. Recording them so they are not forgotten.

- **CORS is wide open.** `app/main.py` sets `allow_origins=["*"]`, which the code's own
  comment already flags ("Fine for a hackathon / local demo; tighten allow_origins
  before any real deployment"). Agreed, and no action needed for the demo.
- **Sessions are an unbounded in-process dict.** `SESSIONS: dict[str, Session] = {}` has
  no expiry and no cap, so a long-running server accumulates every session forever, and
  all state is lost on restart. Fine for a demo; a real deployment wants a TTL or a
  store.

---

## What the voice side does NOT need from the backend side

Recorded so the ask stays small and the backend side is not sent on unnecessary work. The seam is already green end to end (7/7 scenarios, `voice/scripts/smoke_integration.py`) against the backend's code **exactly as it is today**:

- The Lab Agent API is unchanged and needs no changes for the integration to work.
- The voice half adapts to the backend's contract (transcript in, `reply` out), pins
  one backend session per voice session, and keeps speech-side safety on its own side.
- P1 is the only item that will visibly bite during a recorded demo. Everything else is
  a design conversation or a post-hackathon cleanup.

## Suggested order of business

1. **P1**, before recording. It is a small, contained fix (word boundaries), and without
   it a natural spoken confirmation cancels the protocol on camera.
2. **P2**, a short conversation: is an intent-bound confirmation wanted for voice?
3. **P3** and **P4** are optional and can wait.
