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
_COST_EPS = 1e-9


def test_cost_opus_47():
    usage = Usage(input_tokens=1000, output_tokens=200,
                  cache_read_input_tokens=500, cache_creation_input_tokens=100)
    tc = cost_for_turn("claude-opus-4-7", usage)
    # input: 1000 * 5.00 / 1M = 0.005
    # output: 200 * 25.00 / 1M = 0.005
    # cache_read: 500 * 0.50 / 1M = 0.00025
    # cache_write: 100 * 6.25 / 1M = 0.000625
    expected = (1000 * 5.00 + 200 * 25.00 + 500 * 0.50 + 100 * 6.25) / _M
    assert abs(tc.total_usd - expected) < _COST_EPS
    assert tc.assumptions == []


def test_cost_haiku_45():
    usage = Usage(input_tokens=10_000, output_tokens=500, cache_read_input_tokens=0,
                  cache_creation_input_tokens=0)
    tc = cost_for_turn("claude-haiku-4-5-20251001", usage)
    expected = (10_000 * 1.00 + 500 * 5.00) / _M
    assert abs(tc.total_usd - expected) < _COST_EPS


def test_cost_fable_5():
    usage = Usage(input_tokens=1000, output_tokens=200,
                  cache_read_input_tokens=500, cache_creation_input_tokens=100)
    tc = cost_for_turn("claude-fable-5", usage)
    # input: 1000 * 10.00 / 1M = 0.010
    # output: 200 * 50.00 / 1M = 0.010
    # cache_read: 500 * 1.00 / 1M = 0.0005
    # cache_write: 100 * 12.50 / 1M = 0.00125
    expected = (1000 * 10.00 + 200 * 50.00 + 500 * 1.00 + 100 * 12.50) / _M
    assert abs(tc.total_usd - expected) < _COST_EPS
    assert tc.assumptions == []


def test_unknown_fable_variant_falls_back_to_fable_family():
    # A future dated/suffixed Fable id must price off the Fable entry, not Opus.
    from tokenol.enums import AssumptionTag
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    entry, tags = resolve("claude-fable-5-20260601")
    assert entry == CLAUDE_MODELS["claude-fable-5"]
    assert AssumptionTag.UNKNOWN_MODEL_FALLBACK in tags


def test_fable_with_context_suffix_resolves_clean():
    # Claude Code appends "[1m]" for the 1M-context variant. It must price as
    # the base model with no fallback assumption.
    from tokenol.enums import AssumptionTag
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    entry, tags = resolve("claude-fable-5[1m]")
    assert entry == CLAUDE_MODELS["claude-fable-5"]
    assert tags == []
    assert AssumptionTag.UNKNOWN_MODEL_FALLBACK not in tags


def test_context_suffix_stripped_for_known_model():
    # The "[1m]" strip is model-agnostic, not Fable-specific.
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    entry, tags = resolve("claude-sonnet-4-6[1m]")
    assert entry == CLAUDE_MODELS["claude-sonnet-4-6"]
    assert tags == []


def test_opus_48_priced_and_suffix_clean():
    # Opus 4.8 is in the table; "[1m]" turns resolve to it cleanly.
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    entry, tags = resolve("claude-opus-4-8[1m]")
    assert entry == CLAUDE_MODELS["claude-opus-4-8"]
    assert tags == []


def test_cost_sonnet_5():
    # Sonnet 5 introductory pricing (through 2026-08-31) must not fall back
    # to Sonnet 4.6's rate, which is 1.5x more expensive.
    usage = Usage(input_tokens=1000, output_tokens=200,
                  cache_read_input_tokens=500, cache_creation_input_tokens=100)
    tc = cost_for_turn("claude-sonnet-5", usage)
    expected = (1000 * 2.00 + 200 * 10.00 + 500 * 0.20 + 100 * 2.50) / _M
    assert abs(tc.total_usd - expected) < _COST_EPS
    assert tc.assumptions == []


def test_sonnet_5_priced_and_suffix_clean():
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    entry, tags = resolve("claude-sonnet-5[1m]")
    assert entry == CLAUDE_MODELS["claude-sonnet-5"]
    assert tags == []


