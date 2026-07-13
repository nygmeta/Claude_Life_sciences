"""Local WS smoke for addressed-speech detection (Phase 4a).

Starts its OWN orchestrator (LA_ADDRESSED=1, LA_LAB_MODE=1) on a free port against
the shared mock ASR/TTS and the REAL Claude Haiku API (the classifier IS the thing
under test, so it is not mocked). Utterances ride the Part A filename-override hook,
so the mock ASR echoes them back verbatim and the canned-segment counter that the
other smokes depend on is never touched.

Scenarios (ADDR-SMOKE a..d PASS/FAIL + overall):
  a. "dispense 10 microliters into well B2"  -> transcript addressed:true, and the
     turn proceeds exactly as in lab mode (action_pending, irreversible => confirm).
  b. "did you see the game last night"       -> transcript addressed:false, and the
     turn is dropped: no reply, no action, and end_turn produces nothing.
  c. with a confirmation OUTSTANDING, a bare "confirm" -> addressed via the
     deterministic fast path (no model call), and the action executes. This is the
     one that must never regress: a classifier must not be able to eat a
     confirmation.
  d. "I think the assistant is broken"       -> third-person talk ABOUT the
     assistant is side speech: addressed:false, no reply.
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
ADDR_PORT = os.environ.get("LA_ADDR_PORT", "8795")
URL = f"ws://localhost:{ADDR_PORT}/"


def utter_seg(text, pmin=0.97, pmean=0.98):
    token = f"utter;text={quote(text)};pmin={pmin};pmean={pmean}.wav".encode("ascii")
    return json.dumps({"type": "audio_segment",
                       "audio_b64": base64.b64encode(token).decode(), "sample_rate": 16000})


class Client:
    def __init__(self, url):
        self.url = url
        self.ws = None
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
        if t == "error":
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

    async def segment(self, text, pmin=0.97):
        """One segment, waiting only for its transcript (which now carries the
        addressed verdict). Does NOT end the turn."""
        before = self.counts.get("transcript", 0)
        await self.send(utter_seg(text, pmin))
        await self.recv_until_count("transcript", before + 1, timeout=40)
        return self.last("transcript")

    async def turn(self, text, pmin=0.97, timeout=75):
        """A full addressed turn: segment, end_turn, wait for the reply to finish."""
        rbefore = self.counts.get("reply_audio_end", 0)
        await self.segment(text, pmin)
        await self.send({"type": "end_turn"})
        await self.recv_until_count("reply_audio_end", rbefore + 1, timeout=timeout)
        await self.drain(0.5)

    async def dropped_turn(self, text, pmin=0.97):
        """A turn that SHOULD be dropped as side speech: segment, end_turn, then
        give the server a generous window to (incorrectly) start replying."""
        tr = await self.segment(text, pmin)
        await self.send({"type": "end_turn"})
        await self.drain(6.0)
        return tr

    def has(self, t):
        return any(m.get("type") == t for m in self.msgs)

    def last(self, t):
        return next((m for m in reversed(self.msgs) if m.get("type") == t), None)


async def scenario_a():
    print("a: lab command -> addressed:true, turn proceeds (action_pending)", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 10 microliters into well B2", pmin=0.95)
        tr = c.last("transcript")
        ap = c.last("action_pending")
        addressed_ok = tr is not None and tr.get("addressed") is True
        ok = addressed_ok and ap is not None and ap.get("intent") == "dispense"
    print(f"   addressed={tr and tr.get('addressed')} action_pending={ap is not None} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_b():
    print("b: human small talk -> addressed:false, no reply, no action", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        tr = await c.dropped_turn("did you see the game last night")
        not_addressed = tr is not None and tr.get("addressed") is False
        silent = not (c.has("reply_start") or c.has("reply_done") or c.has("reply_audio"))
        no_action = not (c.has("action_pending") or c.has("action_executed"))
        ok = not_addressed and silent and no_action
    print(f"   addressed={tr and tr.get('addressed')} no_reply={silent} no_action={no_action} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_c():
    print("c: pending confirmation + 'confirm dispense' -> addressed fast path, executes", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.turn("dispense 50 microliters into well A3", pmin=0.95)
        pending_ok = c.last("action_pending") is not None
        # the confirm is intent-bound now (dispense is IRREVERSIBLE); the addressed
        # fast path still fires on the "confirm" word, and the bound phrase executes.
        await c.turn("confirm dispense", pmin=0.97)
        tr = c.last("transcript")
        ae = c.last("action_executed")
        # the fast path must mark it addressed (a dropped confirmation is the
        # worst failure this feature could introduce)
        confirm_addressed = tr is not None and tr.get("addressed") is True
        ok = pending_ok and confirm_addressed and ae is not None and ae.get("confirmed") is True
    print(f"   pending={pending_ok} confirm_addressed={confirm_addressed} "
          f"executed={ae is not None and ae.get('confirmed') is True} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_d():
    print("d: third-person talk ABOUT the assistant -> addressed:false, no reply", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        tr = await c.dropped_turn("I think the assistant is broken")
        not_addressed = tr is not None and tr.get("addressed") is False
        silent = not (c.has("reply_start") or c.has("reply_done") or c.has("reply_audio"))
        ok = not_addressed and silent
    print(f"   addressed={tr and tr.get('addressed')} no_reply={silent} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


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
    env["LA_WS_PORT"] = ADDR_PORT
    env["LA_LAB_MODE"] = "1"
    env["LA_ADDRESSED"] = "1"
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(ADDR_PORT):
            print("ADDR-SMOKE: FAIL: orchestrator never bound the port", flush=True)
            return 1
        results = {"a": await scenario_a(), "b": await scenario_b(),
                   "c": await scenario_c(), "d": await scenario_d()}
        for name, ok in results.items():
            print(f"ADDR-SMOKE {name}: {'PASS' if ok else 'FAIL'}", flush=True)
        overall = all(results.values())
        print("ADDR-SMOKE: PASS" if overall else "ADDR-SMOKE: FAIL", flush=True)
        return 0 if overall else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
