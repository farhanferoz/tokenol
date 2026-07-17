"""Flat per-model pricing (USD per 1M tokens) and context windows.

Rates from Anthropic docs (Fable 5 added 2026-06-10; Sonnet 5 added
2026-07-13; cache_write_1h + Sonnet 4.5/Opus 4.5/Opus 4.1/Opus 4/Sonnet 4/
Haiku 3 added 2026-07-17). All current Claude models price flat at all
context sizes — no 1M-tier surcharge. Prompt-cache writes bill at one of
two rates depending on TTL: 1.25x input for a 5-minute cache (``cache_write``)
or 2x input for a 1-hour cache (``cache_write_1h``); cache-read is 0.1x
input regardless of which TTL wrote the entry. Unknown models fall back to
the newest sibling in their family via ModelRegistry.

Sonnet 5 is at introductory pricing ($2/$10, cache-read $0.20, cache-write
$2.50/$4.00) through 2026-08-31; standard pricing ($3/$15, cache-read $0.30,
cache-write $3.75/$6.00 — same as Sonnet 4.6) takes effect 2026-09-01. This
table has no dated tiers, so the entry below will need a manual update to
the standard rate on that date.
"""

from typing import TypedDict


class ModelEntry(TypedDict):
    family: str
    context: int      # tokens
    input: float       # USD / 1M tokens
    output: float
    cache_write: float      # 5-minute TTL cache write (1.25x input)
    cache_write_1h: float    # 1-hour TTL cache write (2x input)
    cache_read: float


CLAUDE_MODELS: dict[str, ModelEntry] = {
    # Fable 5 (top tier, above Opus)
    "claude-fable-5": {
        "family": "fable",
        "context": 1_000_000,
        "input": 10.00,
        "output": 50.00,
        "cache_write": 12.50,
        "cache_write_1h": 20.00,
        "cache_read": 1.00,
    },
    # Opus 4.x
    "claude-opus-4-8": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
    },
    "claude-opus-4-7": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
    },
    # Opus 4.5 — still active, 200K standard context (no 1M default; unlike 4.6+)
    "claude-opus-4-5-20251101": {
        "family": "opus",
        "context": 200_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
    },
    "claude-opus-4-5": {
        "family": "opus",
        "context": 200_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
    },
    # Opus 4.1 — deprecated, retires 2026-08-05
    "claude-opus-4-1-20250805": {
        "family": "opus",
        "context": 200_000,
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_write_1h": 30.00,
        "cache_read": 1.50,
    },
    "claude-opus-4-1": {
        "family": "opus",
        "context": 200_000,
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_write_1h": 30.00,
        "cache_read": 1.50,
    },
    # Opus 4 (original) — retired on the Claude API, same rate as Opus 4.1
    "claude-opus-4-20250514": {
        "family": "opus",
        "context": 200_000,
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_write_1h": 30.00,
        "cache_read": 1.50,
    },
    "claude-opus-4-0": {
        "family": "opus",
        "context": 200_000,
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_write_1h": 30.00,
        "cache_read": 1.50,
    },
    # Sonnet 5 (introductory pricing through 2026-08-31 — see module docstring)
    "claude-sonnet-5": {
        "family": "sonnet",
        "context": 1_000_000,
        "input": 2.00,
        "output": 10.00,
        "cache_write": 2.50,
        "cache_write_1h": 4.00,
        "cache_read": 0.20,
    },
    # Sonnet 4.x
    "claude-sonnet-4-6": {
        "family": "sonnet",
        "context": 1_000_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    # Sonnet 4.5 — still active, 200K standard context (no 1M default)
    "claude-sonnet-4-5-20250929": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-5": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    # Sonnet 4 (original) — retired on the Claude API, same rate as Sonnet 4.5/4.6
    "claude-sonnet-4-20250514": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-0": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    # Haiku 4.x
    "claude-haiku-4-5-20251001": {
        "family": "haiku",
        "context": 200_000,
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_write_1h": 2.00,
        "cache_read": 0.10,
    },
    "claude-haiku-4-5": {
        "family": "haiku",
        "context": 200_000,
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_write_1h": 2.00,
        "cache_read": 0.10,
    },
    # Sonnet 3.x (observed in logs) — retired on the Claude API; 1h cache-write
    # tier is derived from the family-wide 2x-input multiplier (the feature
    # postdates these models, so real logs should never carry this tier for
    # them, but the value is correct if one ever does).
    "claude-sonnet-3-7-20250219": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    "claude-sonnet-3-5-20241022": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    "claude-sonnet-3-5-20240620": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
    # Haiku 3.x
    "claude-haiku-3-5-20241022": {
        "family": "haiku",
        "context": 200_000,
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_write_1h": 1.60,
        "cache_read": 0.08,
    },
    "claude-3-haiku-20240307": {
        "family": "haiku",
        "context": 200_000,
        "input": 0.25,
        "output": 1.25,
        "cache_write": 0.3125,
        "cache_write_1h": 0.50,
        "cache_read": 0.025,
    },
}

def context_window(model: str) -> int | None:
    """Return context window size in tokens for *model*, or None if unknown."""
    entry = CLAUDE_MODELS.get(model)
    return entry["context"] if entry is not None else None


# Family fallback — when an unknown model matches a family prefix, use this
# (newest known) entry in the corresponding family.
FAMILY_FALLBACKS: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
