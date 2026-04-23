"""Context-axis metrics: size, growth, and cache efficiency per session."""

from __future__ import annotations

from tokenol.model.events import Turn


def context_tokens(turn: Turn) -> int:
    """Total tokens the model 'sees': input + cache_read + cache_creation."""
    u = turn.usage
    return u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens


def max_turn_input(turns: list[Turn]) -> int:
    """Largest context_tokens value across all turns in a session."""
    return max((context_tokens(t) for t in turns), default=0)


def cache_reuse_ratio(turns: list[Turn]) -> float | None:
    """cache_read / (cache_read + cache_creation). None if denominator is 0."""
    reads = sum(t.usage.cache_read_input_tokens for t in turns)
    creates = sum(t.usage.cache_creation_input_tokens for t in turns)
    denom = reads + creates
    return reads / denom if denom > 0 else None


def non_cached_input_ratio(turns: list[Turn]) -> float | None:
    """Fraction of total context tokens that are plain (non-cached) input."""
    raw = sum(t.usage.input_tokens for t in turns)
    total = sum(context_tokens(t) for t in turns)
    return raw / total if total > 0 else None


def cache_hit_rate(cache_read: int, cache_creation: int, input_tokens: int) -> float | None:
    """Cache hit rate as a fraction (0-1): cache_read / (cache_read + cache_creation + input).
    Returns None when denominator is 0."""
    denom = cache_read + cache_creation + input_tokens
    return cache_read / denom if denom > 0 else None


def cache_hit_pct(cache_read: int, cache_creation: int, input_tokens: int) -> float | None:
    """Cache hit rate as a percentage: cache_read / (cache_read + cache_creation + input) × 100."""
    rate = cache_hit_rate(cache_read, cache_creation, input_tokens)
    return rate * 100 if rate is not None else None


def ctx_ratio_n_to_1(cache_read: int, output: int) -> float | None:
    """Context ratio — cache_read / output, as N:1. Lower = better.
    Returns None when output is 0."""
    if output <= 0:
        return None
    return cache_read / output


def cache_reuse_n_to_1(cache_read: int, cache_creation: int) -> float | None:
    """Cache reuse — cache_read / cache_creation, as N:1. Low = thrashing.
    Returns None when cache_creation is 0 (no reuse yet to measure)."""
    if cache_creation <= 0:
        return None
    return cache_read / cache_creation


def cost_per_kw(cost_usd: float, output_tokens: int) -> float | None:
    """Cost per 1,000 output tokens. Lower = more efficient.
    Returns None when output_tokens is 0."""
    if output_tokens <= 0:
        return None
    return cost_usd * 1000.0 / output_tokens


def ctx_used_latest(latest_turn: Turn, model_context_window: int | None) -> float | None:
    """Latest turn's visible context as a fraction of the model window.
    Returns None when context window is unknown for the model."""
    if not model_context_window:
        return None
    visible = (
        latest_turn.usage.input_tokens
        + latest_turn.usage.cache_read_input_tokens
        + latest_turn.usage.cache_creation_input_tokens
    )
    return visible / model_context_window


def context_growth_rate(turns: list[Turn]) -> float:
    """Tokens added to context per turn. Least-squares slope over turn index.

    Sorts turns by timestamp before computing. Returns 0.0 for fewer than 2 turns.
    """
    sorted_turns = sorted(turns, key=lambda t: t.timestamp)
    n = len(sorted_turns)
    if n < 2:
        return 0.0
    xs = list(range(n))
    ys = [context_tokens(t) for t in sorted_turns]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den > 0 else 0.0
