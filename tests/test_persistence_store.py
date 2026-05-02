"""Tests for tokenol.persistence.store.HistoryStore."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage
from tokenol.persistence.store import HistoryStore


def _turn(
    key: str,
    sid: str,
    *,
    ts: datetime | None = None,
    model: str = "claude-sonnet-4-6",
    cost: float = 0.01,
) -> Turn:
    return Turn(
        dedup_key=key,
        timestamp=ts or datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        session_id=sid,
        model=model,
        usage=Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=20, cache_creation_input_tokens=10),
        is_sidechain=False,
        stop_reason="end_turn",
        cost_usd=cost,
        is_interrupted=False,
        tool_use_count=2,
        tool_error_count=0,
        tool_names=Counter({"Read": 1, "Bash": 1}),
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
    )


def _session(sid: str, *, source: str = "/tmp/x.jsonl", cwd: str = "/tmp/proj") -> Session:
    return Session(
        session_id=sid,
        source_file=source,
        is_sidechain=False,
        cwd=cwd,
        turns=[],
    )


def test_open_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    store = HistoryStore(db_path)
    # File and parent dir created
    assert db_path.exists()
    store.close()
    # Tables present (verify after closing write connection)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()}
    finally:
        con.close()
    assert {"meta", "sessions", "turns"} <= tables


def test_open_existing_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    HistoryStore(db_path).close()
    HistoryStore(db_path).close()  # must not raise


def test_schema_version_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    store = HistoryStore(db_path)
    try:
        rows = store._con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchall()
        assert rows == [("1",)]
    finally:
        store.close()


def test_flush_inserts_turns_and_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        t1 = _turn("k1", "sess-1")
        t2 = _turn("k2", "sess-1", ts=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))
        store.flush(turns=[t1, t2], sessions=[s])

        rows = store._con.execute(
            "SELECT dedup_key, session_id, cost_usd FROM turns ORDER BY dedup_key"
        ).fetchall()
        assert rows == [("k1", "sess-1", 0.01), ("k2", "sess-1", 0.01)]

        srows = store._con.execute(
            "SELECT session_id, source_file, cwd, turn_count, first_ts, last_ts FROM sessions"
        ).fetchall()
        assert srows == [(
            "sess-1", "/tmp/x.jsonl", "/tmp/proj", 2,
            datetime(2026, 5, 1, 12, 0),
            datetime(2026, 5, 1, 13, 0),
        )]
    finally:
        store.close()


def test_flush_idempotent_on_dedup_key(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        store.flush([_turn("k1", "sess-1")], [s])
        # Re-flushing the same key is a no-op (no error, no duplication).
        store.flush([_turn("k1", "sess-1", cost=999.0)], [s])
        rows = store._con.execute("SELECT cost_usd FROM turns").fetchall()
        assert rows == [(0.01,)]  # original value retained, not overwritten
    finally:
        store.close()


def test_flush_upserts_session_metadata(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1", source="/old/path.jsonl", cwd="/tmp/old")
        store.flush([_turn("k1", "sess-1")], [s])

        s2 = _session("sess-1", source="/new/path.jsonl", cwd="/tmp/new")
        t2 = _turn("k2", "sess-1", ts=datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc))
        store.flush([t2], [s2])

        srows = store._con.execute(
            "SELECT source_file, cwd, turn_count, last_ts FROM sessions"
        ).fetchall()
        assert srows == [(
            "/new/path.jsonl", "/tmp/new", 2,
            datetime(2026, 5, 1, 14, 0),
        )]
    finally:
        store.close()


def test_flush_serializes_tool_names_and_assumptions_as_json(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        t = _turn("k1", "sess-1")
        store.flush([t], [s])
        row = store._con.execute(
            "SELECT tool_names, assumptions FROM turns WHERE dedup_key = 'k1'"
        ).fetchone()
        assert json.loads(row[0]) == {"Read": 1, "Bash": 1}
        assert json.loads(row[1]) == ["UNKNOWN_MODEL_FALLBACK"]
    finally:
        store.close()


def test_flush_refreshes_updated_at_on_upsert(tmp_path: Path) -> None:
    """Subsequent flushes for the same session must bump sessions.updated_at."""
    import time
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("k1", "sess-1")], [_session("sess-1")])
        first_updated = store._con.execute(
            "SELECT updated_at FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()[0]
        time.sleep(0.05)  # ensure CURRENT_TIMESTAMP can advance
        store.flush(
            [_turn("k2", "sess-1", ts=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))],
            [_session("sess-1")],
        )
        second_updated = store._con.execute(
            "SELECT updated_at FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()[0]
        assert second_updated > first_updated
    finally:
        store.close()
