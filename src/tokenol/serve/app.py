"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.serve.session_detail import build_session_detail
from tokenol.serve.state import ParseCache, SnapshotResult, build_snapshot_full

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5


def _build_and_cache_snapshot(request: Request) -> SnapshotResult:
    """Build snapshot from request app state, caching result for drill-down lookups."""
    cfg: ServerConfig = request.app.state.config
    cache: ParseCache = request.app.state.parse_cache
    result = build_snapshot_full(
        cache,
        all_projects=cfg.all_projects,
        reference_usd=cfg.reference_usd,
        tick_seconds=cfg.tick_seconds,
    )
    request.app.state.snapshot_result = result
    return result


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Create and return the FastAPI app, wired with the given config."""
    if config is None:
        config = ServerConfig()

    app = FastAPI(title="tokenol")
    app.state.config = config
    app.state.parse_cache = ParseCache()
    app.state.snapshot_result = None

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index_page():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/session/{session_id}", include_in_schema=False)
    async def session_page(session_id: str):
        return FileResponse(str(STATIC_DIR / "session.html"))

    @app.get("/api/snapshot")
    async def api_snapshot(request: Request):
        return JSONResponse(_build_and_cache_snapshot(request).payload)

    @app.get("/api/session/{session_id}")
    async def api_session_detail(session_id: str, request: Request):
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        session = next((s for s in result.sessions if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return JSONResponse(build_session_detail(session))

    @app.get("/api/stream")
    async def api_stream(request: Request):
        from tokenol.serve.streaming import snapshot_stream

        cfg: ServerConfig = request.app.state.config
        cache: ParseCache = request.app.state.parse_cache

        async def event_generator():
            async for chunk in snapshot_stream(
                parse_cache=cache,
                all_projects=cfg.all_projects,
                reference_usd=cfg.reference_usd,
                get_tick_seconds=lambda: cfg.tick_seconds,
            ):
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/api/prefs")
    async def api_prefs(request: Request):
        cfg: ServerConfig = request.app.state.config
        body = await request.json()
        if "tick_seconds" in body:
            v = int(body["tick_seconds"])
            if v > 0:
                cfg.tick_seconds = v
        if "reference_usd" in body:
            v = float(body["reference_usd"])
            if v > 0:
                cfg.reference_usd = v
        return JSONResponse({"tick_seconds": cfg.tick_seconds, "reference_usd": cfg.reference_usd})

    return app
