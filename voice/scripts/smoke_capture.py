"""Local WS smoke for the opt-in segment capture (LA_CAPTURE).

Starts its OWN orchestrator (LA_CAPTURE=1) on a free port, pointed at a THROWAWAY
captures dir, against the shared mock ASR/TTS. Every utterance rides the Part A
filename-override hook, so the mock ASR echoes it back verbatim and the canned-
segment counter the other smokes depend on is never touched (this suite is
parity-neutral and its position in the run is free).

Scenarios (CAP-SMOKE a..e PASS/FAIL + overall):
  a. connect -> capture_state {on: true}.
  b. client_info + one segment -> the WAV exists, is a valid 16 kHz MONO PCM16 RIFF,
     and holds the EXACT bytes that were uploaded (a header, not a re-encode).
  c. the JSONL record carries the transcript, the ASR confidence block, and the
     connection's client_info.
  d. label_segment -> segment_labeled ack, and the label FOLDS through
     scripts/capture_report.py (the real reader, not a reimplementation here).
  e. an unknown label value is rejected (error, no ack) and writes nothing.
  f. a low-confidence segment (pmean 0.15) -> transcript carries
     discarded:"low_confidence", ending the turn yields no reply, and the capture
     record carries reject_reason (the noise-gate floor, LA_CONF_FLOOR).
  g. degenerate text at HIGH confidence (a "to the to the" loop, pmean 0.98) ->
     discarded:"degenerate" (the floor cannot catch it; the text shape does).

The captures dir it writes is moved to deprecated/ at the end (house rule: never
delete in place).
"""
import asyncio
import base64
import datetime
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.parse import quote

import websockets

APP = Path(__file__).resolve().parent.parent
CAP_PORT = os.environ.get("LA_CAP_PORT", "8794")
URL = f"ws://localhost:{CAP_PORT}/"
# a throwaway captures dir, so the smoke never pollutes a real calibration set
CAP_DIR = APP / "data" / "smoke_captures"

_spec = importlib.util.spec_from_file_location(
    "capture_report", APP / "scripts" / "capture_report.py")
capture_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capture_report)

CLIENT_INFO = {"type": "client_info", "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)",
               "platform": "iPhone", "hw_sample_rate": 48000, "resampled": True,
               "vad_threshold": 0.62, "seg_pause_ms": 500, "turn_pause_ms": 900,
               "viewport": "390x844"}


def utter_bytes(text, pmin=0.97, pmean=0.98) -> bytes:
    """The Part A override token: the mock ASR returns exactly this text, and these
    same bytes are what the server captures (so the WAV check below is exact)."""
    return f"utter;text={quote(text)};pmin={pmin};pmean={pmean}.wav".encode("ascii")


def utter_seg(text, pmin=0.97, pmean=0.98) -> str:
    return json.dumps({"type": "audio_segment",
                       "audio_b64": base64.b64encode(utter_bytes(text, pmin, pmean)).decode(),
                       "sample_rate": 16000})


class Client:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.counts = {}
        self.msgs = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    def _note(self, m):
        t = m.get("type")
        self.counts[t] = self.counts.get(t, 0) + 1
        self.msgs.append(m)

    async def drain(self, timeout):
        try:
            while True:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout)))
        except asyncio.TimeoutError:
            pass

    async def recv_until_count(self, want, target, timeout=40):
        try:
            while self.counts.get(want, 0) < target:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout)))
        except asyncio.TimeoutError:
            print(f"  <- TIMEOUT waiting for {want} >= {target}", flush=True)

    async def send(self, obj):
        await self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))

    async def segment(self, text, pmin=0.97, pmean=0.98):
        before = self.counts.get("transcript", 0)
        await self.send(utter_seg(text, pmin, pmean))
        await self.recv_until_count("transcript", before + 1)
        return self.last("transcript")

    def has(self, t):
        return any(m.get("type") == t for m in self.msgs)

    def last(self, t):
        return next((m for m in reversed(self.msgs) if m.get("type") == t), None)