def test_cache_write_all_1h_tier():
    """Regression for the confirmed bug: cache-creation tokens entirely on the
    1-hour tier must price at 2x input, not the 5-minute tier's 1.25x."""
    usage = Usage(input_tokens=6, output_tokens=6, cache_read_input_tokens=16153,
                  cache_creation_input_tokens=17618, cache_creation_1h_input_tokens=17618)
    tc = cost_for_turn("claude-opus-4-7", usage)
    expected = (6 * 5.00 + 6 * 25.00 + 16153 * 0.50 + 17618 * 10.00) / _M
    assert abs(tc.total_usd - expected) < _COST_EPS
    # The old (wrong) 5-minute-only formula would have given a lower total —
    # assert against it explicitly so a regression back to that formula fails.
    old_wrong_total = (6 * 5.00 + 6 * 25.00 + 16153 * 0.50 + 17618 * 6.25) / _M
    assert tc.total_usd > old_wrong_total


def test_cache_write_split_5m_and_1h():
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                  cache_creation_input_tokens=1000, cache_creation_1h_input_tokens=300)
    tc = cost_for_turn("claude-opus-4-7", usage)
    # 700 tokens @ 5m rate (6.25) + 300 tokens @ 1h rate (10.00)
    expected = (700 * 6.25 + 300 * 10.00) / _M
    assert abs(tc.cache_creation_usd - expected) < _COST_EPS


def test_cache_write_1h_clamped_to_total():
    """Defensive guard: malformed external log data claiming more 1h tokens
    than the total cache-creation count must not go negative on the 5m side."""
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                  cache_creation_input_tokens=100, cache_creation_1h_input_tokens=500)
    tc = cost_for_turn("claude-opus-4-7", usage)
    # Clamped: all 100 tokens treated as 1h tier, none go negative.
    expected = 100 * 10.00 / _M
    assert abs(tc.cache_creation_usd - expected) < _COST_EPS


def test_sonnet_45_resolves_to_own_entry_not_sonnet_5_intro_rate():
    """claude-sonnet-4-5 is a distinct, currently-active model priced at
    standard $3/$15 — it must not silently fall back to Sonnet 5's cheaper
    $2/$10 introductory rate via family fallback."""
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    for model_id in ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"):
        entry, tags = resolve(model_id)
        assert entry == CLAUDE_MODELS[model_id]
        assert entry["input"] == 3.00
        assert tags == []


def test_opus_45_resolves_to_own_entry():
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    for model_id in ("claude-opus-4-5", "claude-opus-4-5-20251101"):
        entry, tags = resolve(model_id)
        assert entry == CLAUDE_MODELS[model_id]
        assert tags == []


def test_opus_41_resolves_to_own_entry_not_opus_48_rate():
    """Opus 4.1 is 3x Opus 4.8's price ($15/$75 vs $5/$25) — misrouting it
    through family fallback would badly underprice it."""
    from tokenol.model.pricing import CLAUDE_MODELS
    from tokenol.model.registry import resolve

    for model_id in ("claude-opus-4-1", "claude-opus-4-1-20250805"):
        entry, tags = resolve(model_id)
        assert entry == CLAUDE_MODELS[model_id]
        assert entry["input"] == 15.00
        assert tags == []


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


def test_daily_rollup_since_filters_older_turns():
    # Regression: rollup_by_date used to zero-fill the [since, until] window but
    # never dropped turns dated before `since`, so Daily History rendered the
    # full series regardless of the selected 7D/30D/90D range.
    from datetime import date
    def _turn(d: date) -> Turn:
        return Turn(
            dedup_key=f"k-{d}", timestamp=datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc),
            session_id="s", model="claude-opus-4-7",
            usage=Usage(input_tokens=1, output_tokens=1,
                cache_read_input_tokens=0, cache_creation_input_tokens=0),
            is_sidechain=False, stop_reason=None,
        )

    turns = [_turn(date(2026, 1, 1)), _turn(date(2026, 4, 10)), _turn(date(2026, 4, 14))]
    rollups = rollup_by_date(turns, since=date(2026, 4, 8), until=date(2026, 4, 14))
    dates = [r.date for r in rollups]
    assert dates[0] == date(2026, 4, 8) and dates[-1] == date(2026, 4, 14)
    assert all(date(2026, 4, 8) <= d <= date(2026, 4, 14) for d in dates)
    populated = {r.date: r.turns for r in rollups if r.turns}
    assert populated == {date(2026, 4, 10): 1, date(2026, 4, 14): 1}


def test_sidechain_cost():
    turns = build_turns([FIXTURES / "sidechain.jsonl"])
    assert len(turns) == 1
    t = turns[0]
    assert t.is_sidechain is True
    # haiku: input 300 * 1.00 + output 50 * 5.00 + cache_read 200 * 0.10 / 1M
    expected = (300 * 1.00 + 50 * 5.00 + 200 * 0.10) / _M
    assert abs(t.cost_usd - expected) < _COST_EPS


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
