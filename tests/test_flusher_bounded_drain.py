"""Shutdown must be bounded, and a large backlog must not be one giant call.

`_drain_once` took the entire pending list in a single executor call, so a first
backfill ran for minutes inside one uninterruptible task. Shutdown could not
complete: measured 2026-09-04, two `--persist` servers both had to be SIGKILLed
after 7 minutes, and one left 101 MB of an expected ~150 MB store — the hang was
also a data-integrity problem, not merely a slow exit.

Batches make each call short; a stop deadline makes exit finite and turns silent
loss into a logged count.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tokenol.persistence.flusher import FlushQueue


def _turns(n: int, offset: int = 0):
    from tokenol.model.events import Turn, Usage

    base = datetime.now(tz=timezone.utc) - timedelta(days=1)
    usage = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return [
        Turn(
            dedup_key=f"k-{offset + i}",
            timestamp=base + timedelta(seconds=offset + i),
            session_id="s-1",
            model="claude-opus-4-8",
            usage=usage,
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=0.001,
        )
        for i in range(n)
    ]


class _RecordingStore:
    """Stands in for HistoryStore, recording the size of each flush call."""

    def __init__(self, delay: float = 0.0) -> None:
        self.batch_sizes: list[int] = []
        self._delay = delay

    def flush(self, turns, sessions) -> None:
        import time

        if self._delay:
            time.sleep(self._delay)
        self.batch_sizes.append(len(turns))


@pytest.mark.asyncio
async def test_drain_is_split_into_bounded_batches() -> None:
    store = _RecordingStore()
    q = FlushQueue(store, count_threshold=10_000, max_batch=1_000)
    q.enqueue(_turns(4_500), [])

    await q._drain_once(q._max_batch)
    assert store.batch_sizes == [1_000], "first drain was not bounded to max_batch"
    assert q.pending_count() == 3_500


@pytest.mark.asyncio
async def test_stop_drains_everything_when_it_fits_in_the_deadline() -> None:
    store = _RecordingStore()
    q = FlushQueue(store, count_threshold=10_000, max_batch=1_000, stop_timeout_seconds=30.0)
    q.enqueue(_turns(4_500), [])

    await q.stop()
    assert q.pending_count() == 0, "a backlog that fits the deadline must be fully written"
    assert sum(store.batch_sizes) == 4_500
    assert max(store.batch_sizes) <= 1_000, "stop() bypassed the batch bound"


@pytest.mark.asyncio
async def test_stop_returns_within_its_deadline_on_a_hopeless_backlog() -> None:
    """The point of the fix: exit is finite even when the queue cannot be cleared."""
    store = _RecordingStore(delay=0.05)
    q = FlushQueue(store, count_threshold=10_000, max_batch=100, stop_timeout_seconds=0.3)
    q.enqueue(_turns(50_000), [])

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await q.stop()
    elapsed = loop.time() - t0

    assert elapsed < 5.0, f"stop() took {elapsed:.1f}s despite a 0.3s deadline"
    assert q.pending_count() > 0, "test did not actually exercise the timeout path"


@pytest.mark.asyncio
async def test_unwritten_turns_are_reported_not_silently_dropped(caplog) -> None:
    store = _RecordingStore(delay=0.05)
    q = FlushQueue(store, count_threshold=10_000, max_batch=100, stop_timeout_seconds=0.2)
    q.enqueue(_turns(20_000), [])

    with caplog.at_level("WARNING"):
        await q.stop()

    assert any("unwritten" in r.message or "unwritten" in r.getMessage() for r in caplog.records), (
        "shutdown dropped turns without saying so"
    )
