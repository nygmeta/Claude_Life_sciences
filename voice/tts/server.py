"""Text-to-speech HTTP service for lab-assistant.

The gepard backend, selected by the `LA_TTS_BACKEND` env var, wraps the reference
PyTorch `gepard_inference` package (NOT gepard-vllm, which targets CUDA 13). Gepard LM
+ NVIDIA NeMo NanoCodec, 22050 Hz output.

The HTTP API:

    GET  /health                 -> {"status": "ok"|"loading", "device", "backend",
                                     "model", "model_id", "sample_rate", "voices"}
    GET  /voices                 -> {"voices": ["default", ...]}
    POST /synthesize   {text, voice?, temperature?, cfg_scale?, top_k?, max_frames?}
                                 -> audio/wav  (mono, PCM16, 22050 Hz)

Deploy note: on the GPU host the service runs in its own conda env (gepard needs
transformers==5.3.0), bound to 127.0.0.1 on :8040.

Env:
  LA_TTS_BACKEND gepard   LA_TTS_HOST 0.0.0.0   LA_TTS_PORT 8040
  LA_TTS_TEMP 0.3   LA_TTS_CFG 1.0   LA_TTS_TOPK 0   LA_TTS_MAXFRAMES 1075
  LA_TTS_DEVICE cuda   LA_TTS_MODEL / LA_TTS_CODEC (repo ids)   LA_TTS_VOICES_DIR
"""
import argparse
import io
import os
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

BACKEND = os.environ.get("LA_TTS_BACKEND", "gepard").strip().lower()

# Default generation params (exposed to the TTS playground).
DEF_TEMP = float(os.environ.get("LA_TTS_TEMP", "0.3"))
DEF_CFG = float(os.environ.get("LA_TTS_CFG", "1.0"))
DEF_TOPK = int(os.environ.get("LA_TTS_TOPK", "0"))
DEF_MAXFRAMES = int(os.environ.get("LA_TTS_MAXFRAMES", "1075"))


def _to_wave_float(audio) -> np.ndarray:
    """Coerce a torch tensor / numpy array / list into a flat float32 mono waveform."""
    a = audio
    if hasattr(a, "detach"):
        a = a.detach().float().cpu().numpy()
    return np.asarray(a, dtype=np.float32).reshape(-1)


def _wav_bytes(wave: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, wave, sample_rate, subtype="PCM_16", format="WAV")
    buf.seek(0)
    return buf.read()


# --------------------------------------------------------------------------- gepard

