#!/usr/bin/env python3
"""Summarize the segment-capture calibration set (data/captures/captures.jsonl).

The capture mode (LA_CAPTURE=1, see web/server.py) saves every uploaded speech
segment as a WAV plus one JSONL record, and lets a tester label a transcript as
noise / other_speaker / speech. This reads that file back and answers THE question
the capture exists for:

    can an ASR-confidence threshold separate noise from real speech?

So the confidence distribution per label is the centerpiece: if the noise clips'
prob_mean / prob_min sit clearly below the speech clips', a server-side gate can
drop them, and this report says where to put the cut. dur_s and rms are printed
alongside because loudness and length are the other two cheap axes a gate could
use (and a fallback if confidence turns out not to separate).

captures.jsonl is APPEND-ONLY: a label is not an in-place edit but a new
{kind: "label", sid, seq, label} record, so labels are FOLDED here, last-write-wins
per (sid, seq).

Usage:
  python3 scripts/capture_report.py                  # summary to stdout
  python3 scripts/capture_report.py --csv clips.csv  # one row per clip
  python3 scripts/capture_report.py --csv -          # ...to stdout
  python3 scripts/capture_report.py --file <path>    # a captures.jsonl elsewhere
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
# Same default (and same env override) as web/server.py, so the report reads
# exactly what the server wrote without being told where it is.
CAPTURES_DIR = Path(os.environ.get("LA_CAPTURE_DIR", str(APP / "data" / "captures")))

UNLABELED = "unlabeled"
LABEL_ORDER = ["noise", "other_speaker", "speech", UNLABELED]
# The stats block per label. confidence.* comes first: it is the thing under test.
METRICS = [
    ("prob_mean", lambda r: (r.get("confidence") or {}).get("prob_mean")),
    ("prob_min", lambda r: (r.get("confidence") or {}).get("prob_min")),
    ("dur_s", lambda r: r.get("dur_s")),
    ("rms", lambda r: r.get("rms")),
]


def load(path: Path):
    """Read captures.jsonl into (segments, labels): the segment records in file
    order, and the folded {(sid, seq): label} map (LAST label per key wins).
    Tolerates a truncated final line (the server appends live) and a record with
    no `kind` (read as a segment)."""
    segments = []
    labels = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue   # a half-written tail line: the writer is appending live
            if rec.get("kind") == "label":
                labels[(rec.get("sid"), rec.get("seq"))] = rec.get("label")
            else:
                segments.append(rec)
    return segments, labels


def label_of(rec, labels) -> str:
    """The folded label for one segment record: a kind="label" record beats the
    segment's own (always null) `label` field; nothing at all means unlabeled."""
    return labels.get((rec.get("sid"), rec.get("seq"))) or rec.get("label") or UNLABELED


def stats(values):
    """min / mean / p50 / p90 / max over the non-null values, or None if there are
    none. Percentiles are nearest-rank: no interpolation, no numpy dependency (the
    orchestrator venv has none, and this must run next to it)."""
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    n = len(vals)

    def pct(p):
        k = max(0, min(n - 1, int(round(p * (n - 1)))))
        return vals[k]

    return {"n": n, "min": vals[0], "mean": sum(vals) / n, "p50": pct(0.50),
            "p90": pct(0.90), "max": vals[-1]}


def _fmt(v):
    return "-" if v is None else f"{v:.4f}"


