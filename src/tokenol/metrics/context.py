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
