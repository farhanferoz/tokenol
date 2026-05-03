"""Tests for tokenol.persistence.store.HistoryStore."""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage
from tokenol.persistence.store import FLUSH_CHUNK_SIZE, HistoryStore


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


def test_hydrate_hot_returns_recent_turns_only(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        now = datetime.now(tz=timezone.utc)
        old = _turn("old", "sess-1", ts=now - timedelta(days=120))
        recent = _turn("recent", "sess-2", ts=now - timedelta(days=10))
        store.flush(
            [old, recent],
            [_session("sess-1"), _session("sess-2")],
        )

        turns, sessions = store.hydrate_hot(window_days=90)
        keys = {t.dedup_key for t in turns}
        assert keys == {"recent"}
        sids = {s.session_id for s in sessions}
        # Sessions tied to hot-window turns are returned. sess-1 has no
        # hot-window turn so it is not hydrated into memory.
        assert sids == {"sess-2"}
    finally:
        store.close()


def test_hydrate_hot_reconstructs_turn_fields(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("k1", "sess-1")], [_session("sess-1")])
        turns, _ = store.hydrate_hot(window_days=365)
        assert len(turns) == 1
        t = turns[0]
        assert t.dedup_key == "k1"
        assert t.session_id == "sess-1"
        assert t.usage.input_tokens == 100
        assert t.usage.cache_read_input_tokens == 20
        assert dict(t.tool_names) == {"Read": 1, "Bash": 1}
        assert [a.value for a in t.assumptions] == ["UNKNOWN_MODEL_FALLBACK"]
    finally:
        store.close()


def test_last_ts_by_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "sess-1", ts=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)),
                _turn("b", "sess-1", ts=datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)),
                _turn("c", "sess-2", ts=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)),
            ],
            [_session("sess-1"), _session("sess-2")],
        )
        marks = store.last_ts_by_session()
        # DuckDB returns naive datetimes from TIMESTAMP columns; we tag them UTC on read.
        assert marks["sess-1"] == datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
        assert marks["sess-2"] == datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)
    finally:
        store.close()


def test_query_turns_filters_by_date_range(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "s1", ts=datetime(2026, 4, 1, tzinfo=timezone.utc)),
                _turn("b", "s1", ts=datetime(2026, 5, 1, tzinfo=timezone.utc)),
                _turn("c", "s1", ts=datetime(2026, 6, 1, tzinfo=timezone.utc)),
            ],
            [_session("s1")],
        )
        rows = store.query_turns(since=date(2026, 5, 1), until=date(2026, 5, 31))
        assert {t.dedup_key for t in rows} == {"b"}
    finally:
        store.close()


def test_query_turns_filters_by_project_and_model(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "s1", model="claude-opus-4-7"),
                _turn("b", "s2", model="claude-sonnet-4-6"),
            ],
            [_session("s1", cwd="/proj/a"), _session("s2", cwd="/proj/b")],
        )
        rows = store.query_turns(project="/proj/a")
        assert {t.dedup_key for t in rows} == {"a"}
        rows = store.query_turns(model="claude-sonnet-4-6")
        assert {t.dedup_key for t in rows} == {"b"}
    finally:
        store.close()


def test_query_session_returns_full_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s1", ts=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))],
            [_session("s1", cwd="/proj/x")],
        )
        s = store.query_session("s1")
        assert s is not None
        assert s.session_id == "s1"
        assert s.cwd == "/proj/x"
        assert {t.dedup_key for t in s.turns} == {"a", "b"}

    finally:
        store.close()


def test_query_session_returns_none_for_missing(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        assert store.query_session("nope") is None
    finally:
        store.close()


def test_forget_session_drops_turns_and_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s2")],
            [_session("s1"), _session("s2")],
        )
        dropped = store.forget(session_ids=["s1"])
        assert dropped == (1, 1)
        assert store._con.execute(
            "SELECT session_id FROM sessions ORDER BY session_id"
        ).fetchall() == [("s2",)]
        assert store._con.execute("SELECT dedup_key FROM turns").fetchall() == [("b",)]
    finally:
        store.close()


