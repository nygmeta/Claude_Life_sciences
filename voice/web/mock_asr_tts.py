"""Local (no-GPU) mocks for the ASR and TTS services, so web/server.py can be
exercised end-to-end on the Mac. The LLM stage is NOT mocked: the orchestrator
calls the real Claude Haiku API, so this also validates the key + streaming.

  ASR mock (default :9001), OpenAI-compatible POST /v1/audio/transcriptions -> canned text
  TTS mock(s), POST /synthesize -> a short real WAV (distinct tone per instance);
               GET /voices (distinct list per instance), GET /health (model_id + sample_rate)

Configure via env (all optional; running with no env vars behaves exactly like
the original single-instance mock):
  MOCK_ASR_PORT       9001
  MOCK_TTS_INSTANCES  "port:model-id,port:model-id,..."  e.g. "9002:gepard-1.0,
                      9003:gepard-1.0-alt". One HTTP server per pair, each answering
                      as a distinct TTS backend (own voice list + tone), so a
                      test can tell which instance actually served a request.
                      Default when unset: a single "9002:gepard-1.0" instance.

Run:  python3 web/mock_asr_tts.py
"""
import io
import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import numpy as np
import soundfile as sf

# Process-wide TTS concurrency gauge, shared across ALL mock TTS instances (they
# run as threads in this one process). Every /synthesize holds the counter for a
# short window (_SYNTH_HOLD_S) so two overlapping requests are actually
# concurrent on the wire; the orchestrator's global GPU semaphore should keep the
# gauge from ever exceeding 1. A smoke test reads /stats and asserts max_active
# never went above 1, and can POST /reset_stats to zero the high-water mark.
_SYNTH_HOLD_S = 0.15
_tts_lock = threading.Lock()
_tts_active = 0
_tts_max_active = 0


def _synth_enter() -> None:
    global _tts_active, _tts_max_active
    with _tts_lock:
        _tts_active += 1
        if _tts_active > _tts_max_active:
            _tts_max_active = _tts_active


def _synth_exit() -> None:
    global _tts_active
    with _tts_lock:
        _tts_active -= 1

# distinct text per segment so a two-segment turn forms one sentence
CANNED_SEGMENTS = ["What is the weather", "like in Tokyo today?"]
_asr_i = 0

# The ASR service now returns an additive nullable "confidence" block on each
# transcription; the mock mirrors that so web/server.py can be exercised
# end-to-end. Contract: prob_* = exp(logprob_*), 4-decimal rounding.
_CANNED_PROB_MEAN = 0.97
_CANNED_PROB_MIN = 0.93

# Override hook for the lab smoke: an uploaded file named
#   utter;text=<urlencoded text>;pmin=<float>;pmean=<float>.wav
# makes the mock return exactly that text + confidence, WITHOUT touching the
# canned-segment counter, so the parity-sensitive existing smokes are unchanged.
_OVERRIDE_RE = re.compile(
    r"^utter;text=(?P<text>.*?);pmin=(?P<pmin>[0-9.]+);pmean=(?P<pmean>[0-9.]+)\.wav$")
_FILENAME_RE = re.compile(rb'filename="([^"]*)"')


def _conf_block(prob_min: float, prob_mean: float, tokens: int) -> dict:
    """A confidence block matching the real ASR contract:
    {logprob_mean, logprob_min, prob_mean, prob_min, tokens}. prob_* are the
    inputs (already probabilities); logprob_* are their natural logs."""
    return {"logprob_mean": round(math.log(prob_mean), 4),
            "logprob_min": round(math.log(prob_min), 4),
            "prob_mean": round(prob_mean, 4),
            "prob_min": round(prob_min, 4),
            "tokens": tokens}


def _match_override(body: bytes):
    """Parse the multipart upload filename and return (text, pmin, pmean) when it
    matches the lab-smoke override pattern, else None. The filename is read from
    the raw multipart body's Content-Disposition (the SDK sets it from the
    BytesIO .name the orchestrator assigns)."""
    m = _FILENAME_RE.search(body)
    if not m:
        return None
    mm = _OVERRIDE_RE.match(m.group(1).decode("utf-8", "replace"))
    if not mm:
        return None
    return unquote(mm.group("text")), float(mm.group("pmin")), float(mm.group("pmean"))

SAMPLE_RATE = 22050

