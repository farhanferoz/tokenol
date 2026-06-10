"""Single source of truth for model resolution and pricing lookup."""

from __future__ import annotations

import re

from tokenol.enums import AssumptionTag
from tokenol.model.pricing import CLAUDE_MODELS, FAMILY_FALLBACKS, ModelEntry

# Canonical family name substrings, ordered by specificity (top tier first).
_FAMILY_KEYWORDS = ["fable", "opus", "sonnet", "haiku"]

# Non-Claude providers whose models appear in logs — unpriced in v1.
_UNPRICED_PREFIXES = ("gemini", "gpt", "o1", "o3", "o4")

# Trailing context-window marker Claude Code appends to logged model IDs,
# e.g. "claude-opus-4-8[1m]" for the 1M-context variant. Stripped before
# lookup so a "[1m]" turn prices as its base model instead of falling back.
_CONTEXT_SUFFIX = re.compile(r"\[[^\[\]]*\]$")


class ModelRegistry:
    """Resolve a raw model string from JSONL to a pricing entry."""

    def resolve(self, model: str) -> tuple[ModelEntry | None, list[AssumptionTag]]:
        """Return (entry, tags) for *model*.

        Returns (None, [GEMINI_UNPRICED]) for non-Claude providers.
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

        # Non-Claude providers.
        lower = clean.lower()
        if any(lower.startswith(p) for p in _UNPRICED_PREFIXES):
            tags.append(AssumptionTag.GEMINI_UNPRICED)
            return None, tags

        # Unknown Claude model — family fallback.
        for family in _FAMILY_KEYWORDS:
            if family in lower:
                fallback_key = FAMILY_FALLBACKS[family][0]
                tags.append(AssumptionTag.UNKNOWN_MODEL_FALLBACK)
                return CLAUDE_MODELS[fallback_key], tags

        # Completely unknown.
        tags.append(AssumptionTag.UNKNOWN_MODEL_FALLBACK)
        fallback_key = next(iter(CLAUDE_MODELS))
        return CLAUDE_MODELS[fallback_key], tags


_registry = ModelRegistry()


def resolve(model: str) -> tuple[ModelEntry | None, list[AssumptionTag]]:
    return _registry.resolve(model)
