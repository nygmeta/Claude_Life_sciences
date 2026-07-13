"""Local WS smoke for speculative LLM start (spec Stage 1).

Runs against web/server.py + the mock ASR/TTS + the REAL Claude Haiku API (see
scripts/run_local_smoke.sh). Covers the five spec scenarios:

  1. invisible: one segment, NO end_turn, wait past the LLM's first token ->
     assert the client received no reply_start (the speculation is gated).
  2. refire: segment -> segment -> end_turn -> assert exactly one reply, and the
     committed latency record shows asr.segments == 2 (both segments, one turn).
  3. committed: segment -> end_turn -> assert the reply arrives and the turn's
     latency record has spec.committed == true, spec.enabled == true, and a sane
     commit_to_first_audio_ms.
  4. disabled: a second orchestrator started with LA_SPEC_START=0 -> its turn's
     record shows spec.enabled == false, spec.committed == false, spec.fired == 0.
  5. cancellation stays green: covered by smoke_cancel.py in the runner (a
     committed speculation behaves exactly like today's committed reply for
     barge-in), so it is not re-tested here.

Placed LAST in the runner: it sends segments and starts
a throwaway second server, so it must not shift state earlier scripts rely on.
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

import websockets

PORT = os.environ.get("LA_WS_PORT", "8765")
URL = f"ws://localhost:{PORT}/"
SEG = json.dumps({"type": "audio_segment",
                  "audio_b64": base64.b64encode(b"\x00\x00" * 8000).decode(),
                  "sample_rate": 16000})

APP = Path(__file__).resolve().parent.parent
LOG_FILE = Path(os.environ.get("LA_LOG_FILE", str(APP / "data" / "latency.jsonl")))
SPEC_OFF_PORT = os.environ.get("LA_SPEC_OFF_PORT", "8798")


def _read_records(sid: str) -> list:
    """All assistant latency records for a session id, oldest first."""
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
        if rec.get("kind") == "assistant" and rec.get("session") == sid:
            out.append(rec)
    return out


async def _committed_record(sid: str, timeout=8.0) -> dict:
    """Poll the log for this session's committed assistant record (has a spec
    block and a terminal status). log_event lands just after reply_audio_end."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        recs = [r for r in _read_records(sid)
                if r.get("spec") is not None
                and str(r.get("status", "")).startswith(("ok", "empty_reply"))]
        if recs:
            return recs[-1]
        await asyncio.sleep(0.4)
    return {}


class Client:
    """A tiny WS harness: connect, capture the session id from session_started,
    count message types, and expose helpers to send segments / end_turn."""

    def __init__(self, url):
        self.url = url
        self.ws = None
        self.sid = None
        self.counts = {}

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    def _note(self, m):
        t = m.get("type")
        self.counts[t] = self.counts.get(t, 0) + 1
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

    async def recv_until_count(self, want, target, timeout=45):
        try:
            while self.counts.get(want, 0) < target:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout)))
        except asyncio.TimeoutError:
            print(f"  <- TIMEOUT waiting for {want} >= {target}", flush=True)

    async def send(self, obj):
        await self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))


