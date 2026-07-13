"""Lab-command gate: the pure, self-contained decision layer between a spoken
command and a physical lab action.

Everything here is import-free of the WebSocket / orchestrator, so it is unit
testable in isolation and is the one seam the real lab-automation system slots
into (replace AutomationStub with the real driver, keep the same async execute /
halt / busy surface). It holds:

  - COMMANDS: the demo command catalog with per-command arg schema + severity.
  - gate(): the confidence x severity decision (proceed / confirm / confirm_strict
    / reject).
  - readback(): a human-hearable, digit-by-digit grounded readback for TTS.
  - is_confirm / is_cancel / is_stop: robust spoken-intent matchers.
  - AutomationStub: an in-memory lab with deterministic canned behaviour.

No em dashes anywhere (house rule).
"""
import asyncio
import os
import re

# Severity tiers, ascending risk. The gate maps (severity, confidence) to an action.
SAFE = "safe"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"
HAZARDOUS = "hazardous"

# The demo command catalog. `args` maps each argument name to a coarse type tag
# (documentation + light validation only; the LLM fills them). abort_run() is NOT
# a tool: it is the fast-path stop, handled off-band as a control utterance.
#
# `keyword` is the one spoken word that BINDS a confirmation to this intent (fix
# F1): an IRREVERSIBLE / HAZARDOUS pending only executes on "confirm <keyword>", so
# a stray "yes" cannot fire the wrong action. SAFE / REVERSIBLE entries carry a
# keyword for uniformity, but it is unused (those pendings keep loose confirmation).
COMMANDS = {
    "read_sensor": {"severity": SAFE, "keyword": "sensor", "args": {"sensor": "str"}},
    "stop_stirrer": {"severity": SAFE, "keyword": "stirrer", "args": {}},
    "set_temperature": {"severity": REVERSIBLE, "keyword": "temperature", "args": {"celsius": "float"}},
    "start_stirrer": {"severity": REVERSIBLE, "keyword": "stirrer", "args": {"rpm": "int"}},
    "dispense": {"severity": IRREVERSIBLE, "keyword": "dispense",
                 "args": {"volume_ul": "float", "well": "str"}},
    "add_reagent": {"severity": IRREVERSIBLE, "keyword": "reagent",
                    "args": {"reagent": "str", "volume_ul": "float", "target": "str"}},
    "start_centrifuge": {"severity": HAZARDOUS, "keyword": "centrifuge",
                         "args": {"rpm": "int", "minutes": "float"}},
    # Protocol walkthrough (Phase 4). All SAFE: navigating a written protocol moves
    # no hardware, so it proceeds without confirmation (the gate still confirms at
    # very low ASR confidence, like any SAFE command).
    "protocol_start": {"severity": SAFE, "keyword": "protocol", "args": {"name": "str"}},  # name optional
    "protocol_next": {"severity": SAFE, "keyword": "protocol", "args": {}},
    "protocol_back": {"severity": SAFE, "keyword": "protocol", "args": {}},
    "protocol_repeat": {"severity": SAFE, "keyword": "protocol", "args": {}},
    "protocol_status": {"severity": SAFE, "keyword": "protocol", "args": {}},
}

# --------------------------------------------------------------------- protocol
# A hardcoded demo protocol: a plasmid miniprep. Each step is
# {n, text (1-2 spoken sentences), timer_s (optional)}. A step that has a timer
# MENTIONS that timer in its spoken text, so the readback and the later completion
# announcement agree with what the operator heard.
PROTOCOL_NAME = "plasmid miniprep"
PROTOCOL_STEPS = [
    {"n": 1, "text": "Resuspend the cell pellet in 250 microliters of resuspension "
                     "buffer. Vortex until the suspension is completely uniform."},
    {"n": 2, "text": "Add 250 microliters of lysis buffer and invert the tube six "
                     "times. Do not vortex, and incubate for 5 minutes.",
     "timer_s": 300, "timer_label": "incubation"},
    {"n": 3, "text": "Add 350 microliters of neutralization buffer and invert "
                     "immediately until a white precipitate forms."},
    {"n": 4, "text": "Centrifuge at 13000 r p m for 10 minutes to pellet the cell "
                     "debris.",
     "timer_s": 600, "timer_label": "spin"},
    {"n": 5, "text": "Transfer the clear supernatant to the spin column, then wash "
                     "with 750 microliters of wash buffer."},
    {"n": 6, "text": "Elute the plasmid DNA with 50 microliters of elution buffer "
                     "into a clean tube."},
]
PROTOCOL_TOTAL = len(PROTOCOL_STEPS)


