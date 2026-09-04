"""Without --persist, the snapshot must still derive incrementally.

`build_snapshot_full` has two derivation paths. The store-backed one parses only
files whose mtime moved and derives just the new turns. The fallback one re-derives
every turn from every cached event, memoised on a frozenset of every JSONL file's
(path, size, mtime).

That memo key changes whenever any single file changes, so on a machine with many
active sessions it misses on essentially every tick. Measured 2026-09-04 on a real
corpus: 3,897 files, 258,856 turns, 2.4-5.3s of CPU per miss against a 5s tick —
one saturated core, sustained, for a server nobody had asked to do any work.

The path was selected by `config.persist` rather than by whether a store exists,
so the cheap path was reachable only by also turning on the writer. These tests
pin the selection to store availability instead.
"""

from __future__ import annotations

from conftest import seed_history_store


def test_existing_store_enables_incremental_derivation_without_persist(tmp_path, monkeypatch):
    """A readable store must put the broadcaster on the incremental path."""
    from tokenol.serve.app import ServerConfig, create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tokenol").mkdir(parents=True)
    seed_history_store(tmp_path / ".tokenol" / "history.duckdb", days_ago=5, turns=10)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")

    assert app.state.warm_store is not None, "read-only store should be open"
    assert app.state.broadcaster._history_store is app.state.warm_store, (
        "without --persist the broadcaster still fell back to full re-derivation"
    )
    assert app.state.broadcaster._flush_queue is None, "no writer may be started without --persist"


def test_hot_window_is_set_on_the_readonly_store(tmp_path, monkeypatch):
    """_store_backed_derivation reads _hot_window_days off the store as a duck-typed attr.

    Left unset it silently defaults to 90 days, so the hot tier would differ between
    persist and non-persist mode for no stated reason.
    """
    from tokenol.serve.app import ServerConfig, create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tokenol").mkdir(parents=True)
    seed_history_store(tmp_path / ".tokenol" / "history.duckdb", days_ago=5, turns=5)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    assert getattr(app.state.warm_store, "_hot_window_days", None) == app.state.prefs.hot_window_days


def test_no_store_keeps_the_opt_in_contract(tmp_path, monkeypatch):
    """With no store on disk nothing changes: no writable store, no writer."""
    from tokenol.serve.app import ServerConfig, create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")

    assert app.state.history_store is None
    assert app.state.flush_queue is None
    assert app.state.warm_store is None
    assert app.state.broadcaster._history_store is None
