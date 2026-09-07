"""A turn already in the store is never rebuilt, whatever its age.

The dedup set used to be seeded from the hot window only, so on every cold
start each turn older than the window (96,867 on the live store) was derived
again from JSONL, appended to the hot tier, queued, and rejected by the
database as a key conflict. Seeding from every persisted key stops the rebuild
at the first check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod
from tokenol.serve.state import ParseCache, build_snapshot_full

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def store_with_old_persisted_copy(tmp_path, monkeypatch):
    """basic.jsonl on disk AND its two turns already in the store, dated 200 days back."""
    pytest.importorskip("duckdb")
    from tokenol.ingest.parser import parse_file
    from tokenol.model.events import Session
    from tokenol.persistence.store import HistoryStore
    from tokenol.serve.state import derive_delta_turns

    (tmp_path / "projects").mkdir(parents=True)
    src = tmp_path / "projects" / "sess-001.jsonl"
    src.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    turns, sessions, _, _ = derive_delta_turns(list(parse_file(src)), set(), set())
    old = datetime.now(tz=timezone.utc) - timedelta(days=200)
    for i, t in enumerate(turns):
        t.timestamp = old + timedelta(seconds=i)
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True)
    store = HistoryStore(db)
    store.flush(
        turns,
        [Session(session_id=s.session_id, source_file=str(src), is_sidechain=False, cwd="/x", turns=[]) for s in sessions],
    )
    store._hot_window_days = 90
    yield store, {t.dedup_key for t in turns}
    store.close()


def test_persisted_turns_older_than_the_window_are_not_rebuilt(store_with_old_persisted_copy) -> None:
    store, persisted_keys = store_with_old_persisted_copy
    from tokenol.persistence.flusher import FlushQueue

    queue = FlushQueue(store)
    cache = ParseCache()

    build_snapshot_full(cache, history_store=store, flush_queue=queue)

    assert persisted_keys <= cache._known_dedup_keys, "store keys were not seeded into the dedup set"
    assert not any(t.dedup_key in persisted_keys for t in cache._hot_turns), "a persisted turn was rebuilt into the hot tier"
    assert queue.pending_count() == 0, "a persisted turn was queued for flush again"
