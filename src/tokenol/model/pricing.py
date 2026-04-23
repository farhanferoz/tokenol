"""Flat per-model pricing (USD per 1M tokens) and context windows.

Rates from Anthropic docs (2026-04-20). All Claude 4.x models price flat
at all context sizes — no 1M-tier surcharge.
Unknown models fall back to nearest family sibling via ModelRegistry.
"""

from typing import TypedDict


class ModelEntry(TypedDict):
    family: str
    context: int      # tokens
    input: float      # USD / 1M tokens
    output: float
    cache_write: float
    cache_read: float


CLAUDE_MODELS: dict[str, ModelEntry] = {
    # Opus 4.x
    "claude-opus-4-7": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    # Sonnet 4.x
    "claude-sonnet-4-6": {
        "family": "sonnet",
        "context": 1_000_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    # Haiku 4.x
    "claude-haiku-4-5-20251001": {
        "family": "haiku",
        "context": 200_000,
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    "claude-haiku-4-5": {
        "family": "haiku",
        "context": 200_000,
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    # Sonnet 3.x (observed in logs)
    "claude-sonnet-3-7-20250219": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-3-5-20241022": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-3-5-20240620": {
        "family": "sonnet",
        "context": 200_000,
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    # Haiku 3.x
    "claude-haiku-3-5-20241022": {
        "family": "haiku",
        "context": 200_000,
        "input": 0.80,
        "output": 4.00,
        "cache_write": 1.00,
        "cache_read": 0.08,
    },
}

def context_window(model: str) -> int | None:
    """Return context window size in tokens for *model*, or None if unknown."""
    entry = CLAUDE_MODELS.get(model)
    return entry["context"] if entry is not None else None


# Family fallback order — when unknown model matches a family prefix,
# use the first (newest) entry in the corresponding list.
FAMILY_FALLBACKS: dict[str, list[str]] = {
    "opus": ["claude-opus-4-7", "claude-opus-4-6"],
    "sonnet": ["claude-sonnet-4-6", "claude-sonnet-3-7-20250219"],
    "haiku": ["claude-haiku-4-5-20251001", "claude-haiku-3-5-20241022"],
}
