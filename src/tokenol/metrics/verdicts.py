"""Blow-up verdict assignment for sessions."""

from __future__ import annotations

from tokenol.enums import BlowUpVerdict
from tokenol.metrics.rollups import SessionRollup

# Thresholds (v0.1 — hardcoded; configurable in a future release)
_RUNAWAY_WINDOW_THRESHOLD_USD = 50.0
_CONTEXT_CREEP_MAX_INPUT_TOKENS = 500_000
_CONTEXT_CREEP_GROWTH_RATE = 2000.0  # tokens/turn
_SIDECHAIN_HEAVY_COST_USD = 5.0
_TOOL_ERROR_STORM_MIN_USES = 10
_TOOL_ERROR_STORM_ERROR_RATIO = 0.3


def compute_verdict(sr: SessionRollup) -> BlowUpVerdict:
    """Return the first matching blow-up verdict (evaluation order is fixed).

    Order: RUNAWAY_WINDOW → CONTEXT_CREEP → TOOL_ERROR_STORM → SIDECHAIN_HEAVY → OK
    """
    if sr.peak_window_cost > _RUNAWAY_WINDOW_THRESHOLD_USD:
        return BlowUpVerdict.RUNAWAY_WINDOW

    if (
        sr.max_turn_input > _CONTEXT_CREEP_MAX_INPUT_TOKENS
        and sr.context_growth_rate_val > _CONTEXT_CREEP_GROWTH_RATE
    ):
        return BlowUpVerdict.CONTEXT_CREEP

    if sr.tool_use_count >= _TOOL_ERROR_STORM_MIN_USES and sr.tool_error_count / sr.tool_use_count > _TOOL_ERROR_STORM_ERROR_RATIO:
        return BlowUpVerdict.TOOL_ERROR_STORM

    if sr.is_sidechain and sr.cost_usd > _SIDECHAIN_HEAVY_COST_USD:
        return BlowUpVerdict.SIDECHAIN_HEAVY

    return BlowUpVerdict.OK
