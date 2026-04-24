"""Integration tests for the FastAPI app: start server, hit /api/snapshot."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod
from tokenol.serve.app import ServerConfig, create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@contextmanager
def _mock_dirs(tmp_path: Path):
    original = _state_mod.get_config_dirs
    _state_mod.get_config_dirs = lambda all_projects=False: [tmp_path]
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original


@pytest.mark.asyncio
async def test_snapshot_endpoint_returns_200(tmp_path: Path) -> None:
    """GET /api/snapshot returns 200 + valid JSON snapshot."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/snapshot")

    assert resp.status_code == 200
    data = resp.json()
    for key in ["generated_at", "config", "thresholds", "period", "topbar_summary", "tiles", "models"]:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_prefs_endpoint_updates_config(tmp_path: Path) -> None:
    """POST /api/prefs updates tick_seconds and reference_usd."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig(tick_seconds=5, reference_usd=50.0), prefs_path=tmp_path / "prefs.json")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/prefs", json={"tick_seconds": 10, "reference_usd": 25.0})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tick_seconds"] == 10
    assert body["reference_usd"] == 25.0


@pytest.mark.asyncio
async def test_prefs_get_endpoint(tmp_path: Path) -> None:
    """GET /api/prefs returns current prefs shape."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig(), prefs_path=tmp_path / "prefs.json")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/prefs")

    assert resp.status_code == 200
    body = resp.json()
    for key in ("tick_seconds", "reference_usd", "thresholds"):
        assert key in body


@pytest.mark.asyncio
async def test_prefs_post_unknown_key(tmp_path: Path) -> None:
    """POST /api/prefs with unknown key → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig(), prefs_path=tmp_path / "prefs.json")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/prefs", json={"unknown_field": 99})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_prefs_post_bad_type(tmp_path: Path) -> None:
    """POST /api/prefs with bad type → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig(), prefs_path=tmp_path / "prefs.json")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/prefs", json={"tick_seconds": "not_a_number"})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_session_detail_404_unknown(tmp_path: Path) -> None:
    """GET /api/session/<unknown> returns 404."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/session/nonexistent-session-id")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_index_page_returns_html(tmp_path: Path) -> None:
    """GET / returns the index HTML page."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hourly_endpoint_happy_path(tmp_path: Path) -> None:
    """GET /api/hourly/{date} returns expected shape."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/hourly/2026-04-14?metric=hit_pct")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("date", "metric", "y_unit", "series"):
        assert key in data
    assert data["y_unit"] == "percent"
    assert isinstance(data["series"], list)


@pytest.mark.asyncio
async def test_hourly_compare_both_rejects(tmp_path: Path) -> None:
    """project=compare and model=compare simultaneously → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/hourly/2026-04-14?project=compare&model=compare")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_daily_endpoint_happy_path(tmp_path: Path) -> None:
    """GET /api/daily returns expected shape."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/daily?range=7d&metric=cost_per_kw")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("range", "metric", "y_unit", "earliest_available", "series"):
        assert key in data
    assert data["y_unit"] == "usd"


@pytest.mark.asyncio
async def test_daily_insufficient_history(tmp_path: Path) -> None:
    """GET /api/daily?range=90d with only ~8 days of data → 400 insufficient_history."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/daily?range=90d")

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "insufficient_history"
    assert "have_days" in body


