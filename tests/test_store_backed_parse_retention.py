"""On the store-backed path, parsed events must not stay resident.

`_store_backed_derivation` reads a file's events once, derives the delta turns,
and never reads that event list again: a changed file is re-parsed under a new
key, and session drill-down re-opens the JSONL from disk. Yet it went through
`ParseCache.get_or_parse`, which retains the whole event list for the life of
the process. On the first tick every file on disk is an edge file, so a server
with a store held every RawEvent of the entire corpus for nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import seed_history_store

from tokenol.serve.state import ParseCache, build_snapshot_full

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def store_and_jsonl(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True)
    seed_history_store(db, days_ago=5, turns=3)
    store = HistoryStore(db)
    store._hot_window_days = 90
    yield store, tmp_path
    store.close()


def test_store_backed_build_leaves_parse_cache_empty(store_and_jsonl, monkeypatch) -> None:
    store, home = store_and_jsonl
    import tokenol.serve.state as _state_mod

    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [home])
    cache = ParseCache()

    result = build_snapshot_full(cache, history_store=store)

    assert result.turns, "fixture turns were not derived"
    assert cache.size == 0, f"store-backed path retained {cache.size} parsed file(s)"


def test_store_backed_build_still_derives_and_marks_files(store_and_jsonl, monkeypatch) -> None:
    """Not caching must not cost the delta derivation or the mtime bookkeeping."""
    store, home = store_and_jsonl
    import tokenol.serve.state as _state_mod

    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [home])
    cache = ParseCache()

    first = build_snapshot_full(cache, history_store=store)
    hot_before = len(cache._hot_turns)
    marks = dict(cache._last_mtime_ns_by_path)
    assert marks, "edge-file mtimes were not recorded"

    second = build_snapshot_full(cache, history_store=store)
    assert len(cache._hot_turns) == hot_before, "an unchanged file was re-derived"
    assert len(second.turns) == len(first.turns)
    assert cache.size == 0
