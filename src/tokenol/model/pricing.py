"""Flat per-model pricing (USD per 1M tokens) and context windows.

Rates from Anthropic docs (Fable 5 added 2026-06-10; Sonnet 5 added
2026-07-13; cache_write_1h + Sonnet 4.5/Opus 4.5/Opus 4.1/Opus 4/Sonnet 4/
Haiku 3 added 2026-07-17; Opus 5 + Fable 5.1 added 2026-09-04; Haiku 5.5
added 2026-10-23). Every model prices flat at all context sizes except
Haiku 5.5, which bills a request whose prompt is over 100,000 tokens at 5x
on every rate (``long_prompt``). Prompt-cache writes bill at one of two rates
depending on TTL: 1.25x input for a 5-minute cache (``cache_write``) or 2x
input for a 1-hour cache (``cache_write_1h``). Cache-read is 0.1x input
except on Fable 5.1 and Mythos 5.1 (0.025x) and Opus 5.5 and Sonnet 5.5
(0.05x) — so a family fallback from an unpriced Fable 5.x to Fable 5
overcharges cache reads 4x. Unknown models fall back to a sibling in their
family via ModelRegistry.

A model whose price changed keeps the rates it billed at before the change
(``earlier``), so history is priced as it was billed rather than at today's
rates.

Sonnet 5's $2/$10 launch rate was announced as introductory pricing through
2026-08-31, with a rise to $3/$15 on 2026-09-01. Anthropic cancelled that
increase and made $2/$10 the standard price (verified on the pricing page
2026-09-04) — do NOT "fix" this entry up to $3/$15.
"""

from datetime import datetime, timezone
from typing import TypedDict


class Rates(TypedDict):
    input: float  # USD / 1M tokens
    output: float
    cache_write: float  # 5-minute TTL cache write (1.25x input)
    cache_write_1h: float  # 1-hour TTL cache write (2x input)
    cache_read: float


class LongPromptRates(Rates):
    # A request whose prompt (input + cache read + cache write) is OVER this many
    # tokens bills at these rates, on every component including output.
    threshold: int


class EarlierRates(Rates):
    until: datetime  # first instant (UTC) the entry's current rates apply


class _ModelEntryBase(Rates):
    family: str
    context: int  # tokens


class ModelEntry(_ModelEntryBase, total=False):
    long_prompt: LongPromptRates
    earlier: EarlierRates


CLAUDE_MODELS: dict[str, ModelEntry] = {
    # Fable 5.1 (top tier, above Opus). Identical to Fable 5 except cache
    # reads bill at 0.025x input ($0.25/MTok) instead of the usual 0.1x.
    "claude-fable-5-1": {
        "family": "fable",
        "context": 1_000_000,
        "input": 10.00,
        "output": 50.00,
        "cache_write": 12.50,
        "cache_write_1h": 20.00,
        "cache_read": 0.25,
    },
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
    # Opus 5.5 — cheaper than Opus 5 on every rate, and reads cache at 0.05x
    # input ($0.20/MTok) rather than the usual 0.1x.
    # Verified 2026-09-28 against platform.claude.com/docs/en/about-claude/pricing
    "claude-opus-5-5": {
        "family": "opus",
        "context": 1_000_000,
        "input": 4.00,
        "output": 20.00,
        "cache_write": 5.00,
        "cache_write_1h": 8.00,
        "cache_read": 0.20,
    },
    # Opus 5
    "claude-opus-5": {
        "family": "opus",
        "context": 1_000_000,
        "input": 5.00,
        "output": 25.00,
        "cache_write": 6.25,
        "cache_write_1h": 10.00,
        "cache_read": 0.50,
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
    # Sonnet 5.5. Reads cache at 0.05x input ($0.10/MTok) since 2026-10-07; before
    # that it read at 0.1x ($0.20/MTok), with every other rate the same. Verified
    # 2026-10-23 against platform.claude.com/docs/en/about-claude/pricing (rates)
    # and .../release-notes/overview (the cut, dated October 7; taken as 00:00 UTC
    # because the note gives no time).
    "claude-sonnet-5-5": {
        "family": "sonnet",
        "context": 1_000_000,
        "input": 2.00,
        "output": 10.00,
        "cache_write": 2.50,
        "cache_write_1h": 4.00,
        "cache_read": 0.10,
        "earlier": {
            "until": datetime(2026, 10, 7, tzinfo=timezone.utc),
            "input": 2.00,
            "output": 10.00,
            "cache_write": 2.50,
            "cache_write_1h": 4.00,
            "cache_read": 0.20,
        },
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
    # Haiku 5.5 — priced by prompt length: a request whose prompt is over 100,000
    # tokens (cache reads and writes included) pays 5x on every rate. Verified
    # 2026-10-23 against platform.claude.com/docs/en/about-claude/pricing (rates,
    # "Long context pricing") and .../models/overview (1M context).
    "claude-haiku-5-5": {
        "family": "haiku",
        "context": 1_000_000,
        "input": 0.10,
        "output": 0.50,
        "cache_write": 0.125,
        "cache_write_1h": 0.20,
        "cache_read": 0.01,
        "long_prompt": {
            "threshold": 100_000,
            "input": 0.50,
            "output": 2.50,
            "cache_write": 0.625,
            "cache_write_1h": 1.00,
            "cache_read": 0.05,
        },
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
# entry in the corresponding family. Each points at a sibling at least as dear
# as the newest one, never a cheaper one: Fable 5.1, Opus 5.5, Sonnet 5.5 and
# Haiku 5.5 all undercut their predecessors (Fable 5.1 reads cache at 0.025x
# input where Fable 5 reads at 0.1x), so falling back to them would silently
# *understate* an unknown model, most of all on cache reads (the dominant cost
# component in an agent workload). Overstating is the safer error for a spend
# monitor, and the dashboard banner names every model priced this way.
FAMILY_FALLBACKS: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
