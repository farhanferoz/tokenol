"""One warm-tier hydration at a time, however many requests arrive together.

`_warm_tier` caches the hydrated store for `_WARM_TIER_TTL_SECONDS`, but the
check and the fill are not guarded, so every request arriving while a hydration
is in flight starts its own. The breakdown page fires six endpoints at once, so
a single page load can hydrate the whole store six times over.

Measured 2026-09-04 on the real store: 91,131 rows, ~2s per hydration, and
`hydrate_hot` accounted for 22% of server CPU with a browser attached — against
a 120s TTL that should have permitted one hydration every two minutes.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import seed_history_store


@pytest.mark.asyncio
async def test_concurrent_requests_hydrate_the_store_once(tmp_path, monkeypatch) -> None:
    """Six simultaneous breakdown requests must trigger exactly one hydration."""
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve import state as _state_mod
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=40)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    store = app.state.warm_store
    assert store is not None

    # Count warm-tier hydrations only. The derivation path hydrates the hot
    # window through hydrate_hot at startup; that one is not what this test is about.
    calls = 0
    real = store.hydrate_before

    def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return real(*a, **kw)

    store.hydrate_before = counting

    endpoints = [
        "/api/breakdown/summary?range=all",
        "/api/breakdown/by-model?range=all",
        "/api/breakdown/by-project?range=all",
        "/api/breakdown/tools?range=all",
        "/api/breakdown/daily-tokens?range=all",
        "/api/daily?range=all",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        results = await asyncio.gather(*(client.get(e) for e in endpoints))
    for r in results:
        assert r.status_code == 200

    assert calls == 1, f"{len(endpoints)} concurrent requests caused {calls} warm-tier hydrations"