def protocol_step(n):
    """The step dict for 1-based step number `n`, or None if out of range."""
    if isinstance(n, int) and 1 <= n <= PROTOCOL_TOTAL:
        return PROTOCOL_STEPS[n - 1]
    return None


def step_timer_s(n):
    """The wall-clock seconds a step's timer should run, or None when the step has
    no timer. LA_PROTOCOL_TIMER_SCALE (default 1.0) compresses every protocol timer:
    the step TEXT always states the real duration (5 minutes), while a demo or a
    smoke can scale the actual wait down so the completion announcement is
    observable without waiting the full incubation."""
    step = protocol_step(n)
    if not step or not step.get("timer_s"):
        return None
    scale = float(os.environ.get("LA_PROTOCOL_TIMER_SCALE", "1"))
    return max(0.0, step["timer_s"] * scale)


def timer_done_text(n):
    """The spoken announcement for a finished step timer, naming the step and the
    REAL duration the operator was told to wait (never the compressed one). None for
    a step with no timer."""
    step = protocol_step(n)
    if not step or not step.get("timer_s"):
        return None
    minutes = int(step["timer_s"] // 60)
    label = step.get("timer_label") or "timer"
    return (f"Step {n} {label} complete: {minutes} minutes elapsed. "
            f"Say next when you are ready to continue.")


# The lab-assistant system-prompt paragraph appended to the base voice-assistant
# prompt when LA_LAB_MODE is on (the server owns the concatenation).
LAB_SYSTEM_SUFFIX = (
    "You are a lab bench voice assistant. For ANY physical action (reading a "
    "sensor, setting temperature, stirring, dispensing, adding a reagent, running "
    "the centrifuge, stopping the stirrer) you MUST call the lab_command tool: "
    "never perform or describe a physical action in prose. NEVER claim an action "
    "happened unless a tool result confirms it; if a tool result says confirmation "
    "is required, say the exact readback it gives you and ask the user to say "
    "confirm or cancel. "
    "You also walk the user through a written protocol. When they ask to start the "
    "protocol, to go to the next or previous step, to repeat a step, or where they "
    "are, call lab_command with the protocol_start / protocol_next / protocol_back "
    "/ protocol_repeat / protocol_status intents. Read the step text from the tool "
    "result back to the user VERBATIM: never invent, reorder, merge, or skip a "
    "protocol step, and never state a step you did not get from a tool result. "
    "Keep spoken replies to 1-2 short sentences (a protocol step counts as one)."
)


def severity_of(intent):
    """The severity tier of a command intent, or None if unknown."""
    spec = COMMANDS.get(intent)
    return spec["severity"] if spec else None


def tool_schema():
    """The single `lab_command` tool definition passed to the Claude API: an
    intent from the catalog plus a free-form args object."""
    return {
        "name": "lab_command",
        "description": (
            "Execute a physical lab-bench action. Call this for EVERY physical "
            "operation the user asks for; do not describe an action as done in "
            "prose. The gate may require the user to confirm before the action "
            "runs, so never assume it happened until the tool result says so."),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(COMMANDS.keys()),
                           "description": "which lab command to run"},
                "args": {"type": "object",
                         "description": ("arguments for the command, e.g. "
                                         "{\"volume_ul\": 50, \"well\": \"A3\"}")},
            },
            "required": ["intent", "args"],
        },
    }


# --------------------------------------------------------------------------- gate
def _conf_low():
    return float(os.environ.get("LA_CONF_LOW", "0.75"))


def _conf_verylow():
    return float(os.environ.get("LA_CONF_VERYLOW", "0.50"))