def report(segments, labels, out=sys.stdout):
    groups = {}
    for rec in segments:
        groups.setdefault(label_of(rec, labels), []).append(rec)

    print(f"clips: {len(segments)}", file=out)
    counts = {lab: len(groups.get(lab, [])) for lab in LABEL_ORDER}
    for lab in LABEL_ORDER:
        print(f"  {lab:<14} {counts[lab]}", file=out)
    for lab in sorted(k for k in groups if k not in LABEL_ORDER):   # a label we don't know
        print(f"  {lab:<14} {len(groups[lab])}", file=out)
    labeled = sum(counts[lab] for lab in LABEL_ORDER if lab != UNLABELED)
    print(f"  labeled: {labeled} / {len(segments)}", file=out)

    # How often the incoming noise gate (LA_CONF_FLOOR) fired, by reason. A rejected
    # segment carries a non-null reject_reason; this shows whether the gate is doing
    # anything and how the two reasons split, so its aggressiveness can be tuned.
    rejected = {}
    for rec in segments:
        rr = rec.get("reject_reason")
        if rr:
            rejected[rr] = rejected.get(rr, 0) + 1
    total_rej = sum(rejected.values())
    detail = ", ".join(f"{r}: {n}" for r, n in sorted(rejected.items())) if rejected else "none"
    print(f"  gate-rejected: {total_rej} / {len(segments)} ({detail})", file=out)
    if not segments:
        return

    print("\nASR confidence by label (can a threshold separate noise from speech?)", file=out)
    print(f"{'label':<14}{'metric':<11}{'n':>5}{'min':>10}{'mean':>10}"
          f"{'p50':>10}{'p90':>10}{'max':>10}", file=out)
    for lab in LABEL_ORDER + sorted(k for k in groups if k not in LABEL_ORDER):
        recs = groups.get(lab)
        if not recs:
            continue
        for i, (name, get) in enumerate(METRICS):
            s = stats(get(r) for r in recs)
            head = lab if i == 0 else ""
            if s is None:
                print(f"{head:<14}{name:<11}{0:>5}{'-':>10}{'-':>10}{'-':>10}{'-':>10}{'-':>10}",
                      file=out)
                continue
            print(f"{head:<14}{name:<11}{s['n']:>5}{_fmt(s['min']):>10}{_fmt(s['mean']):>10}"
                  f"{_fmt(s['p50']):>10}{_fmt(s['p90']):>10}{_fmt(s['max']):>10}", file=out)
        print("", file=out)


CSV_FIELDS = ["wav", "label", "prob_mean", "prob_min", "dur_s", "rms", "peak",
              "accepted", "reject_reason", "addressed", "transcript", "error",
              "sid", "seq", "scope", "ts",
              "ua", "platform", "hw_sample_rate", "resampled", "vad_threshold",
              "seg_pause_ms", "turn_pause_ms", "viewport"]


def csv_rows(segments, labels, root: Path):
    for rec in segments:
        conf = rec.get("confidence") or {}
        ci = rec.get("client_info") or {}
        addressed = rec.get("addressed")
        wav = rec.get("wav")
        yield {
            "wav": str(root / wav) if wav else "",
            "label": label_of(rec, labels),
            "prob_mean": conf.get("prob_mean"),
            "prob_min": conf.get("prob_min"),
            "dur_s": rec.get("dur_s"),
            "rms": rec.get("rms"),
            "peak": rec.get("peak"),
            "accepted": rec.get("accepted"),
            "reject_reason": rec.get("reject_reason"),
            # the classifier verdict block, or None when LA_ADDRESSED was off
            "addressed": addressed.get("addressed") if isinstance(addressed, dict) else None,
            "transcript": rec.get("transcript"),
            "error": rec.get("error"),
            "sid": rec.get("sid"),
            "seq": rec.get("seq"),
            "scope": rec.get("scope"),
            "ts": rec.get("ts"),
            **{k: ci.get(k) for k in ("ua", "platform", "hw_sample_rate", "resampled",
                                      "vad_threshold", "seg_pause_ms", "turn_pause_ms",
                                      "viewport")},
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", default=None,
                    help="captures.jsonl to read (default: $LA_CAPTURE_DIR/captures.jsonl)")
    ap.add_argument("--csv", default=None,
                    help="also write one row per clip to this path ('-' for stdout)")
    args = ap.parse_args()

    path = Path(args.file) if args.file else CAPTURES_DIR / "captures.jsonl"
    if not path.is_file():
        print(f"no capture log at {path}\n"
              f"(run the server with LA_CAPTURE=1 and speak a segment)", file=sys.stderr)
        return 0
    segments, labels = load(path)
    # `--csv -` streams the CSV on stdout, so the human-readable summary goes to
    # stderr: `capture_report.py --csv - > clips.csv` must yield a clean CSV file.
    out = sys.stderr if args.csv == "-" else sys.stdout
    print(f"source: {path}", file=out)
    if not segments:
        print("clips: 0 (the log has no segment records yet)", file=out)
        return 0
    report(segments, labels, out=out)

    if args.csv:
        root = path.parent   # wav paths are stored relative to the captures dir
        if args.csv == "-":
            w = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(csv_rows(segments, labels, root))
        else:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                w.writerows(csv_rows(segments, labels, root))
            print(f"\ncsv: {args.csv} ({len(segments)} rows)", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
