"""Unit tests for the ASR confidence helper `_score_stats`.

These build tiny fake generate() tensors so they need no model. torch and
fastapi are imported via pytest.importorskip so the file SKIPS cleanly on a
machine whose venv lacks them (e.g. the web dev venv) instead of failing.
"""
import math
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("fastapi")  # asr.server imports fastapi at module top

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asr.server import _score_stats  # noqa: E402

VOCAB = 5


def _onehot(idx: int, hi: float = 10.0):
    v = torch.zeros(VOCAB)
    v[idx] = hi
    return v


def test_confident_beats_uniform_and_stops_at_eos():
    # item 0: highly confident tokens 1,2,3. item 1: uniform logits, tokens
    # 1,2 then eos(4) at step 2 (which must stop counting).
    sequences = torch.tensor([[1, 2, 3], [1, 2, 4]])
    scores = (
        torch.stack([_onehot(1), torch.zeros(VOCAB)]),
        torch.stack([_onehot(2), torch.zeros(VOCAB)]),
        torch.stack([_onehot(3), torch.zeros(VOCAB)]),
    )
    stats = _score_stats(sequences, scores, eos_ids={4}, pad_id=None)

    assert len(stats) == 2
    conf, unif = stats

    # Confident item counts all 3 tokens; uniform item stops before eos at 2.
    assert conf["tokens"] == 3
    assert unif["tokens"] == 2

    # Uniform logits over a vocab of 5 give prob 1/5 per token.
    assert unif["prob_mean"] == 0.2
    assert unif["logprob_mean"] == round(math.log(1 / VOCAB), 4)

    # Confidence ordering: the sharp distribution scores higher.
    assert conf["prob_mean"] > unif["prob_mean"]
    assert conf["prob_min"] > unif["prob_min"]
    assert conf["prob_mean"] > 0.99


def test_pad_tokens_are_skipped():
    # step 1 emits the pad token (0), which must not be counted.
    sequences = torch.tensor([[1, 0, 2]])
    scores = (
        torch.stack([_onehot(1)]),
        torch.stack([_onehot(0)]),
        torch.stack([_onehot(2)]),
    )
    stats = _score_stats(sequences, scores, eos_ids=set(), pad_id=0)
    assert len(stats) == 1
    assert stats[0]["tokens"] == 2


def test_all_eos_yields_none():
    # First token is eos -> nothing scored -> None for that item.
    sequences = torch.tensor([[4, 1, 2]])
    scores = (
        torch.stack([_onehot(4)]),
        torch.stack([_onehot(1)]),
        torch.stack([_onehot(2)]),
    )
    stats = _score_stats(sequences, scores, eos_ids={4}, pad_id=None)
    assert stats == [None]


def test_prob_is_exp_of_logprob():
    sequences = torch.tensor([[1, 2]])
    scores = (
        torch.stack([_onehot(1, hi=3.0)]),
        torch.stack([_onehot(2, hi=1.0)]),
    )
    stats = _score_stats(sequences, scores, eos_ids=set(), pad_id=None)
    item = stats[0]
    assert item["prob_mean"] == round(math.exp(item["logprob_mean"]), 4)
    assert item["prob_min"] == round(math.exp(item["logprob_min"]), 4)
