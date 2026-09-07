"""The warm cache lives while it is used and is freed when it is not.

After hydrating only the part of the store below the hot cutoff, the warm set
cannot change for the life of the process except through forget. A
time-to-live therefore bought nothing but a rebuild every two minutes while the
dashboard was open — with the old list still referenced during the build, which
is where the process's peak RSS came from. Cache on use, release on idle, and
clear on forget.

Note on scope: `/api/daily` reaches the warm merge and the main dashboard polls
it continuously, so on a real server with a tab open the cache stays in use and
the release never fires. The unconditional win here is removing the periodic
rebuild; the release reclaims memory only once every tab is closed.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import seed_history_store

import tokenol.serve.state as _state_mod


def _app_with_store(tmp_path, monkeypatch, *, persist: bool = False):
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=10)
    return create_app(ServerConfig(persist=persist), prefs_path=tmp_path / "prefs.json")


def _count_hydrations(store) -> list[int]:
    calls = [0]
    real = store.hydrate_before

    def counting(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)

    store.hydrate_before = counting
    return calls


@pytest.mark.asyncio
async def test_cache_is_released_after_idle_and_rebuilt_on_next_use(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    import tokenol.serve.app as app_mod

    monkeypatch.setattr(app_mod, "_WARM_TIER_IDLE_SECONDS", 0.2)
    app = _app_with_store(tmp_path, monkeypatch)
    calls = _count_hydrations(app.state.warm_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.get("/api/breakdown/summary?range=all")
        assert calls[0] == 1
        assert app.state.warm_tier_cache is not None

        await asyncio.sleep(0.4)
        assert app.state.warm_tier_cache is None, "idle cache was not released"
        assert app.state.warm_merged is None

        await client.get("/api/breakdown/summary?range=all")
        assert calls[0] == 2, "a request after release must rebuild"


@pytest.mark.asyncio
async def test_use_inside_the_idle_window_keeps_the_cache_without_rebuilding(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    import tokenol.serve.app as app_mod

    monkeypatch.setattr(app_mod, "_WARM_TIER_IDLE_SECONDS", 0.3)
    app = _app_with_store(tmp_path, monkeypatch)
    calls = _count_hydrations(app.state.warm_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Three uses 0.2s apart: 0.4s total, longer than the window, but never
        # idle for a whole 0.3s. The old TTL would have rebuilt; idle must not.
        for _ in range(3):
            await client.get("/api/breakdown/summary?range=all")
            await asyncio.sleep(0.2)
    assert calls[0] == 1, "cache was rebuilt while in continuous use"


@pytest.mark.asyncio
async def test_forget_clears_the_warm_cache(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from datetime import datetime, timezone

    from httpx import ASGITransport, AsyncClient

    from tokenol.persistence.forget_handoff import ForgetRequest, submit_forget_request

    app = _app_with_store(tmp_path, monkeypatch, persist=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.get("/api/breakdown/summary?range=all")
        assert app.state.warm_tier_cache is not None

        submit_forget_request(
            ForgetRequest(kind="session", value="warm-sess", submitted_at=datetime.now(tz=timezone.utc))
        )
        await app.state.broadcaster.process_pending_forget()

        assert app.state.warm_tier_cache is None, "forget left forgotten turns cached"
        assert app.state.warm_merged is None
        r = await client.get("/api/breakdown/summary?range=all")
    assert r.json()["turns"] == 0