def read_log():
    """(segments, folded_labels) straight out of the real analysis helper."""
    log = CAP_DIR / "captures.jsonl"
    if not log.is_file():
        return [], {}
    return capture_report.load(log)


async def scenarios():
    results = {}
    text = "start the centrifuge at 3000 r p m"

    print("a: connect -> capture_state on:true", flush=True)
    async with Client(URL) as c:
        await c.drain(3)
        cs = c.last("capture_state")
        results["a"] = cs is not None and cs.get("on") is True
        print(f"   capture_state={cs} -> {'PASS' if results['a'] else 'FAIL'}", flush=True)

        print("b: segment -> WAV saved, valid 16 kHz mono PCM16, exact bytes", flush=True)
        await c.send(CLIENT_INFO)
        tr = await c.segment(text)
        seq = (tr or {}).get("id")
        await asyncio.sleep(1.0)   # the write is deliberately off the latency path (a thread)

        segments, _ = read_log()
        rec = next((r for r in segments if r.get("seq") == seq), None)
        sent = utter_bytes(text)
        sent_even = sent[:len(sent) - (len(sent) % 2)]
        wav_ok = False
        if rec is not None:
            wav_path = CAP_DIR / rec["wav"]
            if wav_path.is_file():
                with wave.open(str(wav_path), "rb") as wf:
                    frames = wf.readframes(wf.getnframes())
                    wav_ok = (wf.getnchannels() == 1 and wf.getsampwidth() == 2
                              and wf.getframerate() == 16000 and frames == sent_even)
        # the clip must live under today's SGT date, keyed by sid + transcript id
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        path_ok = rec is not None and rec["wav"].startswith(today + "/") and rec["wav"].endswith(f"-{seq}.wav")
        results["b"] = bool(rec) and wav_ok and path_ok
        print(f"   wav={rec and rec.get('wav')} valid_riff+exact_bytes={wav_ok} path={path_ok} "
              f"-> {'PASS' if results['b'] else 'FAIL'}", flush=True)

        print("c: JSONL record has transcript + confidence + client_info", flush=True)
        conf = (rec or {}).get("confidence") or {}
        ci = (rec or {}).get("client_info") or {}
        results["c"] = (rec is not None
                        and rec.get("transcript") == text
                        and conf.get("prob_mean") == 0.98 and conf.get("prob_min") == 0.97
                        and ci.get("platform") == "iPhone" and ci.get("hw_sample_rate") == 48000
                        and ci.get("resampled") is True
                        and rec.get("accepted") is True
                        and rec.get("label") is None
                        and isinstance(rec.get("dur_s"), (int, float))
                        and isinstance(rec.get("rms"), (int, float))
                        and isinstance(rec.get("peak"), (int, float)))
        print(f"   transcript={rec and rec.get('transcript')!r} "
              f"conf={conf.get('prob_mean')}/{conf.get('prob_min')} "
              f"platform={ci.get('platform')} -> {'PASS' if results['c'] else 'FAIL'}", flush=True)

        print("d: label_segment -> ack + the label folds", flush=True)
        await c.send({"type": "label_segment", "id": seq, "label": "noise"})
        await c.recv_until_count("segment_labeled", 1, timeout=10)
        ack = c.last("segment_labeled")
        await asyncio.sleep(0.5)
        segments, labels = read_log()
        target = next((r for r in segments if r.get("seq") == seq), None)
        folded = capture_report.label_of(target, labels) if target else None
        ack_ok = ack is not None and ack.get("id") == seq and ack.get("label") == "noise"
        results["d"] = ack_ok and folded == "noise"
        print(f"   ack={ack} folded={folded!r} -> {'PASS' if results['d'] else 'FAIL'}", flush=True)

        print("e: an unknown label value is rejected", flush=True)
        errs_before = c.counts.get("error", 0)
        acks_before = c.counts.get("segment_labeled", 0)
        await c.send({"type": "label_segment", "id": seq, "label": "definitely_not_a_label"})
        await c.drain(2.0)
        rejected = c.counts.get("error", 0) == errs_before + 1
        no_ack = c.counts.get("segment_labeled", 0) == acks_before
        _, labels2 = read_log()
        still_noise = labels2.get(((target or {}).get("sid"), seq)) == "noise"
        results["e"] = rejected and no_ack and still_noise
        print(f"   error={rejected} no_ack={no_ack} label_unchanged={still_noise} "
              f"-> {'PASS' if results['e'] else 'FAIL'}", flush=True)

    # f + g run on their own fresh connections: the noise gate is a per-segment
    # decision, and a fresh session (empty pending) makes "end_turn produced no
    # reply" a real assertion that the rejected segment never entered the turn.
    results["f"] = await _gate_scenario(
        "f", "low-confidence segment -> discarded, dropped from the turn, reject_reason",
        text="mumble background chatter noise", pmin=0.05, pmean=0.15,
        want_discarded="low_confidence")
    results["g"] = await _gate_scenario(
        "g", "degenerate text at HIGH confidence -> discarded degenerate",
        text="to the to the to the to the to the to the to the to the",
        pmin=0.90, pmean=0.98, want_discarded="degenerate")
    return results


