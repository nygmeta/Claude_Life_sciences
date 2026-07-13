"""Produce a stable `default.pt` reference-code voice for the gepard TTS backend.

WHY THIS EXISTS (the drift fix)
    gepard's "default" voice is UNCONDITIONED: in GepardBackend.synthesize
    (tts/server.py) voice="default" resolves to ref_codes=None, and with
    temperature>0 the autoregressive LM invents a fresh speaker on every
    generate() call. The orchestrator streams a reply one sentence at a time,
    each sentence a separate synth call, so a single reply comes out in a
    different speaker per sentence. Dropping a `default.pt` into the voices dir
    overrides that None with fixed ref_codes (GepardBackend._load_voices sets
    self._voices[pt.stem] = codes, and "default" is a valid stem), pinning
    "default" to one stable, conditioned speaker. This script generates that file.

RUNS ON THE GPU POD ONLY. It loads gepard-1.0 + the NeMo NanoCodec, so it needs
the gepard venv and a GPU. It cannot run on a laptop. Invoke it with the same
venv interpreter the TTS smoke uses (the one recorded in the repo's `.tts_python`
marker on the pod), from the repo root, for example:

    $(cat .tts_python) tts/make_gepard_voice.py

Two methods (pick with --method):

  capture (default)
    Run ONE unconditioned generate(), take the codec tokens it produced, and
    reshape them to the [1, T, C] long layout _load_voices validates. This
    freezes a single sample of the model's own voice as the stable "default".
    Fewest unknowns: it reuses the exact generate() call the service already
    runs, so the only step to verify is the token axis order.

  encode
    Obtain a short reference WAV (either --ref-audio, or generate one via a
    single unconditioned synth), then encode it through the NanoCodec to get
    canonical ref_codes in the codec's native format (the same path the preset
    voices were built from). Most format-faithful, but depends on discovering
    the codec's audio->codes helper on the pod.

Recommendation for pod-ops: try `--method capture` first (self-validating via
the built-in round-trip check, no encode-API discovery needed). If generate()
rejects the built ref_codes layout, fall back to `--method encode`, which
round-trips through the codec's own encoder and cannot get the axis order wrong.

Output: a torch.save payload
    {"ref_codes": LongTensor[1, T, C], "name", "sample_rate", "source", "method"}
written to --out (default tts/voices/default.pt). Idempotent: re-running just
overwrites the target. The tensor is saved on CPU so the file is portable.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

# Public HF repo ids (same defaults as tts/server.py); overridable via env so the
# script tracks whatever the service is configured to load.
MODEL = os.environ.get("LA_TTS_MODEL", "nineninesix/gepard-1.0")
CODEC = os.environ.get(
    "LA_TTS_CODEC", "nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps")
SAMPLE_RATE = 22050  # NanoCodec 22 kHz output

# Candidate audio->codes method names on UnfoldedCodecModel. VERIFY ON POD: run
# `sorted(n for n in dir(codec) if 'code' in n.lower() or 'enc' in n.lower())`
# and put the real one first (decode is `decode_from_codes`, so encode is likely a
# sibling such as `get_codes` / `encode`).
ENCODE_CANDIDATES = (
    "get_codes",
    "get_codes_from_audio",
    "encode",
    "encode_from_audio",
    "audio_to_codes",
)


def _shape(t):
    return tuple(t.shape) if hasattr(t, "shape") else None


def _dtype(t):
    return getattr(t, "dtype", None)


def _to_1TC(codes, codebooks):
    """Normalize codec codes of any 2D/3D layout into a [1, T, C] long CPU tensor,
    the shape GepardBackend._load_voices validates (dim==3) and feeds to
    runner.generate(ref_codes=...).

    server.synthesize builds the DECODE input as `tokens.unsqueeze(0)  # [1, 32, T]`
    (i.e. [1, C=codebooks, T]), so a raw generate() token tensor is [C, T] and needs
    a transpose to reach [T, C]. We identify the C axis by matching --codebooks
    (default 32); the other axis is time. VERIFY ON POD: that codebooks matches the
    real NanoCodec codebook count and that ref_codes really is [1, T, C] (feed the
    result back into generate() to confirm)."""
    import torch

    t = codes
    if hasattr(t, "detach"):
        t = t.detach().cpu()
    else:
        t = torch.as_tensor(t)
    t = t.long()

    # Strip leading singleton (batch) dims down to 2D.
    while t.dim() > 2 and t.shape[0] == 1:
        t = t.squeeze(0)
    if t.dim() != 2:
        raise SystemExit(
            f"expected 2D codes after squeezing batch dims, got {tuple(t.shape)}. "
            "VERIFY ON POD: inspect what generate()/encode returned and adjust _to_1TC.")

    a, b = t.shape
    if a == codebooks and b != codebooks:
        layout = "CT"          # [C, T] -> transpose to [T, C]
    elif b == codebooks and a != codebooks:
        layout = "TC"          # already [T, C]
    elif a == b:
        # Square: cannot disambiguate by size. Assume the documented generate()
        # layout [C, T]. VERIFY ON POD.
        layout = "CT"
    else:
        # Neither axis equals codebooks: fall back to "the smaller axis is C",
        # since a short reference has more time frames than codebooks. VERIFY ON POD.
        layout = "CT" if a < b else "TC"
        print(f"[shape] neither axis == codebooks({codebooks}); guessing C is the "
              f"smaller axis (layout={layout}). VERIFY ON POD.", flush=True)

    tc = t.t().contiguous() if layout == "CT" else t.contiguous()  # -> [T, C]
    ref = tc.unsqueeze(0).contiguous()                             # -> [1, T, C]
    print(f"[shape] raw {tuple(t.shape)} (layout={layout}, codebooks={codebooks}) "
          f"-> ref_codes {tuple(ref.shape)} dtype={ref.dtype}", flush=True)
    return ref


def _wave_from_decoded(decoded):
    import numpy as np

    audio = decoded[0] if isinstance(decoded, (tuple, list)) else decoded
    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def _load_runner(args):
    from gepard_inference.runner import GepardRunner

    print(f"[load] runner {MODEL} on {args.device} ...", flush=True)
    return GepardRunner.from_checkpoint(
        MODEL, device=args.device, attn_implementation="eager")


def _load_codec(args):
    from gepard_inference.codec_wrapper import UnfoldedCodecModel

    print(f"[load] codec {CODEC} on {args.device} ...", flush=True)
    return UnfoldedCodecModel.from_pretrained(CODEC).eval().to(args.device)


def _verify_roundtrip(runner, ref, args):
    """Feed the freshly built ref_codes back into generate() to prove the layout is
    accepted. Content differs run to run (temperature>0), so this cannot assert audio
    equality; it asserts the ref_codes shape is consumed without error, which is the
    thing that goes wrong if the axis order is flipped. Listen to two synths with
    voice=default after the restart to confirm the speaker no longer drifts."""
    if not args.verify:
        return
    print("[verify] feeding built ref_codes back into generate() (2x) ...", flush=True)
    import torch

    with torch.no_grad():
        for i in range(2):
            out = runner.generate(
                "This is a short check of the pinned default voice.",
                ref_codes=ref.to(args.device),
                temperature=args.temperature, top_k=args.top_k,
                cfg_scale=args.cfg_scale, max_frames=args.max_frames,
            )
            print(f"[verify] gen {i + 1}: tokens shape={_shape(out)}", flush=True)
    print("[verify] OK: ref_codes accepted by generate().", flush=True)


def method_capture(args):
    """(b) Capture one unconditioned generation's codec tokens as the stable default."""
    import torch

    runner = _load_runner(args)
    print("[capture] one unconditioned generate() to sample the model's own voice ...",
          flush=True)
    with torch.no_grad():
        # VERIFY ON POD: runner.generate returns the codec token tensor (server does
        # `tokens.unsqueeze(0)  # [1, 32, T]`, so this is [C, T]).
        tokens = runner.generate(
            args.text, ref_codes=None,
            temperature=args.temperature, top_k=args.top_k,
            cfg_scale=args.cfg_scale, max_frames=args.max_frames,
        )
    print(f"[capture] raw tokens: shape={_shape(tokens)} dtype={_dtype(tokens)}", flush=True)
    ref = _to_1TC(tokens, args.codebooks)
    _verify_roundtrip(runner, ref, args)
    return ref


