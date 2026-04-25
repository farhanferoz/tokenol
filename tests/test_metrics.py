"""Cost metric unit tests with hand-computed expected values."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokenol.ingest.builder import build_turns
from tokenol.metrics.cost import cache_saved_usd, cost_for_turn, rollup_by_date
from tokenol.metrics.rollups import build_session_rollup, build_tool_mix
from tokenol.model.events import Session, Turn, Usage

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


# ---- cache_saved_usd --------------------------------------------------------


def _turn_with_cache_read(model: str | None, cache_read: int) -> Turn:
    return Turn(
        dedup_key=f"t-{cache_read}",
        timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        session_id="s",
        model=model,
        usage=Usage(
            input_tokens=0, output_tokens=0,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=0,
        ),
        is_sidechain=False,
        stop_reason="end_turn",
    )


def test_cache_saved_usd_zero_reads_returns_zero():
    turns = [_turn_with_cache_read("claude-sonnet-4-6", 0)]
    assert cache_saved_usd(turns) == 0.0


def test_cache_saved_usd_known_model_sonnet_four_six():
    # Sonnet 4.6 pricing: input = $3.00/M, cache_read = $0.30/M.
    # 1_000_000 cache read tokens → (1.00M × $3.00) − (1.00M × $0.30) = $2.70 saved.
    turns = [_turn_with_cache_read("claude-sonnet-4-6", 1_000_000)]
    assert cache_saved_usd(turns) == pytest.approx(2.70, rel=1e-6)


def test_cache_saved_usd_unknown_model_contributes_zero():
    # Completely unrecognised model: registry.resolve returns (None, tags).
    # Those turns contribute 0, others still counted.
    known = _turn_with_cache_read("claude-sonnet-4-6", 1_000_000)
    unknown = _turn_with_cache_read("gpt-4", 1_000_000)
    assert cache_saved_usd([known, unknown]) == pytest.approx(2.70, rel=1e-6)


def test_cache_saved_usd_none_model_contributes_zero():
    turns = [_turn_with_cache_read(None, 500_000)]
    assert cache_saved_usd(turns) == 0.0


def test_cache_saved_usd_sums_across_turns_and_models():
    # Opus 4.7: input $5.00/M, cache_read $0.50/M. 200k reads → (0.2 × 5) − (0.2 × 0.5) = $0.90
    # Sonnet 4.6: 500k reads → (0.5 × 3) − (0.5 × 0.3) = $1.35
    # Total: $2.25
    turns = [
        _turn_with_cache_read("claude-opus-4-7",   200_000),
        _turn_with_cache_read("claude-sonnet-4-6", 500_000),
    ]
    assert cache_saved_usd(turns) == pytest.approx(2.25, rel=1e-6)


# ---- tool_mix / build_tool_mix ----------------------------------------------


def _turn_with_tools(tool_names: dict[str, int]) -> Turn:
    return Turn(
        dedup_key="k",
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        session_id="s1",
        model="claude-opus-4-7",
        usage=Usage(input_tokens=1, output_tokens=1),
        is_sidechain=False,
        stop_reason="tool_use",
        tool_use_count=sum(tool_names.values()),
        tool_names=Counter(tool_names),
    )


def test_session_rollup_sums_tool_mix():
    s = Session(
        session_id="s1", source_file="x.jsonl", is_sidechain=False, cwd="/p",
        turns=[
            _turn_with_tools({"Read": 2, "Edit": 1}),
            _turn_with_tools({"Read": 1, "Bash": 3}),
        ],
    )
    sr = build_session_rollup(s)
    assert sr.tool_mix == Counter({"Bash": 3, "Read": 3, "Edit": 1})


def test_build_tool_mix_ranks_descending():
    srs = [
        Session(session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
                turns=[_turn_with_tools({"Read": 5, "Edit": 2})]),
        Session(session_id="b", source_file="b.jsonl", is_sidechain=False, cwd="/p",
                turns=[_turn_with_tools({"Edit": 3, "Bash": 1})]),
    ]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=10)

    assert result == [
        {"tool": "Read", "count": 5},
        {"tool": "Edit", "count": 5},
        {"tool": "Bash", "count": 1},
    ]


def test_build_tool_mix_collapses_tail_to_others():
    srs = [Session(
        session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
        turns=[_turn_with_tools({
            "Read": 10, "Edit": 8, "Bash": 6, "Grep": 4, "Glob": 3,
            "Write": 2, "Task": 1,
        })],
    )]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=3)

    assert result[0] == {"tool": "Read", "count": 10}
    assert result[1] == {"tool": "Edit", "count": 8}
    assert result[2] == {"tool": "Bash", "count": 6}
    assert result[3] == {"tool": "others", "count": 4 + 3 + 2 + 1}
    assert len(result) == 4


def test_build_tool_mix_no_others_when_under_top_n():
    srs = [Session(
        session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
        turns=[_turn_with_tools({"Read": 2, "Edit": 1})],
    )]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=10)

    assert result == [{"tool": "Read", "count": 2}, {"tool": "Edit", "count": 1}]
    assert all(row["tool"] != "others" for row in result)


def test_build_tool_mix_empty():
    assert build_tool_mix([], top_n=10) == []
