"""`all_written` is true only once store.flush has run for every enqueued turn.

`_drain_once` pops the pending list BEFORE the DuckDB write runs, so an empty
queue is not evidence that anything reached disk. The marks sidecar must not be
written on that evidence; this is the check it uses instead.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest


def _turn(key: str):
    from tokenol.model.events import Turn, Usage

    return Turn(
        dedup_key=key,
        timestamp=datetime.now(tz=timezone.utc),
        session_id="s",
        model="claude-sonnet-4-6",
        usage=Usage(),
        is_sidechain=False,
        stop_reason=None,
    )


def _session():
    from tokenol.model.events import Session

    return Session(session_id="s", source_file="", is_sidechain=False, turns=[])


@pytest.mark.asyncio
async def test_all_written_tracks_the_store_write_not_the_dequeue(tmp_path) -> None:
    pytest.importorskip("duckdb")
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore

    store = HistoryStore(tmp_path / "h.duckdb")
    gate = threading.Event()
    real_flush = store.flush

    def slow_flush(turns, sessions):
        gate.wait(timeout=5)
        return real_flush(turns, sessions)

    store.flush = slow_flush
    q = FlushQueue(store, count_threshold=1_000_000, interval_seconds=1000)
    try:
        assert q.all_written(), "an empty queue that never enqueued anything is all written"
        q.enqueue([_turn("k1")], [_session()])
        assert not q.all_written()

        drain = asyncio.create_task(q._drain_once())
        await asyncio.sleep(0.1)
        assert q.pending_count() == 0, "dequeued (precondition of the test)"
        assert not q.all_written(), "dequeued must not count as written while the flush is in flight"

        gate.set()
        await drain
        assert q.all_written()
    finally:
        gate.set()
        store.close()


@pytest.mark.asyncio
async def test_a_failed_flush_leaves_all_written_false(tmp_path) -> None:
    pytest.importorskip("duckdb")
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore

    store = HistoryStore(tmp_path / "h.duckdb")

    def failing(turns, sessions):
        raise RuntimeError("disk full")

    store.flush = failing
    q = FlushQueue(store, count_threshold=1_000_000, interval_seconds=1000)
    try:
        q.enqueue([_turn("k1")], [_session()])
        await q._drain_once()
        assert q.pending_count() == 1, "failed turns are re-queued (existing behaviour)"
        assert not q.all_written()
    finally:
        store.close()
