"""On the store-backed path, events are derived one file at a time.

The first tick treats every file on disk as an edge file. Accumulating all of
their events before deriving once put the entire corpus of RawEvents in memory
at the same moment: ~1.4-2 GB on a 5,120-file corpus, about half of which the
allocator never gives back. Deriving per file bounds the peak to the largest
single file. Results cannot differ: a dedup_key never spans two files (measured
2026-09-07 on 1,000 files, 0 of 56,626 keys), and within a file the batch is
unchanged, so within-file last-wins still holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import seed_history_store

import tokenol.serve.state as _state_mod
from tokenol.serve.state import ParseCache, build_snapshot_full

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# basic.jsonl: 2 turns / 1 session. multi.jsonl: 4 turns / 3 sessions.
# dedup.jsonl: 2 assistant events sharing one key -> 1 turn (within-file last-wins).
FIXTURE_FILES = ("basic.jsonl", "multi.jsonl", "dedup.jsonl")
EXPECTED_TURNS = 2 + 4 + 1
EXPECTED_SESSIONS = 1 + 3 + 1


@pytest.fixture
def store_and_three_files(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    (tmp_path / "projects").mkdir(parents=True)
    for name in FIXTURE_FILES:
        (tmp_path / "projects" / name).write_bytes((FIXTURES_DIR / name).read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True)
    seed_history_store(db, days_ago=200, turns=3)
    store = HistoryStore(db)
    store._hot_window_days = 90
    yield store
    store.close()


def test_derivation_runs_once_per_edge_file(store_and_three_files, monkeypatch) -> None:
    store = store_and_three_files
    batches: list[int] = []
    real = _state_mod.derive_delta_turns

    def spy(new_events, *a, **kw):
        batches.append(len({ev.source_file for ev in new_events}))
        return real(new_events, *a, **kw)

    monkeypatch.setattr(_state_mod, "derive_delta_turns", spy)

    result = build_snapshot_full(ParseCache(), history_store=store)

    assert len(batches) == len(FIXTURE_FILES), f"expected one derivation per file, got batches={batches}"
    assert all(n == 1 for n in batches), f"a batch mixed events from several files: {batches}"
    # Same answer as the whole-batch derivation gave.
    live = [t for t in result.turns if not t.session_id.startswith("warm-")]
    assert len(live) == EXPECTED_TURNS
    assert len({t.session_id for t in live}) == EXPECTED_SESSIONS


def test_store_backed_snapshot_excludes_non_claude_models(store_and_three_files, tmp_path) -> None:
    """The per-file path derives through derive_delta_turns, which must apply the Claude-only rule too."""
    store = store_and_three_files
    (tmp_path / "projects" / "gemini.jsonl").write_bytes((FIXTURES_DIR / "gemini.jsonl").read_bytes())

    result = build_snapshot_full(ParseCache(), history_store=store)

    assert "gemini-3-flash" not in {t.model for t in result.turns}
    live = [t for t in result.turns if not t.session_id.startswith("warm-")]
    assert len(live) == EXPECTED_TURNS


def test_per_file_derivation_enqueues_once_per_tick(store_and_three_files, monkeypatch) -> None:
    """The flusher's count threshold must see the tick's deltas as one batch."""
    store = store_and_three_files
    from tokenol.persistence.flusher import FlushQueue

    queue = FlushQueue(store)
    calls: list[int] = []
    real = queue.enqueue

    def counting(turns, sessions):
        calls.append(len(turns))
        return real(turns, sessions)

    monkeypatch.setattr(queue, "enqueue", counting)

    build_snapshot_full(ParseCache(), history_store=store, flush_queue=queue)

    assert calls == [EXPECTED_TURNS], f"expected one enqueue of {EXPECTED_TURNS} turns, got {calls}"


def test_a_file_that_fails_to_parse_does_not_lose_the_others(store_and_three_files, monkeypatch, tmp_path) -> None:
    store = store_and_three_files
    real = _state_mod.parse_file

    def flaky(path):
        if path.name == "multi.jsonl":
            raise OSError("simulated read failure")
        return real(path)

    monkeypatch.setattr(_state_mod, "parse_file", flaky)
    cache = ParseCache()

    result = build_snapshot_full(cache, history_store=store)

    live = [t for t in result.turns if not t.session_id.startswith("warm-")]
    assert len(live) == EXPECTED_TURNS - 4
    assert tmp_path / "projects" / "multi.jsonl" not in cache._last_mtime_ns_by_path, "a failed file must not be marked as parsed"
