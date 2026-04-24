"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.metrics.cost import cache_saved_usd, rollup_by_date
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.serve.prefs import Preferences, default_path
from tokenol.serve.session_detail import build_session_detail, build_turn_detail
from tokenol.serve.state import (
    VALID_METRICS,
    ParseCache,
    SnapshotResult,
    build_daily_panel,
    build_day_detail,
    build_hourly_panel,
    build_model_detail,
    build_models_panel,
    build_project_detail,
    build_recent_activity_panel,
    build_search_results,
    build_snapshot_full,
    decode_cwd,
    encode_cwd,
    range_since,
)

_WINDOW_MINUTES: dict[str, int] = {"15m": 15, "60m": 60, "4h": 240, "24h": 1440}
_KNOWN_PREFS_KEYS: frozenset[str] = frozenset({"tick_seconds", "reference_usd", "thresholds"})


def _is_compare_form(param: str) -> bool:
    """Whether a project/model filter value produces multiple series."""
    return param == "compare" or "," in param

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5


def _build_and_cache_snapshot(request: Request, period: str = "today") -> SnapshotResult:
    cfg: ServerConfig = request.app.state.config
    prefs: Preferences = request.app.state.prefs
    cache: ParseCache = request.app.state.parse_cache
    result = build_snapshot_full(
        cache,
        all_projects=cfg.all_projects,
        reference_usd=prefs.reference_usd,
        tick_seconds=prefs.tick_seconds,
        period=period,
        thresholds=prefs.thresholds,
    )
    request.app.state.snapshot_result = result
    return result


