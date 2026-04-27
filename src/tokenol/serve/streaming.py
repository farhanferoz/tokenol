"""SSE broadcaster: one shared producer fans out diffs to N subscribers per period.

Each connected dashboard tab is a subscriber. Without sharing, N tabs cause N
independent snapshot rebuilds per tick (the rebuild dominates serve CPU). The
broadcaster groups subscribers by the only request-scoped axis — `period` — and
runs one background task per group that builds the snapshot once and fans the
payload out to every subscriber's queue. Each subscriber tracks its own
`prev_payload` so the wire format (full first message, shallow-diff thereafter)
is preserved per-tab regardless of when they joined.

Idle back-off: after `IDLE_THRESHOLD` seconds with no payload changes, the
producer's tick stretches to `max(tick * 3, 15)` until the next change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable

from tokenol.serve.state import (
    ParseCache,
    SnapshotResult,
    build_snapshot_full,
    compute_active_keys,
)

log = logging.getLogger(__name__)

IDLE_THRESHOLD = 30.0
IDLE_TICK_FLOOR = 15
SUBSCRIBER_QUEUE_MAXSIZE = 2
# Time-windowed panels (recent_activity = last 60 min, day boundaries) drift even
# when no JSONL files change, so we force a rebuild at this cadence regardless of
# the file-mtime gate. Keeps idle CPU near zero between heartbeats.
DEFAULT_HEARTBEAT_S = 60.0


def _shallow_diff(prev: dict, curr: dict) -> dict:
    """Return only top-level keys whose values changed."""
    return {k: v for k, v in curr.items() if prev.get(k) != v}


def _effective_tick(tick: int, idle_seconds: float) -> int:
    if idle_seconds >= IDLE_THRESHOLD:
        return max(tick * 3, IDLE_TICK_FLOOR)
    return tick


class _Subscriber:
    __slots__ = ("queue",)

    def __init__(self) -> None:
        # Bounded queue: producer drops the oldest entry on overflow rather than
        # blocking, so a slow client can't stall fan-out to other subscribers.
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE)


class _Group:
    """One producer task + its subscriber set, keyed by `period`."""

    def __init__(
        self,
        period: str,
        build_payload: Callable[[str], dict],
        compute_keys: Callable[[], frozenset[tuple[str, int, int]]],
        get_tick_seconds: Callable[[], int],
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    ) -> None:
        self.period = period
        self._build_payload = build_payload
        self._compute_keys = compute_keys
        self._get_tick_seconds = get_tick_seconds
        self._heartbeat_s = heartbeat_s
        self.subscribers: set[_Subscriber] = set()
        self.task: asyncio.Task | None = None
        self.last_payload: dict | None = None

    async def run(self) -> None:
        """Producer loop with a two-stage gate:

        1. Stat all JSONL files (cheap). If the (path, size, mtime_ns) set is
           unchanged AND the last build is younger than `heartbeat_s`, skip the
           build entirely — most idle ticks land here, so per-tick CPU is just
           the cost of stat'ing a few hundred files.
        2. Otherwise rebuild and fan out.
        """
        prev_payload: dict | None = None
        prev_keys: frozenset[tuple[str, int, int]] | None = None
        last_change_ts = time.monotonic()
        last_built_at: float = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                tick = int(self._get_tick_seconds())
                idle_seconds = time.monotonic() - last_change_ts
                sleep_for = _effective_tick(tick, idle_seconds)

                try:
                    keys = await loop.run_in_executor(None, self._compute_keys)
                except Exception:
                    log.exception("active-keys probe failed — forcing build")
                    keys = None

                now = time.monotonic()
                stale = (now - last_built_at) >= self._heartbeat_s
                changed = keys is None or keys != prev_keys
                if not (changed or stale or prev_payload is None):
                    await asyncio.sleep(sleep_for)
                    continue

                try:
                    curr = await loop.run_in_executor(
                        None, self._build_payload, self.period
                    )
                except Exception:
                    log.exception("snapshot build failed — skipping tick")
                    await asyncio.sleep(sleep_for)
                    continue

                if prev_payload is None or _shallow_diff(prev_payload, curr):
                    last_change_ts = time.monotonic()
                self.last_payload = curr
                prev_payload = curr
                prev_keys = keys
                last_built_at = now

                for sub in list(self.subscribers):
                    self._push(sub, curr)

                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _push(sub: _Subscriber, payload: dict) -> None:
        """Non-blocking fan-out; on overflow, drop the oldest entry and replace.

        We never want a stuck client to back-pressure the producer.
        """
        try:
            sub.queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            sub.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait(payload)


class SnapshotBroadcaster:
    """Shared producer for /api/stream: one task per `period`, fan-out to all tabs.

    The broadcaster is created once per app and held on `app.state.broadcaster`.
    Subscribers use `subscribe(period)` as an async generator yielding SSE-
    formatted strings.
    """

    def __init__(
        self,
        parse_cache: ParseCache,
        all_projects: bool,
        get_reference_usd: Callable[[], float],
        get_tick_seconds: Callable[[], int],
        get_thresholds: Callable[[], dict],
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    ) -> None:
        self._parse_cache = parse_cache
        self._all_projects = all_projects
        self._get_reference_usd = get_reference_usd
        self._get_tick_seconds = get_tick_seconds
        self._get_thresholds = get_thresholds
        self._heartbeat_s = heartbeat_s
        self._groups: dict[str, _Group] = {}
        self._lock = asyncio.Lock()

    def _compute_active_keys(self) -> frozenset[tuple[str, int, int]]:
        return compute_active_keys(self._all_projects)

    def _build_payload(self, period: str) -> dict:
        result: SnapshotResult = build_snapshot_full(
            self._parse_cache,
            all_projects=self._all_projects,
            reference_usd=self._get_reference_usd(),
            tick_seconds=int(self._get_tick_seconds()),
            period=period,
            thresholds=self._get_thresholds(),
        )
        return result.payload

    async def subscribe(self, period: str) -> AsyncGenerator[str, None]:
        sub = _Subscriber()
        async with self._lock:
            grp = self._groups.get(period)
            if grp is None:
                grp = _Group(
                    period,
                    self._build_payload,
                    self._compute_active_keys,
                    self._get_tick_seconds,
                    heartbeat_s=self._heartbeat_s,
                )
                grp.task = asyncio.create_task(
                    grp.run(), name=f"snapshot-broadcaster:{period}"
                )
                self._groups[period] = grp
            grp.subscribers.add(sub)
            # Bootstrap a late-joining subscriber with the most recent payload so
            # they don't wait a full tick for their first message.
            if grp.last_payload is not None:
                _Group._push(sub, grp.last_payload)

        prev_payload: dict | None = None
        try:
            while True:
                payload = await sub.queue.get()
                data = payload if prev_payload is None else _shallow_diff(prev_payload, payload)
                prev_payload = payload
                if data:
                    yield f"data: {json.dumps(data)}\n\n"
        finally:
            async with self._lock:
                grp.subscribers.discard(sub)
                if not grp.subscribers:
                    if grp.task is not None:
                        grp.task.cancel()
                    self._groups.pop(period, None)

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [g.task for g in self._groups.values() if g.task is not None]
            for t in tasks:
                t.cancel()
            self._groups.clear()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def group_count(self) -> int:
        """Number of active producer groups (testing aid)."""
        return len(self._groups)

    def cached_payload(self, period: str) -> dict | None:
        """Latest broadcast payload for *period*, or None if no group is live."""
        grp = self._groups.get(period)
        return grp.last_payload if grp is not None else None
