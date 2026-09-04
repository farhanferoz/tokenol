"""Async batch flusher: drains pending Turn/Session deltas to HistoryStore.

Flush triggers:
- Count threshold: ≥100 queued turns → wake immediately.
- Time interval: every 30 seconds → wake regardless.

The drain runs `HistoryStore.flush(...)` in a background executor so the
asyncio event loop stays free. `stop()` cancels the loop and force-drains any
pending turns before returning so graceful shutdown loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tokenol.model.events import Session, Turn
    from tokenol.persistence.store import HistoryStore

log = logging.getLogger(__name__)

DEFAULT_COUNT_THRESHOLD = 100
DEFAULT_INTERVAL_SECONDS = 30.0
# Turns per drain call. A drain used to take the WHOLE backlog in one executor
# call, so a first backfill (~345k turns) ran for minutes inside a single
# uninterruptible task: shutdown could not complete, and a server SIGKILLed while
# it ran left the store partially written. Measured 2026-09-04 — two --persist
# runs both had to be killed after 7 minutes, one leaving 101 MB of an expected
# ~150 MB. Bounded batches keep each call short and let the loop yield between them.
DEFAULT_MAX_BATCH = 5_000
# Ceiling on how long stop() will spend draining. Unbounded shutdown is not a
# choice between "lose data" and "keep data" — it is a choice between losing it
# with a log line and losing it to SIGKILL with a half-written store.
DEFAULT_STOP_TIMEOUT_SECONDS = 30.0


class FlushQueue:
    """Thread-safe enqueue side; asyncio drain side."""

    def __init__(
        self,
        store: HistoryStore,
        count_threshold: int = DEFAULT_COUNT_THRESHOLD,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        max_batch: int = DEFAULT_MAX_BATCH,
        stop_timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._count_threshold = count_threshold
        self._interval = interval_seconds
        self._max_batch = max_batch
        self._stop_timeout = stop_timeout_seconds
        self._lock = threading.Lock()
        self._pending_turns: list[Turn] = []
        self._pending_sessions: dict[str, Session] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        # `drained` is a test aid: set after each successful drain so tests can
        # `await q.drained.wait()` to know the drain happened. Not part of the
        # public lifecycle API.
        self.drained = asyncio.Event()

    def enqueue(self, turns: list[Turn], sessions: list[Session]) -> None:
        if not turns and not sessions:
            return
        with self._lock:
            self._pending_turns.extend(turns)
            for s in sessions:
                self._pending_sessions[s.session_id] = s
            count = len(self._pending_turns)
        if count >= self._count_threshold:
            with suppress(RuntimeError):
                # Loop not running yet — drain will pick up on next start.
                self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="history-flusher")

    def pending_count(self) -> int:
        """Turns still queued. Used by stop() and by tests to observe progress."""
        with self._lock:
            return len(self._pending_turns)

    async def stop(self) -> None:
        """Stop the loop and drain what fits inside the stop timeout.

        Bounded on purpose. Draining everything meant an unbounded shutdown: on a
        large backlog the process could not exit and was killed mid-write. A
        deadline turns that into a bounded exit plus an explicit count of what did
        not make it — and those turns are re-derivable from the JSONL on next
        start, provided it has not been pruned.
        """
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_timeout
        while self.pending_count() or self._pending_sessions:
            if loop.time() >= deadline:
                break
            await self._drain_once(self._max_batch)

        remaining = self.pending_count()
        if remaining:
            log.warning(
                "shutdown drain hit its %.0fs timeout with %d turn(s) unwritten; "
                "they will be re-derived from JSONL on next start",
                self._stop_timeout,
                remaining,
            )

    async def _run(self) -> None:
        try:
            while not self._stopping:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                self._wake.clear()
                if self._stopping:
                    break
                # Drain in bounded batches until caught up, rather than one huge
                # call per interval: a large backlog would otherwise hold the
                # executor for minutes and starve everything behind it.
                while not self._stopping and (self.pending_count() or self._pending_sessions):
                    await self._drain_once(self._max_batch)
        except asyncio.CancelledError:
            return

    async def _drain_once(self, limit: int | None = None) -> None:
        """Flush at most *limit* turns (all of them when None)."""
        with self._lock:
            if limit is None or len(self._pending_turns) <= limit:
                turns = self._pending_turns
                self._pending_turns = []
            else:
                turns = self._pending_turns[:limit]
                del self._pending_turns[:limit]
            # Sessions ride with whichever batch goes first; flush() re-derives
            # their aggregates from the rows actually present, so a session
            # written before all its turns is corrected by a later batch.
            sessions = list(self._pending_sessions.values())
            self._pending_sessions = {}
        if not turns and not sessions:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._store.flush, turns, sessions)
        except Exception:
            log.exception("flush failed — re-queuing %d turns", len(turns))
            with self._lock:
                self._pending_turns[:0] = turns
                for s in sessions:
                    self._pending_sessions.setdefault(s.session_id, s)
            return
        self.drained.set()