def _wait_port(port, timeout=25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True
        time.sleep(0.3)
    return False


async def scenario_invisible() -> bool:
    """(1) A lone segment fires a speculation; with no end_turn, the gated reply
    must not surface: no reply_start / reply_delta reaches the client."""
    print("scenario 1: segment, no end_turn -> speculation stays invisible", flush=True)
    async with Client(URL) as c:
        await c.drain(3)                    # handshake (status, tts_params, session_started)
        await c.send(SEG)
        await c.recv_until_count("transcript", 1, timeout=30)
        await c.drain(7)                    # well past Claude's first-token latency
        ok = c.counts.get("reply_start", 0) == 0 and c.counts.get("reply_delta", 0) == 0
    print(f"  reply_start={c.counts.get('reply_start', 0)} "
          f"reply_delta={c.counts.get('reply_delta', 0)} -> invisible_ok={ok}", flush=True)
    return ok


async def scenario_refire() -> bool:
    """(2) Two segments then end_turn: exactly one reply, and the committed record
    shows both segments folded into one turn (asr.segments == 2)."""
    print("scenario 2: segment -> segment -> end_turn -> one reply, both segments", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.send(SEG)
        await c.send(SEG)
        await c.recv_until_count("transcript", 2, timeout=30)
        await c.send({"type": "end_turn"})
        await c.recv_until_count("reply_audio_end", 1, timeout=45)
        await c.drain(2)
        rec = await _committed_record(c.sid)
    one_reply = (c.counts.get("reply_start", 0) == 1
                 and c.counts.get("reply_done", 0) == 1
                 and c.counts.get("reply_audio_end", 0) == 1)
    two_segments = (rec.get("asr") or {}).get("segments") == 2
    ok = one_reply and two_segments
    print(f"  counts={ {k: c.counts.get(k) for k in ('reply_start','reply_done','reply_audio_end')} } "
          f"asr.segments={(rec.get('asr') or {}).get('segments')} -> refire_ok={ok}", flush=True)
    return ok


async def scenario_committed() -> bool:
    """(3) Segment (short pause so the speculation can start) then end_turn: the
    record is a committed speculation with a sane commit_to_first_audio_ms."""
    print("scenario 3: segment -> pause -> end_turn -> spec.committed record", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        await c.send(SEG)
        await c.recv_until_count("transcript", 1, timeout=30)
        await asyncio.sleep(2.0)             # let the speculation get ahead of commit
        await c.send({"type": "end_turn"})
        await c.recv_until_count("reply_audio_end", 1, timeout=45)
        await c.drain(1)
        rec = await _committed_record(c.sid)
    spec = rec.get("spec") or {}
    cfa = spec.get("commit_to_first_audio_ms")
    ok = (spec.get("enabled") is True and spec.get("committed") is True
          and spec.get("fired", 0) >= 1
          and isinstance(cfa, (int, float)) and cfa >= 0)
    print(f"  spec={spec} -> committed_ok={ok}", flush=True)
    return ok


async def scenario_disabled() -> bool:
    """(4) A throwaway orchestrator with LA_SPEC_START=0: its turn's record shows
    the feature off (enabled false, committed false, fired 0), and the flow still
    produces a normal reply."""
    print(f"scenario 4: LA_SPEC_START=0 orchestrator on :{SPEC_OFF_PORT}", flush=True)
    env = dict(os.environ)
    env["LA_WS_PORT"] = SPEC_OFF_PORT
    env["LA_SPEC_START"] = "0"
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(SPEC_OFF_PORT):
            print("  <- TIMEOUT: spec-off server never bound the port", flush=True)
            return False
        async with Client(f"ws://localhost:{SPEC_OFF_PORT}/") as c:
            await c.drain(3)
            await c.send(SEG)
            await c.recv_until_count("transcript", 1, timeout=30)
            await c.drain(7)                 # feature off: NO gated reply should appear early
            early_reply = c.counts.get("reply_start", 0)
            await c.send({"type": "end_turn"})
            await c.recv_until_count("reply_audio_end", 1, timeout=45)
            await c.drain(1)
            rec = await _committed_record(c.sid)
        spec = rec.get("spec") or {}
        ok = (spec.get("enabled") is False and spec.get("committed") is False
              and spec.get("fired", 0) == 0 and early_reply == 0
              and c.counts.get("reply_audio_end", 0) == 1)
        print(f"  spec={spec} early_reply={early_reply} -> disabled_ok={ok}", flush=True)
        return ok
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


async def main() -> int:
    s1 = await scenario_invisible()
    s2 = await scenario_refire()
    s3 = await scenario_committed()
    s4 = await scenario_disabled()
    ok = s1 and s2 and s3 and s4
    print(f"\ninvisible_ok={s1} refire_ok={s2} committed_ok={s3} disabled_ok={s4}", flush=True)
    print("SMOKE: PASS (speculative start: invisible + refire + committed + disabled)"
          if ok else "SMOKE: FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