async def _gate_scenario(name, desc, *, text, pmin, pmean, want_discarded):
    """Drive one rejected segment on a fresh connection: assert the transcript
    carries discarded=<reason>, that ending the turn yields NO reply (the segment
    never accumulated), and that the capture record carries reject_reason."""
    print(f"{name}: {desc}", flush=True)
    async with Client(URL) as c:
        await c.drain(2)
        sid = (c.last("session_started") or {}).get("id")
        tr = await c.segment(text, pmin=pmin, pmean=pmean)
        seq = (tr or {}).get("id")
        await c.send({"type": "end_turn"})
        await c.drain(4.0)   # a rejected segment leaves the turn empty: no reply must come
        await asyncio.sleep(0.8)   # let the capture thread flush
        disc = (tr or {}).get("discarded")
        no_reply = not (c.has("reply_start") or c.has("reply_done") or c.has("reply_audio"))
        segments, _ = read_log()
        rrec = next((r for r in segments
                     if r.get("sid") == sid and r.get("seq") == seq), None)
        reason_ok = (rrec is not None and rrec.get("reject_reason") == want_discarded
                     and rrec.get("accepted") is False)
        ok = disc == want_discarded and no_reply and reason_ok
    print(f"   discarded={disc!r} no_reply={no_reply} "
          f"reject_reason={rrec and rrec.get('reject_reason')!r} "
          f"-> {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


def _wait_port(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True
        time.sleep(0.3)
    return False


def _cleanup_captures():
    """House rule: move aside, never delete in place."""
    if not CAP_DIR.exists():
        return
    dest = APP / "deprecated" / f"smoke_captures-{int(time.time())}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(CAP_DIR), str(dest))
    except Exception as e:  # noqa: BLE001
        print(f"  (cleanup: could not move {CAP_DIR}: {e})", flush=True)


async def main():
    _cleanup_captures()   # a leftover from a previous run would poison the assertions
    env = dict(os.environ)
    env["LA_WS_PORT"] = CAP_PORT
    env["LA_CAPTURE"] = "1"
    env["LA_CAPTURE_DIR"] = str(CAP_DIR)
    env["LA_LAB_MODE"] = "0"
    env.setdefault("LA_FUNASR_URL", "http://localhost:9001/v1")
    env.setdefault("LA_TTS_MODELS",
                   "gepard-1.0=http://localhost:9002,gepard-1.0-alt=http://localhost:9003")
    env.setdefault("LA_TTS_URL", "http://localhost:9002")
    proc = subprocess.Popen([sys.executable, str(APP / "web" / "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_port(CAP_PORT):
            print("CAP-SMOKE: FAIL: orchestrator never bound the port", flush=True)
            return 1
        results = await scenarios()
        for name in sorted(results):
            print(f"CAP-SMOKE {name}: {'PASS' if results[name] else 'FAIL'}", flush=True)
        overall = all(results.values())
        print("CAP-SMOKE: PASS" if overall else "CAP-SMOKE: FAIL", flush=True)
        return 0 if overall else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        _cleanup_captures()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