def method_encode(args):
    """(a) Encode a short reference clip through the NanoCodec to canonical ref_codes."""
    import numpy as np
    import soundfile as sf
    import torch

    codec = _load_codec(args)

    if args.ref_audio:
        wav, sr = sf.read(args.ref_audio, dtype="float32", always_2d=False)
        wav = np.asarray(wav, dtype=np.float32)
        if wav.ndim > 1:
            wav = wav.mean(axis=1)  # downmix to mono
        wav = wav.reshape(-1)
        if sr != SAMPLE_RATE:
            # VERIFY ON POD: resample to SAMPLE_RATE before encoding (e.g. with
            # librosa/torchaudio); the NanoCodec expects its native 22050 Hz.
            print(f"[encode] WARNING ref audio sr={sr} != {SAMPLE_RATE}; resample "
                  "before encoding for correct codes. VERIFY ON POD.", flush=True)
        print(f"[encode] reference clip {args.ref_audio}: {wav.shape[0]} samples @ {sr} Hz",
              flush=True)
    else:
        # Generate a short clip via one unconditioned synth, then encode it. This keeps
        # the "model's own voice" character while routing through the codec's encoder for
        # a canonically formatted payload. The clip is a runtime artifact, never committed.
        runner = _load_runner(args)
        print("[encode] generating a short reference clip (one unconditioned synth) ...",
              flush=True)
        with torch.no_grad():
            tokens = runner.generate(
                args.text, ref_codes=None,
                temperature=args.temperature, top_k=args.top_k,
                cfg_scale=args.cfg_scale, max_frames=args.max_frames,
            )
            dcodes = tokens.unsqueeze(0).to(args.device)          # [1, C, T] decode layout
            dlen = dcodes.new_tensor([dcodes.shape[-1]])
            decoded = codec.decode_from_codes(dcodes, dlen)
            wav = _wave_from_decoded(decoded)
        sf.write(args.clip_out, wav, SAMPLE_RATE, subtype="PCM_16", format="WAV")
        print(f"[encode] wrote runtime reference clip -> {args.clip_out} "
              f"({wav.shape[0] / SAMPLE_RATE:.1f}s)", flush=True)

    ref = _encode_audio(codec, wav, args)
    return ref


