"""Local WS smoke for the proactive event channel (Phase 3).

Starts its OWN orchestrator (LA_EVENTS=1, LA_LAB_MODE=1) on a free port, against
the shared mock ASR/TTS and the REAL Claude Haiku API (only where a streaming
reply is needed). Operator identity is supplied via the ?email= connect query param
(LA_OPERATOR_EMAILS), like smoke_multi.

Scenarios (EVT-SMOKE a..e PASS/FAIL + overall):
  a. operator injects an info while idle -> announce/announce_audio/announce_end
     in order on BOTH an operator and a public client (broadcast).
  b. alert injected mid-reply -> reply_cancelled, then the announce triple; the
     cancelled turn logs status "cancelled".
  c. info injected mid-reply -> the reply finishes (reply_done + reply_audio_end)
     BEFORE the announce triple (info defers, never interrupts).
  d. a non-operator inject_event -> error, and no announce reaches anyone.
  e. lab confirm flow start_centrifuge minutes=0.02 -> after ~1.2 s the owning
     session (and ONLY it) gets the completion announce triple.
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
EVT_PORT = os.environ.get("LA_EVT_PORT", "8796")
URL = f"ws://localhost:{EVT_PORT}/"
LOG_FILE = Path(os.environ.get("LA_LOG_FILE", str(APP / "data" / "latency.jsonl")))
_ops = [e.strip() for e in os.environ.get("LA_OPERATOR_EMAILS", "").split(",") if e.strip()]
OP_EMAIL = _ops[0] if _ops else "operator.smoke@example.com"

TRIPLE = ("announce", "announce_audio", "announce_end")


def utter_seg(text, pmin=0.97, pmean=0.98):
    token = f"utter;text={quote(text)};pmin={pmin};pmean={pmean}.wav".encode("ascii")
    return json.dumps({"type": "audio_segment",
                       "audio_b64": base64.b64encode(token).decode(), "sample_rate": 16000})


class Client:
    def __init__(self, email=None):
        self.email = email
        self.ws = None
        self.sid = None
        self.counts = {}
        self.msgs = []

    async def __aenter__(self):
        # identity via the ?email= connect query param (Cloudflare Access is gone)
        url = URL + (f"?email={quote(self.email)}" if self.email else "")
        self.ws = await websockets.connect(url, max_size=16 * 1024 * 1024)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    def _note(self, m):
        t = m.get("type")
        self.counts[t] = self.counts.get(t, 0) + 1
        self.msgs.append(m)
        if t == "session_started":
            self.sid = m.get("id")

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
        tbefore = self.counts.get("transcript", 0)
        rbefore = self.counts.get("reply_audio_end", 0)
        await self.send(utter_seg(text, pmin, pmean))
        await self.recv_until_count("transcript", tbefore + 1, timeout=30)
        await self.send({"type": "end_turn"})
        await self.recv_until_count("reply_audio_end", rbefore + 1, timeout=timeout)
        await self.drain(0.4)

    def announce_types(self):
        return [m["type"] for m in self.msgs if m["type"] in TRIPLE]

    def has(self, t):
        return any(m.get("type") == t for m in self.msgs)

    def types_order(self):
        return [m.get("type") for m in self.msgs]


def _read_records(sid):
    if not LOG_FILE.is_file():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if rec.get("session") == sid:
            out.append(rec)
    return out


async def scenario_a():
    print("a: operator injects info (idle) -> triple on operator AND public (broadcast)", flush=True)
    async with Client(OP_EMAIL) as op, Client(None) as pub:
        await op.drain(3)
        await pub.drain(3)
        await op.send({"type": "inject_event", "severity": "info",
                       "text": "Reagent shipment has arrived.", "broadcast": True})
        await op.recv_until_count("announce_end", 1, timeout=30)
        await pub.recv_until_count("announce_end", 1, timeout=30)
        ok = op.announce_types() == list(TRIPLE) and pub.announce_types() == list(TRIPLE)
    print(f"   op_triple={op.announce_types()} pub_triple={pub.announce_types()} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_b():
    print("b: alert mid-reply -> reply_cancelled then triple; turn logs 'cancelled'", flush=True)
    async with Client(OP_EMAIL) as op:
        await op.drain(3)
        # start a streaming reply, then preempt it with an alert before it finishes
        await op.send(utter_seg("tell me a short story about the ocean", pmin=0.97))
        await op.recv_until_count("transcript", 1, timeout=30)
        await op.send({"type": "end_turn"})
        await op.send({"type": "inject_event", "severity": "alert",
                       "text": "Pressure limit exceeded. Halting.", "broadcast": True})
        await op.recv_until_count("announce_end", 1, timeout=45)
        await op.drain(0.5)
        cancelled_first = op.has("reply_cancelled")
        # reply_cancelled must precede the announce triple
        order = op.types_order()
        order_ok = ("reply_cancelled" in order and "announce" in order
                    and order.index("reply_cancelled") < order.index("announce"))
        triple_ok = op.announce_types() == list(TRIPLE)
        await asyncio.sleep(0.3)
        logged_cancelled = any(r.get("status") == "cancelled" for r in _read_records(op.sid))
        ok = cancelled_first and order_ok and triple_ok and logged_cancelled
    print(f"   reply_cancelled={cancelled_first} order_ok={order_ok} triple={triple_ok} "
          f"logged_cancelled={logged_cancelled} -> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_c():
    print("c: info mid-reply -> reply finishes BEFORE the triple (info defers)", flush=True)
    async with Client(OP_EMAIL) as op:
        await op.drain(3)
        await op.send(utter_seg("what is the weather like", pmin=0.97))
        await op.recv_until_count("transcript", 1, timeout=30)
        await op.send({"type": "end_turn"})
        await op.send({"type": "inject_event", "severity": "info",
                       "text": "Sample nine is ready.", "broadcast": True})
        await op.recv_until_count("announce_end", 1, timeout=60)
        await op.drain(0.5)
        order = op.types_order()
        # the reply's terminal messages must precede the announce
        defer_ok = ("reply_audio_end" in order and "announce" in order
                    and order.index("reply_audio_end") < order.index("announce")
                    and order.index("reply_done") < order.index("announce"))
        triple_ok = op.announce_types() == list(TRIPLE)
        ok = defer_ok and triple_ok
    print(f"   reply_before_announce={defer_ok} triple={triple_ok} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_d():
    print("d: non-operator inject_event -> error, no announce anywhere", flush=True)
    async with Client(OP_EMAIL) as op, Client(None) as pub:
        await op.drain(3)
        await pub.drain(3)
        await pub.send({"type": "inject_event", "severity": "info",
                        "text": "should not be delivered", "broadcast": True})
        await pub.recv_until_count("error", 1, timeout=10)
        await op.drain(2)          # give any (erroneous) broadcast time to arrive
        await pub.drain(0.5)
        got_error = pub.has("error")
        no_announce = not op.has("announce") and not pub.has("announce")
        ok = got_error and no_announce
    print(f"   error={got_error} no_announce_anywhere={no_announce} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


async def scenario_e():
    print("e: centrifuge minutes=0.02 (confirm) -> owning session ONLY gets completion", flush=True)
    async with Client(OP_EMAIL) as op, Client(None) as pub:
        await op.drain(3)
        await pub.drain(3)
        await op.turn("start the centrifuge at 3000 rpm for 0.02 minutes", pmin=0.97)
        pending_ok = op.has("action_pending")
        await op.turn("confirm centrifuge", pmin=0.97)   # centrifuge is bound (HAZARDOUS)
        executed_ok = any(m.get("type") == "action_executed" and m.get("confirmed") is True
                          for m in op.msgs)
        # completion event fires ~1.2 s after execute; wait it out on both clients
        await op.recv_until_count("announce_end", 1, timeout=15)
        await pub.drain(1.0)
        owner_triple = op.announce_types() == list(TRIPLE)
        stub_source = any(m.get("type") == "announce" and m.get("source") == "stub"
                          for m in op.msgs)
        pub_silent = not pub.has("announce")
        ok = pending_ok and executed_ok and owner_triple and stub_source and pub_silent
    print(f"   pending={pending_ok} executed={executed_ok} owner_triple={owner_triple} "
          f"source_stub={stub_source} public_silent={pub_silent} "
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
    env["LA_WS_PORT"] = EVT_PORT
    env["LA_LAB_MODE"] = "1"
    env["LA_EVENTS"] = "1"
    env["LA_OPERATOR_EMAILS"] = OP_EMAIL
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(EVT_PORT):
            print("EVT-SMOKE: FAIL: event orchestrator never bound the port", flush=True)
            return 1
        results = {
            "a": await scenario_a(),
            "b": await scenario_b(),
            "c": await scenario_c(),
            "d": await scenario_d(),
            "e": await scenario_e(),
        }
        for name, ok in results.items():
            print(f"EVT-SMOKE {name}: {'PASS' if ok else 'FAIL'}", flush=True)
        overall = all(results.values())
        print("EVT-SMOKE: PASS" if overall else "EVT-SMOKE: FAIL", flush=True)
        return 0 if overall else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