@pytest.mark.asyncio
async def test_daily_compare_both_rejects(tmp_path: Path) -> None:
    """project=compare and model=compare simultaneously → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/daily?project=compare&model=compare")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_hourly_active_projects_scoped_to_target_date(tmp_path: Path) -> None:
    """GET /api/hourly/{date}.active_projects lists only cwds active on THAT date."""
    import json
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)

    def _event(sid: str, cwd: str, model: str, ts: datetime, uid: str) -> str:
        sys_ev = json.dumps({
            "type": "system", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
        })
        asst_ev = json.dumps({
            "type": "assistant", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "requestId": f"req-{uid}", "uuid": f"evt-{uid}",
            "isSidechain": False, "model": model,
            "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
        })
        return sys_ev + "\n" + asst_ev + "\n"

    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    # projA active today; projB active only yesterday.
    (proj / "sess-a.jsonl").write_text(_event(
        "sess-a", "/home/u/projA", "claude-opus-4-7",
        now.replace(hour=10, minute=0, second=0, microsecond=0), "a",
    ))
    (proj / "sess-b.jsonl").write_text(_event(
        "sess-b", "/home/u/projB", "claude-opus-4-7",
        (now - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0), "b",
    ))

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            today_resp = await client.get(f"/api/hourly/{today.isoformat()}")
            yest_resp  = await client.get(f"/api/hourly/{yesterday.isoformat()}")

    assert today_resp.status_code == 200
    today_projects = [p["value"] for p in today_resp.json()["active_projects"]]
    assert today_projects == ["/home/u/projA"]

    assert yest_resp.status_code == 200
    yest_projects = [p["value"] for p in yest_resp.json()["active_projects"]]
    assert yest_projects == ["/home/u/projB"]


@pytest.mark.asyncio
async def test_daily_active_projects_scoped_to_range(tmp_path: Path) -> None:
    """GET /api/daily?range=7d.active_projects excludes cwds whose only turns are older than 7 days."""
    import json
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)

    def _event(sid: str, cwd: str, ts: datetime, uid: str) -> str:
        sys_ev = json.dumps({
            "type": "system", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
        })
        asst_ev = json.dumps({
            "type": "assistant", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "requestId": f"req-{uid}", "uuid": f"evt-{uid}",
            "isSidechain": False, "model": "claude-opus-4-7",
            "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
        })
        return sys_ev + "\n" + asst_ev + "\n"

    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    # Spread turns across 10 days to satisfy the 7d range history check.
    for i in range(10):
        (proj / f"sess-recent-{i}.jsonl").write_text(
            _event(f"sess-recent-{i}", "/home/u/recent", now - timedelta(days=i), f"r{i}")
        )
    # Old cwd only has a turn 15 days ago — should not appear for range=7d.
    (proj / "sess-old.jsonl").write_text(_event(
        "sess-old", "/home/u/oldproj", now - timedelta(days=15), "old",
    ))

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_7d  = await client.get("/api/daily?range=7d&metric=cost")
            resp_all = await client.get("/api/daily?range=all&metric=cost")

    assert resp_7d.status_code == 200
    projects_7d = sorted(p["value"] for p in resp_7d.json()["active_projects"])
    assert projects_7d == ["/home/u/recent"]

    # range=all picks up everything, including the 15-day-old project.
    assert resp_all.status_code == 200
    projects_all = sorted(p["value"] for p in resp_all.json()["active_projects"])
    assert projects_all == ["/home/u/oldproj", "/home/u/recent"]


@pytest.mark.asyncio
async def test_daily_explicit_project_list(tmp_path: Path) -> None:
    """GET /api/daily?project=cwdA,cwdB returns one series per named cwd (no ranking, no 'other')."""
    import json
    from datetime import datetime, timedelta, timezone

    base = datetime.now(tz=timezone.utc) - timedelta(days=2)

    def _events(sid: str, cwd: str, model: str, ts: datetime, uid: str) -> str:
        # A system event carries cwd; the assistant event carries the billable turn.
        sys_ev = json.dumps({
            "type": "system", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
        })
        asst_ev = json.dumps({
            "type": "assistant", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": sid, "requestId": f"req-{uid}", "uuid": f"evt-{uid}",
            "isSidechain": False, "model": model,
            "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
        })
        return sys_ev + "\n" + asst_ev + "\n"

    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    # Three sessions, three distinct cwds — request only two.
    (proj / "sess-a.jsonl").write_text(_events("sess-a", "/home/u/projA", "claude-opus-4-7", base, "a"))
    (proj / "sess-b.jsonl").write_text(_events("sess-b", "/home/u/projB", "claude-opus-4-7", base, "b"))
    (proj / "sess-c.jsonl").write_text(_events("sess-c", "/home/u/projC", "claude-opus-4-7", base, "c"))

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/daily?project=/home/u/projA,/home/u/projC&range=all&metric=cost")

    assert resp.status_code == 200
    labels = sorted(s["label"] for s in resp.json()["series"])
    assert labels == ["/home/u/projA", "/home/u/projC"]


@pytest.mark.asyncio
async def test_daily_list_list_rejects(tmp_path: Path) -> None:
    """project=<list> combined with model=<list> is still dual-compare → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/daily?project=a,b&model=x,y")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_models_endpoint(tmp_path: Path) -> None:
    """GET /api/models returns models panel shape."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/models?range=today")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("range", "rows", "aggregate"):
        assert key in data


@pytest.mark.asyncio
async def test_recent_endpoint(tmp_path: Path) -> None:
    """GET /api/recent returns recent activity shape."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/recent?window=4h")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("window", "aggregate", "rows"):
        assert key in data


