"""Unit tests for the opt-in segment capture (LA_CAPTURE) and its analysis helper.

Three things are pinned here:
  1. `build_capture_record` is a PURE function (pcm + transcript + confidence +
     client_info -> dict). Its shape and its rms / peak / dur_s math are asserted
     against hand-computed values.
  2. Labels FOLD: captures.jsonl is append-only, so a label is a new record and the
     LAST label per (sid, seq) wins. That is the one piece of logic the whole
     labeling design rests on, and it lives in scripts/capture_report.py.
  3. Capture OFF writes nothing. The default is off, and off must mean not one byte
     on disk, or a debug feature has changed production behavior.
"""
import array
import asyncio
import importlib.util
import json
import os
import struct
import sys
import wave
from pathlib import Path

import pytest

os.environ.setdefault("LA_" + "ANTHROPIC_" + "API_" + "KEY", "placeholder")
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from web import server  # noqa: E402

# scripts/ is not a package; load capture_report by path so the fold logic under
# test is the same code the operator actually runs.
_spec = importlib.util.spec_from_file_location(
    "capture_report", APP / "scripts" / "capture_report.py")
capture_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(capture_report)


def pcm(*samples) -> bytes:
    """PCM16 little-endian bytes from int samples."""
    return struct.pack("<" + "h" * len(samples), *samples)


def const_pcm(value: int, n: int) -> bytes:
    a = array.array("h", [value] * n)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


CONF = {"logprob_mean": -0.03, "logprob_min": -0.11, "prob_mean": 0.97,
        "prob_min": 0.89, "tokens": 4}
CLIENT_INFO = {"ua": "Mozilla/5.0 (iPhone)", "platform": "iPhone",
               "hw_sample_rate": 48000, "resampled": True, "vad_threshold": 0.6,
               "seg_pause_ms": 500, "turn_pause_ms": 900, "viewport": "390x844"}


# --------------------------------------------------------------- the pure builder
def test_record_shape_and_join_fields():
    rec = server.build_capture_record(
        sid="ab12cd34", scope="public", seq=3, cap_n=1, pcm=const_pcm(1000, 16000),
        transcript="start the centrifuge", confidence=CONF, hotwords=["Claude"],
        addressed=None, accepted=True, client_info=CLIENT_INFO)

    assert rec["kind"] == "segment"
    assert rec["sid"] == "ab12cd34"
    assert rec["seq"] == 3
    assert rec["scope"] == "public"
    # the wav name carries sid + seq, so the clip joins to this record AND to the
    # transcript line the user clicked to label it.
    assert rec["wav"] == f"{rec['date']}/ab12cd34-3.wav"
    assert rec["transcript"] == "start the centrifuge"
    assert rec["confidence"] == CONF
    assert rec["hotwords_active"] == ["Claude"]
    assert rec["addressed"] is None          # LA_ADDRESSED off
    assert rec["accepted"] is True
    assert rec["reject_reason"] is None      # accepted: the noise gate did not fire
    assert rec["client_info"] == CLIENT_INFO
    assert rec["label"] is None              # labels only ever arrive as separate records
    assert rec["error"] is None
    assert rec["ts"].startswith(rec["date"])
    json.dumps(rec)                          # must be a JSON-serializable line


def test_duration_rms_peak_math():
    # 16000 samples at 16 kHz = exactly 1.0 s, all at half of full scale.
    half = 16384
    rec = server.build_capture_record(
        sid="s", scope="public", seq=1, cap_n=1, pcm=const_pcm(half, 16000),
        transcript="x", confidence=None, hotwords=[], addressed=None,
        accepted=True, client_info=None)
    assert rec["dur_s"] == 1.0
    # a constant signal: rms == peak == |value| / 32768
    assert rec["rms"] == pytest.approx(0.5, abs=1e-4)
    assert rec["peak"] == pytest.approx(0.5, abs=1e-4)

    # silence is 0/0, not a divide-by-zero
    quiet = server.build_capture_record(
        sid="s", scope="public", seq=2, cap_n=2, pcm=const_pcm(0, 800),
        transcript="x", confidence=None, hotwords=[], addressed=None,
        accepted=True, client_info=None)
    assert quiet["dur_s"] == 0.05
    assert quiet["rms"] == 0.0
    assert quiet["peak"] == 0.0


