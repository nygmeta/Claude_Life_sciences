"""End-to-end acceptance with EVERY module real: no mocks anywhere.

The integration smoke (scripts/smoke_integration.py) proves the two halves talk, but
it fakes both ends of the pipe: the mock ASR is handed the transcript AND its
confidence, and the mock TTS returns silence. That is the right tool for testing
control flow and the wrong one for answering "will this work on stage", because the
two numbers the safety gates actually key on (prob_mean, prob_min) were injected by
the test rather than produced by a speech recognizer.

So: speak to it for real.

  gepard-1.0 (real TTS, GPU)  ->  16 kHz PCM  ->  the orchestrator's WebSocket
                                                      |
                                            Fun-ASR-Nano (real ASR, GPU)
                                                      |
                                            Lab Agent API (real planner)
                                                      |
                                                  spoken reply

Every utterance below is SYNTHESIZED as audio by the real TTS and fed to the socket as
if it came from a microphone, so the real ASR does real recognition and reports its
real confidence. That is what makes this run able to answer the question the mock run
could not: is LA_CONFIRM_FLOOR set anywhere near the confidences that real speech
actually produces?

Caveat worth stating out loud: synthesized speech is CLEANER than a scientist in a
room with a centrifuge, so the confidences here are an optimistic bound. They tell us
whether the floor is catastrophically miscalibrated (rejecting good speech); they do
not prove it is right for a noisy bench. The --degrade run adds noise to probe the
other direction.

Usage:
    python3 scripts/e2e_real.py --ws ws://127.0.0.1:8766/ --tts http://127.0.0.1:8040
    python3 scripts/e2e_real.py ... --degrade 0.35    # noisy mic, for the floor probe
"""
import argparse
import asyncio
import base64
import io
import json
import sys
import time

import httpx
import numpy as np
import soundfile as sf
import websockets

TARGET_SR = 16000   # what the orchestrator's segments are (server.SAMPLE_RATE)

# The Lab Agent's demo arc, spoken. The 400 uL is deliberately unsafe: its validator
# must refuse it out loud before anything is built.
# NOTE ON THE ANALYTE. The Lab Agent accepts IL-6, IL-8, TNF-alpha and CRP. This script
# says CRP, not IL-6, and the reason is a limitation of the TEST RIG, not of the system:
# the speaker here is gepard, and gepard cannot pronounce "IL-6" intelligibly. Fun-ASR
# heard its attempts as "Stoyl 6" and "IELTS 6", so the analyte slot never filled and
# the conversation deadlocked. A human saying "IL-6" does not have that problem. CRP is
# spoken and recognized cleanly, so it exercises the same code path without the rig's
# pronunciation defect standing in for a system defect.
#
# The real risk this exposes is still worth knowing before a demo: domain ACRONYMS are
# the most ASR-fragile part of a lab utterance, and they are exactly what the planner
# slot-fills on. See doc/LAB_AGENT_FINDINGS.md.
SCRIPT = [
    ("request", "Run an ELISA on today's plasma samples."),
    ("unsafe", "Let's do CRP with 24 samples, 400 microliters per well."),
    ("fix", "My mistake, make it 100 microliters per well."),
    ("confirm", "Yes, go ahead."),
]