def gate(intent, args, prob_min):
    """Decide what to do with a parsed command given the turn's weakest ASR
    confidence (prob_min). Returns {action, reason} where action is one of
    proceed / confirm / confirm_strict / reject.

    prob_min is the minimum per-segment prob_min across the turn. None means the
    ASR gave no confidence: missing confidence ESCALATES, never relaxes (review fix
    F3). We cannot verify the command was heard correctly, so a physical command
    asks for confirmation rather than auto-proceeding; but we never REJECT solely
    for missing confidence, which would lock out a degraded-ASR user. Rules:
      SAFE          proceed         (confirm if prob_min < VERYLOW; proceed if None)
      REVERSIBLE    proceed         (confirm if prob_min < LOW;     confirm if None)
      IRREVERSIBLE  confirm         (reject  if prob_min < VERYLOW; confirm if None)
      HAZARDOUS     confirm_strict  (reject  if prob_min < VERYLOW; strict  if None)
    """
    if intent not in COMMANDS:
        return {"action": "reject", "reason": f"unknown command: {intent!r}"}
    severity = COMMANDS[intent]["severity"]

    if prob_min is None:
        # No confidence to judge by: escalate one notch toward caution, never
        # reject. SAFE (read-only) still proceeds; everything physical asks to
        # confirm; HAZARDOUS stays strict.
        note = " (confidence unavailable)"
        if severity == SAFE:
            return {"action": "proceed", "reason": f"safe command{note}"}
        if severity == HAZARDOUS:
            return {"action": "confirm_strict",
                    "reason": f"hazardous command requires explicit confirmation{note}"}
        return {"action": "confirm",
                "reason": f"confirmation required{note}"}

    low, verylow = _conf_low(), _conf_verylow()
    pm, note = prob_min, ""

    if severity == SAFE:
        if pm < verylow:
            return {"action": "confirm",
                    "reason": f"very low transcription confidence{note}"}
        return {"action": "proceed", "reason": f"safe command{note}"}
    if severity == REVERSIBLE:
        if pm < low:
            return {"action": "confirm",
                    "reason": f"low transcription confidence{note}"}
        return {"action": "proceed", "reason": f"reversible command{note}"}
    if severity == IRREVERSIBLE:
        if pm < verylow:
            return {"action": "reject",
                    "reason": f"transcription confidence too low for an "
                              f"irreversible command{note}"}
        return {"action": "confirm",
                "reason": f"irreversible command requires confirmation{note}"}
    # HAZARDOUS
    if pm < verylow:
        return {"action": "reject",
                "reason": f"transcription confidence too low for a hazardous "
                          f"command{note}"}
    return {"action": "confirm_strict",
            "reason": f"hazardous command requires explicit confirmation{note}"}


# ------------------------------------------------------------------ argument aliases
# The lab_command tool takes a FREE-FORM args object (no per-key schema), so Claude
# picks the arg names itself and does not always use the catalog's canonical key: it
# is nondeterministic (set_temperature has been seen emitting "temperature" AND
# "temperature_celsius" for the same request). A live audit found four commands whose
# emitted names drift from the schema (set_temperature, start_stirrer, start_centrifuge,
# add_reagent); dispense was clean. Rather than make the demo depend on the model
# guessing a key, every command reads its args through arg(), which accepts the
# canonical name OR any known alias. The COMMANDS schema keeps the canonical name.
_ARG_ALIASES = {
    "celsius": ("celsius", "temperature", "temperature_celsius", "temp", "degrees"),
    "rpm": ("rpm", "speed_rpm", "speed"),
    "minutes": ("minutes", "duration_minutes", "duration", "time_minutes"),
    "volume_ul": ("volume_ul", "volume", "microliters", "ul", "volume_microliters"),
    "well": ("well", "target", "well_id"),
    "target": ("target", "well", "destination", "target_well"),
    "reagent": ("reagent", "reagent_name", "name"),
    "sensor": ("sensor", "sensor_name", "name"),
}


