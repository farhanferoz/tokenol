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


class FlushQueue:
    """Thread-safe enqueue side; asyncio drain side."""

    def __init__(
        self,
        store: HistoryStore,
        count_threshold: int = DEFAULT_COUNT_THRESHOLD,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._count_threshold = count_threshold
        self._interval = interval_seconds
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

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Synchronous force-drain in case anything was added between the last
        # async drain and stop().
        await self._drain_once()

    async def _run(self) -> None:
        try:
            while not self._stopping:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                self._wake.clear()
                if self._stopping:
                    break
                await self._drain_once()
        except asyncio.CancelledError:
            return

    async def _drain_once(self) -> None:
        with self._lock:
            turns = self._pending_turns
            sessions = list(self._pending_sessions.values())
            self._pending_turns = []
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
