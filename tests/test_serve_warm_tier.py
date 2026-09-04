"""Warm-tier merge: every endpoint answering a historical question must read it.

The hot tier is only ever as complete as the JSONL still on disk, and Claude
Code prunes old transcripts. Before this was wired up, the whole Breakdown page
read the hot tier alone, so `range=all` silently meant "all the history that
happens to survive on disk" — measured on a real store, April 2026 read $790
from JSONL against $6,513 of persisted turns for the same month.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Endpoints that answer a question about the past. Each must fold in the warm
# tier; a new one added without the merge is the regression this guards.
HISTORICAL_ENDPOINTS = [
    "/api/breakdown/summary?range=all",
    "/api/breakdown/daily-tokens?range=all",
    "/api/breakdown/by-project?range=all",
    "/api/breakdown/by-model?range=all",
    "/api/breakdown/tools?range=all",
    "/api/breakdown/skills?range=all",
    "/api/daily?range=all",
]


def _seed_store(db_path: Path, *, days_ago: int, turns: int) -> float:
    """Write `turns` persisted turns dated `days_ago` back. Returns total cost."""
    from tokenol.metrics.cost import cost_for_turn
    from tokenol.model.events import Session, Turn, Usage
    from tokenol.persistence.store import HistoryStore

    ts = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    made = [
        Turn(
            dedup_key=f"warm-{i}",
            timestamp=ts + timedelta(seconds=i),
            session_id="warm-sess",
            model="claude-opus-4-8",
            usage=Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0),
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=cost_for_turn("claude-opus-4-8", Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0)).total_usd,
        )
        for i in range(turns)
    ]
    session = Session(session_id="warm-sess", source_file="", is_sidechain=False, cwd="/dev/archived", turns=made)
    store = HistoryStore(db_path)
    store.flush(made, [session])
    total = sum(t.cost_usd for t in made)
    store.close()
    return total


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", HISTORICAL_ENDPOINTS)
async def test_historical_endpoints_read_the_warm_tier(endpoint, tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    warm_cost = _seed_store(db, days_ago=200, turns=40)
    assert warm_cost > 0

    app = create_app(ServerConfig(persist=True), prefs_path=tmp_path / "prefs.json")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        merged = await client.get(endpoint)
    assert merged.status_code == 200, f"{endpoint} -> {merged.status_code}"

    # Same request with no store wired. The warm turns are 200 days old, well
    # outside anything the JSONL fixture covers, so a response that reads the
    # warm tier cannot be byte-identical to one that does not. Comparing the two
    # keeps this honest for every response shape, including ones where the warm
    # rows surface only as a token or cost total.
    app_hot = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs_hot.json")
    async with AsyncClient(transport=ASGITransport(app=app_hot), base_url="http://t") as client:
        hot = await client.get(endpoint)
    assert hot.status_code == 200

    assert merged.text != hot.text, f"{endpoint} ignores the warm tier — identical to the hot-only response"


@pytest.mark.asyncio
async def test_warm_turns_are_counted_not_just_present(tmp_path, monkeypatch) -> None:
    """The summary's turn count and cost must actually include warm-tier rows."""
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    warm_cost = _seed_store(db, days_ago=200, turns=40)

    app = create_app(ServerConfig(persist=True), prefs_path=tmp_path / "prefs.json")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        merged = (await client.get("/api/breakdown/summary?range=all")).json()

    app_hot = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs_hot.json")
    async with AsyncClient(transport=ASGITransport(app=app_hot), base_url="http://t") as client:
        hot = (await client.get("/api/breakdown/summary?range=all")).json()

    assert merged["turns"] == hot["turns"] + 40, f"{merged['turns']} vs hot {hot['turns']} + 40 warm"
    assert merged["cost_usd"] == pytest.approx(hot["cost_usd"] + warm_cost, rel=1e-6)


@pytest.mark.asyncio
async def test_warm_merge_is_a_noop_without_a_store(tmp_path, monkeypatch) -> None:
    """Default mode (no --persist) must behave exactly as before: hot tier only."""
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    assert app.state.history_store is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/api/breakdown/summary?range=all")
    assert resp.status_code == 200
    assert resp.json()["turns"] > 0
