"""Local WS smoke for barge-in cancellation (cancel_turn / reply_cancelled).

Runs against web/server.py + the mock ASR/TTS + the REAL Claude Haiku API (see
scripts/run_local_smoke.sh). The LLM stage is real, so a turn's reply streams
over the network: we start a turn, wait for the first reply_delta (guaranteeing
some partial text exists and the reply is still in flight), then send cancel_turn
and assert the server interrupts it. Flow + assertions:

  (a) cancel mid-reply: after the first reply_delta, send cancel_turn; expect a
      terminal reply_cancelled carrying the partial text streamed so far.
  (b) recovery: a following normal turn still produces reply_start/reply_done and
      closes with reply_audio_end, proving the pipeline is unblocked.
  (c) idle no-op: cancel_turn with nothing in flight sends no reply_cancelled and
      the socket stays live (a ping still gets a pong).

Placed LAST in the runner so its ASR segments do not
shift the mock ASR's process-global canned-segment counter for earlier scripts.
"""
import asyncio
import base64
import json
import os
import sys

import websockets

PORT = os.environ.get("LA_WS_PORT", "8765")
URL = f"ws://localhost:{PORT}/"
SEG = json.dumps({"type": "audio_segment",
                  "audio_b64": base64.b64encode(b"\x00\x00" * 8000).decode(),
                  "sample_rate": 16000})


async def main() -> int:
    counts = {}
    cancelled_msgs = []

    def note(m):
        t = m.get("type")
        counts[t] = counts.get(t, 0) + 1
        if t == "reply_cancelled":
            cancelled_msgs.append(m)
            print(f"  <- reply_cancelled: text={m.get('text')!r}", flush=True)
        elif t == "error":
            print(f"  <- ERROR: {m.get('text')}", flush=True)
        elif t in ("reply_start", "reply_done", "reply_audio_end", "status"):
            print(f"  <- {t}: {m.get('text','')}", flush=True)

    async def recv_one(timeout):
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))

    async def drain(timeout):
        try:
            while True:
                note(await recv_one(timeout))
        except asyncio.TimeoutError:
            pass

    async def recv_until_type(want, timeout=45):
        while True:
            m = await recv_one(timeout)
            note(m)
            if m.get("type") == want:
                return m

    async def recv_until_count(want, target, timeout=45):
        try:
            while counts.get(want, 0) < target:
                note(await recv_one(timeout))
        except asyncio.TimeoutError:
            print(f"  <- TIMEOUT waiting for {want} >= {target}", flush=True)

    async with websockets.connect(URL, max_size=16 * 1024 * 1024) as ws:
        # connect handshake (status, tts_params, session_started) and settle
        await drain(3)

        # (a) cancel mid-reply --------------------------------------------------
        print("turn A: segment + end_turn, cancel on first reply_delta", flush=True)
        await ws.send(SEG)
        await recv_until_type("transcript", timeout=30)
        await ws.send(json.dumps({"type": "end_turn"}))
        # wait for the reply to actually start streaming so partial text exists
        await recv_until_type("reply_delta", timeout=45)
        await ws.send(json.dumps({"type": "cancel_turn"}))
        # reply_cancelled is the terminal message of the cancelled turn
        await recv_until_type("reply_cancelled", timeout=30)
        cancel_ok = (len(cancelled_msgs) == 1
                     and isinstance(cancelled_msgs[0].get("text"), str)
                     and cancelled_msgs[0].get("text").strip() != "")
        # nothing more should follow reply_cancelled for this turn
        await drain(2)
        terminal_ok = len(cancelled_msgs) == 1

        # (b) recovery: a normal turn still works -------------------------------
        print("turn B: normal turn after a cancel, expect reply_audio_end", flush=True)
        start_before = counts.get("reply_start", 0)
        done_before = counts.get("reply_done", 0)
        end_before = counts.get("reply_audio_end", 0)
        await ws.send(SEG)
        await recv_until_type("transcript", timeout=30)
        await ws.send(json.dumps({"type": "end_turn"}))
        await recv_until_count("reply_audio_end", end_before + 1, timeout=45)
        recover_ok = (counts.get("reply_start", 0) == start_before + 1
                      and counts.get("reply_done", 0) == done_before + 1
                      and counts.get("reply_audio_end", 0) == end_before + 1)

        # (c) idle cancel is a no-op --------------------------------------------
        print("idle: cancel_turn with nothing in flight, expect no reply_cancelled", flush=True)
        cancelled_before = len(cancelled_msgs)
        await ws.send(json.dumps({"type": "cancel_turn"}))
        await drain(2)
        await ws.send(json.dumps({"type": "ping"}))
        pong = await recv_until_type("status", timeout=10)
        idle_ok = (len(cancelled_msgs) == cancelled_before
                   and (pong.get("text") == "pong"))

    print(f"\ncounts: {counts}", flush=True)
    print(f"cancel_ok={cancel_ok} terminal_ok={terminal_ok} "
          f"recover_ok={recover_ok} idle_ok={idle_ok}", flush=True)
    ok = cancel_ok and terminal_ok and recover_ok and idle_ok
    print("SMOKE: PASS (cancel mid-reply + recovery + idle no-op)"
          if ok else "SMOKE: FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
