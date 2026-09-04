"""compaction_reinflation must read the main thread only.

Compaction acts on the orchestrator's context. A sub-agent carries its own, and
session.turns interleaves both, so comparing consecutive raw context sizes across
the mix made a large sub-agent turn beside a small orchestrator turn look like a
compact-then-regrow cycle that never happened. Every concurrent-agent session was
a false positive; it caused a real cost spike to be misdiagnosed on 2026-07-13.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tokenol.metrics.patterns import detect_patterns
from tokenol.model.events import Turn, Usage

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _turn(i: int, cache_read: int, *, sidechain: bool = False) -> Turn:
    """A turn whose context size is driven by cache_read (see context_tokens)."""
    return Turn(
        dedup_key=f"k{i}",
        timestamp=BASE + timedelta(seconds=i),
        session_id="s",
        model="claude-opus-4-8",
        usage=Usage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=0,
        ),
        is_sidechain=sidechain,
        stop_reason="end_turn",
        cost_usd=0.01,
    )


def _kinds(turns):
    return [h.kind for h in detect_patterns(turns)]


def test_sidechain_turns_do_not_fake_a_compaction_cycle() -> None:
    """A steady main thread interleaved with big sub-agent turns is not a cycle."""
    turns = []
    for i in range(12):
        turns.append(_turn(i * 2, 100_000))                      # steady orchestrator
        turns.append(_turn(i * 2 + 1, 5_000, sidechain=True))    # small sub-agent
    assert "compaction_reinflation" not in _kinds(turns)


def test_large_sidechain_beside_small_main_turn_is_not_a_cycle() -> None:
    """The inverse shape: sub-agent dwarfs the orchestrator."""
    turns = []
    for i in range(12):
        turns.append(_turn(i * 2, 10_000))
        turns.append(_turn(i * 2 + 1, 400_000, sidechain=True))
    assert "compaction_reinflation" not in _kinds(turns)


def test_a_real_main_thread_compaction_is_still_detected() -> None:
    """The fix must not blind the detector to the thing it exists for."""
    sizes = [200_000, 200_000, 20_000, 120_000, 190_000, 200_000, 25_000, 150_000, 195_000]
    turns = [_turn(i, v) for i, v in enumerate(sizes)]
    assert "compaction_reinflation" in _kinds(turns)


def test_reported_indices_still_point_into_session_turns() -> None:
    """Indices are consumed by the UI to highlight rows, so they must not shift."""
    sizes = [200_000, 200_000, 20_000, 120_000, 190_000, 200_000, 25_000, 150_000, 195_000]
    turns: list[Turn] = []
    for i, v in enumerate(sizes):
        turns.append(_turn(i, v))
        turns.append(_turn(1000 + i, 300_000, sidechain=True))  # noise between each

    hits = [h for h in detect_patterns(turns) if h.kind == "compaction_reinflation"]
    assert hits, "real compaction lost once sidechain noise was interleaved"
    for idx in hits[0].turn_indices:
        assert 0 <= idx < len(turns)
        assert not turns[idx].is_sidechain, "reported a sidechain turn as part of a compaction cycle"
