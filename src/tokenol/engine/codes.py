"""The one mapping between ``AssumptionTag`` sets and the integer the engine stores."""

from __future__ import annotations

from collections.abc import Iterable

from tokenol.enums import AssumptionTag

# Explicit, never derived from enum order: a stored integer must keep its meaning
# if the enum is reordered. A new tag needs a new bit appended here.
ASSUMPTION_BITS: dict[AssumptionTag, int] = {
    AssumptionTag.WINDOW_BOUNDARY_HEURISTIC: 0,
    AssumptionTag.UNKNOWN_MODEL_FALLBACK: 1,
    AssumptionTag.DEDUP_PASSTHROUGH: 2,
    AssumptionTag.INTERRUPTED_TURN_SKIPPED: 3,
    AssumptionTag.GEMINI_UNPRICED: 4,
}

_missing = [tag.name for tag in AssumptionTag if tag not in ASSUMPTION_BITS]
if _missing or len(set(ASSUMPTION_BITS.values())) != len(ASSUMPTION_BITS):
    raise RuntimeError(
        f"ASSUMPTION_BITS must give every AssumptionTag its own bit; missing {_missing}"
    )

_BY_BIT: tuple[tuple[int, AssumptionTag], ...] = tuple(
    sorted((bit, tag) for tag, bit in ASSUMPTION_BITS.items())
)


def encode_assumptions(tags: Iterable[AssumptionTag]) -> int:
    """Set the bit of every tag in *tags* (duplicates collapse)."""
    bits = 0
    for tag in tags:
        bits |= 1 << ASSUMPTION_BITS[tag]
    return bits


def decode_assumptions(bits: int) -> list[AssumptionTag]:
    """The tags whose bits are set in *bits*, in bit order."""
    return [tag for bit, tag in _BY_BIT if bits >> bit & 1]
