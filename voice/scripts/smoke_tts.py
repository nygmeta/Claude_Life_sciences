"""On-GPU smoke test for the gepard TTS stack. Run with the gepard env interpreter:

    $(cat "$LA_APP_DIR/.tts_python") "$LA_APP_DIR/scripts/smoke_tts.py"

Loads Gepard + NanoCodec and synthesizes one sentence through the SAME call shape the
production service uses (tts/server.py GepardBackend.synthesize): a conditioned voice
(tts/voices/en_oak.pt) passed as ref_codes PLUS max_frames, then asserts the result is
real audio before exiting. If this passes, the NeMo / transformers 5.3.0 / torch install
is good and tts/server.py will work.

This is also the place to fix the generate()/
decode_from_codes() call shapes if the gepard_inference API differs from expected.

The exit code is honest: it is nonzero if synth under-produces. An early-EOS regression
in generate() (hitting end-of-speech after ~2 NanoCodec frames) still writes a VALID but
~0.1s wav, so a bare "a RIFF file was written" check passes on a broken model. This test
instead asserts real duration and non-silence, and prints the frame count so that
under-production is visible at a glance. The output path and voices dir follow LA_APP_DIR
(falling back to the script's repo root), so this runs as an unprivileged user on any host.
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from gepard_inference.runner import GepardRunner
from gepard_inference.codec_wrapper import UnfoldedCodecModel

MODEL = "nineninesix/gepard-1.0"
CODEC = "nvidia/nemo-nano-codec-22khz-1.89kbps-21.5fps"
TEXT = "Hello, this is a test of the gepard text to speech voice."
SR = 22050  # NanoCodec 22 kHz output
MAX_FRAMES = 1075  # tts/server.py DEF_MAXFRAMES; omitting this is what the old smoke did wrong
VOICE = "en_oak.pt"  # a conditioned preset, mirroring the service's default synth path

# Thresholds that make the exit code honest. An early-EOS under-production is a valid but
# tiny wav, so "a file exists" is not enough; require real duration AND non-silence.
MIN_DURATION_S = 2.0
MIN_PEAK = 0.01

# Repo root: LA_APP_DIR on the pod, else two levels up from this script (repo>/scripts/).
REPO = Path(os.environ.get("LA_APP_DIR") or Path(__file__).resolve().parent.parent)
VOICES_DIR = REPO / "tts" / "voices"
OUT = str(REPO / "out.wav")


def load_voice(device):
    """Load tts/voices/en_oak.pt the way GepardBackend._load_voices does: a dict with
    'ref_codes' [1,T,C] long, or a bare tensor. Missing/broken -> print a clear warning
    and return None (the model's own unconditioned voice) rather than crash."""
    pt = VOICES_DIR / VOICE
    if not pt.is_file():
        print(f"[smoke] WARNING voice {pt} missing; falling back to ref_codes=None "
              "(unconditioned). This does NOT exercise the service's default synth path.",
              flush=True)
        return None
    try:
        payload = torch.load(pt, map_location="cpu", weights_only=True)
        codes = payload["ref_codes"] if isinstance(payload, dict) else payload
        if codes.dim() != 3:
            raise ValueError(f"expected ref_codes [1,T,C], got {tuple(codes.shape)}")
        ref = codes.long().to(device)
        print(f"[smoke] voice {pt.name}: ref_codes {tuple(ref.shape)} {ref.dtype}", flush=True)
        return ref
    except Exception as e:  # noqa: BLE001
        print(f"[smoke] WARNING failed to load {pt.name} ({type(e).__name__}: {e}); "
              "falling back to ref_codes=None.", flush=True)
        return None


device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"torch {torch.__version__}  cuda={torch.cuda.is_available()} "
      f"{torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}", flush=True)

t0 = time.time()
runner = GepardRunner.from_checkpoint(MODEL, device=device, attn_implementation="eager")
codec = UnfoldedCodecModel.from_pretrained(CODEC).eval().to(device)
print(f"loaded in {time.time() - t0:.1f}s", flush=True)

ref = load_voice(device)

t1 = time.time()
with torch.no_grad():
    tokens = runner.generate(
        TEXT, ref_codes=ref, temperature=0.3, top_k=0, cfg_scale=1.0,
        max_frames=MAX_FRAMES,
    )
    codes = tokens.unsqueeze(0).to(device)          # [1, 32, T]
    frames = int(codes.shape[-1])                   # T: NanoCodec frames the LM emitted
    codes_len = codes.new_tensor([frames])
    decoded = codec.decode_from_codes(codes, codes_len)
    audio = decoded[0] if isinstance(decoded, (tuple, list)) else decoded

wave = np.asarray(audio.detach().float().cpu().numpy(), dtype=np.float32).reshape(-1)
sf.write(OUT, wave, SR, subtype="PCM_16", format="WAV")

samples = int(wave.shape[0])
dur = samples / SR
peak = float(np.abs(wave).max()) if samples else 0.0
print(f"synth {time.time() - t1:.1f}s  frames={frames}  samples={samples}  "
      f"({dur:.2f}s audio)  peak={peak:.3f}  -> {OUT}", flush=True)

# Honest exit code: the under-production we are hunting writes a valid but ~0.1s wav, so
# assert real duration AND non-silence, not just "a file was written".
problems = []
if dur < MIN_DURATION_S:
    problems.append(f"audio too short: {dur:.2f}s < {MIN_DURATION_S:.1f}s "
                    f"(frames={frames}; generate() likely hit EOS early)")
if peak < MIN_PEAK:
    problems.append(f"audio is ~silence: peak {peak:.4f} < {MIN_PEAK}")
if problems:
    for p in problems:
        print(f"FAIL {p}", flush=True)
    sys.exit(1)

print("OK", flush=True)
