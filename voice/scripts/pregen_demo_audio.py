"""Pre-render the demo's spoken replies, so Demo mode needs nothing but the page.

Demo mode's whole promise is that it runs from fixtures: no backend, no API key, no
network, so a bad conference connection cannot break it on stage. Speaking those replies
through the live TTS quietly broke that promise: the demo suddenly needed a GPU service
to be up. This script puts the promise back by rendering the audio ahead of time.

It synthesizes each fixture reply through the SAME path a live reply takes, which is the
only way the recording sounds like the system rather than like a different system:

  reply text -> speakable.to_speakable()  (so "e.g." is spoken "for example" and "IL-6"
                                           is spelled out, exactly as it is live)
             -> split into sentences      (gepard has a frame cap; the live path splits
                                           for the same reason, and a long unsplit reply
                                           can be truncated)
             -> gepard, voice + params pinned to the server's defaults
             -> concatenated, encoded to MP3 (about 20x smaller than the raw WAV)

Output: web/demo-audio/<scenario>-<index>.mp3 plus a manifest.json the console reads.
The console plays the file when it has one and falls back to the live voice service when
it does not, so a fixture added later still speaks, just over the network until this is
re-run.

Run it whenever the fixtures or the default voice change:

    python3 scripts/pregen_demo_audio.py            # needs the TTS service reachable
    python3 scripts/pregen_demo_audio.py --voice nurisa --tts http://127.0.0.1:8040
"""
import argparse
import io
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

import httpx

VOICE_DIR = Path(__file__).resolve().parents[1]        # .../voice
sys.path.insert(0, str(VOICE_DIR))

from web.server import split_sentences                  # noqa: E402
from web.speakable import to_speakable                  # noqa: E402

CONSOLE = VOICE_DIR / "web" / "console.html"
OUT_DIR = VOICE_DIR / "web" / "demo-audio"


def load_fixtures(path: Path) -> dict:
    """Pull the FIXTURES object out of the console page.

    Parsed from the page rather than kept in a second file on purpose: two copies of the
    demo script would drift, and the audio would then be of sentences the demo no longer
    says.
    """
    src = path.read_text(encoding="utf-8")
    m = re.search(r"FIXTURES\s*=\s*(\{.*?\});", src, re.S)
    if not m:
        raise SystemExit(f"!! could not find FIXTURES in {path}")
    return json.loads(m.group(1))


def synth(tts: str, text: str, voice: str, params: dict) -> bytes:
    """One sentence -> WAV bytes, through the same params the server pins."""
    body = {"text": text, "voice": voice, **params}
    r = httpx.post(f"{tts}/synthesize", json=body, timeout=180)
    r.raise_for_status()
    return r.content


def concat_wavs(wavs: list[bytes]) -> bytes:
    """Join the per-sentence WAVs into one, keeping the first one's format."""
    frames, params = [], None
    for w in wavs:
        with wave.open(io.BytesIO(w), "rb") as wf:
            if params is None:
                params = wf.getparams()
            frames.append(wf.readframes(wf.getnframes()))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        for f in frames:
            out.writeframes(f)
    return buf.getvalue()


def to_mp3(wav: bytes, dest: Path) -> None:
    """MP3 because every browser plays it with no fuss, and 64 kbps mono is about 20x
    smaller than the raw 22 kHz PCM: the whole demo's audio fits in well under a
    megabyte, which is the difference between shipping it with the page and not."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
         "-codec:a", "libmp3lame", "-b:a", "64k", "-ac", "1", str(dest)],
        input=wav, check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tts", default="http://127.0.0.1:8040")
    ap.add_argument("--voice", default="nurisa")
    # Keep in step with the server's DEF_TEMP / DEF_VOICE. If these drift, Demo and Live
    # stop sounding like the same assistant, which is worse than either value alone.
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--cfg_scale", type=float, default=1.0)
    ap.add_argument("--top_k", type=int, default=0)
    ap.add_argument("--force", action="store_true",
                    help="re-render clips that already exist (after a fixture or voice change)")
    args = ap.parse_args()

    try:
        httpx.get(f"{args.tts}/health", timeout=5).raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"!! TTS not reachable at {args.tts}: {e}")
        print("   start the GPU services (deploy/dev-forward.sh) and retry.")
        return 1

    params = {"temperature": args.temperature, "cfg_scale": args.cfg_scale,
              "top_k": args.top_k}
    fixtures = load_fixtures(CONSOLE)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {"voice": args.voice, "params": params, "clips": {}}
    total = 0
    for scenario, turns in fixtures.items():
        if not isinstance(turns, list):
            continue        # FIXTURES also carries non-scenario keys; they have no replies
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            reply = (turn.get("reply") or "").strip()
            if not reply:
                continue
            dest = OUT_DIR / f"{scenario}-{i}.mp3"
            if dest.exists() and not args.force:
                kb = dest.stat().st_size / 1024
                total += kb
                manifest["clips"][f"{scenario}:{i}"] = {
                    "file": f"demo-audio/{scenario}-{i}.mp3", "kb": round(kb, 1)}
                print(f"  {scenario}:{i:<12} exists, skipping ({kb:.0f} KB). --force to redo.",
                      flush=True)
                continue
            key = f"{scenario}:{i}"
            spoken = to_speakable(reply)
            sentences, tail = split_sentences(spoken + " ")
            tail = tail.strip()
            if len(tail) >= 2:
                sentences.append(tail)
            if not sentences:
                sentences = [spoken]

            print(f"  {key:14} {len(sentences)} sentence(s)  {reply[:58]!r}", flush=True)
            wavs = [synth(args.tts, s, args.voice, params) for s in sentences]
            to_mp3(concat_wavs(wavs), dest)

            kb = dest.stat().st_size / 1024
            total += kb
            # Keyed by scenario+index, not by a hash of the text: the console knows which
            # turn it is playing, and a text hash would silently miss after a typo fix.
            manifest["clips"][key] = {"file": f"demo-audio/{scenario}-{i}.mp3",
                                      "kb": round(kb, 1)}
            print(f"                 -> {dest.name} ({kb:.0f} KB)", flush=True)

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\n{len(manifest['clips'])} clips, {total:.0f} KB total, voice={args.voice}")
    print(f"manifest: {OUT_DIR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
