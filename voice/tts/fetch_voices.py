"""Download the gepard Space's preset speaker reference-code files into a local
voices directory.

WHAT / WHY
    The gepard TTS backend (GepardBackend._load_voices in tts/server.py)
    enumerates *.pt reference-code files in a voices directory and exposes each
    file stem as a selectable voice over GET /voices. The upstream public
    HuggingFace Space "nineninesix/gepard" ships 18 preset speakers under its
    speakers/ folder; this script pulls those files down so the pod has more
    than just the pinned "default" voice.

    Public Space: no HF_TOKEN is required. Never overwrites default.pt (see
    tts/make_gepard_voice.py for why that one file is special: it is the
    pinned, conditioned default speaker, not one of the upstream presets).

RUNS ON THE GPU POD, with the TTS venv interpreter (needs huggingface_hub):
    $(cat .tts_python) tts/fetch_voices.py [dest_dir]

dest_dir defaults to the LA_VOICES_DIR env var, then tts/voices next to this
file.
"""
import os
import shutil
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

def main():
    if len(sys.argv) > 1:
        dest = Path(sys.argv[1])
    elif os.environ.get("LA_VOICES_DIR"):
        dest = Path(os.environ["LA_VOICES_DIR"])
    else:
        dest = Path(__file__).resolve().parent / "voices"
    dest.mkdir(parents=True, exist_ok=True)

    snap = snapshot_download(
        repo_id="nineninesix/gepard", repo_type="space",
        allow_patterns=["speakers/*.pt"],
    )
    src = Path(snap) / "speakers"

    copied = 0
    for pt in sorted(src.glob("*.pt")):
        if pt.name == "default.pt":
            print(f"skipping {pt.name}: never overwrite the pinned default voice")
            continue
        shutil.copy(pt, dest / pt.name)   # resolves the HF cache symlink to a real file
        copied += 1

    print(f"copied {copied} voices -> {dest}")
    print(sorted(p.name for p in dest.glob("*.pt")))

if __name__ == "__main__":
    main()