def arg(args, canonical):
    """Read a command argument by its canonical name OR any known alias, returning the
    first present non-None value (or None). The LLM emits a free-form args object and
    does not reliably use the schema key, so reading through the alias set keeps a
    command working whatever reasonable name the model chose."""
    args = args or {}
    for key in _ARG_ALIASES.get(canonical, (canonical,)):
        v = args.get(key)
        if v is not None:
            return v
    return None


# ----------------------------------------------------------------------- readback
_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
# Unit tokens spoken in full so TTS never reads an abbreviation as letters.
_UNIT_WORDS = {"ul": "microliters", "c": "degrees Celsius", "celsius": "degrees Celsius",
               "rpm": "r p m", "minutes": "minutes", "min": "minutes"}


def expand_unit(unit):
    """Expand a unit abbreviation to its spoken form (ul -> microliters,
    C -> degrees Celsius, rpm -> r p m); unknown units pass through unchanged."""
    return _UNIT_WORDS.get(str(unit).strip().lower(), str(unit))


def say_digits(value):
    """Render a value character by character for careful spoken digits:
    50 -> "five zero", 37.5 -> "three seven point five", "A3" -> "A three".
    Letters are spoken as-is (uppercased), "." -> "point", "-" -> "minus"."""
    out = []
    for ch in _fmt_num(value):
        if ch in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[ch])
        elif ch == ".":
            out.append("point")
        elif ch == "-":
            out.append("minus")
        elif ch.strip():
            out.append(ch.upper())
    return " ".join(out)


