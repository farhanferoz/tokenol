"""Efficiency threshold defaults and colour helpers for Phase 5 metrics.

`DEFAULTS` holds the factory values. A future settings UI will supply overrides
at runtime; call-sites receive a `thresholds: dict` argument populated from
prefs (falling back to DEFAULTS) rather than importing constants directly.
"""

from __future__ import annotations

DEFAULTS: dict[str, float] = {
    # Hit% — cache_read / (cache_read + cache_creation + input), as percentage
    "hit_rate_good_pct": 95.0,
    "hit_rate_red_pct":  85.0,
    # $/kW — cost per 1,000 output tokens
    "cost_per_kw_good":  0.20,
    "cost_per_kw_red":   0.40,
    # Ctx N:1 — cache_read / output; high = blowing up
    "ctx_ratio_red":     400.0,
    # Cache reuse N:1 — cache_read / cache_creation; low = thrashing
    "cache_reuse_good":  50.0,
    "cache_reuse_red":   20.0,
    # Pattern detectors
    "idle_expiry_gap_seconds":        3600,   # 1 hour
    "idle_expiry_creation_ratio":      0.8,   # cache_creation / input tokens
    "compaction_drop_ratio":           0.8,   # visible-token drop vs prev peak
    "compaction_reinflate_ratio":      0.8,   # return to 80% of prev peak
    "compaction_red_cycles":           3,
    "context_plateau_fraction":        0.9,   # ≥90% of context window
    "context_plateau_min_turns":       20,
    "sidechain_explosion_cost_share":  0.4,
    "tool_error_storm_window":         10,
    "tool_error_storm_rate":           0.2,
}


def colour_for_hit_pct(pct: float, thresholds: dict = DEFAULTS) -> str:
    """Return 'good' | 'amber' | 'red' | 'mute' for a hit-rate percentage."""
    if pct >= thresholds["hit_rate_good_pct"]:
        return "good"
    if pct >= thresholds["hit_rate_red_pct"]:
        return "amber"
    return "red"


def colour_for_cost_per_kw(value: float, thresholds: dict = DEFAULTS) -> str:
    """Return 'good' | 'amber' | 'red' for $/kW."""
    if value <= thresholds["cost_per_kw_good"]:
        return "good"
    if value <= thresholds["cost_per_kw_red"]:
        return "amber"
    return "red"


def colour_for_ctx_ratio(value: float, thresholds: dict = DEFAULTS) -> str:
    """Return 'good' | 'amber' | 'red' for Ctx N:1."""
    if value >= thresholds["ctx_ratio_red"]:
        return "red"
    if value >= thresholds["ctx_ratio_red"] * 0.5:
        return "amber"
    return "good"


def colour_for_cache_reuse(value: float, thresholds: dict = DEFAULTS) -> str:
    """Return 'good' | 'amber' | 'red' for cache reuse N:1."""
    if value >= thresholds["cache_reuse_good"]:
        return "good"
    if value >= thresholds["cache_reuse_red"]:
        return "amber"
    return "red"


# ---------------------------------------------------------------------------
# Legacy constants — still consumed by rollups.py flagging logic (Phase 4).
# These will be removed when T3 rewires the payload to Phase 5 shape.
# ---------------------------------------------------------------------------

CONTEXT_GROWTH_AMBER = 2_000.0
CONTEXT_GROWTH_RED = 5_000.0

CACHE_HIT_RATE_AMBER = 0.50
CACHE_HIT_RATE_RED = 0.25

CACHE_CREATION_DOMINANCE_AMBER = 1.0
CACHE_CREATION_DOMINANCE_RED = 2.0

TOOL_ERROR_RATE_AMBER = 0.10
TOOL_ERROR_RATE_RED = 0.25
