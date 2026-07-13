# Feature Tour and Verification Guide

*Created: 2026-07-13 00:50 +08 (SGT).*

This is an operator-facing tour of what lab-assistant does beyond a basic voice agent,
and how to check that each piece actually works on the deployed system, not just in a
test. It is written for someone testing the product, not reading the code; for exact
WebSocket message shapes and implementation detail, see `doc/INTEGRATION.md`.

## What "beyond a basic voice agent" means here

The basic pipeline is browser voice activity detection into FunASR-Nano (speech to
text) into Claude Haiku (the reply) into gepard-1.0 (text to speech), the same shape
as any voice assistant demo. Everything below this line exists because a lab bench is
not a living room: the operator's hands are busy with a pipette or a plate, so the
assistant is the only free interface; a misheard word can trigger a physical action
that cannot be undone, so the system has to know when NOT to act; and the microphone
hears the whole room, not just the person addressing it, so it has to tell a command
from a stray sentence. The features below are the answer to those three constraints:
confidence-aware gating on top of the ASR, a confirm/execute handshake before any
physical action, and a set of "who and what is this speech actually for" checks
layered around the basic pipeline.

## ASR per-token confidence

**What it does**: every transcript the assistant produces carries a confidence score,
not just the words.

**How it works**: the ASR service wraps the Fun-ASR-Nano decoder's own token
generation step to capture per-token log-probabilities, and reports a summary
(`prob_mean`, `prob_min`, among other fields) alongside every transcript. This costs
no extra model call and no extra latency; it reads numbers the decoder was already
computing. Toggle with `FUNASR_CONFIDENCE` (on by default); everything downstream
degrades gracefully to "no confidence" if it is off.

**How to verify**: scenario **F2** in `scripts/verify_features.py` inspects the
transcript from an earlier clean-speech turn and checks a real, in-range confidence
score is attached to it.

## Confidence x severity command gate with spoken readback

**What it does**: before any physical action runs, the system decides whether to run
it immediately, ask the operator to confirm it out loud, or refuse it outright, based
on how clearly it heard the command and how bad it would be to get that command wrong.