def _fmt_num(value):
    """String form of a number for readback: an integral float prints without a
    trailing .0 (50.0 -> "50"), everything else prints normally."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _grounded(value):
    """A number spoken digit-by-digit and then normally, so a mis-hear is caught:
    50 -> "five zero, that is 50"."""
    return f"{say_digits(value)}, that is {_fmt_num(value)}"


def readback(intent, args):
    """A grounded, human-hearable readback of a command for TTS confirmation:
    every numeric argument is spoken digit-by-digit then normally, and every unit
    is spoken in full. Example: dispense(50, "A3") ->
    "dispense five zero, that is 50, microliters into well A three"."""
    args = args or {}
    if intent == "read_sensor":
        return f"read the {arg(args, 'sensor') or 'selected'} sensor"
    if intent == "stop_stirrer":
        return "stop the stirrer"
    if intent == "set_temperature":
        c = arg(args, "celsius")
        if c is None:                          # None guard: never read back "None degrees"
            return "set the temperature"
        return f"set the temperature to {_grounded(c)}, {expand_unit('C')}"
    if intent == "start_stirrer":
        return f"start the stirrer at {_grounded(arg(args, 'rpm'))}, {expand_unit('rpm')}"
    if intent == "dispense":
        return (f"dispense {_grounded(arg(args, 'volume_ul'))}, {expand_unit('ul')} "
                f"into well {say_digits(arg(args, 'well') or '')}")
    if intent == "add_reagent":
        return (f"add {_grounded(arg(args, 'volume_ul'))}, {expand_unit('ul')} of "
                f"{arg(args, 'reagent') or 'reagent'} to {say_digits(arg(args, 'target') or '')}")
    if intent == "start_centrifuge":
        return (f"start the centrifuge at {_grounded(arg(args, 'rpm'))}, "
                f"{expand_unit('rpm')} for {_grounded(arg(args, 'minutes'))}, "
                f"{expand_unit('minutes')}")
    if intent == "protocol_start":
        return f"start the {args.get('name') or PROTOCOL_NAME} protocol"
    if intent == "protocol_next":
        return "go to the next protocol step"
    if intent == "protocol_back":
        return "go back one protocol step"
    if intent == "protocol_repeat":
        return "repeat the current protocol step"
    if intent == "protocol_status":
        return "report where we are in the protocol"
    # unknown command: a best-effort generic readback
    parts = ", ".join(f"{k} {say_digits(v)}" for k, v in args.items())
    return f"{intent} with {parts}" if parts else str(intent)


def completion_announce(intent, args):
    """The spoken completion announcement for a command that fires a delayed event
    (centrifuge finished, target temperature reached), or None for a command with
    no such event. Alias-tolerant and None-guarded, so it never speaks a missing
    value ("None degrees Celsius") when the LLM used an unexpected arg key."""
    if intent == "start_centrifuge":
        rpm = int(arg(args, "rpm") or 0)
        minutes = arg(args, "minutes")
        m = _fmt_num(minutes) if minutes is not None else 0
        return f"Centrifuge run complete. {rpm} r p m for {m} minutes."
    if intent == "set_temperature":
        c = arg(args, "celsius")
        if c is None:
            return "Target temperature reached."
        return f"Target temperature reached: {_fmt_num(c)} degrees Celsius."
    return None


# ------------------------------------------------------------- intent matchers
# Loose affirmations and the strict "confirm" word. Cancel words. All matched on
# word boundaries, case-insensitively, so punctuation and casing do not matter.
_CONFIRM_LOOSE_RE = re.compile(
    r"\b(confirm|confirmed|yes|yeah|yep|yup|sure|affirmative|go ahead|do it|proceed)\b", re.I)
_CONFIRM_STRICT_RE = re.compile(r"\bconfirm(?:ed)?\b", re.I)
_CANCEL_RE = re.compile(
    r"\b(cancel|cancelled|abort|never mind|nevermind|no|nope|stop|don'?t|do not)\b", re.I)


def is_confirm(text, strict):
    """True when `text` is a confirmation. strict=True requires the literal word
    "confirm" (used for HAZARDOUS commands); strict=False accepts loose
    affirmations (yes, yeah, go ahead, do it, proceed, ...)."""
    if not text:
        return False
    rx = _CONFIRM_STRICT_RE if strict else _CONFIRM_LOOSE_RE
    return bool(rx.search(text))


def is_cancel(text):
    """True when `text` cancels the pending action (cancel, no, stop, abort,
    never mind, don't, ...)."""
    if not text:
        return False
    return bool(_CANCEL_RE.search(text))


def keyword_of(intent):
    """The intent's confirmation keyword (see COMMANDS), or None if unknown."""
    spec = COMMANDS.get(intent)
    return spec.get("keyword") if spec else None


def confirm_phrase(intent):
    """The exact phrase a user must say to confirm a bound (IRREVERSIBLE /
    HAZARDOUS) command: "confirm <keyword>", e.g. "confirm dispense". Falls back to
    a bare "confirm" for an unknown intent."""
    kw = keyword_of(intent)
    return f"confirm {kw}" if kw else "confirm"


def is_confirm_bound(text, keyword):
    """True iff `text` is an INTENT-BOUND confirmation: it contains BOTH the literal
    word "confirm" AND the command's keyword, each on a word boundary (case- and
    punctuation-insensitive). So "confirm dispense", "confirm the dispense", and
    "please confirm dispense now" all fire for keyword "dispense"; a bare "confirm",
    a stray "yes", and "yes dispense" (no confirm word) do NOT. This is what stops a
    loose affirmation from firing the wrong irreversible action (fix F1)."""
    if not text or not keyword:
        return False
    if not _CONFIRM_STRICT_RE.search(text):
        return False
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text, re.I))


# STOP_RE detects the fast-path emergency stop: a SHORT, standalone stop-like
# utterance. It must fire on "stop", "halt", "abort", "emergency stop", "please
# stop" but NOT on "stop the stirrer" (that is a real command, routed through the
# LLM as stop_stirrer). Encoded as: at most 4 words, at least one stop keyword,
# and every word either a stop keyword or a benign filler.
STOP_RE = re.compile(r"\b(stop|halt|abort|freeze|emergency)\b", re.I)
_STOP_KEYWORDS = {"stop", "halt", "abort", "freeze", "emergency"}
_STOP_FILLERS = {"please", "now", "just", "right", "it", "all", "everything",
                 "the", "run", "immediately"}


def is_stop(text):
    """True for a short standalone emergency-stop utterance (<= 4 words, only stop
    keywords + benign fillers). "stop the stirrer" returns False: it carries the
    content word "stirrer", so it is a command, not a bare stop."""
    if not text:
        return False
    words = re.findall(r"[a-z]+", text.lower())
    if not words or len(words) > 4:
        return False
    if not any(w in _STOP_KEYWORDS for w in words):
        return False
    return all(w in _STOP_KEYWORDS or w in _STOP_FILLERS for w in words)