def _encode_audio(codec, wav, args):
    """Encode a mono float waveform to ref_codes via the codec. The method name and
    signature are pod-verified: we probe ENCODE_CANDIDATES and try (audio, length)
    then (audio). VERIFY ON POD."""
    import numpy as np
    import torch

    audio = torch.as_tensor(np.asarray(wav, dtype=np.float32)).reshape(1, -1).to(args.device)
    audio_len = torch.tensor([audio.shape[-1]], dtype=torch.long, device=args.device)

    method = next((n for n in ENCODE_CANDIDATES if hasattr(codec, n)), None)
    if method is None:
        raise SystemExit(
            "no audio->codes method found on the codec. VERIFY ON POD: run "
            "`sorted(dir(codec))`, add the real encode method to ENCODE_CANDIDATES, "
            "and confirm its call signature.")
    fn = getattr(codec, method)
    print(f"[encode] using codec.{method}(...)  # VERIFY ON POD", flush=True)
    with torch.no_grad():
        try:
            out = fn(audio, audio_len)   # VERIFY ON POD: arg order (audio, length)
        except TypeError:
            out = fn(audio)              # VERIFY ON POD: some encoders take audio only
    codes = out[0] if isinstance(out, (tuple, list)) else out
    print(f"[encode] raw codes: shape={_shape(codes)} dtype={_dtype(codes)}", flush=True)
    return _to_1TC(codes, args.codebooks)