class GepardBackend:
    """The gepard-1.0 path: Gepard LM + NeMo NanoCodec, 22050 Hz, `*.pt` reference-code
    voices from the voices dir. `default` = the model's own voice (ref_codes=None)."""

    name = "gepard"
    model_id = "gepard-1.0"
    sample_rate = 22050  # NanoCodec 22kHz output

    def __init__(self):
        self.device = os.environ.get("LA_TTS_DEVICE", "cuda")
        self.model_repo = os.environ.get("LA_TTS_MODEL", "nineninesix/gepard-1.0")
        self.codec_repo = os.environ.get(
            "LA_TTS_CODEC", "nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps")
        self.voices_dir = os.environ.get("LA_TTS_VOICES_DIR") or str(
            Path(__file__).resolve().parent / "voices")
        self._runner = None
        self._codec = None
        # name -> ref_codes tensor on device. "default" seeds to None = the model's own
        # UNCONDITIONED voice. With temperature>0 that voice is re-sampled on every
        # generate() call, so a multi-sentence reply (each sentence a separate synth)
        # drifts to a different speaker per sentence. A `default.pt` in the voices dir
        # intentionally OVERRIDES this None: _load_voices() sets self._voices["default"]
        # to its ref_codes, pinning "default" to one fixed conditioned speaker. That file
        # override is the primary drift fix (generate it with tts/make_gepard_voice.py);
        # LA_TTS_GEPARD_DEFAULT_VOICE below is a secondary alias path.
        self._voices = {"default": None}
        # Secondary drift fix: if set to an already-loaded voice name, "default" aliases
        # to it at load() time (only when no default.pt already conditioned "default").
        self.default_alias = os.environ.get("LA_TTS_GEPARD_DEFAULT_VOICE", "").strip()
        self.loading = True

    @property
    def model_label(self):
        return self.model_repo

    def _load_voices(self):
        """Load every *.pt in the voices dir as a named voice (payload format per the
        gepard SpeakerLibrary: dict with 'ref_codes' [1,T,C] long, or a bare tensor).
        Failures are skipped, not fatal."""
        import torch
        vdir = Path(self.voices_dir)
        if not vdir.is_dir():
            return
        for pt in sorted(vdir.glob("*.pt")):
            try:
                payload = torch.load(pt, map_location="cpu", weights_only=True)
                codes = payload["ref_codes"] if isinstance(payload, dict) else payload
                if codes.dim() != 3:
                    raise ValueError(f"expected ref_codes [1,T,C], got {tuple(codes.shape)}")
                self._voices[pt.stem] = codes.long().to(self.device)
            except Exception as e:  # noqa: BLE001
                print(f"[gepard] skip voice {pt.name}: {type(e).__name__}: {e}", flush=True)

    def load(self):
        from gepard_inference.runner import GepardRunner
        from gepard_inference.codec_wrapper import UnfoldedCodecModel

        t0 = time.time()
        print(f"[gepard] loading LM {self.model_repo} on {self.device} ...", flush=True)
        self._runner = GepardRunner.from_checkpoint(
            self.model_repo, device=self.device, attn_implementation="eager")
        print(f"[gepard] loading codec {self.codec_repo} ...", flush=True)
        self._codec = UnfoldedCodecModel.from_pretrained(self.codec_repo).eval().to(self.device)
        self._load_voices()
        # Secondary drift-fix path: alias "default" to a named voice when requested. The
        # default.pt file override takes precedence, so only apply this if "default" is
        # still None (no default.pt loaded).
        if self.default_alias:
            if self._voices.get("default") is not None:
                print("[gepard] LA_TTS_GEPARD_DEFAULT_VOICE ignored: a default.pt "
                      "reference is already loaded (file override wins)", flush=True)
            elif self._voices.get(self.default_alias) is not None:
                self._voices["default"] = self._voices[self.default_alias]
                print(f"[gepard] default voice aliased to '{self.default_alias}' "
                      "(LA_TTS_GEPARD_DEFAULT_VOICE)", flush=True)
            else:
                print(f"[gepard] LA_TTS_GEPARD_DEFAULT_VOICE='{self.default_alias}' not "
                      "among loaded voices; default stays UNCONDITIONED", flush=True)
        # Drift-fix status: is "default" pinned to a stable reference or still the
        # per-call unconditioned (drifting) speaker?
        if self._voices.get("default") is not None:
            print("[gepard] default voice is CONDITIONED (stable reference loaded; "
                  "no per-sentence speaker drift)", flush=True)
        else:
            print("[gepard] default voice is UNCONDITIONED (no default.pt / alias; "
                  "per-sentence speaker drift possible). Fix: tts/make_gepard_voice.py",
                  flush=True)
        self.loading = False
        print(f"[gepard] ready in {time.time() - t0:.1f}s  voices={list(self._voices)}", flush=True)

    def voices(self):
        names = sorted(n for n in self._voices if n != "default")
        return ["default"] + names

    def synthesize(self, text, voice, temperature, cfg_scale, top_k, max_frames) -> bytes:
        import torch
        ref = self._voices.get(voice)  # None for "default" / unknown -> model's own voice
        with torch.no_grad():
            tokens = self._runner.generate(
                text, ref_codes=ref, temperature=temperature, top_k=top_k,
                cfg_scale=cfg_scale, max_frames=max_frames,
            )
            codes = tokens.unsqueeze(0).to(self.device)          # [1, 32, T]
            codes_len = codes.new_tensor([codes.shape[-1]])
            decoded = self._codec.decode_from_codes(codes, codes_len)
            audio = decoded[0] if isinstance(decoded, (tuple, list)) else decoded
        return _wav_bytes(_to_wave_float(audio), self.sample_rate)


def make_backend():
    if BACKEND in ("gepard", ""):
        return GepardBackend()
    raise SystemExit(f"unknown LA_TTS_BACKEND={BACKEND!r} (expected 'gepard')")


app = FastAPI(title="lab-assistant TTS")
_backend = make_backend()


class SynthesizeRequest(BaseModel):
    text: str
    voice: str | None = None
    temperature: float | None = None
    cfg_scale: float | None = None
    top_k: int | None = None
    max_frames: int | None = None


@app.on_event("startup")
def _startup():
    _backend.load()


@app.get("/health")
def health():
    return {
        "status": "loading" if _backend.loading else "ok",
        "device": getattr(_backend, "device", ""),
        "backend": _backend.name,
        "model": _backend.model_label,        # repo id (kept for backward compat)
        "model_id": _backend.model_id,        # stable short id: gepard-1.0
        "sample_rate": _backend.sample_rate,
        "voices": len(_backend.voices()),
    }


@app.get("/voices")
def voices():
    return {"voices": _backend.voices()}


@app.post("/synthesize")
def synthesize_endpoint(req: SynthesizeRequest):
    if _backend.loading:
        return JSONResponse({"error": "model still loading"}, status_code=503)
    text = (req.text or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    voice = req.voice or "default"
    temp = req.temperature if req.temperature is not None else DEF_TEMP
    cfg = req.cfg_scale if req.cfg_scale is not None else DEF_CFG
    topk = req.top_k if req.top_k is not None else DEF_TOPK
    maxf = req.max_frames if req.max_frames is not None else DEF_MAXFRAMES
    try:
        t0 = time.time()
        wav = _backend.synthesize(text, voice, temp, cfg, topk, maxf)
        print(f"[{_backend.name}] {time.time()-t0:.2f}s voice={voice} {len(wav)}B "
              f"<- {text[:50]!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{_backend.name}] synth failed: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(content=wav, media_type="audio/wav")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("LA_TTS_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LA_TTS_PORT", "8040")))
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
