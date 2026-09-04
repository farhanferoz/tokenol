"""stop()'s timeout must bound the WHOLE shutdown, not just the tail of it.

The drain was already bounded per batch (`DEFAULT_MAX_BATCH`), but the stop
deadline was started *after* `await self._task`, which itself waits for the
in-flight `_drain_once` to finish. So a shutdown that arrived mid-batch cost
`(in-flight batch) + stop_timeout + (one more batch)`, while both the
docstring and the 0.7.4 release notes described a 30-second deadline.

Observed 2026-09-04: a --persist server held the DuckDB store lock for ~90s
after its checkpoint had already landed, which is what blocks a user trying to
restart their own server (they get `Conflicting lock is held ... (PID n)`).

The uninterruptible part cannot be removed: `_drain_once` hands the write to a
thread via run_in_executor, and `HistoryStore.close()` takes the same lock the
flush holds, so cancelling the coroutine frees nothing. What CAN be fixed is
spending the budget from the moment stop() is called.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tokenol.persistence.flusher import FlushQueue


class _SlowStore:
    """Store stand-in whose flush blocks, like a real DuckDB write."""

    def __init__(self, per_flush_seconds: float) -> None:
        self.per_flush_seconds = per_flush_seconds
        self.flush_calls = 0

    def flush(self, turns, sessions) -> None:  # runs on the executor thread
        self.flush_calls += 1
        time.sleep(self.per_flush_seconds)


def _turns(n: int):
    """Opaque placeholders — the flusher only ever counts and hands them on."""
    return [object() for _ in range(n)]


@pytest.mark.asyncio
async def test_stop_does_not_start_a_new_batch_once_the_budget_is_spent():
    """If waiting for the in-flight batch already blew the deadline, stop.

    Today the deadline starts only after that wait, so stop() goes on to drain
    at least one more full batch — the bug.
    """
    store = _SlowStore(per_flush_seconds=0.6)
    q = FlushQueue(
        store,
        count_threshold=1,
        interval_seconds=0.01,
        max_batch=10,
        stop_timeout_seconds=0.1,  # far smaller than one batch
    )
    await q.start()
    q.enqueue(_turns(500), [])

    # Let the loop get into an in-flight flush.
    await asyncio.wait_for(q.drained.wait(), timeout=5)
    calls_before_stop = store.flush_calls

    await q.stop()

    # The 0.1s budget is spent long before the in-flight 0.6s batch returns, so
    # no further batch may start after it.
    assert store.flush_calls <= calls_before_stop + 1, (
        f"stop() ran {store.flush_calls - calls_before_stop} batches after the "
        f"budget was already spent; expected at most the in-flight one"
    )


@pytest.mark.asyncio
async def test_stop_budget_is_measured_from_the_call_not_from_after_the_task():
    """Total stop() time must not exceed in-flight batch + budget + slack."""
    per_flush = 0.5
    budget = 0.1
    store = _SlowStore(per_flush_seconds=per_flush)
    q = FlushQueue(
        store,
        count_threshold=1,
        interval_seconds=0.01,
        max_batch=10,
        stop_timeout_seconds=budget,
    )
    await q.start()
    q.enqueue(_turns(500), [])
    await asyncio.wait_for(q.drained.wait(), timeout=5)

    t0 = time.monotonic()
    await q.stop()
    elapsed = time.monotonic() - t0

    # One uninterruptible in-flight batch is unavoidable. A second one is the bug.
    assert elapsed < per_flush * 2, (
        f"stop() took {elapsed:.2f}s; one in-flight batch is {per_flush}s and the "
        f"budget is {budget}s, so anything near {per_flush * 2}s means it drained again"
    )