def create_app(
    config: ServerConfig | None = None,
    prefs_path: Path | None = None,
) -> FastAPI:
    """Create and return the FastAPI app, wired with the given config."""
    if config is None:
        config = ServerConfig()
    _prefs_path = prefs_path or default_path()
    prefs = Preferences.load(_prefs_path)

    app = FastAPI(title="tokenol")
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = ParseCache()
    app.state.snapshot_result = None

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index_page():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/breakdown", include_in_schema=False)
    async def breakdown_page():
        return FileResponse(str(STATIC_DIR / "breakdown.html"))

    @app.get("/session/{session_id}", include_in_schema=False)
    async def session_page(session_id: str):
        return FileResponse(str(STATIC_DIR / "session.html"))

    @app.get("/project/{cwd_b64}", include_in_schema=False)
    async def project_page(cwd_b64: str):
        return FileResponse(str(STATIC_DIR / "project.html"))

    @app.get("/day/{target_date}", include_in_schema=False)
    async def day_page(target_date: str):
        return FileResponse(str(STATIC_DIR / "day.html"))

    @app.get("/model/{name}", include_in_schema=False)
    async def model_page(name: str):
        p = STATIC_DIR / "model.html"
        return FileResponse(str(p)) if p.exists() else FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/snapshot")
    async def api_snapshot(request: Request, period: str = "today"):
        return JSONResponse(_build_and_cache_snapshot(request, period=period).payload)

    @app.get("/api/session/{session_id}")
    async def api_session_detail(session_id: str, request: Request):
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        session = next((s for s in result.sessions if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return JSONResponse(build_session_detail(session))

    @app.get("/api/session/{session_id}/turn/{turn_idx}")
    async def api_turn_detail(session_id: str, turn_idx: int, request: Request):
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        session = next((s for s in result.sessions if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        detail = build_turn_detail(session, turn_idx)
        if detail is None:
            raise HTTPException(status_code=404, detail="Turn index out of range")
        return JSONResponse(detail)

    @app.get("/api/project/{cwd_b64}")
    async def api_project_detail(cwd_b64: str, request: Request, range: str = "14d"):
        try:
            cwd = decode_cwd(cwd_b64)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cwd encoding") from None
        if range not in ("1d", "7d", "14d", "30d", "all"):
            raise HTTPException(status_code=400, detail="Invalid range — use 1d, 7d, 14d, 30d, or all")
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        detail = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: build_project_detail(cwd, result.sessions, range_key=range),
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="Project not found or no activity in range")
        return JSONResponse(detail)

    @app.get("/api/day/{target_date}")
    async def api_day_detail(target_date: str, request: Request):
        try:
            parsed_date = date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD") from None
        if parsed_date > date.today():
            raise HTTPException(status_code=400, detail="Future dates not supported")
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        detail = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: build_day_detail(parsed_date, result.turns, result.sessions),
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="No data for this date")
        return JSONResponse(detail)

    @app.get("/api/stream")
    async def api_stream(request: Request, period: str = "today"):
        from tokenol.serve.streaming import snapshot_stream

        cfg: ServerConfig = request.app.state.config
        prefs: Preferences = request.app.state.prefs
        cache: ParseCache = request.app.state.parse_cache

        async def event_generator():
            async for chunk in snapshot_stream(
                parse_cache=cache,
                all_projects=cfg.all_projects,
                reference_usd=prefs.reference_usd,
                get_tick_seconds=lambda: prefs.tick_seconds,
                period=period,
                thresholds=prefs.thresholds,
            ):
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/api/hourly/{target_date}")
    async def api_hourly(
        target_date: str,
        request: Request,
        metric: str = "hit_pct",
        project: str = "all",
        model: str = "all",
    ):
        if _is_compare_form(project) and _is_compare_form(model):
            raise HTTPException(status_code=400, detail="Cannot compare both project and model simultaneously")
        if metric not in VALID_METRICS:
            raise HTTPException(status_code=400, detail=f"Unknown metric — valid: {sorted(VALID_METRICS)}")
        try:
            parsed = date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date — use YYYY-MM-DD") from None
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        return JSONResponse(build_hourly_panel(parsed, result.turns, result.sessions, metric, project, model))

    @app.get("/api/daily")
    async def api_daily(
        request: Request,
        range: str = "30d",
        metric: str = "hit_pct",
        project: str = "all",
        model: str = "all",
    ):
        if _is_compare_form(project) and _is_compare_form(model):
            raise HTTPException(status_code=400, detail="Cannot compare both project and model simultaneously")
        if metric not in VALID_METRICS:
            raise HTTPException(status_code=400, detail=f"Unknown metric — valid: {sorted(VALID_METRICS)}")
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(status_code=400, detail="range must be 7d, 30d, 90d, or all")
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        if range != "all" and result.turns:
            today = date.today()
            since = range_since(range, today)
            if since is not None:
                earliest = min(t.timestamp.date() for t in result.turns)
                if earliest > since:
                    have_days = (today - earliest).days + 1
                    return JSONResponse(
                        status_code=400,
                        content={"error": "insufficient_history", "have_days": have_days},
                    )
        return JSONResponse(build_daily_panel(result.turns, result.sessions, range, metric, project, model))

    @app.get("/api/models")
    async def api_models(request: Request, range: str = "today"):
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today())
        turns = [t for t in result.turns if t.timestamp.date() >= since] if since else result.turns
        return JSONResponse(build_models_panel(turns, range))

    @app.get("/api/recent")
    async def api_recent(request: Request, window: str = "60m"):
        if window not in _WINDOW_MINUTES:
            raise HTTPException(status_code=400, detail=f"window must be one of: {list(_WINDOW_MINUTES)}")
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        now = datetime.now(tz=timezone.utc)
        return JSONResponse(build_recent_activity_panel(
            result.turns, result.sessions, now, _WINDOW_MINUTES[window]
        ))

    @app.get("/api/model/{name}")
    async def api_model_detail(name: str, request: Request):
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        detail = build_model_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Model not found")
        return JSONResponse(detail)

    @app.get("/api/search")
    async def api_search(request: Request, q: str = ""):
        if not q.strip():
            return JSONResponse({"hits": [], "query": q})
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        return JSONResponse(build_search_results(q, result.turns, result.sessions))

    @app.get("/api/prefs")
    async def api_prefs_get(request: Request):
        return JSONResponse(request.app.state.prefs.to_dict())

    @app.post("/api/prefs")
    async def api_prefs_post(request: Request):
        prefs: Preferences = request.app.state.prefs
        body = await request.json()

        unknown = set(body.keys()) - _KNOWN_PREFS_KEYS
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown keys: {sorted(unknown)}")

        if "tick_seconds" in body:
            v = body["tick_seconds"]
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise HTTPException(status_code=400, detail="tick_seconds must be a positive integer")
            prefs.tick_seconds = v

        if "reference_usd" in body:
            v = body["reference_usd"]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise HTTPException(status_code=400, detail="reference_usd must be a positive number")
            prefs.reference_usd = float(v)

        if "thresholds" in body:
            t = body["thresholds"]
            if t == "reset":
                prefs.thresholds = dict(DEFAULTS)
            elif not isinstance(t, dict):
                raise HTTPException(status_code=400, detail="thresholds must be an object or 'reset'")
            else:
                unknown_thresh = set(t.keys()) - set(DEFAULTS.keys())
                if unknown_thresh:
                    raise HTTPException(status_code=400, detail=f"Unknown threshold keys: {sorted(unknown_thresh)}")
                for k, v in t.items():
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        raise HTTPException(status_code=400, detail=f"Threshold {k!r} must be numeric")
                prefs.thresholds.update(t)

        prefs.save(request.app.state.prefs_path)
        return JSONResponse(prefs.to_dict())

    @app.get("/api/breakdown/summary")
    async def api_breakdown_summary(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None
        if since is None:
            turns = list(result.turns)
            sessions = list(result.sessions)
        else:
            turns = [t for t in result.turns if t.timestamp.date() >= since]
            sessions = [
                s for s in result.sessions
                if any(t.timestamp.date() >= since for t in s.turns)
            ]

        return JSONResponse({
            "range": range,
            "sessions": len(sessions),
            "turns": len(turns),
            "input_tokens": sum(t.usage.input_tokens for t in turns),
            "output_tokens": sum(t.usage.output_tokens for t in turns),
            "cache_read_tokens": sum(t.usage.cache_read_input_tokens for t in turns),
            "cache_creation_tokens": sum(t.usage.cache_creation_input_tokens for t in turns),
            "cost_usd": sum(t.cost_usd for t in turns),
            "cache_saved_usd": cache_saved_usd(turns),
        })

    @app.get("/api/breakdown/daily-tokens")
    async def api_breakdown_daily_tokens(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None
        if since is None:
            turns = list(result.turns)
            rollups = rollup_by_date(turns)
        else:
            turns = [t for t in result.turns if t.timestamp.date() >= since]
            rollups = rollup_by_date(turns, since=since)

        return JSONResponse({
            "range": range,
            "days": [
                {
                    "date": r.date.isoformat(),
                    "input": r.input_tokens,
                    "output": r.output_tokens,
                    "cache_creation": r.cache_creation_tokens,
                    "cache_read": r.cache_read_tokens,
                    "cost_usd": r.cost_usd,
                }
                for r in rollups
            ],
        })

    @app.get("/api/breakdown/by-project")
    async def api_breakdown_by_project(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None

        buckets: dict[str, dict[str, int]] = {}
        for s in result.sessions:
            cwd = s.cwd or "(unknown)"
            for t in s.turns:
                if since is not None and t.timestamp.date() < since:
                    continue
                if t.is_interrupted:
                    continue
                b = buckets.setdefault(cwd, {
                    "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                })
                b["input"] += t.usage.input_tokens
                b["output"] += t.usage.output_tokens
                b["cache_read"] += t.usage.cache_read_input_tokens
                b["cache_creation"] += t.usage.cache_creation_input_tokens

        projects = []
        for cwd, b in buckets.items():
            denom = b["cache_read"] + b["cache_creation"] + b["input"]
            hit_rate = (b["cache_read"] / denom) if denom > 0 else None
            projects.append({
                "project": Path(cwd).name if cwd != "(unknown)" else "(unknown)",
                "cwd": cwd,
                "cwd_b64": encode_cwd(cwd) if cwd != "(unknown)" else None,
                "input": b["input"],
                "output": b["output"],
                "cache_hit_rate": hit_rate,
            })
        projects.sort(key=lambda p: p["input"] + p["output"], reverse=True)
        return JSONResponse({"range": range, "projects": projects})

    return app
