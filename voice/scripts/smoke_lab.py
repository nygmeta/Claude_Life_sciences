"""Local WS smoke for the lab-command gate + confirm/execute handshake (PART B).

Starts its OWN orchestrator instance (LA_LAB_MODE=1) on a free port, against the
shared mock ASR/TTS and the REAL Claude Haiku API. Utterances are injected via
the Part A filename-override hook: each segment payload is the token bytes
"utter;text=<urlencoded>;pmin=<f>;pmean=<f>.wav", which the orchestrator uploads
under that filename and the mock ASR echoes back as an exact transcript +
confidence. This never touches the mock ASR canned-segment counter, so the other
smokes are unaffected.

Scenarios (LAB-SMOKE PASS/FAIL per scenario + overall):
  a. "read the temperature sensor"                     -> action_executed, no confirm
  b. "dispense 50 microliters into well A3" (pmin .93) -> action_pending (readback in
     reply, confirm_phrase advertised), then "confirm dispense" -> action_executed
  c. same dispense at pmin .40                          -> action_rejected
  d. "start the centrifuge ..." then bare "yes" AND bare "confirm" -> both stay
     pending (intent-bound), then "confirm centrifuge"  -> action_executed confirmed:true
  e. "what is the weather like"                         -> no action_* messages
  f. "dispense 10 microliters into well B2" then "cancel" -> action_cancelled
  g. pending action + a bare "stop" segment             -> action_halted, pending cleared
  h. protocol start -> next (the timed step announces) -> repeat
  i. protocol back / status, and back DISARMS the abandoned step's timer
  j. quiet "confirm dispense" (pmean .15) is heard but re-prompts (below
     LA_CONFIRM_FLOOR), pending kept; a clear one then executes (F2: execution floor)
  k. pending expires (LA_PENDING_TTL_S=1 on a dedicated orchestrator): a later stale
     "confirm dispense" is cancelled(expired), never executed; stale UNRELATED speech
     passes through to a normal reply (F4 + round-3 passthrough)
"""
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import websockets

APP = Path(__file__).resolve().parent.parent
LAB_PORT = os.environ.get("LA_LAB_PORT", "8797")
URL = f"ws://localhost:{LAB_PORT}/"
# Scenario i waits this long, from the moment the timed step was entered, before
# concluding its timer was really disarmed. Must exceed the scaled step-2 countdown
# (12 s at LA_PROTOCOL_TIMER_SCALE=0.04) with margin, or the absence of an announce
# would only mean "not yet".
TIMER_DISARM_WAIT_S = 16.0


def utter_seg(text, pmin=0.97, pmean=0.98):
    """A segment payload whose bytes ARE the mock-ASR override token, so the mock
    returns exactly `text` with the given confidence."""
    token = f"utter;text={quote(text)};pmin={pmin};pmean={pmean}.wav".encode("ascii")
    return json.dumps({"type": "audio_segment",
                       "audio_b64": base64.b64encode(token).decode(), "sample_rate": 16000})


class Client:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.sid = None
        self.counts = {}
        self.msgs = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    def _note(self, m):
        t = m.get("type")
        self.counts[t] = self.counts.get(t, 0) + 1
        self.msgs.append(m)
        if t == "session_started":
            self.sid = m.get("id")
        elif t == "error":
            print(f"  <- ERROR: {m.get('text')}", flush=True)

    async def drain(self, timeout):
        try:
            while True:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout)))
        except asyncio.TimeoutError:
            pass

    async def recv_until_count(self, want, target, timeout=70):
        try:
            while self.counts.get(want, 0) < target:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout)))
        except asyncio.TimeoutError:
            print(f"  <- TIMEOUT waiting for {want} >= {target}", flush=True)

    async def send(self, obj):
        await self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))

    async def turn(self, text, pmin=0.97, pmean=0.98, timeout=75):
        """One user turn: inject the utterance, end the turn, wait for the reply to
        finish (reply_audio_end terminates every turn, including canned ones)."""
        tbefore = self.counts.get("transcript", 0)
        rbefore = self.counts.get("reply_audio_end", 0)
        await self.send(utter_seg(text, pmin, pmean))
        await self.recv_until_count("transcript", tbefore + 1, timeout=30)
        await self.send({"type": "end_turn"})
        await self.recv_until_count("reply_audio_end", rbefore + 1, timeout=timeout)
        await self.drain(0.5)

    def has(self, t):
        return any(m.get("type") == t for m in self.msgs)

    def last(self, t):
        return next((m for m in reversed(self.msgs) if m.get("type") == t), None)


