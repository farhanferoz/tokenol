"""HistoryStore must tolerate concurrent readers and writers on one connection.

A duckdb.DuckDBPyConnection is not safe for concurrent use. Two threads issuing
queries on the same connection race inside ClientContext::PendingQuery and take
the whole process down with SIGSEGV — not an exception, a coredump. Confirmed on
2026-09-04: `tokenol serve --persist` died with

    #0 __memcpy_avx_unaligned_erms (libc.so.6)
    #1 duckdb::ClientContext::PendingQuery(...)

on a flusher thread while the event loop held the GIL. The store is genuinely
multi-threaded in production — the flusher writes from its executor thread while
dashboard requests read from theirs — so every use of the connection is
serialised on an RLock.

A segfault kills the test runner outright, so a regression here shows up as the
run dying rather than as a failure report. That is the intended signal.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _turns(n: int, offset: int = 0):
    from tokenol.metrics.cost import cost_for_turn
    from tokenol.model.events import Turn, Usage

    base = datetime.now(tz=timezone.utc) - timedelta(days=1)
    usage = Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=10, cache_creation_input_tokens=5)
    return [
        Turn(
            dedup_key=f"k-{offset + i}",
            timestamp=base + timedelta(seconds=offset + i),
            session_id=f"s-{(offset + i) % 7}",
            model="claude-opus-4-8",
            usage=usage,
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=cost_for_turn("claude-opus-4-8", usage).total_usd,
        )
        for i in range(n)
    ]


def test_concurrent_read_and_write_does_not_crash(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    store = HistoryStore(tmp_path / "h.duckdb")
    store.flush(_turns(500), [])

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for batch in range(20):
                if stop.is_set():
                    return
                store.flush(_turns(200, offset=1000 + batch * 200), [])
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted on below
            errors.append(exc)
            stop.set()

    def reader() -> None:
        try:
            for _ in range(40):
                if stop.is_set():
                    return
                store.query_turns()
                store.last_ts_by_session()
                store.query_session("s-3")
                store.hydrate_hot(window_days=90)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            stop.set()

    threads = [threading.Thread(target=writer), *(threading.Thread(target=reader) for _ in range(3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not any(t.is_alive() for t in threads), "a store thread deadlocked"
    assert not errors, f"concurrent access raised: {errors!r}"
    assert len(store.query_turns()) == 500 + 20 * 200
    store.close()


def test_store_exposes_a_reentrant_connection_lock(tmp_path: Path) -> None:
    """flush() nests _tx() inside already-locked regions, so a plain Lock would
    self-deadlock. Guards against someone swapping RLock for Lock."""
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    store = HistoryStore(tmp_path / "h.duckdb")
    assert isinstance(store._lock, type(threading.RLock()))
    with store._lock, store._lock:
        pass  # a non-reentrant lock would hang here
    store.close()