def test_peak_is_the_extreme_sample_and_clamps_at_full_scale():
    rec = server.build_capture_record(
        sid="s", scope="public", seq=1, cap_n=1, pcm=pcm(0, 100, -32768, 200),
        transcript="x", confidence=None, hotwords=[], addressed=None,
        accepted=True, client_info=None)
    assert rec["peak"] == 1.0   # -32768 is full scale, and must not report 1.00003
    assert 0.0 < rec["rms"] <= 1.0


def test_failed_transcription_is_still_recorded_without_a_seq():
    """A segment that failed to transcribe is exactly the clip worth keeping, but it
    has no transcript id (nothing was shown), so it is named from cap_n and cannot
    be labeled."""
    rec = server.build_capture_record(
        sid="ab12cd34", scope="public", seq=None, cap_n=7, pcm=const_pcm(500, 320),
        transcript=None, confidence=None, hotwords=[], addressed=None,
        accepted=False, client_info=None, error="TimeoutError")
    assert rec["seq"] is None
    assert rec["wav"] == f"{rec['date']}/ab12cd34-n7.wav"
    assert rec["transcript"] is None
    assert rec["error"] == "TimeoutError"
    assert rec["accepted"] is False


def test_gate_rejected_segment_records_the_reason():
    rec = server.build_capture_record(
        sid="s", scope="public", seq=5, cap_n=5, pcm=const_pcm(300, 160),
        transcript="uh the the", confidence={"prob_mean": 0.12, "prob_min": 0.03},
        hotwords=["Claude"], addressed=None, accepted=False, client_info=None,
        reject_reason="low_confidence")
    assert rec["accepted"] is False
    assert rec["reject_reason"] == "low_confidence"


def test_side_speech_records_the_verdict_and_is_not_accepted():
    verdict = {"addressed": False, "confidence": 0.9, "reason": "small talk"}
    rec = server.build_capture_record(
        sid="s", scope="deadbeefdeadbeef", seq=4, cap_n=4, pcm=const_pcm(200, 160),
        transcript="did you see the game", confidence=CONF, hotwords=["Claude"],
        addressed=verdict, accepted=False, client_info=None)
    assert rec["addressed"] == verdict
    assert rec["accepted"] is False
    assert rec["scope"] == "deadbeefdeadbeef"


def test_client_info_is_sanitized():
    """client_info is client-supplied and goes to disk: unknown keys are dropped and
    long strings are capped, so a client cannot grow the record."""
    ci = server.sanitize_client_info(
        {"ua": "U" * 5000, "platform": "iPhone", "evil": "x" * 100,
         "hw_sample_rate": 48000, "viewport": {"w": 390, "h": 844}})
    assert "evil" not in ci
    assert len(ci["ua"]) == server._CI_MAX_STR
    assert ci["platform"] == "iPhone"
    assert ci["hw_sample_rate"] == 48000
    assert ci["viewport"] == {"w": 390, "h": 844}
    assert set(ci) == set(server._CI_KEYS)   # every known key present (None when unsent)
    assert ci["vad_threshold"] is None


