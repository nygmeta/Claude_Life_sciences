"""Local WS smoke test for TTS model routing: list_tts_models returns both
configured models with the right default, a playground tts_test targeting the
non-default model succeeds, list_voices for that model returns its own voice
list (proving the request actually reached the second mock backend and not
the default one), set_tts_model switches the assistant model, and the next
turn's latency record shows tts.model for the newly selected model. Runs
against web/server.py + two mock TTS instances (see scripts/run_local_smoke.sh).
"""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import websockets

PORT = os.environ.get("LA_WS_PORT", "8765")
URL = f"ws://localhost:{PORT}/"
LOG_FILE = Path(os.environ.get("LA_LOG_FILE",
                str(Path(__file__).resolve().parent.parent / "data" / "latency.jsonl")))
SEG = json.dumps({"type": "audio_segment",
                  "audio_b64": base64.b64encode(b"\x00\x00" * 8000).decode(),
                  "sample_rate": 16000})
ALT_MODEL = "gepard-1.0-alt"   # the non-default model configured by run_local_smoke.sh


async def recv_until(ws, want, timeout=15):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if m.get("type") == "error":
            print(f"  <- ERROR: {m.get('text')}", flush=True)
        if m.get("type") == want:
            return m


def _line_count() -> int:
    if not LOG_FILE.is_file():
        return 0
    return sum(1 for _ in LOG_FILE.open(encoding="utf-8"))


def _last_assistant_record():
    if not LOG_FILE.is_file():
        return None
    for line in reversed(LOG_FILE.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "assistant":
            return rec
    return None


async def main() -> int:
    async with websockets.connect(URL, max_size=16 * 1024 * 1024) as ws:
        print("list_tts_models", flush=True)
        await ws.send(json.dumps({"type": "list_tts_models"}))
        models_msg = await recv_until(ws, "tts_models")
        ids = [m.get("id") for m in models_msg.get("models", [])]
        print(f"  <- models={ids} default={models_msg.get('default')} "
              f"current={models_msg.get('current')}", flush=True)
        models_ok = (len(ids) == 2 and models_msg.get("default") == ids[0]
                     and models_msg.get("current") == models_msg.get("default"))

        print(f"list_voices(model={ALT_MODEL})", flush=True)
        await ws.send(json.dumps({"type": "list_voices", "model": ALT_MODEL}))
        voices_msg = await recv_until(ws, "voices")
        print(f"  <- voices={voices_msg.get('voices')} model={voices_msg.get('model')}", flush=True)
        voices_ok = bool(voices_msg.get("voices")) and voices_msg.get("model") == ALT_MODEL

        print(f"tts_test(model={ALT_MODEL})", flush=True)
        await ws.send(json.dumps({"type": "tts_test", "text": "hello from the alt model",
                                  "model": ALT_MODEL}))
        tts_msg = await recv_until(ws, "tts_test_audio")
        print(f"  <- {len(tts_msg.get('audio_b64',''))} b64 @ {tts_msg.get('sample_rate')}Hz", flush=True)
        playground_ok = bool(tts_msg.get("audio_b64"))

        print(f"set_tts_model({ALT_MODEL})", flush=True)
        await ws.send(json.dumps({"type": "set_tts_model", "model": ALT_MODEL}))
        set_msg = await recv_until(ws, "tts_models")
        print(f"  <- current={set_msg.get('current')}", flush=True)
        set_ok = set_msg.get("current") == ALT_MODEL

        print("unknown model is rejected without changing the session", flush=True)
        await ws.send(json.dumps({"type": "set_tts_model", "model": "no-such-model"}))
        err_msg = await recv_until(ws, "error")
        print(f"  <- error: {err_msg.get('text')}", flush=True)
        reject_ok = bool(err_msg.get("text"))

        print("end_turn on the switched model", flush=True)
        lines_before = _line_count()
        await ws.send(SEG)
        await recv_until(ws, "transcript")
        await ws.send(json.dumps({"type": "end_turn"}))
        await recv_until(ws, "reply_audio_end", timeout=45)

    # the latency record write can lag reply_audio_end by a beat: poll briefly
    rec = None
    for _ in range(30):
        if _line_count() > lines_before:
            rec = _last_assistant_record()
            if rec is not None:
                break
        await asyncio.sleep(0.1)

    model_in_log = ((rec or {}).get("tts") or {}).get("model")
    print(f"  <- latency record: status={(rec or {}).get('status')} tts.model={model_in_log!r}",
          flush=True)
    log_ok = rec is not None and rec.get("status") == "ok" and model_in_log == ALT_MODEL

    ok = models_ok and voices_ok and playground_ok and set_ok and reject_ok and log_ok
    print(f"\nmodels_ok={models_ok} voices_ok={voices_ok} playground_ok={playground_ok} "
          f"set_ok={set_ok} reject_ok={reject_ok} log_ok={log_ok}", flush=True)
    print("SMOKE: PASS (tts model routing + switch)" if ok else "SMOKE: FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
