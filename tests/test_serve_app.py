"""Integration tests for the FastAPI app: start server, hit /api/snapshot."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

import tokenol.serve.state as _state_mod
from tokenol.serve.app import ServerConfig, create_app
from tokenol.serve.state import ParseCache


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
    for key in ["generated_at", "config", "today", "daily_90d", "sessions", "projects", "models", "heatmap_14d"]:
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_prefs_endpoint_updates_config(tmp_path: Path) -> None:
    """POST /api/prefs updates tick_seconds and reference_usd."""
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig(tick_seconds=5, reference_usd=50.0))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/prefs", json={"tick_seconds": 10, "reference_usd": 25.0})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tick_seconds"] == 10
    assert body["reference_usd"] == 25.0


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