@pytest.mark.asyncio
async def test_model_detail_endpoint(tmp_path: Path) -> None:
    """GET /api/model/{name} returns model detail or 404."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_ok = await client.get("/api/model/opus-4-7")
            resp_404 = await client.get("/api/model/does-not-exist")

    assert resp_ok.status_code == 200
    data = resp_ok.json()
    for key in ("name", "total_cost", "total_turns", "projects_using_model"):
        assert key in data
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_api_project_invalid_range_returns_400(tmp_path: Path) -> None:
    """GET /api/project/<b64>?range=bogus returns 400."""
    import json

    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.state import encode_cwd

    proj_dir = tmp_path / "projects" / "-repo-proj"
    proj_dir.mkdir(parents=True)
    import datetime
    ts = datetime.date.today().isoformat() + "T10:00:00Z"
    ev = json.dumps({
        "type": "user", "timestamp": ts, "sessionId": "s1", "cwd": "/repo/proj",
        "message": {"role": "user", "content": "hi"},
    })
    asst = json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": "s1",
        "requestId": "req-x", "uuid": "evt-x", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {"id": "msg-x", "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
    })
    (proj_dir / "s1.jsonl").write_text(ev + "\n" + asst + "\n")

    b64 = encode_cwd("/repo/proj")
    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/project/{b64}?range=bogus")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_api_project_range_param_passthrough(tmp_path: Path) -> None:
    """range param is reflected in the response payload."""
    import json

    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.state import encode_cwd

    proj_dir = tmp_path / "projects" / "-repo-proj"
    proj_dir.mkdir(parents=True)
    import datetime
    ts = datetime.date.today().isoformat() + "T10:00:00Z"
    ev = json.dumps({
        "type": "user", "timestamp": ts, "sessionId": "s2", "cwd": "/repo/proj",
        "message": {"role": "user", "content": "hi"},
    })
    asst = json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": "s2",
        "requestId": "req-y", "uuid": "evt-y", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {"id": "msg-y", "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
    })
    (proj_dir / "s2.jsonl").write_text(ev + "\n" + asst + "\n")

    b64 = encode_cwd("/repo/proj")
    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/project/{b64}?range=7d")

    assert resp.status_code == 200
    assert resp.json()["range_key"] == "7d"


@pytest.mark.asyncio
async def test_search_cwd_prefix(tmp_path: Path) -> None:
    """GET /api/search?q=cwd:... returns only cwd-kind results."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/search?q=cwd:automl")

    assert resp.status_code == 200
    data = resp.json()
    assert "hits" in data
    assert "query" in data
    for hit in data["hits"]:
        assert hit["kind"] == "project"


@pytest.mark.asyncio
async def test_search_empty_query(tmp_path: Path) -> None:
    """GET /api/search with empty q returns empty hits."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/search?q=")

    assert resp.status_code == 200
    assert resp.json()["hits"] == []


@pytest.mark.asyncio
async def test_models_rows_have_new_fields(tmp_path: Path) -> None:
    """Models rows include context_window_k and tool_error_rate."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/models?range=all")  # all-time so fixture data is included

    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows, "Expected at least one model row"
    row = rows[0]
    assert "context_window_k" in row
    assert "tool_error_rate" in row


