#!/usr/bin/env python3
"""On-pod smoke test for the ASR confidence shim.

Runs against a live Fun-ASR-Nano server (default http://127.0.0.1:8030). It
transcribes a clean wav, then a degraded (noisy) copy of the same wav, and
checks that the clean decode reports a higher mean token probability than the
noisy one. Prints "CONF-SMOKE: PASS" and exits 0 when
clean.prob_mean > noisy.prob_mean, otherwise "CONF-SMOKE: FAIL" and exits 1.

Stdlib-only by default (urllib, json, wave, struct). If numpy + soundfile are
importable it uses them to add gaussian noise at roughly 0 dB SNR; otherwise it
falls back to a pure-stdlib amplitude-crush plus white noise.

Usage:
    python asr/smoke_confidence.py [--wav PATH] [--url http://127.0.0.1:8030]
                                   [--language en]

With no --wav it discovers the example clip shipped in the model snapshot under
$HF_HOME/hub/models--FunAudioLLM--Fun-ASR-Nano-2512/snapshots/*/example/, which
ships per-language .mp3 files (en/zh/ja/ko/yue), not .wav. An English clip is
preferred. A .wav clip (via --wav or a future snapshot) is used as-is; an .mp3
is converted in-process to 16-bit PCM wav using soundfile (present in the asr
venv, whose libsndfile decodes mp3). If soundfile is not importable the script
prints an actionable error naming the file it found and how to convert it. The
soundfile import stays optional; everything else is stdlib-only.
"""
import argparse
import glob
import io
import json
import os
import random
import struct
import sys
import tempfile
import urllib.request
import uuid
import wave


def _discover_example_clip() -> str | None:
    """Find an example clip in the model snapshot. The snapshot ships per-language
    .mp3 files (en/zh/ja/ko/yue); a future snapshot could ship .wav. Prefer an
    English clip, then wav over mp3, then lexical order."""
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    base = os.path.join(
        hf_home,
        "hub",
        "models--FunAudioLLM--Fun-ASR-Nano-2512",
        "snapshots",
        "*",
        "example",
    )
    matches: list[str] = []
    for ext in ("wav", "mp3"):
        matches.extend(glob.glob(os.path.join(base, "*." + ext)))
    if not matches:
        return None

    def _rank(p: str):
        name = os.path.basename(p).lower()
        stem = os.path.splitext(name)[0]
        is_en = 0 if (stem == "en" or stem.startswith("en_") or stem.startswith("en-")
                      or "english" in stem) else 1
        is_wav = 0 if name.endswith(".wav") else 1
        return (is_en, is_wav, name)

    return sorted(matches, key=_rank)[0]


def _ensure_wav(path: str) -> str | None:
    """Return a path to a wav usable by the stdlib `wave` module. A .wav is used
    as-is; any other container (the snapshot's .mp3) is converted in-process to a
    temp 16-bit PCM wav via soundfile, whose libsndfile build decodes mp3. If
    soundfile is not importable, print an actionable error and return None."""
    if path.lower().endswith(".wav"):
        return path
    try:
        import soundfile as sf  # type: ignore
    except Exception:
        print(
            f"CONF-SMOKE: FAIL (found {path} but it is not a .wav and soundfile is "
            f"not importable to decode it; run this in the asr venv, which has "
            f"soundfile, or convert the clip to 16-bit PCM wav first with "
            f"soundfile: read {os.path.basename(path)} then write a .wav with "
            f"subtype='PCM_16', and pass it via --wav)",
            flush=True,
        )
        return None
    data, rate = sf.read(path, dtype="int16", always_2d=False)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    sf.write(tmp.name, data, rate, subtype="PCM_16", format="WAV")
    return tmp.name


def _read_wav(path: str):
    with wave.open(path, "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    return params, frames


def _write_wav_bytes(params, frames: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return buf.getvalue()


def _degrade(params, frames: bytes) -> bytes:
    """Return a noisy version of the 16-bit PCM audio. Uses numpy + soundfile
    when available for gaussian noise at ~0 dB SNR, else a stdlib fallback that
    crushes the signal and adds white noise."""
    if params.sampwidth != 2:
        # Only 16-bit PCM is handled by the stdlib path; pass through unchanged.
        return frames

    try:
        import numpy as np  # type: ignore
        import soundfile as sf  # type: ignore

        n_ch = params.nchannels
        sig = np.frombuffer(frames, dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(sig ** 2))) or 1.0
        noise = np.random.normal(0.0, rms, size=sig.shape).astype(np.float32)
        mixed = np.clip(sig + noise, -32768, 32767).astype("<i2")
        buf = io.BytesIO()
        data = mixed.reshape(-1, n_ch) if n_ch > 1 else mixed
        sf.write(buf, data, params.framerate, subtype="PCM_16", format="WAV")
        return buf.getvalue()
    except Exception:
        pass

    # Pure-stdlib fallback: scale to 6% and add white noise.
    count = len(frames) // 2
    samples = struct.unpack("<%dh" % count, frames)
    rng = random.Random(1234)
    out = []
    for s in samples:
        v = int(s * 0.06) + rng.randint(-4000, 4000)
        v = max(-32768, min(32767, v))
        out.append(v)
    crushed = struct.pack("<%dh" % count, *out)
    return _write_wav_bytes(params, crushed)


def _post(url: str, wav_bytes: bytes, language: str) -> dict:
    boundary = "----conf-smoke-" + uuid.uuid4().hex
    parts = []

    def _field(name: str, value: str):
        parts.append(("--" + boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        parts.append(b"")
        parts.append(value.encode())

    _field("model", "fun-asr-nano")
    _field("language", language)
    _field("response_format", "json")

    parts.append(("--" + boundary).encode())
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="audio.wav"'
    )
    parts.append(b"Content-Type: audio/wav")
    parts.append(b"")
    parts.append(wav_bytes)
    parts.append(("--" + boundary + "--").encode())
    parts.append(b"")
    body = b"\r\n".join(parts)

    req = urllib.request.Request(
        url.rstrip("/") + "/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _prob_mean(payload: dict):
    conf = payload.get("confidence")
    if not conf:
        return None
    return conf.get("prob_mean")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=None, help="clean wav path (default: model example clip)")
    ap.add_argument("--url", default="http://127.0.0.1:8030")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    clip_path = args.wav or _discover_example_clip()
    if not clip_path or not os.path.exists(clip_path):
        print("CONF-SMOKE: FAIL (no clip; pass --wav or set HF_HOME)", flush=True)
        return 1

    wav_path = _ensure_wav(clip_path)
    if not wav_path:
        return 1  # _ensure_wav already printed the actionable FAIL line

    params, frames = _read_wav(wav_path)
    clean_bytes = _write_wav_bytes(params, frames)
    noisy_bytes = _degrade(params, frames)

    clean = _post(args.url, clean_bytes, args.language)
    noisy = _post(args.url, noisy_bytes, args.language)

    clean_pm = _prob_mean(clean)
    noisy_pm = _prob_mean(noisy)

    print(f"clean : text={clean.get('text')!r} confidence={clean.get('confidence')}", flush=True)
    print(f"noisy : text={noisy.get('text')!r} confidence={noisy.get('confidence')}", flush=True)

    if clean_pm is None or noisy_pm is None:
        print("CONF-SMOKE: FAIL (missing confidence in response)", flush=True)
        return 1

    ok = clean_pm > noisy_pm
    print(
        f"CONF-SMOKE: {'PASS' if ok else 'FAIL'} "
        f"(clean.prob_mean={clean_pm} vs noisy.prob_mean={noisy_pm})",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