# ------------------------------------------------------------------ label folding
def test_label_folding_takes_the_last_label_per_sid_seq(tmp_path):
    """The whole append-only design rests on this: a re-label appends a second
    record and the LAST one wins, per (sid, seq) independently."""
    log = tmp_path / "captures.jsonl"
    lines = [
        {"kind": "segment", "sid": "aaa", "seq": 1, "label": None, "dur_s": 1.0,
         "rms": 0.1, "confidence": {"prob_mean": 0.5, "prob_min": 0.2}},
        {"kind": "segment", "sid": "aaa", "seq": 2, "label": None, "dur_s": 1.0,
         "rms": 0.2, "confidence": {"prob_mean": 0.9, "prob_min": 0.8}},
        {"kind": "segment", "sid": "bbb", "seq": 1, "label": None, "dur_s": 1.0,
         "rms": 0.3, "confidence": {"prob_mean": 0.7, "prob_min": 0.6}},
        {"kind": "label", "sid": "aaa", "seq": 1, "label": "speech"},
        {"kind": "label", "sid": "aaa", "seq": 1, "label": "noise"},    # corrected: this wins
        {"kind": "label", "sid": "bbb", "seq": 1, "label": "other_speaker"},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    segments, labels = capture_report.load(log)
    assert len(segments) == 3          # label records are not clips
    assert capture_report.label_of(segments[0], labels) == "noise"          # last wins
    assert capture_report.label_of(segments[1], labels) == "unlabeled"      # never labeled
    assert capture_report.label_of(segments[2], labels) == "other_speaker"  # keyed per sid too


def test_load_tolerates_a_truncated_tail_line(tmp_path):
    """The server appends live, so the last line may be half-written."""
    log = tmp_path / "captures.jsonl"
    log.write_text(json.dumps({"kind": "segment", "sid": "a", "seq": 1}) + "\n"
                   + '{"kind": "segment", "sid": "a", "se', encoding="utf-8")
    segments, labels = capture_report.load(log)
    assert len(segments) == 1
    assert labels == {}


def test_stats_percentiles_and_empty():
    s = capture_report.stats([0.1, 0.2, 0.3, 0.4, 0.5, None])
    assert s["n"] == 5
    assert s["min"] == 0.1
    assert s["max"] == 0.5
    assert s["p50"] == 0.3
    assert s["mean"] == pytest.approx(0.3)
    assert capture_report.stats([None, None]) is None
    assert capture_report.stats([]) is None


# ---------------------------------------------------------------- the on/off gate
def _run_capture(sess, pcm_bytes, **kw):
    """Drive capture_segment on a real event loop and wait for its worker thread,
    since the write is deliberately off the caller's critical path."""
    async def go():
        server.capture_segment(sess, pcm_bytes, **kw)
        pending = list(server._CAPTURE_TASKS)
        if pending:
            await asyncio.gather(*pending)
    asyncio.run(go())


def _session():
    sess = server.Session()
    sess.sid = "cafebabe"
    return sess


KW = {"seq": 1, "transcript": "hello there", "confidence": CONF,
      "hotwords": ["Claude"], "addressed": None, "accepted": True}


def test_capture_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CAPTURE_ENABLED", False)
    monkeypatch.setattr(server, "CAPTURES_DIR", tmp_path / "captures")
    monkeypatch.setattr(server, "CAPTURES_LOG", tmp_path / "captures" / "captures.jsonl")
    sess = _session()

    _run_capture(sess, const_pcm(1000, 1600), **KW)

    assert not (tmp_path / "captures").exists()   # not even the directory
    assert sess.cap_n == 0
    assert sess.captured == set()                 # so nothing is labelable either


def test_capture_on_writes_a_valid_wav_and_a_record(tmp_path, monkeypatch):
    cdir = tmp_path / "captures"
    monkeypatch.setattr(server, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(server, "CAPTURES_DIR", cdir)
    monkeypatch.setattr(server, "CAPTURES_LOG", cdir / "captures.jsonl")
    sess = _session()
    sess.client_info = server.sanitize_client_info(CLIENT_INFO)

    _run_capture(sess, const_pcm(8000, 3200), **KW)

    recs = [json.loads(x) for x in (cdir / "captures.jsonl").read_text().splitlines()]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "segment"
    assert rec["sid"] == "cafebabe"
    assert rec["seq"] == 1
    assert rec["transcript"] == "hello there"
    assert rec["confidence"] == CONF
    assert rec["client_info"]["platform"] == "iPhone"
    assert rec["dur_s"] == 0.2
    assert sess.captured == {1}          # now labelable

    # the audio is a real 16 kHz mono PCM16 WAV holding the exact samples
    with wave.open(str(cdir / rec["wav"]), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 3200
        assert wf.readframes(3200) == const_pcm(8000, 3200)


def test_label_record_appends_and_never_rewrites(tmp_path, monkeypatch):
    cdir = tmp_path / "captures"
    monkeypatch.setattr(server, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(server, "CAPTURES_DIR", cdir)
    monkeypatch.setattr(server, "CAPTURES_LOG", cdir / "captures.jsonl")
    sess = _session()
    _run_capture(sess, const_pcm(8000, 1600), **KW)
    before = (cdir / "captures.jsonl").read_text()

    server._append_capture_line(server.build_label_record(sess.sid, 1, "noise"))

    after = (cdir / "captures.jsonl").read_text()
    assert after.startswith(before)   # the segment line is untouched: append only
    segments, labels = capture_report.load(cdir / "captures.jsonl")
    assert len(segments) == 1
    assert capture_report.label_of(segments[0], labels) == "noise"


def test_a_disk_failure_does_not_raise(tmp_path, monkeypatch):
    """Capture must never cost the user a turn: a write error is a warning, not an
    exception on the voice path."""
    monkeypatch.setattr(server, "CAPTURE_ENABLED", True)
    # a FILE where the captures dir should be: every mkdir/write under it fails
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setattr(server, "CAPTURES_DIR", blocker)
    monkeypatch.setattr(server, "CAPTURES_LOG", blocker / "captures.jsonl")

    _run_capture(_session(), const_pcm(1000, 1600), **KW)   # must not raise