def test_forget_project_drops_all_matching(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s2"), _turn("c", "s3")],
            [_session("s1", cwd="/proj/x"), _session("s2", cwd="/proj/x"), _session("s3", cwd="/proj/y")],
        )
        dropped = store.forget(cwd="/proj/x")
        assert dropped == (2, 2)
        assert store._con.execute("SELECT cwd FROM sessions").fetchall() == [("/proj/y",)]
    finally:
        store.close()


def test_forget_older_than_drops_old_turns_only(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recent = datetime(2026, 5, 1, tzinfo=timezone.utc)
        store.flush(
            [
                _turn("old1", "s1", ts=old),
                _turn("old2", "s2", ts=old),
                _turn("recent", "s1", ts=recent),
            ],
            [_session("s1"), _session("s2")],
        )
        cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
        dropped = store.forget(older_than=cutoff)
        # s2 fully gone (1 session dropped); 2 turns total dropped (old1, old2)
        assert dropped == (1, 2)

        # s1 retained with only its recent turn; first_ts/turn_count refreshed.
        srows = store._con.execute(
            "SELECT session_id, first_ts, turn_count FROM sessions"
        ).fetchall()
        assert srows == [("s1", datetime(2026, 5, 1), 1)]
        assert store._con.execute(
            "SELECT dedup_key FROM turns ORDER BY dedup_key"
        ).fetchall() == [("recent",)]
    finally:
        store.close()


def test_forget_all_wipes_store(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("a", "s1")], [_session("s1")])
        dropped = store.forget(all=True)
        assert dropped == (1, 1)
        assert store._con.execute("SELECT COUNT(*) FROM turns").fetchone() == (0,)
        assert store._con.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        # Schema/meta retained.
        assert store._con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
    finally:
        store.close()


def test_forget_requires_exactly_one_kind(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        with pytest.raises(ValueError, match="exactly one"):
            store.forget()
        with pytest.raises(ValueError, match="exactly one"):
            store.forget(session_ids=["x"], cwd="/p")
    finally:
        store.close()


def test_forget_empty_session_list_is_noop(tmp_path: Path) -> None:
    """forget(session_ids=[]) is a deliberate no-op so callers can pass empty filter results."""
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("a", "s1")], [_session("s1")])
        # Empty list means "no sessions matched the filter" — return (0, 0), don't raise.
        assert store.forget(session_ids=[]) == (0, 0)
        # Original data untouched.
        assert store._con.execute("SELECT COUNT(*) FROM turns").fetchone() == (1,)
        assert store._con.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)
    finally:
        store.close()


def test_flush_chunks_large_batch_without_oom(tmp_path: Path) -> None:
    """Regression: a single ~90k-row INSERT ON CONFLICT with JSON columns
    OOM'd at >24 GiB on a real corpus. The fix chunks per FLUSH_CHUNK_SIZE
    and commits per chunk so DuckDB releases memory between batches. This
    test crosses several chunk boundaries to exercise that path."""
    n = FLUSH_CHUNK_SIZE * 3 + 7  # spans 4 chunks; the +7 catches off-by-one
    base_ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    turns = [
        _turn(f"k-{i:06d}", f"s-{i % 5}", ts=base_ts + timedelta(seconds=i))
        for i in range(n)
    ]
    sessions = [_session(f"s-{i}") for i in range(5)]

    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(turns, sessions)
        assert store._con.execute("SELECT COUNT(*) FROM turns").fetchone() == (n,)
        # Denormalized counts must agree with the chunked-insert outcome.
        per_session = dict(store._con.execute(
            "SELECT session_id, turn_count FROM sessions ORDER BY session_id"
        ).fetchall())
        assert per_session == {f"s-{i}": sum(1 for j in range(n) if j % 5 == i) for i in range(5)}

        # ON CONFLICT DO NOTHING keeps re-flush idempotent across chunk splits.
        store.flush(turns, sessions)
        assert store._con.execute("SELECT COUNT(*) FROM turns").fetchone() == (n,)
    finally:
        store.close()
