"""The warm tier must hydrate only what the hot tier does not already hold.

`_warm_tier` used to hydrate the whole store (a 100,000-day window) and then
`_snapshot_with_warm_tier` discarded every row whose dedup_key the hot tier
already had. The hot tier holds everything at or after its startup cutoff, so
every row in that range was built into a Turn object only to be thrown away —
on a --persist server that is the entire last 90 days, twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import seed_history_store

import tokenol.serve.state as _state_mod


def _app_with_store(tmp_path, monkeypatch):
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=40, session_id="warm-old")
    seed_history_store(db, days_ago=5, turns=7, session_id="warm-recent")
    return create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")


@pytest.mark.asyncio
async def test_warm_tier_hydrates_only_rows_older_than_the_hot_cutoff(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    app = _app_with_store(tmp_path, monkeypatch)
    store = app.state.warm_store
    seen: list[datetime] = []
    real = store.hydrate_before

    def spy(cutoff):
        seen.append(cutoff)
        return real(cutoff)

    store.hydrate_before = spy

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # _snapshot_with_warm_tier calls _current_snapshot_result first, which
        # builds the snapshot (running the store-backed derivation and setting
        # _hot_cutoff) before it asks for the warm tier. No priming needed.
        r = await client.get("/api/breakdown/summary?range=all")
    assert r.status_code == 200
    assert len(seen) == 1, f"expected one before-cutoff hydration, got {len(seen)}"

    hot_cutoff = app.state.parse_cache._hot_cutoff
    assert seen[0] == hot_cutoff, "warm hydration must use the exact cutoff the hot tier was hydrated at"
    assert abs((datetime.now(tz=timezone.utc) - timedelta(days=90)) - hot_cutoff) < timedelta(minutes=5)

    # Completeness: both seeded sessions are visible through the merged view.
    # The summary payload carries a top-level "turns" count (verified against the
    # live endpoint: {"range":"all","sessions":…,"turns":…,…}).
    assert r.json()["turns"] == 47


@pytest.mark.asyncio
async def test_warm_tier_falls_back_to_the_pref_window_without_a_hot_cutoff(tmp_path, monkeypatch) -> None:
    """With no derivation yet, _warm_tier must still hydrate, using the pref window.

    Unreachable over HTTP (every endpoint builds a snapshot first), so the
    function is called directly with the app's state. The fallback exists so a
    future caller that does not build a snapshot first cannot get an empty warm
    tier; overlap with a later hot tier is deduplicated by the merge.
    """
    pytest.importorskip("duckdb")
    from types import SimpleNamespace

    from tokenol.serve.app import _warm_tier

    app = _app_with_store(tmp_path, monkeypatch)
    assert not hasattr(app.state.parse_cache, "_hot_cutoff")
    store = app.state.warm_store
    seen: list[datetime] = []
    real = store.hydrate_before

    def spy(cutoff):
        seen.append(cutoff)
        return real(cutoff)

    store.hydrate_before = spy

    turns, _sessions = await _warm_tier(SimpleNamespace(app=app))

    assert len(seen) == 1
    expected = datetime.now(tz=timezone.utc) - timedelta(days=app.state.prefs.hot_window_days)
    assert abs(seen[0] - expected) < timedelta(minutes=5)
    # 200-day-old rows are below the 90-day fallback cutoff; 5-day-old rows are not.
    assert {t.session_id for t in turns} == {"warm-old"}


def _app_with_nested_projects(tmp_path, monkeypatch):
    """A warm-only session at /dev/proj and a hot one at /dev/proj/backend.

    Project grouping rolls a nested cwd up to its shortest ancestor across every
    known session, so the hot session's project label depends on the warm one.
    """
    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])
    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    seed_history_store(db, days_ago=200, turns=4, session_id="old", cwd="/dev/proj")
    seed_history_store(db, days_ago=5, turns=3, session_id="new", cwd="/dev/proj/backend")
    return create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")


@pytest.mark.asyncio
async def test_range_inside_the_hot_window_builds_no_warm_turns_and_answers_the_same(tmp_path, monkeypatch) -> None:
    """A 30-day request never builds warm Turns, yet grouping and earliest date are unchanged.

    Every warm row predates the hot cutoff, so a 30-day filter drops all of them;
    the dashboard polls exactly such a request every minute, which kept the whole
    warm tier resident. What must survive the skip: the warm session's cwd (the
    hot session still groups under /dev/proj) and the oldest date held (the
    daily panel's `earliest_available`, read by the UI's range picker).
    """
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    app = _app_with_nested_projects(tmp_path, monkeypatch)
    calls: list[datetime] = []
    real = app.state.warm_store.hydrate_before
    app.state.warm_store.hydrate_before = lambda cutoff: calls.append(cutoff) or real(cutoff)
    oldest = (datetime.now(tz=timezone.utc) - timedelta(days=200)).date().isoformat()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        daily = (await client.get("/api/daily?range=30d&metric=cost&project=all&model=all")).json()
        by_project = (await client.get("/api/breakdown/by-project?range=30d")).json()
        for endpoint in ("summary", "daily-tokens", "by-model", "tools", "skills"):
            assert (await client.get(f"/api/breakdown/{endpoint}?range=30d")).status_code == 200
        assert calls == [], "a range inside the hot window must not build warm Turns"

        assert daily["earliest_available"] == oldest
        assert "note" not in daily
        assert [p["value"] for p in daily["active_projects"]] == ["/dev/proj"]
        assert [p["cwd"] for p in by_project["projects"]] == ["/dev/proj"]

        # A compare view ranks series over every turn, so it still needs them all.
        assert (await client.get("/api/daily?range=30d&metric=cost&project=compare&model=all")).status_code == 200
        assert len(calls) == 1