# per-model voice list + tone frequency: purely so a smoke test can tell which
# mock instance actually answered a routed request, without inspecting audio.
_MODEL_VOICES = {
    "gepard-1.0": ["default", "en_andrew", "mx_f"],
    "gepard-1.0-alt": ["alt-default", "alt-warm"],
}
_MODEL_FREQ_HZ = {"gepard-1.0": 440, "gepard-1.0-alt": 660}


def _make_wav(freq_hz: float) -> bytes:
    """A short (0.4s) sine wave as PCM16 WAV, standing in for real TTS audio."""
    t = np.linspace(0, 0.4, int(SAMPLE_RATE * 0.4), endpoint=False)
    buf = io.BytesIO()
    sf.write(buf, (0.2 * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32), SAMPLE_RATE,
             subtype="PCM_16", format="WAV")
    return buf.getvalue()


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""


class ASRHandler(_Base):
    def do_GET(self):
        if self.path.rstrip("/").endswith("/health"):
            return self._json(200, {"status": "ok"})
        if self.path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "fun-asr-nano"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        global _asr_i
        body = self._read_body()  # keep it: the filename override rides in here
        if self.path.endswith("/audio/transcriptions"):
            override = _match_override(body)
            if override is not None:
                text, pmin, pmean = override
                return self._json(200, {"text": text,
                                        "confidence": _conf_block(pmin, pmean, len(text.split()) or 1)})
            text = CANNED_SEGMENTS[_asr_i % len(CANNED_SEGMENTS)]
            _asr_i += 1
            return self._json(200, {"text": text,
                                    "confidence": _conf_block(_CANNED_PROB_MIN, _CANNED_PROB_MEAN,
                                                              len(text.split()) or 1)})
        return self._json(404, {"error": "not found"})


def _make_tts_handler(model_id: str, wav_bytes: bytes):
    """Build a TTSHandler class bound to one model's voice list + canned WAV,
    so each mock instance answers as a distinct TTS backend."""
    voices = _MODEL_VOICES.get(model_id, ["default"])

    class TTSHandler(_Base):
        def do_GET(self):
            if self.path.rstrip("/").endswith("/health"):
                return self._json(200, {"status": "ok", "model_id": model_id,
                                         "sample_rate": SAMPLE_RATE})
            if self.path.rstrip("/").endswith("/voices"):
                return self._json(200, {"voices": voices})
            if self.path.rstrip("/").endswith("/stats"):
                # process-wide concurrency gauge (shared across instances)
                with _tts_lock:
                    return self._json(200, {"active": _tts_active, "max_active": _tts_max_active})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            global _tts_max_active
            self._read_body()
            if self.path.rstrip("/").endswith("/reset_stats"):
                with _tts_lock:
                    _tts_max_active = _tts_active   # zero the high-water mark to the current in-flight count
                return self._json(200, {"active": _tts_active, "max_active": _tts_max_active})
            if self.path.endswith("/synthesize"):
                _synth_enter()
                try:
                    time.sleep(_SYNTH_HOLD_S)   # widen the overlap window so unserialized synths collide
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                finally:
                    _synth_exit()
                return
            return self._json(404, {"error": "not found"})

    return TTSHandler


def _serve(port, handler):
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


def _parse_tts_instances() -> list:
    """Parse MOCK_TTS_INSTANCES ("port:model-id,port:model-id,...") into a list
    of (port, model_id) tuples. Defaults to a single gepard-1.0 instance on
    :9002, so running this script with no env vars is unchanged."""
    raw = os.environ.get("MOCK_TTS_INSTANCES", "").strip()
    if not raw:
        return [(9002, "gepard-1.0")]
    out = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        port_s, model_id = pair.split(":", 1)
        port_s, model_id = port_s.strip(), model_id.strip()
        if port_s and model_id:
            out.append((int(port_s), model_id))
    return out or [(9002, "gepard-1.0")]


if __name__ == "__main__":
    asr_port = int(os.environ.get("MOCK_ASR_PORT", "9001"))
    threading.Thread(target=_serve, args=(asr_port, ASRHandler), daemon=True).start()

    instances = _parse_tts_instances()
    for port, model_id in instances:
        freq = _MODEL_FREQ_HZ.get(model_id, 440)
        handler = _make_tts_handler(model_id, _make_wav(freq))
        threading.Thread(target=_serve, args=(port, handler), daemon=True).start()

    tts_desc = "  ".join(f"{model_id}@:{port}" for port, model_id in instances)
    print(f"mock ASR :{asr_port}  mock TTS {tts_desc}  (Ctrl-C to stop)", flush=True)
    threading.Event().wait()