**How it works**: every lab command in the demo catalog (read a sensor, set
temperature, start or stop the stirrer, dispense, add a reagent, run the centrifuge)
is tagged with a severity: SAFE, REVERSIBLE, IRREVERSIBLE, or HAZARDOUS. The gate
fuses that tag with the turn's ASR confidence: SAFE and REVERSIBLE commands proceed by
default and only ask for confirmation when confidence drops below `LA_CONF_VERYLOW`
(default `0.50`, SAFE) or `LA_CONF_LOW` (default `0.75`, REVERSIBLE); IRREVERSIBLE and
HAZARDOUS commands always ask for confirmation, and refuse outright below
`LA_CONF_VERYLOW` rather than execute on a guess. When a confirmation is needed, the
assistant reads the command back digit by digit before asking ("dispense five zero,
that is 50, microliters into well A three"), so a transcription error is audible before
anything happens, not just written on screen.

**How to verify**: scenario **F3** speaks a clear SAFE command and checks it runs with
no confirmation step; scenario **F4** speaks a clear IRREVERSIBLE command (dispense)
and checks it asks for confirmation with a spoken, digit-by-digit readback before
running.

## Intent-bound confirmation

**What it does**: confirming an irreversible or hazardous command requires naming that
specific command, not just saying "yes" or a bare "confirm".

**How it works**: an IRREVERSIBLE or HAZARDOUS pending command only executes on the
exact phrase "confirm `<keyword>`" (for example, "confirm dispense"), matched
case- and punctuation-insensitively. A bare "confirm", a loose "yes", or even "yes
dispense" (the wrong shape) does not execute the command: it re-prompts with the exact
phrase required, and the pending command stays armed rather than firing or being
dropped. This closes an accidental-bystander gap: someone nearby saying "yes" about
something else entirely can no longer fire a physical action that happened to be
pending.

**Important boundary**: this is deliberately **not authentication**. It verifies WHICH
command is being confirmed, not WHO is confirming it. Anyone within earshot who knows
the keyword can still say the bound phrase and confirm the command; a genuinely
different speaker overhearing and confirming a pending command is only partially
addressed today, and fully solving it needs a real speaker-verification layer that
does not exist yet (see the honest status table below). Connecting this gate to real,
non-stub lab hardware should treat that speaker-gate layer as a precondition, not a
nice-to-have.

**How to verify**: scenario **F4** (above) also checks that a bare "yes" against a
pending dispense command does NOT execute it, and that "confirm dispense" does.

## Re-prompts and expiry

**What it does**: a confirmation that was not clearly heard, or that arrives too long
after it was asked for, is never silently acted on and never silently dropped, it is
re-asked.

**How it works**: two things gate execution of an otherwise-correct confirmation, both
resulting in a re-prompt that keeps the pending command armed rather than executing or
cancelling it: a confirmation with no ASR confidence reading at all (decision
`reprompt_noconf`), and a confirmation whose confidence (`prob_mean`) falls below
`LA_CONFIRM_FLOOR` (default `0.40`; `0` disables the check). Separately, a pending
command older than `LA_PENDING_TTL_S` (default `120` seconds; `0` disables) expires:
if the operator then tries to confirm or cancel it, they hear an explicit "that
request expired, please repeat the command" instead of the command silently firing; if
they say something unrelated instead, the stale pending is dropped quietly and their
actual request is answered normally, so a genuinely new question is never swallowed by
an old command's expiry notice.

**How to verify**: scenario **F10** exercises the expiry path and checks a stale
confirmation is refused rather than executed. Run `scripts/verify_features.py --slow`
to include it, since it waits out the full `LA_PENDING_TTL_S` (about two real minutes
at the default); without `--slow`, F10 is skipped rather than run.

## Cancel and the fast-path stop

**What it does**: saying "cancel" or "stop" always works, at any confidence level,
with no gate of its own.

**How it works**: cancel is deliberately never confidence-gated; a spurious
low-confidence cancel only costs re-issuing the original command (cheap and
reversible), so there is no reason to make it harder to say than a confirmation.
Separately, a short, standalone stop-like word ("stop", "halt", "abort", "emergency
stop") is matched before the reply pipeline even reaches the language model, at the
moment speech ends, and immediately cancels any reply in progress and halts the
demo lab state. A command that merely contains a stop-sounding word, like "stop the
stirrer", is not treated as a bare stop; it is a real command routed through the
normal gate. This is a supervisory stop layered over whatever hardware emergency-stop
a real lab deployment has, not a replacement for one.

**How to verify**: scenario **F5** arms a pending command and checks a plain "cancel"
clears it without executing; scenario **F6** arms a pending command and checks a bare
"stop" halts it immediately, without waiting for the turn to end.

## Proactive announcements with priority arbitration

**What it does**: the assistant can speak up on its own, not just in reply to
something said to it, when the lab itself produces an event (a run finishing, a
temperature reached), and it does so without garbling whatever it might already be
saying.

**How it works**: an announcement is either an `alert` or an `info`. An `alert`
preempts: it interrupts an in-flight reply (or clears a pending confirmation), plays a
short two-tone earcon, and then speaks, so it is never buried under something less
urgent. An `info` defers: it waits until the assistant has fallen quiet and then
speaks, never interrupting. Announcements come from two sources: the demo automation
stub's own timers (a centrifuge run completing, a target temperature reached) and an
operator-triggered announcement. Toggle with `LA_EVENTS` (on by default).

**How to verify**: scenario **F7** runs a timed command (setting the temperature) and
checks the stub's completion timer delivers the full announcement (an `info`) a few
seconds later. The preempt-vs-defer arbitration itself is not exercised by the script;
verify it live by triggering an announcement while the assistant is mid-reply (see THE
PHONE CHECKLIST below).

## Protocol walkthrough with timed steps

**What it does**: the assistant can walk an operator through a written lab protocol
hands-free, reading back each step verbatim, tracking where the operator is, and
announcing on its own when a timed step (an incubation, for example) finishes.

**How it works**: navigating a protocol ("start the protocol", "next step", "go back",
"repeat that", "what step am I on") never touches hardware, so it always proceeds
without a confirmation (it still confirms at very low confidence, like any command).
The assistant is instructed to read the step's text back exactly as written, never
inventing, reordering, or skipping a step. A timed step schedules a proactive
announcement (above) for when its wait ends; navigating away from a step, or halting,
cancels that step's timer, so an abandoned step never announces. `LA_PROTOCOL_TIMER_SCALE`
(default `1.0`) compresses only the actual wait for a demo, never the spoken duration:
the assistant always says the real number of minutes, even if the wait itself is sped
up.

**How to verify**: scenario **F8** starts the demo protocol, steps forward, and checks
the tracked step number, without waiting for the timed step's completion announcement
(that half is covered by F7's timer check above).

## Noise gate

**What it does**: background noise that the speech recognizer mis-hears as fluent
words is caught and dropped before it can be treated as a command or a chat message,
without ever dropping a real, quiet utterance.

**How it works**: two independent checks, calibrated against 29 hand-labeled real
audio clips: a confidence floor (`LA_CONF_FLOOR`, default `0.40`; `0` disables it)
below which a transcript's `prob_mean` is treated as noise, and a separate check for
runaway, repetitive text ("to the to the to the...") that can fool the confidence
score into looking confident. A pending command's own confirm or cancel word, and a
would-halt stop, are always exempt from this gate, so a quiet safety word is never
mistaken for noise. A dropped segment is still shown in the transcript, greyed out, so
the operator can see what was heard and ignored, rather than disappearing silently.

**How to verify**: scenario **F9** feeds the live stack real white noise and reports
what happened (rejected, or the ASR heard nothing, or, rarely, a hallucinated
transcript slipped through): this is informational (`INFO`, never a pass/fail), since
whether real noise is rejected is itself the open question being checked. Every other
scenario's spoken commands are clean speech that passes through normally, so the
noise-gate exemptions for control words are already exercised in scenarios F5 and F6.

## Addressed-speech detection (off by default, no script scenario)

**What it does**: on an open mic in a lab, the assistant also hears colleagues talking
to each other, not just to it. This feature classifies every transcribed segment as
addressed to the assistant or overheard side speech, so side speech never triggers a
reply or a physical command.

**How it works**: most utterances are decided with no model call at all, by
deterministic rules (a pending confirm/cancel, a stop word, the assistant addressed by
name, a bare filler); the genuinely ambiguous rest gets one bounded Claude Haiku call.
It fails OPEN on any error or timeout, meaning the worst case is one unnecessary reply,
never a dropped turn. It is off by default (`LA_ADDRESSED`) because it has not yet
been tuned against real background chatter, and is the one feature here that can
discard real user speech if it gets it wrong.

**How to verify (manual only)**: set `LA_ADDRESSED=1` and restart the orchestrator,
then have a companion talk near the microphone without addressing the assistant by
name or topic; confirm their speech shows up greyed as "ignored: side speech" and
draws no reply, while normal speech to the assistant still works.

## Segment capture and labeling (internal testing tool)

**What it does**: an opt-in mode that saves every spoken segment as an audio clip plus
a record of how it was scored, and lets a tester tag a clip as noise, another speaker,
or real speech, building the exact calibration data the noise gate above was tuned
against.

**How it works**: enabled with `LA_CAPTURE` (off by default: not one byte is written
to disk when it is off). Every uploaded segment is saved under `LA_CAPTURE_DIR` as a
WAV file plus a line in an append-only log. In the browser, capture mode shows a small
numbered chip under each turn, one per recorded segment, for tagging it. Run
`python3 scripts/capture_report.py` to see the label breakdown and the confidence
numbers per label, the report that produced the noise gate's threshold.

**Consent posture**: this mode is for internal testing with the testers' knowledge and
consent only. It deliberately ships with no on-page recording indicator; the labeling
chips are the only visible sign anything unusual is happening. This should not be used
with anyone who has not separately agreed to it, and an explicit on-screen indicator
should be added before it ever is.

## Per-user hints

**What it does**: each user gets their own ASR hotwords and text-replacement rules,
instead of one shared set that any user could silently change for everyone else.

**How it works**: hints (domain hotwords that bias what the speech recognizer expects,
and after-the-fact text corrections) are stored one file per user, keyed off the
verified identity described below, with no cross-user access even for the operator
account, since hints rewrite what a user hears the assistant respond to. A user with
no saved hints yet gets sensible defaults (the hotword "Claude" and a "cloud code" to
"Claude Code" correction).

**How to verify**: scenario **F11** sets a replacement rule and confirms `get_hints`
reads back the same value (persisting it under this connection's own scope), then
restores the original hints afterward so the run leaves nothing behind. Per-user
isolation itself, that a different connection does not see this change, is not
exercised by this scenario; verify it manually with two separate browser sessions
(or profiles) if needed.

## Multi-user isolation, the operator view, and the email gate

**What it does**: the app opens behind an email gate: enter an address to get in, and
from then on the deployed system can serve more than one person at once, with each
person only ever seeing their own conversation history; a designated operator account
sees everyone's.

**How it works**: on load, a full-screen gate asks for an email before anything else
is usable; the browser sends it to the server on every connection, and it becomes
that connection's identity. `LA_ALLOWLIST` (a server-side, comma-separated list) can
restrict who is allowed to connect at all, so an address that is not on the list, or
no address, is turned away outright. An operator (an email listed in
`LA_OPERATOR_EMAILS`) gets an aggregated view across every user's session history,
each entry tagged with whose it is; anyone else only ever sees their own.

**Important boundary**: the email is self-asserted, not verified by anything (no
login, no OTP). This gates casual access and keeps each user's history private from
every other user, but it is not authentication: anyone who knows or guesses an
allowlisted address can connect as that identity, and the operator address in
particular functions as a shared secret rather than a login. See
`doc/MULTI_CLIENT.md` for the full identity, scoping, and trust model.

## iOS client work (device-only, no script scenario)

**What it does**: makes the whole experience actually work on an iPhone, where several
things silently failed before.

**How it works**: the microphone now captures at whatever rate the device's hardware
actually offers (iOS refuses a requested 16 kHz context) and resamples to the wire
format in the browser itself. All assistant audio, replies and announcements alike,
now plays through one shared, gesture-unlocked audio context, because the microphone
permission prompt does not also unlock audio playback on iOS the way it does on
desktop; a playback failure now shows up in the status line instead of failing
silently. Capture mode's per-segment labeling chips, the intent-bound confirmation
phrase in the pending strip, and the alert earcon (now guarded against a barge-in race
mid-beep) all needed their own device-specific fixes to work reliably on a phone.

**How to verify**: device-only, see THE PHONE CHECKLIST below; there is no script
scenario for this category, since it is inherently about real hardware and a real
speaker/microphone.

## Verifying everything

### The script

Prerequisites (the script checks all three at startup and fails fast with
instructions if any is down): the orchestrator reachable at `http://localhost:8765`,
and the GPU host's gepard TTS (`:8040`) and FunASR ASR (`:8030`) reachable through the
SSH tunnel (`deploy/dev-forward.sh`), whether run by hand or via the corresponding
LaunchAgents installed by `deploy/install-mac-agents.sh`.

Run:

```
venv-web/bin/python scripts/verify_features.py
```

(the system Python works too, as long as the `websockets` package is importable).

Add `--slow` to also run the pending-expiry scenario (F10), which waits out the full
`LA_PENDING_TTL_S` (about two minutes at the default); without `--slow`, F10 is
skipped entirely and prints `SKIP` with a reason, it does not run with a shortened
TTL.

The script speaks to the LIVE deployed stack with REAL synthesized speech: gepard
voices each test command, and the real FunASR-Nano service transcribes it back, the
same path an actual operator's voice takes. A pass means the deployed system works
end to end, not that a mock objects agree with each other.

Output is one line per scenario, `FEAT <id> <label>: PASS|FAIL|INFO|SKIP` plus a
one-line detail, followed by a summary line, `VERIFY: PASS (n/m deterministic
scenarios)` (or `FAIL`); the run exits 0 only if every deterministic scenario
(excluding `INFO` and `SKIP` ones) passed. As of this writing the harness has not yet
completed a full run against the live stack (the prereq checks and pure audio helpers
are verified; the scenarios themselves are not yet exercised end to end), so treat its
first successful full run as its own acceptance test.

### THE PHONE CHECKLIST (device-only)

This is the one part of this document that cannot be scripted, and it is the
outstanding acceptance gate for everything client-side above: code-complete, but not
yet exercised on a real phone by the operator. Walk through this on an iPhone against
the live deployed page:

- On first load, enter an allowlisted email at the gate and confirm it lets you in
  (and, separately, that a non-allowlisted or blank email is turned away with a clear
  reason); confirm "Sign out" clears the stored email and shows the gate again.
- Tap Start: the mic comes on and the status line reports the real hardware sample
  rate (for example "listening (48000 Hz -> 16000)"), not a silent failure.
- Speak a question and confirm the reply is actually audible, not silent.
- Barge in while the assistant is speaking: the audio cuts immediately.
- Trigger an alert-severity announcement (or wait for a stub timer): confirm the
  earcon plays, then the announcement; then trigger one again and barge in during the
  earcon itself, confirming it leaves silence rather than still speaking over you.
- With `LA_CAPTURE` on, confirm the per-segment chips appear under a turn and can be
  tapped to label a clip.
- Say something that triggers the noise gate or side speech a few times in a row and
  confirm consecutive drops collapse into one small, muted "N ignored (reason x
  count)" line rather than one row per drop; tap it to expand the individual dropped
  segments, each still labelable in capture mode.
- Trigger a pending confirmation on an irreversible command and confirm the pending
  strip shows the exact required phrase (for example "confirm dispense"), not a
  generic "say confirm".
- Sanity-check the theme toggle and the History panel (rename, delete, viewing a past
  session) still work as expected on a phone screen.

## Honest status table

| Feature | Verified by | Outstanding |
|---|---|---|
| ASR per-token confidence | `verify_features.py` F2 | none known |
| Command gate + spoken readback | `verify_features.py` F3, F4 | none known |
| Intent-bound confirmation | `verify_features.py` F4 | mitigates, does not solve, an `other_speaker` bystander confirming a pending command; a real speaker-verification layer is the full fix and stays open, and is a precondition for real hardware integration |
| Re-prompts and expiry | `verify_features.py` F10 (requires `--slow`, skipped otherwise) | none known |
| Cancel and fast-path stop | `verify_features.py` F5, F6 | supervisory stop only, never a substitute for a real hardware e-stop |
| Proactive announcements | `verify_features.py` F7 (timer completion only) | preempt-vs-defer arbitration itself is manual-only, see THE PHONE CHECKLIST |
| Protocol walkthrough | `verify_features.py` F8 (step tracking only) | timed-step announcement is covered by F7, not F8 itself |
| Noise gate | `verify_features.py` F9 (reports `INFO`, not pass/fail, since real noise behavior is what is being observed) | thresholds calibrated on 29 clips; will benefit from a larger labeled set over time |
| Addressed-speech detection | manual only (see above) | off by default; not yet tuned against real background chatter, so left opt-in |
| Segment capture + labeling | `scripts/capture_report.py` | internal testing tool, not a user feature; no on-page recording indicator by deliberate design |
| Per-user hints | `verify_features.py` F11 (persistence only) | cross-connection isolation itself is manual-only |
| Multi-user isolation + operator view + email gate | THE PHONE CHECKLIST above; see `doc/MULTI_CLIENT.md` | the email is self-asserted, not verified (no login, no OTP); it gates access and scopes history, but is not authentication |
| iOS client work | THE PHONE CHECKLIST above | **device pass outstanding**: code-complete, not yet exercised live on a phone by the operator |
| `LAB-SMOKE i` (protocol timer-disarm test flake) | n/a | dev-side test flakiness only (measured roughly 50% failure rate in isolation), not a product defect; needs a deterministic rework of that one test |
