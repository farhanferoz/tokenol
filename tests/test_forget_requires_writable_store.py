"""A forget request must never be consumed by a server that cannot act on it.

`process_pending_forget` gated on "is there a store?", which stopped meaning "can
we write?" once a plain `tokenol serve` began opening the history store read-only
for its derivation path. `take_forget_request()` unlinks the request file before
the store is touched, so on a read-only server the request was destroyed and the
delete then failed inside DuckDB, swallowed by the hook's broad except. The user
would see a delete that reported nothing and did nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import seed_history_store


def test_forget_refuses_a_read_only_store(tmp_path: Path) -> None:
    """The refusal must happen at the boundary, not deep inside DuckDB."""
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "history.duckdb"
    seed_history_store(db, days_ago=200, turns=5)
    store = HistoryStore(db, read_only=True)
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            store.forget(all=True)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_read_only_server_leaves_the_request_file_alone(tmp_path, monkeypatch) -> None:
    """Without a writer, the hook must not consume the pending request."""
    pytest.importorskip("duckdb")
    from tokenol.persistence import forget_handoff
    from tokenol.serve import state as _state_mod
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=5)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    assert app.state.warm_store is not None
    assert app.state.flush_queue is None

    req = forget_handoff.request_path()
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text('{"kind": "all", "value": null, "submitted_at": "2026-09-04T00:00:00+00:00"}')

    await app.state.broadcaster.process_pending_forget()

    assert req.exists(), "read-only server consumed a forget request it could not honour"