def parse_args():
    default_out = Path(__file__).resolve().parent / "voices" / "default.pt"
    default_clip = Path(tempfile.gettempdir()) / "gepard_default_ref.wav"
    ap = argparse.ArgumentParser(
        description="Generate a stable default.pt reference voice for gepard TTS.")
    ap.add_argument("--method", choices=("capture", "encode"), default="capture",
                    help="capture (default): freeze one unconditioned generation's "
                         "tokens. encode: round-trip a clip through the NanoCodec.")
    ap.add_argument("--out", type=Path, default=default_out,
                    help="output .pt path (default tts/voices/default.pt).")
    ap.add_argument("--ref-audio", default="",
                    help="[encode] use this WAV as the reference instead of generating "
                         "one. Should be ~22050 Hz mono.")
    ap.add_argument("--clip-out", type=Path, default=default_clip,
                    help="[encode] where to write the generated runtime reference clip "
                         "(never committed).")
    ap.add_argument("--device", default=os.environ.get("LA_TTS_DEVICE", "cuda"))
    ap.add_argument("--text",
                    default="Hello, this is the stable default voice for the assistant.",
                    help="text used for the one unconditioned synth.")
    ap.add_argument("--temperature", type=float,
                    default=float(os.environ.get("LA_TTS_TEMP", "0.3")))
    ap.add_argument("--top-k", type=int, default=int(os.environ.get("LA_TTS_TOPK", "0")))
    ap.add_argument("--cfg-scale", type=float,
                    default=float(os.environ.get("LA_TTS_CFG", "1.0")))
    ap.add_argument("--max-frames", type=int,
                    default=int(os.environ.get("LA_TTS_MAXFRAMES", "1075")))
    ap.add_argument("--codebooks", type=int, default=32,
                    help="NanoCodec codebook count, used to find the C axis "
                         "(server builds [1,32,T]). VERIFY ON POD.")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="[capture] skip feeding the built ref_codes back into generate().")
    return ap.parse_args()


def main():
    args = parse_args()

    import torch
    if not torch.cuda.is_available():
        print("[warn] torch.cuda.is_available() is False. This script must run on the "
              "GPU pod in the gepard venv; on CPU the model load will be slow or fail.",
              file=sys.stderr, flush=True)
    else:
        print(f"[env] torch {torch.__version__}  device={args.device}  "
              f"{torch.cuda.get_device_name(0)}", flush=True)

    ref = method_capture(args) if args.method == "capture" else method_encode(args)

    # Mirror the validation GepardBackend._load_voices performs before it accepts a voice.
    if ref.dim() != 3:
        raise SystemExit(f"built ref_codes must be [1, T, C] (dim 3), got {tuple(ref.shape)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        print(f"[save] overwriting existing {args.out}", flush=True)
    payload = {
        "ref_codes": ref.cpu().long().contiguous(),
        "name": "default",
        "sample_rate": SAMPLE_RATE,
        "source": args.ref_audio or "one unconditioned gepard synth",
        "method": args.method,
    }
    torch.save(payload, args.out)
    saved = payload["ref_codes"]
    print(f"[save] wrote {args.out}", flush=True)
    print(f"[save] ref_codes shape={tuple(saved.shape)} dtype={saved.dtype}", flush=True)
    print("[done] restart TTS; 'default' now loads this stable reference. Confirm in "
          "logs: '[gepard] default voice is CONDITIONED ...'.", flush=True)


if __name__ == "__main__":
    main()
