"""Tests for tokenol.persistence.store.HistoryStore."""

from __future__ import annotations

from pathlib import Path

import duckdb

from tokenol.persistence.store import HistoryStore


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
