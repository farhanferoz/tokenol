"""Blow-up verdict tests — one per verdict branch plus OK."""

from __future__ import annotations

from datetime import datetime, timezone

from tokenol.enums import BlowUpVerdict
from tokenol.metrics.rollups import SessionRollup
from tokenol.metrics.verdicts import compute_verdict

_TS = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)


def _base_rollup(**kwargs) -> SessionRollup:
    defaults = dict(
        session_id="sess-test",
        source_file="test.jsonl",
        is_sidechain=False,
        cwd="/home/user/project",
        first_ts=_TS,
        last_ts=_TS,
        turns=5,
        input_tokens=10_000,
        output_tokens=2_000,
        cache_read_tokens=5_000,
        cache_creation_tokens=1_000,
        cost_usd=1.0,
        max_turn_input=100_000,
        cache_reuse_ratio=0.8,
        context_growth_rate_val=500.0,
        tool_use_count=2,
        tool_error_count=0,
        peak_window_cost=5.0,
        verdict=BlowUpVerdict.OK,
        model="claude-sonnet-4-6",
    )
    defaults.update(kwargs)
    return SessionRollup(**defaults)


def test_verdict_ok():
    sr = _base_rollup()
    assert compute_verdict(sr) == BlowUpVerdict.OK


def test_verdict_runaway_window():
    sr = _base_rollup(peak_window_cost=50.01)
    assert compute_verdict(sr) == BlowUpVerdict.RUNAWAY_WINDOW


def test_verdict_runaway_window_boundary():
    """Exactly $50 is NOT RUNAWAY_WINDOW (condition is >50)."""
    sr = _base_rollup(peak_window_cost=50.0)
    assert compute_verdict(sr) == BlowUpVerdict.OK


def test_verdict_context_creep():
    sr = _base_rollup(max_turn_input=500_001, context_growth_rate_val=2001.0)
    assert compute_verdict(sr) == BlowUpVerdict.CONTEXT_CREEP


def test_verdict_context_creep_only_high_input():
    """High max_turn_input alone is not CONTEXT_CREEP without high growth."""
    sr = _base_rollup(max_turn_input=600_000, context_growth_rate_val=1000.0)
    assert compute_verdict(sr) == BlowUpVerdict.OK


def test_verdict_tool_error_storm():
    sr = _base_rollup(tool_use_count=10, tool_error_count=4)  # 40% error rate
    assert compute_verdict(sr) == BlowUpVerdict.TOOL_ERROR_STORM


def test_verdict_tool_error_storm_boundary():
    """Exactly 30% error rate is NOT TOOL_ERROR_STORM (condition is >0.3)."""
    sr = _base_rollup(tool_use_count=10, tool_error_count=3)  # exactly 30%
    assert compute_verdict(sr) == BlowUpVerdict.OK


def test_verdict_sidechain_heavy():
    sr = _base_rollup(is_sidechain=True, cost_usd=5.01)
    assert compute_verdict(sr) == BlowUpVerdict.SIDECHAIN_HEAVY


def test_verdict_sidechain_cheap_is_ok():
    sr = _base_rollup(is_sidechain=True, cost_usd=4.99)
    assert compute_verdict(sr) == BlowUpVerdict.OK


def test_verdict_order_runaway_beats_context_creep():
    """RUNAWAY_WINDOW should win over CONTEXT_CREEP when both match."""
    sr = _base_rollup(
        peak_window_cost=100.0,
        max_turn_input=600_000,
        context_growth_rate_val=3000.0,
    )
    assert compute_verdict(sr) == BlowUpVerdict.RUNAWAY_WINDOW
