"""OpenAI Whisper-compatible API server for Fun-ASR-Nano-2512 (GPU).

Wraps funasr's AutoModel and exposes an OpenAI-compatible /v1 transcription
contract so the browser client can post speech segments and get text back.

Canonical inference call (from the HF model card):

    from funasr import AutoModel
    model = AutoModel(model="FunAudioLLM/Fun-ASR-Nano-2512", hub="hf",
                      trust_remote_code=True, device="cuda:0")
    res = model.generate(input=[wav], cache={}, batch_size=1,
                         hotwords=["..."], language="中文", itn=True)
    text = res[0]["text"]

Endpoints:
    GET  /health                     -> {"status": "ok"|"loading"}
    GET  /v1/models                  -> list of model ids
    POST /v1/audio/transcriptions    -> {"text": ..., "confidence": {...}|null}
                                        (multipart: file, model, language, prompt, response_format)

The "confidence" block (present on json and verbose_json responses) carries
per-token logprob stats scraped from the decoder: logprob_mean, logprob_min,
prob_mean, prob_min, tokens. It is null when confidence collection is off
(FUNASR_CONFIDENCE=0) or the decoder produced no scored tokens.
"""
import argparse
import math
import os
import re
import tempfile
import time
import zlib
from collections import Counter
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
MODEL_REPO = os.environ.get("FUNASR_MODEL", "FunAudioLLM/Fun-ASR-Nano-2512")
MODEL_ID = os.environ.get("FUNASR_MODEL_ID", "fun-asr-nano")
DEVICE = os.environ.get("FUNASR_DEVICE", "cuda:0")
DEFAULT_LANG = os.environ.get("FUNASR_DEFAULT_LANG", "zh")
# When enabled (default), wrap the inner decoder's generate() to collect
# per-token logprob stats and attach a "confidence" block to responses.
CONFIDENCE_ENABLED = os.environ.get("FUNASR_CONFIDENCE", "1") != "0"

# funasr's Fun-ASR-Nano takes the language as a Chinese name, not an ISO code.
LANG_MAP = {
    "zh": "中文", "中文": "中文", "chinese": "中文",
    "en": "英文", "英文": "英文", "english": "英文",
    "ja": "日文", "日文": "日文", "japanese": "日文",
    "yue": "粤语", "粤语": "粤语",
}

app = FastAPI(title="Fun-ASR-Nano API")
_model = None
_loading = True


def _map_language(lang: str | None) -> str:
    if not lang:
        lang = DEFAULT_LANG
    return LANG_MAP.get(lang.strip().lower(), lang)


def _is_degenerate(text: str) -> bool:
    """Reference-free runaway-repetition check: high zlib compression ratio OR
    one token dominating the output."""
    if not text or len(text) < 24:
        return False
    raw = text.encode("utf-8")
    comp = zlib.compress(raw, 6)
    ratio = len(raw) / max(len(comp), 1)
    if ratio > 2.4:
        return True
    toks = re.findall(r"\w+", text)
    if len(toks) >= 8:
        top = Counter(toks).most_common(1)[0][1]
        if top / len(toks) > 0.40:
            return True
    return False


# ----------------------------------------------------------------------------
# Confidence: per-token logprob stats scraped from the decoder's generate()
# ----------------------------------------------------------------------------
class _ConfidenceHolder:
    """Module-level scratch space the wrapped generate() writes into. Each
    generate() call overwrites `stats` with a per-item list (dict or None)."""

    def __init__(self):
        self.stats: list | None = None


_confidence = _ConfidenceHolder()


def _score_stats(sequences, scores, eos_ids: set[int], pad_id: int | None) -> list:
    """Pure helper: turn a generate() (sequences, scores) pair into per-item
    confidence dicts. `sequences` is a [batch, steps] LongTensor of generated
    tokens; `scores` is a tuple of `steps` [batch, vocab] logit tensors. For
    each item we take the log-softmax at each step, gather the chosen token's
    logprob, stop at the first eos and skip pad tokens, then summarise. Items
    with no scored tokens yield None. Returns a list[dict | None]."""
    import torch

    n_items = int(sequences.shape[0])
    n_steps = min(len(scores), int(sequences.shape[1]))
    out: list = []
    for i in range(n_items):
        logprobs: list[float] = []
        for t in range(n_steps):
            tok = int(sequences[i, t])
            if tok in eos_ids:
                break  # stop at first eos; do not count it or anything after
            if pad_id is not None and tok == pad_id:
                continue  # skip pad tokens
            step_lp = torch.log_softmax(scores[t][i].float(), dim=-1)
            logprobs.append(float(step_lp[tok]))
        if not logprobs:
            out.append(None)
            continue
        lp_mean = sum(logprobs) / len(logprobs)
        lp_min = min(logprobs)
        out.append(
            {
                "logprob_mean": round(lp_mean, 4),
                "logprob_min": round(lp_min, 4),
                "prob_mean": round(math.exp(lp_mean), 4),
                "prob_min": round(math.exp(lp_min), 4),
                "tokens": len(logprobs),
            }
        )
    return out


