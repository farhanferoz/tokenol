"""Cost metric unit tests with hand-computed expected values."""

from pathlib import Path

from tokenol.ingest.builder import build_turns
from tokenol.metrics.cost import cost_for_turn, rollup_by_date
from tokenol.model.events import Usage

FIXTURES = Path(__file__).parent / "fixtures"

_M = 1_000_000


def test_cost_opus_47():
    usage = Usage(input_tokens=1000, output_tokens=200,
                  cache_read_input_tokens=500, cache_creation_input_tokens=100)
    tc = cost_for_turn("claude-opus-4-7", usage)
    # input: 1000 * 5.00 / 1M = 0.005
    # output: 200 * 25.00 / 1M = 0.005
    # cache_read: 500 * 0.50 / 1M = 0.00025
    # cache_write: 100 * 6.25 / 1M = 0.000625
    expected = (1000 * 5.00 + 200 * 25.00 + 500 * 0.50 + 100 * 6.25) / _M
    assert abs(tc.total_usd - expected) < 1e-9
    assert tc.assumptions == []


def test_cost_haiku_45():
    usage = Usage(input_tokens=10_000, output_tokens=500, cache_read_input_tokens=0,
                  cache_creation_input_tokens=0)
    tc = cost_for_turn("claude-haiku-4-5-20251001", usage)
    expected = (10_000 * 1.00 + 500 * 5.00) / _M
    assert abs(tc.total_usd - expected) < 1e-9


def test_cost_gemini_unpriced():
    usage = Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                  cache_creation_input_tokens=0)
    tc = cost_for_turn("gemini-3-flash", usage)
    assert tc.total_usd == 0.0
    from tokenol.enums import AssumptionTag
    assert AssumptionTag.GEMINI_UNPRICED in tc.assumptions


def test_cost_unknown_claude_model():
    usage = Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                  cache_creation_input_tokens=0)
    tc = cost_for_turn("claude-opus-99-ultra", usage)
    # Should use opus family fallback — not zero
    assert tc.total_usd > 0
    from tokenol.enums import AssumptionTag
    assert AssumptionTag.UNKNOWN_MODEL_FALLBACK in tc.assumptions


def test_daily_rollup():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    rollups = rollup_by_date(turns)
    assert len(rollups) == 1
    r = rollups[0]
    from datetime import date
    assert r.date == date(2026, 4, 14)
    assert r.turns == 2
    assert r.output_tokens == 500   # 200 + 300


def test_sidechain_cost():
    turns = build_turns([FIXTURES / "sidechain.jsonl"])
    assert len(turns) == 1
    t = turns[0]
    assert t.is_sidechain is True
    # haiku: input 300 * 1.00 + output 50 * 5.00 + cache_read 200 * 0.10 / 1M
    expected = (300 * 1.00 + 50 * 5.00 + 200 * 0.10) / _M
    assert abs(t.cost_usd - expected) < 1e-9
