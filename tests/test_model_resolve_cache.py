"""Model resolution is memoized, and the memo must not leak mutable state.

`resolve` runs once per turn per request. On a 345k-turn corpus one breakdown
page refresh called it over a million times to distinguish ~10 distinct model
strings, costing roughly 11% of server CPU. The keyspace is fixed at import, so
it caches cleanly — but the return carries a list of assumption tags, and handing
the same list to every caller would let one caller's mutation corrupt the rest.
"""

from __future__ import annotations

from tokenol.enums import AssumptionTag
from tokenol.model import registry


def test_repeated_resolution_is_memoized() -> None:
    registry._resolve_cached.cache_clear()
    for _ in range(50):
        registry.resolve("claude-opus-4-8")
    info = registry._resolve_cached.cache_info()
    assert info.misses == 1, f"expected one miss, got {info.misses}"
    assert info.hits == 49


def test_each_caller_gets_its_own_tag_list() -> None:
    """A caller mutating the returned tags must not poison the cache."""
    registry._resolve_cached.cache_clear()
    _entry, tags_a = registry.resolve("some-unknown-opus-model")
    assert tags_a, "an unknown model should carry a fallback tag"
    tags_a.append(AssumptionTag.GEMINI_UNPRICED)

    _entry, tags_b = registry.resolve("some-unknown-opus-model")
    assert AssumptionTag.GEMINI_UNPRICED not in tags_b, "cached tag list was shared and got mutated"
    assert tags_a is not tags_b


def test_memoization_does_not_change_answers() -> None:
    """Cached results must match what the uncached registry returns."""
    registry._resolve_cached.cache_clear()
    for model in (
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-fable-5-1",
        "claude-sonnet-5",
        "claude-opus-4-8[1m]",
        "claude-opus-4-8-thinking",
        "gemini-2.5-pro",
        "totally-made-up-model",
        "some-unknown-haiku-thing",
    ):
        direct_entry, direct_tags = registry._registry.resolve(model)
        cached_entry, cached_tags = registry.resolve(model)
        assert cached_entry == direct_entry, model
        assert cached_tags == direct_tags, model