def _wrap_llm_generate() -> None:
    """Wrap the inner Qwen decoder's generate() once so it records per-token
    logprob stats into `_confidence` as a side effect, while still returning
    the plain `sequences` tensor funasr's downstream batch_decode expects.

    Everything is guarded: if the attribute path is missing or the stats math
    fails, transcription keeps working and confidence is simply reported as
    null."""
    if _model is None:
        return
    try:
        llm = _model.model.llm  # torch module -> HF Qwen causal LM
    except AttributeError:
        print("[funasr] confidence: decoder path (_model.model.llm) not found, "
              "skipping wrap", flush=True)
        return

    orig_generate = getattr(llm, "generate", None)
    if orig_generate is None:
        print("[funasr] confidence: decoder has no generate(), skipping wrap", flush=True)
        return
    if getattr(orig_generate, "_confidence_wrapped", False):
        return

    # Read eos/pad from the decoder config once and close over them. eos may be
    # a single int or a list; pad may be absent.
    cfg = getattr(llm, "config", None)
    eos_raw = getattr(cfg, "eos_token_id", None) if cfg is not None else None
    if eos_raw is None:
        eos_ids: set[int] = set()
    elif isinstance(eos_raw, (list, tuple, set)):
        eos_ids = {int(e) for e in eos_raw}
    else:
        eos_ids = {int(eos_raw)}
    pad_raw = getattr(cfg, "pad_token_id", None) if cfg is not None else None
    pad_id = int(pad_raw) if pad_raw is not None else None

    def wrapped(*args, **kwargs):
        forced = dict(kwargs)
        forced["return_dict_in_generate"] = True
        forced["output_scores"] = True
        try:
            out = orig_generate(*args, **forced)
        except TypeError as e:
            # Forced kwargs unsupported by this transformers version: fall back
            # to the caller's original call so transcription never breaks.
            print(f"[funasr] confidence: forced generate kwargs rejected ({e}), "
                  "falling back without stats", flush=True)
            _confidence.stats = None
            return orig_generate(*args, **kwargs)
        try:
            _confidence.stats = _score_stats(out.sequences, out.scores, eos_ids, pad_id)
        except Exception as e:  # noqa: BLE001
            print(f"[funasr] confidence: stats computation failed: {e}", flush=True)
            _confidence.stats = None
        # If a future transformers ignored return_dict_in_generate, `out` is
        # already the tensor batch_decode wants; return it unchanged.
        return getattr(out, "sequences", out)

    wrapped._confidence_wrapped = True
    llm.generate = wrapped
    print(f"[funasr] confidence: wrapped decoder generate() "
          f"(eos={sorted(eos_ids)} pad={pad_id})", flush=True)


def _snapshot_confidence() -> dict | None:
    """Read the stats left by the most recent generate() call. We transcribe a
    single utterance (input=[wav]) so item 0 is the answer."""
    stats = _confidence.stats
    if not stats:
        return None
    return stats[0]


def load_model():
    global _model, _loading
    from funasr import AutoModel  # imported lazily so the process starts fast

    print(f"[funasr] loading {MODEL_REPO} on {DEVICE} ...", flush=True)
    t0 = time.time()
    _model = AutoModel(
        model=MODEL_REPO,
        hub="hf",
        trust_remote_code=True,
        device=DEVICE,
        disable_update=True,
    )
    if CONFIDENCE_ENABLED:
        _wrap_llm_generate()
    else:
        print("[funasr] confidence: disabled via FUNASR_CONFIDENCE=0", flush=True)
    _loading = False
    print(f"[funasr] model ready in {time.time() - t0:.1f}s", flush=True)


@app.on_event("startup")
def _startup():
    load_model()


@app.get("/health")
def health():
    return {"status": "loading" if _loading else "ok", "model": MODEL_ID, "device": DEVICE}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_ID, "object": "model", "owned_by": "FunAudioLLM"},
            {"id": "whisper-1", "object": "model", "owned_by": "FunAudioLLM"},
        ],
    }


def _run_asr(wav_path: str, language: str, hotwords: list[str]) -> tuple[str, dict | None]:
    kwargs = dict(input=[wav_path], cache={}, batch_size=1, language=language, itn=True)
    if hotwords:
        kwargs["hotwords"] = hotwords
    res = _model.generate(**kwargs)
    conf = _snapshot_confidence()
    text = (res[0].get("text") if res else "") or ""
    text = text.strip()

    # Best-effort runaway-repetition guard: re-decode once with anti-repeat
    # sampling. Wrapped so an unsupported kwarg can never break the main path.
    # Snapshot confidence again so we report the stats of whichever decode
    # produced the final text.
    if _is_degenerate(text):
        try:
            res2 = _model.generate(
                **kwargs, no_repeat_ngram_size=3, repetition_penalty=1.1
            )
            conf2 = _snapshot_confidence()
            t2 = ((res2[0].get("text") if res2 else "") or "").strip()
            if t2 and not _is_degenerate(t2):
                text = t2
                conf = conf2
        except Exception as e:  # noqa: BLE001
            print(f"[funasr] degenerate re-decode skipped: {e}", flush=True)
    return text, conf


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(MODEL_ID),
    language: str = Form(None),
    response_format: str = Form("json"),
    prompt: str = Form(None),
):
    if _loading:
        return JSONResponse({"error": "model still loading"}, status_code=503)

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    lang = _map_language(language)
    hotwords = [w.strip() for w in prompt.split(",")] if prompt else []
    hotwords = [w for w in hotwords if w]

    try:
        t0 = time.time()
        text, confidence = _run_asr(tmp_path, lang, hotwords)
        dt = time.time() - t0
        print(f"[funasr] lang={lang} hot={len(hotwords)} {dt:.2f}s -> {text!r}", flush=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "verbose_json":
        return JSONResponse(
            {
                "task": "transcribe",
                "language": lang,
                "text": text,
                "confidence": confidence,
                "segments": [],
            }
        )
    return JSONResponse({"text": text, "confidence": confidence})


# Translations endpoint (to English): reuse the same model with language hint.
@app.post("/v1/audio/translations")
async def translate(
    file: UploadFile = File(...),
    model: str = Form(MODEL_ID),
    response_format: str = Form("json"),
    prompt: str = Form(None),
):
    return await transcribe(file=file, model=model, language="英文",
                            response_format=response_format, prompt=prompt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8030)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
