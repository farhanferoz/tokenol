"""Context metric unit tests with hand-computed expected values."""

from __future__ import annotations

from pathlib import Path

from tokenol.ingest.builder import build_turns
from tokenol.metrics.context import (
    cache_reuse_n_to_1,
    cache_reuse_ratio,
    context_growth_rate,
    context_tokens,
    cost_per_kw,
    ctx_ratio_n_to_1,
    ctx_used_latest,
    max_turn_input,
    non_cached_input_ratio,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_context_tokens_basic():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    # Turn 1: input=1000, cache_read=500, cache_creation=100 -> 1600
    # Turn 2: input=2000, cache_read=1000, cache_creation=0 -> 3000
    ctxs = sorted(context_tokens(t) for t in turns)
    assert ctxs == [1600, 3000]


def test_max_turn_input_basic():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    assert max_turn_input(turns) == 3000


def test_context_growth_rate_basic():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    # Two turns: (0, 1600), (1, 3000)
    # mean_x=0.5, mean_y=2300
    # num = (0-0.5)*(1600-2300) + (1-0.5)*(3000-2300) = 350 + 350 = 700
    # den = (0-0.5)^2 + (1-0.5)^2 = 0.25 + 0.25 = 0.5
    # slope = 700 / 0.5 = 1400.0
    rate = context_growth_rate(turns)
    assert abs(rate - 1400.0) < 1e-9


def test_cache_reuse_ratio_basic():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    # reads=500+1000=1500, creates=100+0=100, denom=1600
    # ratio = 1500/1600 = 0.9375
    ratio = cache_reuse_ratio(turns)
    assert ratio is not None
    assert abs(ratio - 1500 / 1600) < 1e-9


def test_non_cached_input_ratio_basic():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    # raw=1000+2000=3000, total=1600+3000=4600
    ratio = non_cached_input_ratio(turns)
    assert ratio is not None
    assert abs(ratio - 3000 / 4600) < 1e-9


def test_context_growth_rate_single_turn():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    assert context_growth_rate([turns[0]]) == 0.0


def test_max_turn_input_empty():
    assert max_turn_input([]) == 0


def test_cache_reuse_ratio_none_when_no_cache():
    # Test None branch with empty list
    assert cache_reuse_ratio([]) is None


def test_non_cached_input_ratio_none_when_empty():
    assert non_cached_input_ratio([]) is None


# ---- ctx_ratio_n_to_1 --------------------------------------------------

def test_ctx_ratio_none_when_output_zero():
    assert ctx_ratio_n_to_1(1000, 0) is None


def test_ctx_ratio_basic():
    assert ctx_ratio_n_to_1(1000, 100) == 10.0


def test_ctx_ratio_zero_reads():
    assert ctx_ratio_n_to_1(0, 100) == 0.0


# ---- cache_reuse_n_to_1 ------------------------------------------------

def test_cache_reuse_none_when_creation_zero():
    assert cache_reuse_n_to_1(0, 0) is None
    assert cache_reuse_n_to_1(500, 0) is None


def test_cache_reuse_basic():
    assert cache_reuse_n_to_1(500, 100) == 5.0


def test_cache_reuse_zero_reads():
    assert cache_reuse_n_to_1(0, 100) == 0.0


# ---- cost_per_kw -------------------------------------------------------

def test_cost_per_kw_none_when_output_zero():
    assert cost_per_kw(0.0, 0) is None
    assert cost_per_kw(1.0, 0) is None


def test_cost_per_kw_basic():
    assert cost_per_kw(0.5, 1000) == 0.5


def test_cost_per_kw_zero_cost():
    assert cost_per_kw(0.0, 1000) == 0.0


# ---- ctx_used_latest ---------------------------------------------------

def test_ctx_used_latest_none_when_window_unknown():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    assert ctx_used_latest(turns[0], None) is None


def test_ctx_used_latest_fraction():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    # Turn with input=1000, cache_read=500, cache_creation=100 → visible=1600
    turn = min(turns, key=lambda t: t.usage.input_tokens)
    result = ctx_used_latest(turn, 200_000)
    assert result is not None
    assert 0.0 < result < 1.0
