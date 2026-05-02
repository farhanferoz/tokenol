"""Tests for tokenol.persistence.flusher.FlushQueue."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage
from tokenol.persistence.flusher import FlushQueue
from tokenol.persistence.store import HistoryStore


def _turn(key: str, sid: str) -> Turn:
    return Turn(
        dedup_key=key,
        timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        session_id=sid,
        model="claude-sonnet-4-6",
        usage=Usage(input_tokens=100, output_tokens=50,
                    cache_read_input_tokens=20, cache_creation_input_tokens=10),
        is_sidechain=False, stop_reason="end_turn", cost_usd=0.01, is_interrupted=False,
        tool_use_count=0, tool_error_count=0, tool_names=Counter(),
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
    )


def _session(sid: str) -> Session:
    return Session(
        session_id=sid, source_file="/tmp/x.jsonl",
        is_sidechain=False, cwd="/tmp/proj", turns=[],
    )


@pytest.mark.asyncio
async def test_drains_at_count_threshold(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=3, interval_seconds=60)
    try:
        await q.start()
        for i in range(3):
            q.enqueue([_turn(f"k{i}", "s")], [_session("s")])
        # The threshold trigger fires the drain immediately.
        await asyncio.wait_for(q.drained.wait(), timeout=2.0)
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (3,)
    finally:
        await q.stop()
        store.close()


@pytest.mark.asyncio
async def test_drains_at_time_interval(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=1000, interval_seconds=0.2)
    try:
        await q.start()
        q.enqueue([_turn("k1", "s")], [_session("s")])
        # Below threshold, but interval is short — drain should fire on the timer.
        await asyncio.sleep(0.5)
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (1,)
    finally:
        await q.stop()
        store.close()


@pytest.mark.asyncio
async def test_force_drain_on_shutdown(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=1000, interval_seconds=60)
    try:
        await q.start()
        q.enqueue([_turn("k1", "s")], [_session("s")])
        await q.stop()  # must drain before returning
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (1,)
    finally:
        store.close()
