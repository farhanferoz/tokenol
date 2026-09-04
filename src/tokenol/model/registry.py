"""Single source of truth for model resolution and pricing lookup."""

from __future__ import annotations

import re
from functools import lru_cache

from tokenol.enums import AssumptionTag
from tokenol.model.pricing import CLAUDE_MODELS, FAMILY_FALLBACKS, ModelEntry

# Canonical family name substrings, ordered by specificity (top tier first).
_FAMILY_KEYWORDS = ["fable", "opus", "sonnet", "haiku"]

# Trailing context-window marker Claude Code appends to logged model IDs,
# e.g. "claude-opus-4-8[1m]" for the 1M-context variant. Stripped before
# lookup so a "[1m]" turn prices as its base model instead of falling back.
_CONTEXT_SUFFIX = re.compile(r"\[[^\[\]]*\]$")


class ModelRegistry:
    """Resolve a raw model string from JSONL to a pricing entry."""

    def is_claude(self, model: str | None) -> bool:
        """Return True if model is a recognized or family-matched Claude model."""
        if not model:
            return False
        clean = _CONTEXT_SUFFIX.sub("", model.replace("-thinking", ""))
        if clean in CLAUDE_MODELS:
            return True
        lower = clean.lower()
        return lower.startswith("claude") or any(family in lower for family in _FAMILY_KEYWORDS)

    def resolve(self, model: str) -> tuple[ModelEntry | None, list[AssumptionTag]]:
        """Return (entry, tags) for *model*.

        Returns (None, [GEMINI_UNPRICED]) for non-Claude providers (anything not matching the Claude family).
        Returns (fallback_entry, [UNKNOWN_MODEL_FALLBACK]) for unknown Claude models.
        Returns (entry, []) for known models.
        """
        tags: list[AssumptionTag] = []

        # Strip -thinking suffix (unreliable across versions) and any trailing
        # context-window marker like "[1m]".
        clean = _CONTEXT_SUFFIX.sub("", model.replace("-thinking", ""))

        # Exact match first.
        if clean in CLAUDE_MODELS:
            return CLAUDE_MODELS[clean], tags

        lower = clean.lower()

        # Unknown Claude model — family fallback.
        for family in _FAMILY_KEYWORDS:
            if family in lower:
                fallback_key = FAMILY_FALLBACKS[family]
                tags.append(AssumptionTag.UNKNOWN_MODEL_FALLBACK)
                return CLAUDE_MODELS[fallback_key], tags

        # Unrecognized model with claude prefix (e.g. claude-not-a-real-model-99) — fallback.
        if lower.startswith("claude"):
            fallback_key = FAMILY_FALLBACKS["sonnet"]
            tags.append(AssumptionTag.UNKNOWN_MODEL_FALLBACK)
            return CLAUDE_MODELS[fallback_key], tags

        # Anything not matching the Claude family [fable, opus, sonnet, haiku] is non-Claude.
        tags.append(AssumptionTag.GEMINI_UNPRICED)
        return None, tags


_registry = ModelRegistry()


@lru_cache(maxsize=512)
def _resolve_cached(model: str) -> tuple[ModelEntry | None, tuple[AssumptionTag, ...]]:
    """Memoized inner resolve. Tags are a tuple so the cached value is immutable.

    Resolution is a regex substitution, a lowercase and a family-prefix scan, and
    it runs once per turn per request: on a 345k-turn corpus a single breakdown
    page refresh called it over a million times to distinguish about ten distinct
    model strings. It was ~11% of server samples. The keyspace is tiny and fixed
    at import (CLAUDE_MODELS is a module constant), so the cache never staleness-
    traps — 512 entries is far more than any real corpus has distinct models.
    """
    entry, tags = _registry.resolve(model)
    return entry, tuple(tags)


def resolve(model: str) -> tuple[ModelEntry | None, list[AssumptionTag]]:
    """Resolve a model name to its pricing entry and assumption tags.

    Returns a FRESH tag list each call: callers extend their own list from it and
    one caller does a membership test, but handing out a shared mutable list from
    a cache would make any future mutation corrupt every later lookup.
    """
    entry, tags = _resolve_cached(model)
    return entry, list(tags)


def is_claude(model: str | None) -> bool:
    """Return True if model is a recognized or family-matched Claude model."""
    return _registry.is_claude(model)