# --------------------------------------------------------------- automation stub
class AutomationStub:
    """An in-memory stand-in for the lab-automation system. Deterministic, so
    tests and the smoke are reproducible. execute() applies a command after a
    small simulated latency; halt() cancels running activity; busy reports whether
    anything is in flight or running. Swap this class for the real driver at
    integration time, keeping the same async surface."""

    def __init__(self):
        self.temperature = 22.0
        self.stirrer = {"on": False, "rpm": 0}
        self.centrifuge = {"running": False, "rpm": 0, "minutes": 0.0}
        self.wells = {"A3": 0.0, "B2": 0.0}
        self.sensors = {"temperature": 22.0, "ph": 7.2, "pressure_kpa": 101.3}
        self._active = 0   # number of execute() calls in flight
        # Protocol walkthrough state. step 0 = not started; 1..PROTOCOL_TOTAL = the
        # current step; done = the last step has been passed.
        self.protocol = {"name": None, "step": 0, "total": PROTOCOL_TOTAL, "done": False}
        # Pending completion-event timers (asyncio tasks the server creates and
        # registers here). halt() and disconnect cancel them so no event fires.
        self._timers = set()
        self._protocol_timer = None   # at most one step timer at a time

    def register_timer(self, task):
        """Track a completion-event timer task so halt()/disconnect can cancel it.
        Self-pruning: a finished timer removes itself."""
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    def cancel_protocol_timer(self):
        """Disarm the current step's timer. Called whenever the user navigates the
        protocol: a timer left armed for an ABANDONED step would later announce a
        completion for work the operator moved on from."""
        if self._protocol_timer is not None and not self._protocol_timer.done():
            self._protocol_timer.cancel()
        self._protocol_timer = None

    def set_protocol_timer(self, task):
        """Install the timer for the step just arrived at, replacing the previous
        step's timer."""
        self.cancel_protocol_timer()
        self._protocol_timer = task
        self.register_timer(task)

    def cancel_timers(self):
        """Cancel every pending completion-event timer (no event will fire)."""
        for t in list(self._timers):
            t.cancel()
        self._timers.clear()
        self._protocol_timer = None

    @property
    def busy(self):
        """True while an execute() is in flight or a long activity (stirrer /
        centrifuge) is running: the fast-path stop keys on this."""
        return self._active > 0 or self.stirrer["on"] or self.centrifuge["running"]

    def snapshot(self):
        """A plain-dict copy of the current lab state (safe to serialize)."""
        return {"temperature": self.temperature, "stirrer": dict(self.stirrer),
                "centrifuge": dict(self.centrifuge), "wells": dict(self.wells),
                "protocol": dict(self.protocol)}

    def _step_detail(self, n):
        """The spoken form of one protocol step: position plus its verbatim text."""
        step = protocol_step(n)
        return f"Step {n} of {PROTOCOL_TOTAL}. {step['text']}" if step else ""

    async def execute(self, intent, args):
        """Apply a command after a 0.15 s simulated latency. Returns
        {ok, detail, state}. Unknown intents return ok=False."""
        self._active += 1
        try:
            await asyncio.sleep(0.15)
            return self._apply(intent, args or {})
        finally:
            self._active -= 1

    def halt(self):
        """Cancel any running activity (stirrer, centrifuge), cancel pending
        completion-event timers (so no delayed event fires), and clear the
        in-flight counter. Returns {halted: [...what was stopped...], state}."""
        self.cancel_timers()
        halted = []
        if self.centrifuge["running"]:
            halted.append(f"centrifuge at {self.centrifuge['rpm']} rpm")
            self.centrifuge = {"running": False, "rpm": 0, "minutes": 0.0}
        if self.stirrer["on"]:
            halted.append(f"stirrer at {self.stirrer['rpm']} rpm")
            self.stirrer = {"on": False, "rpm": 0}
        self._active = 0
        return {"halted": halted or ["nothing was running"], "state": self.snapshot()}

    # -- command application (deterministic) --
    def _ok(self, detail):
        return {"ok": True, "detail": detail, "state": self.snapshot()}

    def _apply(self, intent, args):
        if intent == "read_sensor":
            sensor = str(arg(args, "sensor") or "temperature")
            val = self.sensors.get(sensor)
            if val is None:
                # deterministic canned reading for an unknown sensor name
                val = round(20 + (sum(map(ord, sensor)) % 100) / 10.0, 1)
            return self._ok(f"{sensor} reads {val}")
        if intent == "stop_stirrer":
            self.stirrer = {"on": False, "rpm": 0}
            return self._ok("stirrer stopped")
        if intent == "set_temperature":
            c = arg(args, "celsius")
            if c is not None:                 # None guard: keep the last-known temp, never set None
                self.temperature = float(c)
                self.sensors["temperature"] = self.temperature
            return self._ok(f"temperature set to {self.temperature} degrees Celsius")
        if intent == "start_stirrer":
            rpm = int(arg(args, "rpm") or 0)
            self.stirrer = {"on": True, "rpm": rpm}
            return self._ok(f"stirrer running at {rpm} rpm")
        if intent == "dispense":
            vol = float(arg(args, "volume_ul") or 0)
            well = str(arg(args, "well") or "?")
            self.wells[well] = round(self.wells.get(well, 0.0) + vol, 3)
            return self._ok(f"dispensed {vol} microliters into well {well}")
        if intent == "add_reagent":
            vol = float(arg(args, "volume_ul") or 0)
            target = str(arg(args, "target") or "?")
            reagent = str(arg(args, "reagent") or "reagent")
            self.wells[target] = round(self.wells.get(target, 0.0) + vol, 3)
            return self._ok(f"added {vol} microliters of {reagent} to {target}")
        if intent == "start_centrifuge":
            rpm = int(arg(args, "rpm") or 0)
            minutes = float(arg(args, "minutes") or 0)
            self.centrifuge = {"running": True, "rpm": rpm, "minutes": minutes}
            return self._ok(f"centrifuge running at {rpm} rpm for {minutes} minutes")

        # -- protocol walkthrough --
        if intent == "protocol_start":
            self.protocol = {"name": str(args.get("name") or PROTOCOL_NAME), "step": 1,
                             "total": PROTOCOL_TOTAL, "done": False}
            return self._ok(f"Starting the {self.protocol['name']} protocol. "
                            + self._step_detail(1))
        if intent in ("protocol_next", "protocol_back", "protocol_repeat", "protocol_status"):
            step = self.protocol["step"]
            if step == 0:
                if intent == "protocol_status":
                    return self._ok("No protocol is running. Say start the protocol to begin.")
                return {"ok": False,
                        "detail": "No protocol is running. Say start the protocol first.",
                        "state": self.snapshot()}
            if intent == "protocol_status":
                if self.protocol["done"]:
                    return self._ok(f"The {self.protocol['name']} protocol is complete. "
                                    f"All {PROTOCOL_TOTAL} steps are done.")
                return self._ok(self._step_detail(step))
            if intent == "protocol_repeat":
                return self._ok(self._step_detail(step))
            if intent == "protocol_next":
                if self.protocol["done"]:
                    return self._ok("The protocol is already complete.")
                if step >= PROTOCOL_TOTAL:
                    self.protocol["done"] = True
                    return self._ok(f"That was the last step. The "
                                    f"{self.protocol['name']} protocol is complete.")
                self.protocol["step"] = step + 1
                return self._ok(self._step_detail(step + 1))
            # protocol_back
            if self.protocol["done"]:
                # stepping back out of the completed state returns to the last step
                self.protocol["done"] = False
                return self._ok(self._step_detail(PROTOCOL_TOTAL))
            if step <= 1:
                return self._ok("You are already on step 1. " + self._step_detail(1))
            self.protocol["step"] = step - 1
            return self._ok(self._step_detail(step - 1))

        return {"ok": False, "detail": f"unknown command: {intent}", "state": self.snapshot()}
