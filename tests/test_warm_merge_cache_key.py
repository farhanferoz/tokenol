"""The warm merge must be reused across ticks when the data has not changed.

`_snapshot_with_warm_tier` dedups the hot and warm turn lists and sorts the union
— on this corpus ~350,000 turns — then caches the result. It keyed that cache on
the snapshot's `generated_at`, which `build_snapshot_full` regenerates on every
tick. With a 5-second tick, the cache could never survive one, so every breakdown
request paid for a full re-sort of the union no matter how little had changed.

The key has to identify the DATA, not the build. These tests pin that: a new
`generated_at` over an unchanged turn set must reuse the merge, while an actually
changed turn set must not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import seed_history_store


def _hot_turn(key: str):
    from tokenol.model.events import Turn, Usage

    usage = Usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return Turn(
        dedup_key=key,
        timestamp=datetime.now(tz=timezone.utc),
        session_id="hot-sess",
        model="claude-opus-4-8",
        usage=usage,
        is_sidechain=False,
        stop_reason="end_turn",
        cost_usd=0.001,
    )


def _app_with_store(tmp_path, monkeypatch):
    from tokenol.serve import state as _state_mod
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=20)
    return create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")


@pytest.mark.asyncio
async def test_merge_survives_a_tick_when_data_is_unchanged(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from tokenol.serve.app import _snapshot_with_warm_tier
    from tokenol.serve.state import SnapshotResult

    app = _app_with_store(tmp_path, monkeypatch)
    app.state.broadcaster = None  # force the app-state fallback path

    turns = [_hot_turn("hot-1")]
    app.state.snapshot_result = SnapshotResult(payload={"generated_at": "t1"}, turns=turns, sessions=[])
    request = SimpleNamespace(app=app)

    first = await _snapshot_with_warm_tier(request)
    # A new tick: same turns, new stamp — exactly what happens every 5 seconds.
    app.state.snapshot_result = SnapshotResult(payload={"generated_at": "t2"}, turns=turns, sessions=[])
    second = await _snapshot_with_warm_tier(request)

    assert second is first, "merge was recomputed across a tick despite unchanged turns"


@pytest.mark.asyncio
async def test_merge_is_recomputed_when_turns_actually_change(tmp_path, monkeypatch) -> None:
    """The cache must not be so sticky that new turns are missed."""
    pytest.importorskip("duckdb")
    from tokenol.serve.app import _snapshot_with_warm_tier
    from tokenol.serve.state import SnapshotResult

    app = _app_with_store(tmp_path, monkeypatch)
    app.state.broadcaster = None

    turns = [_hot_turn("hot-1")]
    app.state.snapshot_result = SnapshotResult(payload={"generated_at": "t1"}, turns=turns, sessions=[])
    request = SimpleNamespace(app=app)
    first = await _snapshot_with_warm_tier(request)

    grown = [*turns, _hot_turn("hot-2")]
    app.state.snapshot_result = SnapshotResult(payload={"generated_at": "t2"}, turns=grown, sessions=[])
    second = await _snapshot_with_warm_tier(request)

    assert second is not first, "a new turn did not invalidate the merge"
    assert len(second.turns) > len(first.turns)