async def scenario_a():
    print("a: 'read the temperature sensor' -> action_executed, no confirm", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("read the temperature sensor", pmin=0.97)
        ae = c.last("action_executed")
        ok = ae is not None and ae.get("confirmed") is False and not c.has("action_pending")
    print(f"   action_executed={ae is not None} confirmed={ae and ae.get('confirmed')} "
          f"no_pending={not c.has('action_pending')} -> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_b():
    print("b: dispense (pmin .93) -> pending+readback, then 'confirm dispense' -> executed", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 50 microliters into well A3", pmin=0.93)
        ap = c.last("action_pending")
        rd = c.last("reply_done")
        pending_ok = (ap is not None and ap.get("intent") == "dispense"
                      and ap.get("confirm_phrase") == "confirm dispense")   # bound: phrase advertised
        reply_text = (rd.get("text") if rd else "") or ""
        readback_ok = any(w in reply_text.lower()
                          for w in ("confirm", "cancel", "microliter", "well"))
        await c.turn("confirm dispense", pmin=0.97)
        ae = c.last("action_executed")
        ok = pending_ok and readback_ok and ae is not None and ae.get("confirmed") is True
    print(f"   pending={pending_ok} readback_in_reply={readback_ok} "
          f"executed_confirmed={ae is not None and ae.get('confirmed') is True} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_c():
    print("c: dispense at pmin .40 -> action_rejected", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 50 microliters into well A3", pmin=0.40)
        ar = c.last("action_rejected")
        ok = ar is not None and not c.has("action_executed") and not c.has("action_pending")
    print(f"   rejected={ar is not None} no_execute={not c.has('action_executed')} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_d():
    print("d: centrifuge (bound) -> bare 'yes' AND bare 'confirm' both stay pending -> "
          "'confirm centrifuge' executes", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("start the centrifuge at 3000 rpm for 5 minutes", pmin=0.97)
        ap = c.last("action_pending")
        pending_ok = (ap is not None and ap.get("intent") == "start_centrifuge"
                      and ap.get("confirm_phrase") == "confirm centrifuge")
        # a bare 'yes' is not intent-bound: it must NOT execute (re-prompts, pending kept)
        await c.turn("yes", pmin=0.97)
        yes_held = not c.has("action_executed")
        # a bare 'confirm' (no keyword) is also not bound: still must NOT execute
        await c.turn("confirm", pmin=0.97)
        confirm_held = not c.has("action_executed")
        # the exact bound phrase executes
        await c.turn("confirm centrifuge", pmin=0.97)
        ae = c.last("action_executed")
        ok = (pending_ok and yes_held and confirm_held
              and ae is not None and ae.get("confirmed") is True)
    print(f"   pending={pending_ok} yes_held={yes_held} confirm_held={confirm_held} "
          f"bound_executed={ae is not None and ae.get('confirmed') is True} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_e():
    print("e: 'what is the weather like' -> no action_* messages", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("what is the weather like", pmin=0.97)
        ok = (not c.has("action_executed") and not c.has("action_pending")
              and not c.has("action_rejected") and c.has("reply_done"))
    print(f"   no_action={not (c.has('action_executed') or c.has('action_pending') or c.has('action_rejected'))} "
          f"got_reply={c.has('reply_done')} -> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_f():
    print("f: dispense (pending) then 'cancel' -> action_cancelled", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 10 microliters into well B2", pmin=0.95)
        ap = c.last("action_pending")
        await c.turn("cancel", pmin=0.97)
        ac = c.last("action_cancelled")
        ok = (ap is not None and ac is not None and ac.get("reason") == "user"
              and not c.has("action_executed"))
    print(f"   pending={ap is not None} cancelled={ac is not None and ac.get('reason') == 'user'} "
          f"no_execute={not c.has('action_executed')} -> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_g():
    """Fast-path emergency stop: with a confirmation pending, a bare 'stop' segment
    (no end_turn) is intercepted in handle_segment -> action_halted, and the pending
    action is cleared (so a following turn is a normal turn, not a confirm)."""
    print("g: pending action + bare 'stop' segment -> action_halted, pending cleared", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 50 microliters into well A3", pmin=0.93)   # -> pending
        pending_ok = c.last("action_pending") is not None
        await c.send(utter_seg("stop", 0.97))   # bare stop: fast path, no end_turn
        await c.recv_until_count("action_halted", 1, timeout=30)
        ah = c.last("action_halted")
        halted_ok = ah is not None and isinstance(ah.get("halted"), str)
        # pending was cleared: a fresh benign chat turn must not be read as a confirm
        await c.turn("what is the weather like", pmin=0.97)
        cleared_ok = not c.has("action_executed")
        ok = pending_ok and halted_ok and cleared_ok
    print(f"   pending={pending_ok} halted_str={halted_ok} pending_cleared={cleared_ok} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_h():
    """Protocol walkthrough: start / next / repeat via the protocol_* SAFE intents
    (no confirmation), step text read back verbatim, and the timed step's countdown
    firing a completion announcement through the Phase 3 event channel (the timer is
    compressed by LA_PROTOCOL_TIMER_SCALE, so the spoken '5 minutes' lands in ~1.2 s)."""
    print("h: protocol start -> next (timed step announces) -> repeat", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("start the plasmid miniprep protocol", pmin=0.97)
        ae1 = c.last("action_executed")
        step1_ok = (ae1 is not None
                    and (ae1.get("result") or {}).get("state", {}).get("protocol", {}).get("step") == 1
                    and "resuspend" in ((ae1.get("result") or {}).get("detail") or "").lower())

        await c.turn("next step", pmin=0.97)
        ae2 = c.last("action_executed")
        step2_ok = (ae2 is not None
                    and (ae2.get("result") or {}).get("state", {}).get("protocol", {}).get("step") == 2)

        # step 2 is the timed lysis incubation: its compressed timer announces
        await c.recv_until_count("announce_end", 1, timeout=25)
        triple = [m["type"] for m in c.msgs
                  if m["type"] in ("announce", "announce_audio", "announce_end")]
        timer_ok = (triple == ["announce", "announce_audio", "announce_end"]
                    and any(m.get("type") == "announce" and m.get("source") == "stub"
                            for m in c.msgs))

        await c.turn("repeat that step", pmin=0.97)
        ae3 = c.last("action_executed")
        repeat_ok = (ae3 is not None
                     and (ae3.get("result") or {}).get("state", {}).get("protocol", {}).get("step") == 2)

        no_confirm = not c.has("action_pending")   # protocol navigation is SAFE
        ok = step1_ok and step2_ok and timer_ok and repeat_ok and no_confirm
    print(f"   step1={step1_ok} step2={step2_ok} timer_announce={timer_ok} "
          f"repeat_held={repeat_ok} no_confirm={no_confirm} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


def _step_of(action_executed):
    """The protocol step number carried in an action_executed's lab state, or None."""
    if not action_executed:
        return None
    state = (action_executed.get("result") or {}).get("state") or {}
    return (state.get("protocol") or {}).get("step")


async def scenario_i():
    """Protocol back / status, and the safety property behind them: navigating away
    from a timed step DISARMS that step's timer. A stale timer would otherwise
    announce the completion of an incubation the user already abandoned, which in a
    lab is a lie about the state of the bench.

    Timing note: this is why LA_PROTOCOL_TIMER_SCALE is 0.04 rather than something
    tighter. Step 2's timer must still be counting when "go back" lands, and a real
    Claude turn takes a couple of seconds, so the scaled countdown (12 s) has to
    outlast the turn that cancels it. We then wait past the original deadline and
    assert that NOTHING announced."""
    print("i: protocol back / status; back disarms the abandoned step's timer", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("start the plasmid miniprep protocol", pmin=0.97)
        await c.turn("next step", pmin=0.97)        # step 2: the timed lysis incubation
        armed_at = time.time()
        step2_ok = _step_of(c.last("action_executed")) == 2

        await c.turn("go back to the previous step", pmin=0.97)
        ae_back = c.last("action_executed")
        back_ok = ae_back is not None and ae_back.get("intent") == "protocol_back" \
            and _step_of(ae_back) == 1

        await c.turn("what step am I on", pmin=0.97)
        ae_st = c.last("action_executed")
        status_ok = ae_st is not None and ae_st.get("intent") == "protocol_status" \
            and _step_of(ae_st) == 1

        # Sit past the abandoned step's (scaled) deadline: a disarmed timer announces
        # nothing. An info announce also defers behind a committed reply, so draining
        # here past the deadline is what makes the absence meaningful.
        while time.time() < armed_at + TIMER_DISARM_WAIT_S:
            await c.drain(1.0)
        stale = [m for m in c.msgs if str(m.get("type", "")).startswith("announce")]
        disarmed_ok = not stale

        ok = step2_ok and back_ok and status_ok and disarmed_ok
    print(f"   step2_armed={step2_ok} back_to_1={back_ok} status_1={status_ok} "
          f"timer_disarmed={disarmed_ok} (stale announces: {len(stale)}) "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_j():
    """The two halves of the confirmation design together: a quiet 'confirm'
    (pmean 0.15) is HEARD (exempt from the noise gate, never dropped or discarded)
    but NOT clear enough to EXECUTE (below LA_CONFIRM_FLOOR), so it re-prompts and
    KEEPS the pending; a clear 'confirm' (pmean 0.97) then executes. The word is
    never eaten, but execution needs a clear confirm."""
    print("j: quiet 'confirm dispense' (pmean .15) -> re-prompt, pending kept; "
          "clear 'confirm dispense' -> executes", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 50 microliters into well A3", pmin=0.93)
        pending_ok = c.last("action_pending") is not None
        # quiet bound confirm: heard (not discarded) but re-prompted, not executed
        await c.turn("confirm dispense", pmin=0.15, pmean=0.15)
        quiet_tr = c.last("transcript")
        quiet_rd = c.last("reply_done")
        not_discarded = quiet_tr is not None and quiet_tr.get("discarded") is None
        reprompted = (not c.has("action_executed")
                      and quiet_rd is not None and "clearly" in (quiet_rd.get("text") or "").lower())
        # clear bound confirm: executes
        await c.turn("confirm dispense", pmin=0.97, pmean=0.97)
        ae = c.last("action_executed")
        ok = (pending_ok and not_discarded and reprompted
              and ae is not None and ae.get("confirmed") is True)
    print(f"   pending={pending_ok} quiet_not_discarded={not_discarded} quiet_reprompted={reprompted} "
          f"clear_executed={ae is not None and ae.get('confirmed') is True} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_k():
    """Pending expiry (LA_PENDING_TTL_S) on a DEDICATED orchestrator with a 1 s TTL
    (a 1 s TTL on the shared orchestrator would expire the other pending scenarios,
    whose confirm arrives seconds after the readback). Two cases:
      k1 stale CONFIRM: arm a dispense, age it past the TTL, then 'confirm dispense'
         -> action_cancelled(expired), NOT executed. A stale confirm must not fire.
      k2 stale UNRELATED speech: arm a dispense, age it past the TTL, then an
         unrelated request -> the pending is dropped (action_cancelled expired, no
         notice) and the request is answered as a NORMAL turn (round-3 passthrough)."""
    print("k: pending expires (TTL 1s) -> stale 'confirm dispense' cancelled(expired); "
          "stale unrelated speech passes through to a normal reply", flush=True)
    port = os.environ.get("LA_LAB_TTL_PORT", "8793")
    proc = _spawn_orch(port, {"LA_PENDING_TTL_S": "1"})
    try:
        if not _wait_port(port):
            print("   FAIL: ttl orchestrator never bound the port", flush=True)
            return False
        url = f"ws://localhost:{port}/"
        async with Client(url) as c:
            await c.drain(3)
            await c.turn("dispense 50 microliters into well A3", pmin=0.93)
            pending_ok = c.last("action_pending") is not None
            await asyncio.sleep(2.0)   # let the pending age past the 1 s TTL (no clock trickery here)
            await c.turn("confirm dispense", pmin=0.97, pmean=0.97)
            ac = c.last("action_cancelled")
            expired = ac is not None and ac.get("reason") == "expired"
            no_exec = not c.has("action_executed")
        async with Client(url) as c2:
            await c2.drain(3)
            await c2.turn("dispense 50 microliters into well A3", pmin=0.93)
            pending2 = c2.last("action_pending") is not None
            await asyncio.sleep(2.0)
            await c2.turn("read the temperature sensor", pmin=0.97)
            ac2 = c2.last("action_cancelled")
            pass_expired = ac2 is not None and ac2.get("reason") == "expired"
            # the unrelated request is answered as a normal turn, and no dispense fired
            answered = c2.has("reply_done")
            no_dispense = not any(m.get("type") == "action_executed" and m.get("intent") == "dispense"
                                  for m in c2.msgs)
        ok = pending_ok and expired and no_exec and pending2 and pass_expired and answered and no_dispense
        print(f"   k1[pending={pending_ok} expired={expired} no_exec={no_exec}] "
              f"k2[pending={pending2} expired={pass_expired} answered={answered} no_dispense={no_dispense}] "
              f"-> {'PASS' if ok else 'FAIL'}", flush=True)
        return ok
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


def _spawn_orch(port, extra_env=None):
    """Start a lab-mode orchestrator on `port` against the shared mock stack, with
    optional env overrides. Used by main() and by scenario_k (its own TTL=1 pod)."""
    env = dict(os.environ)
    env["LA_WS_PORT"] = str(port)
    env["LA_LAB_MODE"] = "1"
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    env.update(extra_env or {})
    return subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_port(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True
        time.sleep(0.3)
    return False


async def main():
    env = dict(os.environ)
    env["LA_WS_PORT"] = LAB_PORT
    env["LA_LAB_MODE"] = "1"
    # Compress protocol timers so a timed step's completion announcement is
    # observable in a smoke instead of the real 5 minutes the step text (correctly)
    # states: step 2's 300 s becomes 12 s. Not tighter than that, because scenario i
    # has to cancel that countdown from inside a real Claude turn, which takes a
    # couple of seconds; the timer must outlive the turn that disarms it.
    env["LA_PROTOCOL_TIMER_SCALE"] = env.get("LA_PROTOCOL_TIMER_SCALE", "0.04")
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(LAB_PORT):
            print("LAB-SMOKE: FAIL: lab orchestrator never bound the port", flush=True)
            return 1
        results = {
            "a": await scenario_a(),
            "b": await scenario_b(),
            "c": await scenario_c(),
            "d": await scenario_d(),
            "e": await scenario_e(),
            "f": await scenario_f(),
            "g": await scenario_g(),
            "h": await scenario_h(),
            "i": await scenario_i(),
            "j": await scenario_j(),
            "k": await scenario_k(),
        }
        for name, ok in results.items():
            print(f"LAB-SMOKE {name}: {'PASS' if ok else 'FAIL'}", flush=True)
        overall = all(results.values())
        print("LAB-SMOKE: PASS" if overall else "LAB-SMOKE: FAIL", flush=True)
        return 0 if overall else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