@pytest.mark.asyncio
async def test_recent_rows_have_latest_session_id(tmp_path: Path) -> None:
    """Recent activity rows include latest_session_id when activity falls within window."""
    import json
    from datetime import datetime, timezone

    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = json.dumps({
        "type": "assistant", "timestamp": now_iso, "sessionId": "sess-fresh",
        "requestId": "req-fresh", "uuid": "evt-fresh", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {"id": "msg-fresh", "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    })
    dst = tmp_path / "projects" / "sess-fresh.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_text(line + "\n")

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/recent?window=60m")

    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert rows, "Expected at least one recent row with fresh fixture"
    assert "latest_session_id" in rows[0]


@pytest.mark.asyncio
async def test_model_detail_projects_have_cwd_b64(tmp_path: Path) -> None:
    """Model detail projects_using_model includes cwd_b64 and last_active."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/model/opus-4-7")

    assert resp.status_code == 200
    projects = resp.json()["projects_using_model"]
    assert projects, "Expected at least one project"
    proj = projects[0]
    assert "cwd_b64" in proj
    assert "last_active" in proj


def _write_session_with_text(path: Path, sid: str) -> None:
    """Write a JSONL with user + assistant events containing text content."""
    import json as _json
    user_ev = _json.dumps({
        "type": "user", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": sid, "uuid": "u1", "isSidechain": False,
        "message": {"role": "user", "content": [{"type": "text", "text": "x" * 600}]},
    })
    asst_ev = _json.dumps({
        "type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": sid, "requestId": "req-t1", "uuid": "a1", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {
            "id": "msg-t1", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "assistant reply"}],
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
        },
    })
    path.write_text(user_ev + "\n" + asst_ev + "\n")


@pytest.mark.asyncio
async def test_turn_detail_happy_path(tmp_path: Path) -> None:
    """GET /api/session/{id}/turn/0 returns expected keys for a known session."""
    sid = "sess-td-001"
    dst = tmp_path / "projects" / f"{sid}.jsonl"
    dst.parent.mkdir(parents=True)
    _write_session_with_text(dst, sid)

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/session/{sid}/turn/0")

    assert resp.status_code == 200
    data = resp.json()
    for key in ("session_id", "turn_idx", "ts", "model", "stop_reason", "is_sidechain",
                "cost_components", "token_counts", "tool_calls",
                "user_prompt", "assistant_preview", "source_file", "source_line"):
        assert key in data, f"Missing key: {key}"
    assert set(data["cost_components"]) == {"input", "output", "cache_read", "cache_creation"}
    assert set(data["token_counts"]) == {"input", "output", "cache_read", "cache_creation", "total_visible"}


@pytest.mark.asyncio
async def test_turn_detail_invalid_idx_returns_404(tmp_path: Path) -> None:
    """GET /api/session/{id}/turn/999 returns 404 when out of range."""
    sid = "sess-td-002"
    dst = tmp_path / "projects" / f"{sid}.jsonl"
    dst.parent.mkdir(parents=True)
    _write_session_with_text(dst, sid)

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/session/{sid}/turn/999")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_turn_detail_unknown_session_returns_404(tmp_path: Path) -> None:
    """GET /api/session/unknown/turn/0 returns 404."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/session/no-such-id/turn/0")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_turn_detail_snippet_truncation(tmp_path: Path) -> None:
    """User prompt > 500 chars is truncated with … suffix."""
    sid = "sess-td-003"
    dst = tmp_path / "projects" / f"{sid}.jsonl"
    dst.parent.mkdir(parents=True)
    _write_session_with_text(dst, sid)  # writes 600-char user prompt

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/session/{sid}/turn/0")

    assert resp.status_code == 200
    prompt = resp.json()["user_prompt"]
    assert prompt.endswith("…"), f"Expected truncation, got: {prompt!r}"


@pytest.mark.asyncio
async def test_prefs_thresholds_reset_sentinel(tmp_path: Path) -> None:
    """POST /api/prefs with thresholds='reset' restores defaults."""
    from httpx import ASGITransport, AsyncClient

    from tokenol.metrics.thresholds import DEFAULTS  # noqa: PLC0415

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Mutate a threshold
            await client.post("/api/prefs", json={"thresholds": {"hit_rate_good_pct": 50}})
            # Reset
            resp = await client.post("/api/prefs", json={"thresholds": "reset"})

    assert resp.status_code == 200
    got = resp.json()["thresholds"]
    assert got["hit_rate_good_pct"] == DEFAULTS["hit_rate_good_pct"]


@pytest.mark.asyncio
async def test_breakdown_summary_returns_scorecard_fields(tmp_path: Path) -> None:
    """GET /api/breakdown/summary returns all scorecard fields."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/summary?range=all")

    assert resp.status_code == 200
    data = resp.json()
    for key in [
        "range", "sessions", "turns",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "cache_saved_usd",
    ]:
        assert key in data, f"Missing field: {key}"
    assert data["range"] == "all"
    assert data["sessions"] >= 1
    assert data["turns"] >= 1
    assert isinstance(data["cost_usd"], (int, float))
    assert isinstance(data["cache_saved_usd"], (int, float))


@pytest.mark.asyncio
async def test_breakdown_summary_rejects_unknown_range(tmp_path: Path) -> None:
    """GET /api/breakdown/summary with invalid range → 400."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/summary?range=14d")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_breakdown_daily_tokens_returns_day_array(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/daily-tokens?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "days" in data
    assert len(data["days"]) >= 1
    day = data["days"][0]
    for key in ["date", "input", "output", "cache_creation", "cache_read", "cost_usd"]:
        assert key in day, f"Missing field: {key}"
    # Dates are ISO strings (YYYY-MM-DD).
    assert len(day["date"]) == 10 and day["date"][4] == "-" and day["date"][7] == "-"


@pytest.mark.asyncio
async def test_breakdown_daily_tokens_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/daily-tokens?range=14d")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_breakdown_route_returns_html(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/breakdown")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
