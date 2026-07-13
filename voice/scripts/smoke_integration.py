"""End-to-end integration smoke: voice half <-> Lab Agent API.

This is the test the two halves exist for. It stands up BOTH sides for real:

  mock ASR/TTS  <-  voice orchestrator (web/server.py, LA_LAB_BACKEND_URL set)
                        |
                        +--HTTP-->  Lab Agent API (app/main.py, uvicorn)
                                      planner -> compiler -> validator -> adapter

and then talks to it the way a scientist would: by speaking. Each utterance is
injected through the mock ASR's filename-override token, so the transcript AND its
confidence are exact and reproducible, which is what lets us test the confidence
floor rather than hope for it.

Checks:
  a. a spoken protocol request is planned by the backend and read back for sign-off
     (state reaches awaiting_confirmation, and the voice half SPEAKS the readback)
  b. a spoken "yes" executes it (state reaches executed, simulation runs)
  c. a LOW-CONFIDENCE confirmation is refused: it never reaches the backend, the
     state stays awaiting_confirmation, and nothing is executed
  d. a spoken cancel abandons the plan, executing nothing
  e. the seam never double-plans: the local lab_gate tool path stays out of the way
     (no action_pending / action_executed from the voice half's own gate)

Run: python3 scripts/smoke_integration.py     (from voice/, with the mock on :9001/:9002)
Needs: the Lab Agent's deps installed (fastapi, uvicorn). It runs with NO Anthropic
key: the backend's planner falls back to its deterministic mock, and the voice half
needs no key of its own when the backend owns the planner.
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

import httpx
import websockets

VOICE = Path(__file__).resolve().parents[1]      # .../voice
REPO = VOICE.parent                              # repo root (holds app/)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def utter(text, pmin=0.97, pmean=0.98):
    """A segment whose bytes ARE the mock-ASR override token, so the transcript and
    its confidence are exactly what we asked for."""
    token = f"utter;text={quote(text)};pmin={pmin};pmean={pmean}.wav".encode("ascii")
    return json.dumps({"type": "audio_segment",
                       "audio_b64": base64.b64encode(token).decode(), "sample_rate": 16000})


class Voice:
    """One spoken conversation with the orchestrator."""

    def __init__(self, url):
        self.url = url
        self.msgs = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024)
        await self._drain(2)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    async def _drain(self, t):
        try:
            while True:
                self.msgs.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=t)))
        except asyncio.TimeoutError:
            pass

    async def say(self, text, pmin=0.97, pmean=0.98, timeout=60):
        """Speak one turn and wait for the assistant to finish speaking back."""
        start = len(self.msgs)
        await self.ws.send(utter(text, pmin, pmean))
        # wait for the transcript, then commit the turn
        while not any(m.get("type") == "transcript" for m in self.msgs[start:]):
            self.msgs.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=20)))
        await self.ws.send(json.dumps({"type": "end_turn"}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            self.msgs.append(m)
            if m.get("type") == "reply_audio_end":
                break
        await self._drain(0.4)
        return self.msgs[start:]

    @staticmethod
    def last(msgs, t):
        return next((m for m in reversed(msgs) if m.get("type") == t), None)

    @staticmethod
    def has(msgs, t):
        return any(m.get("type") == t for m in msgs)

    @staticmethod
    def spoke(msgs):
        """Did the assistant actually SAY something (audio, not just text)?"""
        return any(m.get("type") == "reply_audio" for m in msgs)


def spawn_backend(port):
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # force the deterministic mock planner
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def spawn_voice(port, backend_url):
    env = dict(os.environ)
    env["LA_WS_PORT"] = str(port)
    env["LA_LAB_MODE"] = "1"
    env["LA_LAB_BACKEND_URL"] = backend_url
    env["LA_CONFIRM_FLOOR"] = "0.40"
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS", "gepard-1.0=http://localhost:9002")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    return subprocess.Popen([sys.executable, str(VOICE / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_http(url, tries=40):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                if (await c.get(url)).status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
    return False


async def backend_state(base, sid):
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{base}/session/{sid}/audit")
        return r.json() if r.status_code == 200 else {}


async def main():
    bport, vport = free_port(), free_port()
    base = f"http://127.0.0.1:{bport}"
    bp = spawn_backend(bport)
    if not await wait_http(f"{base}/health"):
        print("FAIL: Lab Agent API never came up", flush=True)
        bp.terminate()
        return 1
    vp = spawn_voice(vport, base)
    await asyncio.sleep(3)
    url = f"ws://127.0.0.1:{vport}/"
    results = []

    # The Lab Agent's own conversation, mirroring its demo arc. A bare request is
    # incomplete, so it asks for the missing details. The volume given below is
    # DELIBERATELY unsafe (400 uL exceeds the well's working volume), so the
    # validator refuses to build the protocol and the scientist corrects it. That
    # refusal is the point of the whole system, and here it has to be SPOKEN.
    REQUEST = "Run an ELISA on today's plasma samples"
    UNSAFE = "Let's do IL-6 with 24 samples, 400 microliters per well"
    FIX = "My mistake, make it 100 microliters per well"
    CONFIRM = "Yes, go ahead"

    async def plan_up_to_confirmation(v):
        """Drive the turns that leave the backend awaiting_confirmation."""
        await v.say(REQUEST)
        await v.say(UNSAFE)
        return await v.say(FIX)

    try:
        # --- a: spoken request -> the backend asks for the missing details -------
        print("a: speak an ELISA request -> backend plans, asks the missing details", flush=True)
        async with Voice(url) as v:
            turn = await v.say(REQUEST)
            lb = v.last(turn, "lab_backend")
            rd = v.last(turn, "reply_done")
            planned = lb is not None and lb.get("intent") == "elisa"
            gathering = lb is not None and lb.get("state") == "gathering"
            asked = bool((lb or {}).get("questions"))
            spoke = v.spoke(turn)          # it must SAY the question, not just print it
            # the voice half's OWN gate must stay out of the way: exactly one planner
            no_local_gate = not v.has(turn, "action_pending") and not v.has(turn, "action_executed")
            ok_a = planned and gathering and asked and spoke and no_local_gate
            print(f"   intent={lb and lb.get('intent')} state={lb and lb.get('state')} "
                  f"asked_question={asked} spoke={spoke} no_local_double_gate={no_local_gate} "
                  f"-> {'PASS' if ok_a else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_a)

            # --- b: an UNSAFE volume is refused, out loud, before anything runs --
            print("b: give an unsafe per-well volume -> validator refuses it ALOUD", flush=True)
            turn = await v.say(UNSAFE)
            lb = v.last(turn, "lab_backend")
            rd = v.last(turn, "reply_done")
            refused = lb is not None and lb.get("state") == "validation_failed"
            failed_validation = (lb or {}).get("validation_passed") is False
            had_error = any(i.get("severity") == "error" for i in ((lb or {}).get("issues") or []))
            ok_b = refused and failed_validation and had_error and v.spoke(turn)
            print(f"   state={lb and lb.get('state')} validation_passed={failed_validation is False} "
                  f"spoke_the_refusal={v.spoke(turn)} -> {'PASS' if ok_b else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_b)

            # --- c: correct it -> compiled, validated, read back for sign-off ----
            print("c: correct the volume -> validated plan read back, awaiting sign-off", flush=True)
            turn = await v.say(FIX)
            lb = v.last(turn, "lab_backend")
            rd = v.last(turn, "reply_done")
            armed = lb is not None and lb.get("state") == "awaiting_confirmation"
            validated = (lb or {}).get("validation_passed") is True
            has_ops = ((lb or {}).get("operations") or 0) > 0
            ok_c = armed and validated and has_ops and v.spoke(turn)
            print(f"   state={lb and lb.get('state')} validation_passed={validated} "
                  f"operations={(lb or {}).get('operations')} -> {'PASS' if ok_c else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_c)

            # --- d: confirm it aloud -> the backend runs it ----------------------
            print("d: confirm aloud -> backend executes (simulated run)", flush=True)
            turn = await v.say(CONFIRM, pmin=0.95, pmean=0.96)
            lb = v.last(turn, "lab_backend")
            rd = v.last(turn, "reply_done")
            ok_d = lb is not None and lb.get("state") == "executed" and v.spoke(turn)
            print(f"   state={lb and lb.get('state')} spoke_result={v.spoke(turn)} "
                  f"-> {'PASS' if ok_d else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_d)

        # --- e: a MISHEARD confirmation must never reach the backend -------------
        print("e: plan again, then MUMBLE the confirmation (prob_mean 0.20 < floor 0.40)", flush=True)
        async with Voice(url) as v:
            await plan_up_to_confirmation(v)
            turn = await v.say(CONFIRM, pmin=0.18, pmean=0.20)
            lb = v.last(turn, "lab_backend")
            rej = v.last(turn, "action_rejected")
            rd = v.last(turn, "reply_done")
            # No lab_backend message means no POST happened at all, so the backend's
            # state machine cannot have moved: the misheard "yes" never existed to it.
            not_forwarded = lb is None
            refused = rej is not None and "low_confidence" in (rej.get("reason") or "")
            reprompted = "confirm" in ((rd or {}).get("text") or "").lower()
            ok_e = not_forwarded and refused and reprompted and v.spoke(turn)
            print(f"   not_forwarded_to_backend={not_forwarded} refused={refused} "
                  f"reprompted_aloud={reprompted} -> {'PASS' if ok_e else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_e)

            # --- f: the floor refuses an utterance, it does not wedge the session -
            print("f: say it clearly this time -> executes", flush=True)
            turn = await v.say(CONFIRM, pmin=0.95, pmean=0.96)
            lb = v.last(turn, "lab_backend")
            ok_f = lb is not None and lb.get("state") == "executed"
            print(f"   state={lb and lb.get('state')} -> {'PASS' if ok_f else 'FAIL'}", flush=True)
            results.append(ok_f)

        # --- g: cancel executes nothing -----------------------------------------
        print("g: plan, then say cancel -> nothing runs", flush=True)
        async with Voice(url) as v:
            await plan_up_to_confirmation(v)
            turn = await v.say("cancel", pmin=0.95, pmean=0.96)
            lb = v.last(turn, "lab_backend")
            rd = v.last(turn, "reply_done")
            state = (lb or {}).get("state")
            ok_g = state is not None and state != "executed"
            print(f"   state={state} (not executed) -> {'PASS' if ok_g else 'FAIL'}", flush=True)
            print(f"   said: {(rd or {}).get('text','')[:110]!r}", flush=True)
            results.append(ok_g)

    finally:
        vp.terminate()
        bp.terminate()

    ok = all(results)
    print(f"\nINTEGRATION-SMOKE: {'PASS' if ok else 'FAIL'} ({sum(results)}/{len(results)})", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