async def synth(tts_url: str, text: str, voice: str) -> np.ndarray:
    """Speak a line with the real TTS, return float32 mono at TARGET_SR."""
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{tts_url}/synthesize",
                         json={"text": text, "voice": voice})
        r.raise_for_status()
    wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != TARGET_SR:
        # Anti-aliased resample. This matters more than it looks: a naive
        # np.interp() decimation from 22050 to 16000 folds everything above 8 kHz
        # back down as alias noise, and the ASR hears the artefacts. Measured: with
        # linear interp, a clean synthesized line came back as unrelated text. Use a
        # polyphase filter (resample_poly low-passes before decimating).
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(TARGET_SR, sr)
        wav = resample_poly(wav, TARGET_SR // g, sr // g).astype("float32")
    return wav


def to_pcm16(wav: np.ndarray, degrade: float = 0.0) -> bytes:
    """float32 -> raw PCM16 bytes, optionally degraded to simulate a poor mic.

    `degrade` is the standard deviation of additive white noise relative to the
    signal's RMS, plus an attenuation. This is a crude stand-in for a scientist
    speaking away from the mic in a noisy room, and it exists to push the ASR into
    the low-confidence regime the confirmation floor is supposed to catch.
    """
    if degrade > 0:
        rms = float(np.sqrt(np.mean(wav ** 2))) or 1e-6
        wav = wav * 0.45 + np.random.normal(0, degrade * rms, len(wav)).astype("float32")
    peak = float(np.max(np.abs(wav))) or 1.0
    wav = np.clip(wav / max(peak, 1.0), -1.0, 1.0)
    return (wav * 32767).astype("<i2").tobytes()


class Client:
    def __init__(self, url):
        self.url = url
        self.msgs = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=32 * 1024 * 1024)
        await self._drain(2)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    async def _drain(self, t):
        try:
            while True:
                self.msgs.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=t)))
        except asyncio.TimeoutError:
            pass

    async def speak(self, pcm: bytes, timeout=120):
        """Send one utterance as audio and wait for the assistant to finish replying."""
        start = len(self.msgs)
        await self.ws.send(json.dumps({
            "type": "audio_segment",
            "audio_b64": base64.b64encode(pcm).decode(),
            "sample_rate": TARGET_SR,
        }))
        # wait for the REAL ASR to come back with a transcript
        while not any(m.get("type") in ("transcript", "segment_dropped", "segment_labeled")
                      for m in self.msgs[start:]):
            self.msgs.append(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=60)))
        await self.ws.send(json.dumps({"type": "end_turn"}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            except asyncio.TimeoutError:
                break
            self.msgs.append(m)
            if m.get("type") == "reply_audio_end":
                break
        await self._drain(0.5)
        return self.msgs[start:]

    @staticmethod
    def last(msgs, t):
        return next((m for m in reversed(msgs) if m.get("type") == t), None)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8766/")
    ap.add_argument("--tts", default="http://127.0.0.1:8040")
    ap.add_argument("--voice", default="default")
    ap.add_argument("--degrade", type=float, default=0.0,
                    help="additive noise (x signal RMS) to simulate a poor mic")
    ap.add_argument("--email", default="")
    args = ap.parse_args()

    url = args.ws + (f"?email={args.email}" if args.email else "")

    print("synthesizing the script with the REAL TTS...", flush=True)
    audio = []
    for tag, line in SCRIPT:
        wav = await synth(args.tts, line, args.voice)
        audio.append((tag, line, to_pcm16(wav, args.degrade)))
        print(f"  {tag:8} {len(wav)/TARGET_SR:5.1f}s  {line!r}", flush=True)

    confs = []
    print(f"\nspeaking to {url}\n", flush=True)
    async with Client(url) as c:
        for tag, line, pcm in audio:
            turn = await c.speak(pcm)
            tr = c.last(turn, "transcript")
            lb = c.last(turn, "lab_backend")
            rd = c.last(turn, "reply_done")
            drop = c.last(turn, "segment_dropped") or c.last(turn, "segment_labeled")

            heard = (tr or {}).get("text", "")
            conf = (tr or {}).get("confidence") or {}
            pmean, pmin = conf.get("prob_mean"), conf.get("prob_min")
            if pmean is not None:
                confs.append((tag, pmean, pmin))

            print(f"[{tag}]", flush=True)
            print(f"   said : {line!r}", flush=True)
            print(f"   heard: {heard!r}", flush=True)
            print(f"   ASR confidence: prob_mean={pmean} prob_min={pmin}"
                  f"{'   (segment DROPPED by the noise gate)' if drop and not tr else ''}", flush=True)
            if lb:
                print(f"   backend: state={lb.get('state')} intent={lb.get('intent')} "
                      f"validation_passed={lb.get('validation_passed')} "
                      f"ops={lb.get('operations')}", flush=True)
            else:
                print("   backend: NOT CALLED this turn", flush=True)
            print(f"   spoke: {((rd or {}).get('text') or '')[:100]!r}\n", flush=True)

    print("=" * 72, flush=True)
    if confs:
        means = [c[1] for c in confs]
        print("REAL ASR CONFIDENCE (this is what the gates key on):", flush=True)
        for tag, pmean, pmin in confs:
            print(f"   {tag:8} prob_mean={pmean:.3f}  prob_min={pmin:.3f}", flush=True)
        print(f"   -> min prob_mean seen: {min(means):.3f}", flush=True)
        print(f"   -> LA_CONFIRM_FLOOR must sit BELOW {min(means):.3f} or a good "
              f"confirmation gets refused.", flush=True)
    else:
        print("NO CONFIDENCE REPORTED: the ASR returned no confidence block. The "
              "confirmation floor cannot bite at all (it fails open).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
