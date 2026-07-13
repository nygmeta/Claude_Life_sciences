"""Local WS smoke test for turn-batching + sentence-streamed TTS AND the
per-session assistant TTS params contract. Flow:

  1. Handshake: on connect the server sends a tts_params message (session params
     all unset + server generation defaults); assert it is present.
  2. Batching: send TWO speech segments (no reply should fire), then end_turn
     (exactly one text reply: reply_start/delta/done + >=1 reply_audio ending in
     one reply_audio_end, seq 0-based and contiguous).
  3. set_tts_params round-trip: send new params, assert the tts_params echo
     carries the updated values.
  4. Second turn: an assistant turn AFTER params are set still streams
     reply_audio chunks and closes with reply_audio_end.

Runs against web/server.py + the mock ASR/TTS + REAL Claude Haiku (see
scripts/run_local_smoke.sh).
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
# assistant-path TTS params to set mid-session: a voice plus all four gen knobs.
DESIRED = {"voice": "en_andrew", "temperature": 0.7, "cfg_scale": 2.0,
           "top_k": 20, "max_frames": 500}
_PARAM_KEYS = ("voice", "temperature", "cfg_scale", "top_k", "max_frames")
_DEFAULT_KEYS = ("temperature", "cfg_scale", "top_k", "max_frames")


def _handshake_ok(m) -> bool:
    """The connect tts_params: session params all unset (None) + numeric defaults."""
    p = m.get("params") or {}
    d = m.get("defaults") or {}
    params_unset = all(p.get(k) is None for k in _PARAM_KEYS)
    defaults_numeric = all(isinstance(d.get(k), (int, float)) for k in _DEFAULT_KEYS)
    return params_unset and defaults_numeric


async def main() -> int:
    counts = {}
    reply_text = []
    audio_seqs = []
    tts_params_msgs = []

    def note(m):
        t = m.get("type")
        counts[t] = counts.get(t, 0) + 1
        if t == "reply_delta":
            reply_text.append(m.get("text", ""))
        elif t == "reply_audio":
            audio_seqs.append(m.get("seq"))
        elif t == "tts_params":
            tts_params_msgs.append(m)
        elif t == "error":
            print(f"  <- ERROR: {m.get('text')}", flush=True)
        if t in ("transcript", "reply_start", "reply_done", "reply_audio",
                 "reply_audio_end", "tts_params"):
            d = m.get("text", "")
            if t == "reply_audio":
                d = f"seq={m.get('seq')} {len(m.get('audio_b64',''))} b64 @ {m.get('sample_rate')}Hz"
            elif t == "reply_audio_end":
                d = f"chunks={m.get('chunks')}"
            elif t == "tts_params":
                d = f"params={m.get('params')} defaults={m.get('defaults')}"
            print(f"  <- {t}: {d}", flush=True)

    async with websockets.connect(URL, max_size=16 * 1024 * 1024) as ws:
        async def drain(timeout):
            try:
                while True:
                    note(json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout)))
            except asyncio.TimeoutError:
                pass

        async def recv_until(count_type, target, timeout=45):
            try:
                while counts.get(count_type, 0) < target:
                    note(json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout)))
            except asyncio.TimeoutError:
                print(f"  <- TIMEOUT waiting for {count_type} >= {target}", flush=True)

        print("send segment 1 + segment 2 (one turn, no end_turn yet)", flush=True)
        await ws.send(SEG)
        await ws.send(SEG)
        # collect transcripts + the connect handshake (status, tts_params); nothing
        # should reply until we end the turn.
        await drain(4)
        pre_reply = counts.get("reply_start", 0)
        handshake_ok = bool(tts_params_msgs) and _handshake_ok(tts_params_msgs[0])

        print("send end_turn -> expect one text reply + >=1 audio chunk", flush=True)
        await ws.send(json.dumps({"type": "end_turn"}))
        await recv_until("reply_audio_end", 1)
        first = dict(counts)              # snapshot: turn-1 assertions read from here
        first_seqs = list(audio_seqs)

        print(f"set_tts_params {DESIRED} -> expect a tts_params echo", flush=True)
        before = len(tts_params_msgs)
        await ws.send(json.dumps({"type": "set_tts_params", **DESIRED}))
        try:
            while len(tts_params_msgs) <= before:
                note(json.loads(await asyncio.wait_for(ws.recv(), timeout=10)))
        except asyncio.TimeoutError:
            print("  <- TIMEOUT waiting for tts_params echo", flush=True)
        echo = tts_params_msgs[-1] if tts_params_msgs else {}
        set_ok = echo.get("params") == DESIRED

        # A two-segment second turn (mirrors turn 1). Two segments also keep this
        # script's ASR-request count EVEN: the mock ASR alternates its two canned
        # segments off a process-global counter shared with the later smoke
        # scripts, and smoke_hints.py relies on that parity to land on the
        # "...weather..." segment its replacement fixes up.
        print("second turn on the new params -> expect reply_audio again", flush=True)
        await ws.send(SEG)
        await ws.send(SEG)
        await recv_until("transcript", first.get("transcript", 0) + 2)
        await ws.send(json.dumps({"type": "end_turn"}))
        await recv_until("reply_audio_end", 2)

    rt = "".join(reply_text).strip()
    print(f"\ncounts: {counts}", flush=True)
    print(f"turn-1 reply_audio seqs: {first_seqs}", flush=True)
    print(f"assistant reply: {rt!r}", flush=True)

    seqs_contiguous = first_seqs == list(range(len(first_seqs)))
    second_audio = counts.get("reply_audio", 0) - first.get("reply_audio", 0)
    turn1_ok = (first.get("transcript", 0) >= 2       # both segments transcribed
                and pre_reply == 0                    # no reply before end_turn (batched)
                and first.get("reply_start", 0) == 1  # exactly one reply for the turn
                and first.get("reply_done", 0) == 1
                and first.get("reply_audio", 0) >= 1
                and seqs_contiguous                   # seq 0, 1, 2, ... in order
                and first.get("reply_audio_end", 0) == 1)
    turn2_ok = (second_audio >= 1                     # audio still flows after set_tts_params
                and counts.get("reply_audio_end", 0) == 2)
    ok = turn1_ok and handshake_ok and set_ok and turn2_ok and bool(rt)
    print(f"\nturn1_ok={turn1_ok} handshake_ok={handshake_ok} set_ok={set_ok} "
          f"turn2_ok={turn2_ok} (second_audio={second_audio})", flush=True)
    print("SMOKE: PASS (batched turn + tts_params round-trip + audio on new params)"
          if ok else "SMOKE: FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
