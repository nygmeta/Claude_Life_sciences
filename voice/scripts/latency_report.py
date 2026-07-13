#!/usr/bin/env python3
"""Summarize the latency log written by web/server.py.

Reads the JSONL latency log (one record per assistant turn or playground synth,
each with per-component timings) and prints count + min/mean/p50/p90/p95/max for
every stage, so you can see where total latency goes.

Usage:
    python3 scripts/latency_report.py [path-to-latency.jsonl]

Default path: ../data/latency.jsonl relative to this script (the server default).
Stdlib only, so it runs anywhere without the app's venv.
"""
import json
import sys
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent.parent / "data" / "latency.jsonl"


def pct(values, p):
    """Linear-interpolation percentile of a numeric list (p in 0..100)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(values):
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return {"n": len(xs), "min": min(xs), "mean": sum(xs) / len(xs),
            "p50": pct(xs, 50), "p90": pct(xs, 90), "p95": pct(xs, 95), "max": max(xs)}


def fmt_row(name, s):
    if not s:
        return f"  {name:<16} (no data)"
    return (f"  {name:<16} n={s['n']:<4} "
            f"min={s['min']:>7.1f}  mean={s['mean']:>7.1f}  p50={s['p50']:>7.1f}  "
            f"p90={s['p90']:>7.1f}  p95={s['p95']:>7.1f}  max={s['max']:>7.1f}")


def dig(rec, *path):
    cur = rec
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def by_model_breakdown(records, label):
    """Per-TTS-model tts ms + rtf, so a multi-model demo can compare backends
    directly. Only shown when 2+ distinct TTS models appear in the log;
    records predating tts.model group under "unknown"."""
    groups = {}
    for r in records:
        model = dig(r, "tts", "model") or "unknown"
        groups.setdefault(model, []).append(r)
    if len(groups) < 2:
        return
    print(f"\n{label} tts by model (ms):")
    for model in sorted(groups):
        g = groups[model]
        print(fmt_row(model, stats([dig(r, "tts", "ms") for r in g])))
        rtf_s = stats([dig(r, "tts", "rtf") for r in g])
        if rtf_s:
            print(f"    rtf: mean={rtf_s['mean']:.2f}  p50={rtf_s['p50']:.2f}")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    if not path.is_file():
        print(f"no log file at {path}", file=sys.stderr)
        return 1

    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    asst = [r for r in recs if r.get("kind") == "assistant"]
    play = [r for r in recs if r.get("kind") == "playground"]
    asst_ok = [r for r in asst if r.get("status") == "ok"]
    play_ok = [r for r in play if r.get("status") == "ok"]

    print(f"latency report  {path}")
    print(f"records: {len(recs)}  assistant: {len(asst)} ({len(asst_ok)} ok)  "
          f"playground: {len(play)} ({len(play_ok)} ok)")

    if asst_ok:
        print("\nassistant turns (ms), full pipeline VAD-segment -> spoken reply:")
        print(fmt_row("asr.total", stats([dig(r, "asr", "total_ms") for r in asst_ok])))
        print(fmt_row("llm.ttft", stats([dig(r, "llm", "ttft_ms") for r in asst_ok])))
        print(fmt_row("llm.total", stats([dig(r, "llm", "ms") for r in asst_ok])))
        print(fmt_row("tts", stats([dig(r, "tts", "ms") for r in asst_ok])))
        # tts.first_ms and top-level first_audio_ms exist from the sentence-streaming
        # change onward; older records lack them, so `dig`/`.get` fall back to None
        # and stats() (which drops None) reports "(no data)" instead of raising.
        print(fmt_row("tts.first_ms", stats([dig(r, "tts", "first_ms") for r in asst_ok])))
        print(fmt_row("first_audio", stats([r.get("first_audio_ms") for r in asst_ok])))
        print(fmt_row("reply_latency", stats([r.get("reply_latency_ms") for r in asst_ok])))
        print(fmt_row("total", stats([r.get("total_ms") for r in asst_ok])))
        streamed = sum(1 for r in asst_ok if r.get("stream"))
        print(f"  (reply_latency = end_turn -> LAST reply_audio = llm + tts; "
              f"total = first speech -> LAST reply_audio; "
              f"first_audio = end_turn -> FIRST reply_audio, the perceived-latency "
              f"headline; {streamed}/{len(asst_ok)} turns had TTS_STREAM on)")
        by_model_breakdown(asst_ok, "assistant")

    if play_ok:
        print("\nplayground synths:")
        print(fmt_row("tts (ms)", stats([dig(r, "tts", "ms") for r in play_ok])))
        print(fmt_row("audio (s)", stats([dig(r, "tts", "audio_s") for r in play_ok])))
        print(fmt_row("rtf", stats([dig(r, "tts", "rtf") for r in play_ok])))
        by_model_breakdown(play_ok, "playground")

    errs = [r for r in recs if r.get("status") and r.get("status") != "ok"]
    if errs:
        by = {}
        for r in errs:
            by[r["status"]] = by.get(r["status"], 0) + 1
        print("\nnon-ok records:")
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
