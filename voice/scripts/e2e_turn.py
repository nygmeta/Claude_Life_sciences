"""One real end-to-end turn against the LIVE local stack: real ASR (GPU box via
the forward), real Claude (from the orchestrator's own context), real gepard TTS.
Sends an actual speech wav as a segment, ends the turn, and asserts the full
reply pipeline.

Run with the project's Python:

    python3 scripts/e2e_turn.py [ws_url] [wav_path]
"""
import asyncio
import base64
import json
import sys
import wave

import websockets

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8765/"
WAV = sys.argv[2] if len(sys.argv) > 2 else "data/pod-rescue/logs/pin_test_1.wav"


async def main() -> int:
    with wave.open(WAV, "rb") as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    print(f"sending {len(pcm)} bytes PCM @ {rate} Hz from {WAV}")

    transcript, reply, audio_chunks, audio_end = [], [], 0, False
    async with websockets.connect(URL, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "audio_segment", "sample_rate": rate,
                                  "audio_b64": base64.b64encode(pcm).decode()}))
        await ws.send(json.dumps({"type": "end_turn"}))
        try:
            while not audio_end:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
                t = m.get("type")
                if t == "transcript":
                    transcript.append(m.get("text", ""))
                    print(f"  <- transcript: {m.get('text')!r}")
                elif t == "reply_delta":
                    reply.append(m.get("text", ""))
                elif t == "reply_done":
                    print(f"  <- reply_done: {''.join(reply)[:120]!r}")
                elif t == "reply_audio":
                    audio_chunks += 1
                elif t == "reply_audio_end":
                    audio_end = True
                    print(f"  <- reply_audio_end after {audio_chunks} chunk(s)")
                elif t == "error":
                    print(f"  <- ERROR: {m.get('text')}")
        except asyncio.TimeoutError:
            print("  <- TIMEOUT")

    ok = bool("".join(transcript).strip()) and bool("".join(reply).strip()) \
        and audio_chunks >= 1 and audio_end
    print(f"\ntranscript_ok={bool(''.join(transcript).strip())} "
          f"reply_ok={bool(''.join(reply).strip())} "
          f"audio_chunks={audio_chunks} audio_end={audio_end}")
    print("E2E TURN: PASS" if ok else "E2E TURN: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
