"""Unit tests for web/lab_gate.py: the pure lab-command gate, readback, spoken
intent matchers, and the in-memory AutomationStub. No network, no WebSocket."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web import lab_gate  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ gate matrix
def test_gate_safe_proceeds_and_confirms_only_below_verylow(monkeypatch):
    monkeypatch.setenv("LA_CONF_LOW", "0.75")
    monkeypatch.setenv("LA_CONF_VERYLOW", "0.50")
    assert lab_gate.gate("read_sensor", {"sensor": "temp"}, 0.99)["action"] == "proceed"
    assert lab_gate.gate("read_sensor", {"sensor": "temp"}, 0.60)["action"] == "proceed"
    # only a very-low confidence downgrades a SAFE command to confirm
    assert lab_gate.gate("read_sensor", {"sensor": "temp"}, 0.40)["action"] == "confirm"
    assert lab_gate.gate("stop_stirrer", {}, 0.40)["action"] == "confirm"


def test_gate_reversible_confirms_below_low(monkeypatch):
    monkeypatch.setenv("LA_CONF_LOW", "0.75")
    monkeypatch.setenv("LA_CONF_VERYLOW", "0.50")
    assert lab_gate.gate("set_temperature", {"celsius": 37.0}, 0.90)["action"] == "proceed"
    assert lab_gate.gate("set_temperature", {"celsius": 37.0}, 0.70)["action"] == "confirm"
    assert lab_gate.gate("start_stirrer", {"rpm": 300}, 0.60)["action"] == "confirm"


def test_gate_irreversible_always_at_least_confirm_reject_below_verylow(monkeypatch):
    monkeypatch.setenv("LA_CONF_LOW", "0.75")
    monkeypatch.setenv("LA_CONF_VERYLOW", "0.50")
    # high confidence still needs confirmation for an irreversible command
    assert lab_gate.gate("dispense", {"volume_ul": 50, "well": "A3"}, 0.99)["action"] == "confirm"
    assert lab_gate.gate("dispense", {"volume_ul": 50, "well": "A3"}, 0.55)["action"] == "confirm"
    # below very-low it is rejected outright
    assert lab_gate.gate("dispense", {"volume_ul": 50, "well": "A3"}, 0.40)["action"] == "reject"
    assert lab_gate.gate("add_reagent", {"reagent": "x", "volume_ul": 5, "target": "B2"},
                         0.40)["action"] == "reject"


def test_gate_hazardous_confirm_strict_reject_below_verylow(monkeypatch):
    monkeypatch.setenv("LA_CONF_LOW", "0.75")
    monkeypatch.setenv("LA_CONF_VERYLOW", "0.50")
    assert lab_gate.gate("start_centrifuge", {"rpm": 3000, "minutes": 5},
                         0.99)["action"] == "confirm_strict"
    assert lab_gate.gate("start_centrifuge", {"rpm": 3000, "minutes": 5},
                         0.40)["action"] == "reject"


def test_gate_none_confidence_escalates_never_relaxes():
    # Review fix F3: missing confidence ESCALATES one notch toward caution and never
    # rejects (rejecting would lock out a degraded-ASR user). This is a deliberate
    # spec change from the old "None treated as 1.0" behavior.
    d_safe = lab_gate.gate("read_sensor", {"sensor": "temp"}, None)
    assert d_safe["action"] == "proceed"          # SAFE (read-only) still proceeds
    assert "confidence unavailable" in d_safe["reason"]
    # REVERSIBLE now CONFIRMS on missing confidence (was proceed): the escalation.
    d_rev = lab_gate.gate("set_temperature", {"celsius": 37.0}, None)
    assert d_rev["action"] == "confirm"
    assert "confidence unavailable" in d_rev["reason"]
    # IRREVERSIBLE still confirms (not reject) on missing confidence
    d_irr = lab_gate.gate("dispense", {"volume_ul": 50, "well": "A3"}, None)
    assert d_irr["action"] == "confirm"
    assert "confidence unavailable" in d_irr["reason"]
    # HAZARDOUS still confirm_strict, never rejected for missing confidence alone
    d_haz = lab_gate.gate("start_centrifuge", {"rpm": 3000, "minutes": 5}, None)
    assert d_haz["action"] == "confirm_strict"
    assert "confidence unavailable" in d_haz["reason"]


def test_gate_unknown_command_rejected():
    assert lab_gate.gate("frobnicate", {}, 0.99)["action"] == "reject"


def test_gate_custom_thresholds_from_env(monkeypatch):
    # tighten LOW so an otherwise-proceeding reversible command now confirms
    monkeypatch.setenv("LA_CONF_LOW", "0.95")
    monkeypatch.setenv("LA_CONF_VERYLOW", "0.50")
    assert lab_gate.gate("set_temperature", {"celsius": 37.0}, 0.90)["action"] == "confirm"


# -------------------------------------------------------------------- readback
def test_readback_dispense_digits_and_units():
    rb = lab_gate.readback("dispense", {"volume_ul": 50, "well": "A3"})
    assert rb == "dispense five zero, that is 50, microliters into well A three"


def test_readback_integral_float_drops_trailing_zero():
    rb = lab_gate.readback("dispense", {"volume_ul": 50.0, "well": "A3"})
    assert "that is 50," in rb
    assert "50.0" not in rb


def test_readback_decimal_says_point():
    rb = lab_gate.readback("set_temperature", {"celsius": 37.5})
    assert "three seven point five" in rb
    assert "that is 37.5" in rb
    assert "degrees Celsius" in rb


def test_readback_rpm_expands_to_letters():
    rb = lab_gate.readback("start_centrifuge", {"rpm": 3000, "minutes": 5})
    assert "r p m" in rb
    assert "three zero zero zero" in rb
    assert "minutes" in rb


def test_say_digits_and_unit_expansion():
    assert lab_gate.say_digits(50) == "five zero"
    assert lab_gate.say_digits("A3") == "A three"
    assert lab_gate.say_digits(37.5) == "three seven point five"
    assert lab_gate.expand_unit("ul") == "microliters"
    assert lab_gate.expand_unit("C") == "degrees Celsius"
    assert lab_gate.expand_unit("rpm") == "r p m"


# ------------------------------------------------------------- intent matchers
def test_is_confirm_loose_vs_strict():
    for t in ["yes", "yeah", "go ahead", "do it", "proceed", "confirm", "Confirmed."]:
        assert lab_gate.is_confirm(t, strict=False), t
    # strict requires the word "confirm"
    assert lab_gate.is_confirm("please confirm", strict=True)
    assert lab_gate.is_confirm("CONFIRM", strict=True)
    for t in ["yes", "yeah", "go ahead", "do it", "proceed"]:
        assert not lab_gate.is_confirm(t, strict=True), t
    assert not lab_gate.is_confirm("", strict=False)


def test_is_cancel():
    for t in ["cancel", "no", "stop", "abort", "never mind", "don't", "do not"]:
        assert lab_gate.is_cancel(t), t
    assert not lab_gate.is_cancel("yes go ahead")
    assert not lab_gate.is_cancel("")


def test_keyword_and_confirm_phrase():
    assert lab_gate.keyword_of("dispense") == "dispense"
    assert lab_gate.keyword_of("add_reagent") == "reagent"
    assert lab_gate.keyword_of("start_centrifuge") == "centrifuge"
    assert lab_gate.keyword_of("frobnicate") is None
    assert lab_gate.confirm_phrase("dispense") == "confirm dispense"
    assert lab_gate.confirm_phrase("add_reagent") == "confirm reagent"
    assert lab_gate.confirm_phrase("start_centrifuge") == "confirm centrifuge"
    assert lab_gate.confirm_phrase("frobnicate") == "confirm"   # fallback for unknown


def test_is_confirm_bound_word_boundary_matrix():
    kw = "dispense"
    # positives: "confirm" AND the keyword both present, on word boundaries
    for t in ["confirm dispense", "confirm the dispense", "please confirm dispense now",
              "CONFIRM DISPENSE", "confirm, dispense.", "confirmed dispense",
              "dispense, confirm"]:
        assert lab_gate.is_confirm_bound(t, kw), t
    # negatives: missing the confirm word, or missing the keyword, or a substring
    for t in ["yes dispense", "dispense", "confirm", "yes", "confirm the reagent",
              "confirm dispenser", "dispensed, confirm it"]:
        assert not lab_gate.is_confirm_bound(t, kw), t
    assert not lab_gate.is_confirm_bound("", kw)
    assert not lab_gate.is_confirm_bound("confirm dispense", "")


def test_is_stop_positive_short_standalone():
    for t in ["stop", "stop!", "Stop.", "halt", "abort", "emergency stop",
              "please stop", "stop now", "stop everything", "abort the run"]:
        assert lab_gate.is_stop(t), t


def test_is_stop_negative_commands_and_long_utterances():
    # a real command that contains "stop" is NOT a bare stop
    assert not lab_gate.is_stop("stop the stirrer")
    assert not lab_gate.is_stop("please stop the centrifuge now")   # > 4 words + content
    assert not lab_gate.is_stop("what is the weather")
    assert not lab_gate.is_stop("")
    # matcher is punctuation/case robust but content-word sensitive
    assert not lab_gate.is_stop("stop dispensing reagent")


# ------------------------------------------------------------- automation stub
def test_stub_execute_dispense_and_state():
    stub = lab_gate.AutomationStub()
    res = run(stub.execute("dispense", {"volume_ul": 50, "well": "A3"}))
    assert res["ok"] is True
    assert res["state"]["wells"]["A3"] == 50.0
    # deterministic accumulation
    res2 = run(stub.execute("dispense", {"volume_ul": 10, "well": "A3"}))
    assert res2["state"]["wells"]["A3"] == 60.0


def test_stub_read_sensor_deterministic():
    stub = lab_gate.AutomationStub()
    a = run(stub.execute("read_sensor", {"sensor": "temperature"}))
    b = run(stub.execute("read_sensor", {"sensor": "temperature"}))
    assert a["detail"] == b["detail"]
    assert "temperature reads" in a["detail"]


# --------------------------------------- LLM arg-name aliases (live-audit findings)
def test_arg_reads_canonical_and_aliases():
    assert lab_gate.arg({"celsius": 30}, "celsius") == 30
    assert lab_gate.arg({"temperature": 30}, "celsius") == 30            # F7 bug
    assert lab_gate.arg({"temperature_celsius": 30}, "celsius") == 30
    assert lab_gate.arg({"speed_rpm": 300}, "rpm") == 300                # start_stirrer
    assert lab_gate.arg({"duration_minutes": 5}, "minutes") == 5         # start_centrifuge
    assert lab_gate.arg({"well": "D4"}, "target") == "D4"                # add_reagent
    assert lab_gate.arg({}, "celsius") is None
    assert lab_gate.arg({"celsius": None}, "celsius") is None            # None is not "present"


def test_set_temperature_accepts_temperature_alias():
    # the LLM-natural key must set the stub state and readback to 30, never None.
    for key in ("celsius", "temperature", "temperature_celsius"):
        stub = lab_gate.AutomationStub()
        res = run(stub.execute("set_temperature", {key: 30}))
        assert res["ok"] is True
        assert res["state"]["temperature"] == 30.0, key
        assert "30" in res["detail"] and "None" not in res["detail"], key
        rb = lab_gate.readback("set_temperature", {key: 30})
        assert "None" not in rb and ("3" in rb)   # digit-by-digit "three zero"


def test_set_temperature_missing_arg_keeps_last_and_no_none():
    stub = lab_gate.AutomationStub()
    run(stub.execute("set_temperature", {"celsius": 25}))
    res = run(stub.execute("set_temperature", {}))          # no usable arg
    assert res["state"]["temperature"] == 25.0              # last-known kept, not None
    assert "None" not in res["detail"]
    rb = lab_gate.readback("set_temperature", {})
    assert rb == "set the temperature" and "None" not in rb


def test_other_aliased_commands_apply_to_state():
    stub = lab_gate.AutomationStub()
    run(stub.execute("start_stirrer", {"speed_rpm": 300}))
    assert stub.snapshot()["stirrer"] == {"on": True, "rpm": 300}
    res = run(stub.execute("start_centrifuge", {"rpm": 3000, "duration_minutes": 5}))
    assert res["state"]["centrifuge"] == {"running": True, "rpm": 3000, "minutes": 5.0}
    res2 = run(stub.execute("add_reagent", {"reagent": "ethanol", "volume_ul": 20, "well": "D4"}))
    assert res2["state"]["wells"]["D4"] == 20.0            # "well" alias for "target"


def test_completion_announce_carries_number_and_guards_none():
    # set_temperature: the value is spoken, and a missing value never says "None".
    assert lab_gate.completion_announce("set_temperature", {"temperature": 30}) == \
        "Target temperature reached: 30 degrees Celsius."
    assert lab_gate.completion_announce("set_temperature", {"celsius": 37.5}) == \
        "Target temperature reached: 37.5 degrees Celsius."
    assert lab_gate.completion_announce("set_temperature", {}) == "Target temperature reached."
    assert "None" not in lab_gate.completion_announce("set_temperature", {})
    # centrifuge reads its aliased minutes/rpm too
    ann = lab_gate.completion_announce("start_centrifuge", {"rpm": 3000, "duration_minutes": 5})
    assert "3000" in ann and "5 minutes" in ann
    # a command with no completion event returns None
    assert lab_gate.completion_announce("dispense", {"volume_ul": 50, "well": "A3"}) is None


def test_stub_busy_and_halt():
    stub = lab_gate.AutomationStub()
    assert stub.busy is False
    run(stub.execute("start_centrifuge", {"rpm": 3000, "minutes": 5}))
    assert stub.busy is True          # centrifuge running
    halted = stub.halt()
    assert any("centrifuge" in h for h in halted["halted"])
    assert stub.busy is False
    assert stub.centrifuge["running"] is False


def test_stub_busy_true_during_execute():
    stub = lab_gate.AutomationStub()

    async def drive():
        task = asyncio.create_task(stub.execute("read_sensor", {"sensor": "ph"}))
        await asyncio.sleep(0.05)      # inside the 0.15 s simulated latency
        busy_mid = stub.busy
        await task
        return busy_mid

    assert run(drive()) is True
    assert stub.busy is False


def test_stub_halt_nothing_running():
    stub = lab_gate.AutomationStub()
    halted = stub.halt()
    assert halted["halted"] == ["nothing was running"]


def test_stub_unknown_intent_not_ok():
    stub = lab_gate.AutomationStub()
    res = run(stub.execute("frobnicate", {}))
    assert res["ok"] is False


# ---------------------------------------------------------- protocol walkthrough
def test_protocol_intents_are_safe_and_proceed():
    for intent in ("protocol_start", "protocol_next", "protocol_back",
                   "protocol_repeat", "protocol_status"):
        assert lab_gate.severity_of(intent) == lab_gate.SAFE
        assert lab_gate.gate(intent, {}, 0.97)["action"] == "proceed"
    # a SAFE command still confirms when the transcript is very low confidence
    assert lab_gate.gate("protocol_next", {}, 0.30)["action"] == "confirm"


def test_protocol_start_next_back_repeat_status():
    stub = lab_gate.AutomationStub()
    total = lab_gate.PROTOCOL_TOTAL

    # status before starting
    st = run(stub.execute("protocol_status", {}))
    assert st["ok"] is True and "No protocol is running" in st["detail"]
    # next before starting is refused (nothing to advance)
    nx = run(stub.execute("protocol_next", {}))
    assert nx["ok"] is False

    start = run(stub.execute("protocol_start", {}))
    assert start["ok"] is True
    assert f"Step 1 of {total}" in start["detail"]
    assert lab_gate.PROTOCOL_STEPS[0]["text"] in start["detail"]   # verbatim step text
    assert stub.protocol["step"] == 1

    nxt = run(stub.execute("protocol_next", {}))
    assert f"Step 2 of {total}" in nxt["detail"]
    assert stub.protocol["step"] == 2

    back = run(stub.execute("protocol_back", {}))
    assert f"Step 1 of {total}" in back["detail"]
    assert stub.protocol["step"] == 1

    rep = run(stub.execute("protocol_repeat", {}))
    assert f"Step 1 of {total}" in rep["detail"]
    assert stub.protocol["step"] == 1        # repeat does not advance

    stat = run(stub.execute("protocol_status", {}))
    assert f"Step 1 of {total}" in stat["detail"]


def test_protocol_back_at_first_step_stays():
    stub = lab_gate.AutomationStub()
    run(stub.execute("protocol_start", {}))
    res = run(stub.execute("protocol_back", {}))
    assert stub.protocol["step"] == 1
    assert "already on step 1" in res["detail"].lower()


def test_protocol_next_past_last_step_completes():
    stub = lab_gate.AutomationStub()
    run(stub.execute("protocol_start", {}))
    for _ in range(lab_gate.PROTOCOL_TOTAL - 1):      # walk to the last step
        run(stub.execute("protocol_next", {}))
    assert stub.protocol["step"] == lab_gate.PROTOCOL_TOTAL
    assert stub.protocol["done"] is False

    done = run(stub.execute("protocol_next", {}))     # one past the last step
    assert stub.protocol["done"] is True
    assert "complete" in done["detail"].lower()

    again = run(stub.execute("protocol_next", {}))    # idempotent once complete
    assert "already complete" in again["detail"].lower()

    st = run(stub.execute("protocol_status", {}))
    assert "complete" in st["detail"].lower()

    # stepping back out of the completed state returns to the last step
    back = run(stub.execute("protocol_back", {}))
    assert stub.protocol["done"] is False
    assert f"Step {lab_gate.PROTOCOL_TOTAL} of {lab_gate.PROTOCOL_TOTAL}" in back["detail"]


def test_protocol_start_accepts_a_name():
    stub = lab_gate.AutomationStub()
    res = run(stub.execute("protocol_start", {"name": "colony PCR"}))
    assert stub.protocol["name"] == "colony PCR"
    assert "colony PCR" in res["detail"]


def test_timed_steps_mention_their_timer_in_the_spoken_text():
    timed = [s for s in lab_gate.PROTOCOL_STEPS if s.get("timer_s")]
    assert timed, "the demo protocol should have at least one timed step"
    for step in timed:
        minutes = int(step["timer_s"] // 60)
        assert f"{minutes} minutes" in step["text"], step   # the text states the wait


def test_timer_done_text_names_the_step_and_real_duration(monkeypatch):
    monkeypatch.setenv("LA_PROTOCOL_TIMER_SCALE", "0.001")   # compression must not leak in
    n = next(s["n"] for s in lab_gate.PROTOCOL_STEPS if s.get("timer_s"))
    step = lab_gate.protocol_step(n)
    text = lab_gate.timer_done_text(n)
    assert f"Step {n}" in text
    assert step["timer_label"] in text                        # names what finished
    assert f"{int(step['timer_s'] // 60)} minutes elapsed" in text   # the REAL wait
    # a step without a timer has no completion announcement
    untimed = next(s["n"] for s in lab_gate.PROTOCOL_STEPS if not s.get("timer_s"))
    assert lab_gate.timer_done_text(untimed) is None


def test_cancel_protocol_timer_disarms_an_abandoned_step():
    """Navigating away from a timed step must disarm its countdown, or it would later
    announce a completion for a step the operator abandoned."""
    class FakeTimer:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

        def add_done_callback(self, _cb):
            pass

    stub = lab_gate.AutomationStub()
    t1 = FakeTimer()
    stub.set_protocol_timer(t1)
    assert stub._protocol_timer is t1

    # arriving at a step with no timer still disarms the old one
    stub.cancel_protocol_timer()
    assert t1.cancelled is True
    assert stub._protocol_timer is None

    # and installing a new step timer replaces (cancels) the previous one
    t2, t3 = FakeTimer(), FakeTimer()
    stub.set_protocol_timer(t2)
    stub.set_protocol_timer(t3)
    assert t2.cancelled is True and t3.cancelled is False
    assert stub._protocol_timer is t3

    stub.halt()                       # halt cancels it too
    assert t3.cancelled is True
    assert stub._protocol_timer is None


def test_step_timer_s_scales_and_is_none_without_a_timer(monkeypatch):
    timed_n = next(s["n"] for s in lab_gate.PROTOCOL_STEPS if s.get("timer_s"))
    untimed_n = next(s["n"] for s in lab_gate.PROTOCOL_STEPS if not s.get("timer_s"))
    raw = lab_gate.protocol_step(timed_n)["timer_s"]

    monkeypatch.delenv("LA_PROTOCOL_TIMER_SCALE", raising=False)
    assert lab_gate.step_timer_s(timed_n) == raw          # unscaled by default
    assert lab_gate.step_timer_s(untimed_n) is None       # no timer on this step

    monkeypatch.setenv("LA_PROTOCOL_TIMER_SCALE", "0.01")
    assert lab_gate.step_timer_s(timed_n) == raw * 0.01   # compressed for demo/smoke
    assert lab_gate.step_timer_s(999) is None             # out of range
