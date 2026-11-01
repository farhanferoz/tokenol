"""Without --persist, the server still reads the existing store and derives incrementally.

The usage engine is opened from whatever history store exists, whether or not the
writer is on. The snapshot then ingests only the JSONL files that changed (the old
fallback re-derived every turn on every tick: 2.4-5.3 s of CPU per miss, measured
2026-09-04 on 258,856 turns, against a 5 s tick). These tests pin the selection to
store availability, not to `--persist`.
"""

from __future__ import annotations

from conftest import seed_history_store


def test_existing_store_is_read_into_the_engine_without_persist(tmp_path, monkeypatch):
    """A readable store must be loaded into the engine, with no writer started."""
    from tokenol.engine.engine import EngineMode
    from tokenol.serve.app import ServerConfig, create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tokenol").mkdir(parents=True)
    seed_history_store(tmp_path / ".tokenol" / "history.duckdb", days_ago=5, turns=10)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")

    engine = app.state.usage.engine
    assert engine.mode is EngineMode.READ_ONLY_STORE
    assert len(engine.session_turns("warm-sess")) == 10, "the stored turns were not loaded"
    assert app.state.parse_cache.usage is app.state.usage
    assert app.state.broadcaster._history_store is None, "no store handle may be open without --persist"
    assert app.state.broadcaster._flush_queue is None, "no writer may be started without --persist"


def test_no_store_keeps_the_opt_in_contract(tmp_path, monkeypatch):
    """With no store on disk nothing changes: no writable store, no writer, an empty engine."""
    from tokenol.serve.app import ServerConfig, create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")

    assert app.state.history_store is None
    assert app.state.flush_queue is None
    assert app.state.usage.engine.session_infos() == []
    assert app.state.broadcaster._history_store is None
