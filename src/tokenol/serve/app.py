"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.metrics.cost import cache_saved_usd, rollup_by_date
from tokenol.metrics.rollups import _rank_counter_with_others
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.serve.prefs import Preferences, default_path

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
from tokenol.serve.session_detail import build_session_detail, build_turn_detail
from tokenol.serve.state import (
    VALID_METRICS,
    ParseCache,
    SnapshotResult,
    _grouped_cwd_by_sid,
    build_daily_panel,
    build_day_detail,
    build_hourly_panel,
    build_model_detail,
    build_models_panel,
    build_project_detail,
    build_recent_activity_panel,
    build_search_results,
    build_snapshot_full,
    build_tool_detail,
    decode_cwd,
    encode_cwd,
    range_since,
)
from tokenol.serve.streaming import SnapshotBroadcaster

_WINDOW_MINUTES: dict[str, int] = {"15m": 15, "60m": 60, "4h": 240, "24h": 1440}
_KNOWN_PREFS_KEYS: frozenset[str] = frozenset({"tick_seconds", "reference_usd", "hot_window_days", "thresholds"})
_BREAKDOWN_RANGES: frozenset[str] = frozenset({"7d", "30d", "90d", "all"})


def _validate_breakdown_range(range_: str) -> None:
    if range_ not in _BREAKDOWN_RANGES:
        raise HTTPException(
            status_code=400,
            detail="range must be 7d, 30d, 90d, or all",
        )


def _bucket_turns(
    sessions: list,
    since,
    key_fn,
) -> dict[str, dict[str, float]]:
    """Group non-interrupted turns into buckets and sum the usage + cost fields.

    `key_fn` receives `(session, turn)` and returns the bucket key; handlers
    pass a lambda that closes over whatever grouping dict they precomputed
    (e.g. `cwd_by_sid`). Returns a dict mapping each key to a sub-dict with
    token totals (`input`, `output`, `cache_read`, `cache_creation`) and
    per-component cost totals in USD (`input_cost`, `output_cost`,
    `cache_read_cost`, `cache_creation_cost`). Callers may ignore unused
    fields.
    """
    from tokenol.metrics.cost import cost_for_turn
    buckets: dict[str, dict[str, float]] = {}
    for s in sessions:
        for t in s.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            key = key_fn(s, t)
            b = buckets.setdefault(key, {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                "input_cost": 0.0, "output_cost": 0.0,
                "cache_read_cost": 0.0, "cache_creation_cost": 0.0,
                "total_cost": 0.0,
            })
            b["input"] += t.usage.input_tokens
            b["output"] += t.usage.output_tokens
            b["cache_read"] += t.usage.cache_read_input_tokens
            b["cache_creation"] += t.usage.cache_creation_input_tokens
            tc = cost_for_turn(t.model, t.usage)
            b["input_cost"] += tc.input_usd
            b["output_cost"] += tc.output_usd
            b["cache_read_cost"] += tc.cache_read_usd
            b["cache_creation_cost"] += tc.cache_creation_usd
            b["total_cost"] += tc.total_usd
    return buckets


def _is_compare_form(param: str) -> bool:
    """Whether a project/model filter value produces multiple series."""
    return param == "compare" or "," in param

STATIC_DIR = Path(__file__).parent / "static"


def _warn_if_orphan_store_exists() -> None:
    """Warn on stderr if a history store exists but persistence is off."""
    from rich.console import Console

    _env = os.environ.get("TOKENOL_HISTORY_PATH")
    store_path = Path(_env) if _env else Path.home() / ".tokenol" / "history.duckdb"
    try:
        size_mb = store_path.stat().st_size / (1024 * 1024)
    except OSError:
        return
    Console(stderr=True).print(
        f"[yellow]Found existing history store at {store_path} ({size_mb:.0f} MB).\n"
        f"Persistence is OFF — pass --persist to use it.[/yellow]"
    )


@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5
    persist: bool = False


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
        history_store=request.app.state.history_store,
        flush_queue=request.app.state.flush_queue,
    )
    request.app.state.snapshot_result = result
    return result


def _current_snapshot_result(request: Request) -> SnapshotResult:
    """Prefer the broadcaster's latest build; fall back to the app cache or a fresh build."""
    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is not None:
        latest = broadcaster.latest_result()
        if latest is not None:
            return latest
    return request.app.state.snapshot_result or _build_and_cache_snapshot(request)


def create_app(
    config: ServerConfig | None = None,
    prefs_path: Path | None = None,
) -> FastAPI:
    """Create and return the FastAPI app, wired with the given config."""
    if config is None:
        config = ServerConfig()
    _prefs_path = prefs_path or default_path()
    prefs = Preferences.load(_prefs_path)

    parse_cache = ParseCache()

    history_store: HistoryStore | None = None
    flush_queue: FlushQueue | None = None
    write_pidfile_fn = None
    clear_pidfile_fn = None

    if config.persist:
        from tokenol.persistence.flusher import FlushQueue as _FlushQueue
        from tokenol.persistence.forget_handoff import (
            clear_pidfile as _clear_pidfile,
        )
        from tokenol.persistence.forget_handoff import (
            write_pidfile as _write_pidfile,
        )
        from tokenol.persistence.store import (
            HistoryStore as _HistoryStore,
        )

        history_store = _HistoryStore()
        # Hot-tier window is read by _store_backed_derivation as a duck-typed attr.
        history_store._hot_window_days = prefs.hot_window_days
        flush_queue = _FlushQueue(history_store)
        write_pidfile_fn = _write_pidfile
        clear_pidfile_fn = _clear_pidfile
    else:
        _warn_if_orphan_store_exists()

    broadcaster = SnapshotBroadcaster(
        parse_cache=parse_cache,
        all_projects=config.all_projects,
        get_reference_usd=lambda: prefs.reference_usd,
        get_tick_seconds=lambda: prefs.tick_seconds,
        get_thresholds=lambda: prefs.thresholds,
        history_store=history_store,
        flush_queue=flush_queue,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if config.persist:
            assert write_pidfile_fn is not None
            assert flush_queue is not None
            write_pidfile_fn()
            await flush_queue.start()
        try:
            yield
        finally:
            await broadcaster.shutdown()
            if config.persist:
                assert flush_queue is not None
                assert history_store is not None
                assert clear_pidfile_fn is not None
                await flush_queue.stop()
                history_store.close()
                clear_pidfile_fn()

    app = FastAPI(title="tokenol", lifespan=lifespan)
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = parse_cache
    app.state.snapshot_result = None
    app.state.broadcaster = broadcaster
    app.state.history_store = history_store
    app.state.flush_queue = flush_queue

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

    @app.get("/tool/{name}", include_in_schema=False)
    async def tool_page(name: str):
        p = STATIC_DIR / "tool.html"
        return FileResponse(str(p)) if p.exists() else FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/snapshot")
    async def api_snapshot(request: Request, period: str = "today"):
        # Reuse the broadcaster's cached payload when a SSE group is live for
        # this period — avoids a second full rebuild for polling backstops.
        broadcaster = getattr(request.app.state, "broadcaster", None)
        if broadcaster is not None:
            cached = broadcaster.cached_payload(period)
            if cached is not None:
                return JSONResponse(cached)
        return JSONResponse(_build_and_cache_snapshot(request, period=period).payload)

    @app.get("/api/session/{session_id}")
    async def api_session_detail(session_id: str, request: Request):
        result = _current_snapshot_result(request)
        session = next((s for s in result.sessions if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return JSONResponse(build_session_detail(session))

    @app.get("/api/session/{session_id}/turn/{turn_idx}")
    async def api_turn_detail(session_id: str, turn_idx: int, request: Request):
        result = _current_snapshot_result(request)
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
        result = _current_snapshot_result(request)
        if range == "all" and request.app.state.history_store is not None:
            loop = asyncio.get_running_loop()
            warm_turns = await loop.run_in_executor(
                None,
                lambda: request.app.state.history_store.query_turns(project=cwd),
            )
            if warm_turns:
                existing_keys = {t.dedup_key for t in result.turns}
                merged_turns = list(result.turns) + [
                    t for t in warm_turns if t.dedup_key not in existing_keys
                ]
                merged_turns.sort(key=lambda t: t.timestamp)

                # Build a superset of sessions: existing + warm-tier sessions for this cwd.
                warm_sids = {t.session_id for t in warm_turns}
                existing_sids = {s.session_id for s in result.sessions}
                missing_sids = warm_sids - existing_sids
                warm_sessions: list = []
                for sid in missing_sids:
                    s = await loop.run_in_executor(
                        None,
                        lambda sid=sid: request.app.state.history_store.query_session(sid),
                    )
                    if s is not None:
                        s.archived = True  # JSONL is gone — content snippets unavailable
                        warm_sessions.append(s)
                result = replace(
                    result,
                    turns=merged_turns,
                    sessions=list(result.sessions) + warm_sessions,
                )
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
        result = _current_snapshot_result(request)
        detail = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: build_day_detail(parsed_date, result.turns, result.sessions),
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="No data for this date")
        return JSONResponse(detail)

    @app.get("/api/stream")
    async def api_stream(request: Request, period: str = "today"):
        broadcaster: SnapshotBroadcaster = request.app.state.broadcaster

        async def event_generator():
            async for chunk in broadcaster.subscribe(period):
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
        result = _current_snapshot_result(request)
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
        result = _current_snapshot_result(request)
        # Warm-tier merge: when range=all and a store is wired, fold in any persisted
        # turns older than the in-memory hot window so the chart includes them.
        if range == "all" and request.app.state.history_store is not None:
            prefs: Preferences = request.app.state.prefs
            hot_cutoff = date.today() - timedelta(days=prefs.hot_window_days)
            loop = asyncio.get_running_loop()
            warm_turns = await loop.run_in_executor(
                None,
                lambda: request.app.state.history_store.query_turns(until=hot_cutoff),
            )
            if warm_turns:
                existing_keys = {t.dedup_key for t in result.turns}
                merged = list(result.turns) + [
                    t for t in warm_turns if t.dedup_key not in existing_keys
                ]
                merged.sort(key=lambda t: t.timestamp)
                result = replace(result, turns=merged)
        # Fall back silently to the longest available window when the requested range
        # exceeds the data we have — return 200 with a `note` so the UI can caption it.
        # Returning 400 here forced clients to special-case "policy" failures even though
        # the request itself is well-formed.
        effective_range = range
        note: str | None = None
        have_days: int | None = None
        if range != "all" and result.turns:
            today = date.today()
            since = range_since(range, today)
            if since is not None:
                earliest = min(t.timestamp.date() for t in result.turns)
                if earliest > since:
                    have_days = (today - earliest).days + 1
                    effective_range = "all"
                    note = (
                        f"Only {have_days} days of history available — "
                        f"showing all data instead of {range}."
                    )
        panel = build_daily_panel(result.turns, result.sessions, effective_range, metric, project, model)
        if note is not None:
            panel["requested_range"] = range
            panel["have_days"] = have_days
            panel["note"] = note
        return JSONResponse(panel)

    @app.get("/api/models")
    async def api_models(request: Request, range: str = "today"):
        result = _current_snapshot_result(request)
        since = range_since(range, date.today())
        turns = [t for t in result.turns if t.timestamp.date() >= since] if since else result.turns
        return JSONResponse(build_models_panel(turns, range))

    @app.get("/api/recent")
    async def api_recent(request: Request, window: str = "60m"):
        if window not in _WINDOW_MINUTES:
            raise HTTPException(status_code=400, detail=f"window must be one of: {list(_WINDOW_MINUTES)}")
        result = _current_snapshot_result(request)
        now = datetime.now(tz=timezone.utc)
        return JSONResponse(build_recent_activity_panel(
            result.turns, result.sessions, now, _WINDOW_MINUTES[window]
        ))

    @app.get("/api/model/{name}")
    async def api_model_detail(name: str, request: Request):
        result = _current_snapshot_result(request)
        detail = build_model_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Model not found")
        return JSONResponse(detail)

    @app.get("/api/tool/{name}")
    async def api_tool_detail(name: str, request: Request):
        result = _current_snapshot_result(request)
        detail = build_tool_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Tool not found")
        return JSONResponse(detail)

    @app.get("/api/search")
    async def api_search(request: Request, q: str = ""):
        if not q.strip():
            return JSONResponse({"hits": [], "query": q})
        result = _current_snapshot_result(request)
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

        if "hot_window_days" in body:
            v = body["hot_window_days"]
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 3650):
                raise HTTPException(
                    status_code=400,
                    detail="hot_window_days must be an integer between 1 and 3650 (takes effect on next startup)",
                )
            prefs.hot_window_days = v

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
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
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
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = range_since(range, date.today()) if range != "all" else None
        if since is None:
            turns = list(result.turns)
            rollups = rollup_by_date(turns)
        else:
            turns = [t for t in result.turns if t.timestamp.date() >= since]
            rollups = rollup_by_date(turns, since=since)

        # Per-component cost per day, computed once and joined in below.
        from tokenol.metrics.cost import cost_for_turn
        cost_by_date: dict = {}
        for t in turns:
            if t.is_interrupted:
                continue
            d = t.timestamp.date()
            slot = cost_by_date.setdefault(d, {"input": 0.0, "output": 0.0, "cache_creation": 0.0})
            tc = cost_for_turn(t.model, t.usage)
            slot["input"] += tc.input_usd
            slot["output"] += tc.output_usd
            slot["cache_creation"] += tc.cache_creation_usd

        _empty_cost = {"input": 0.0, "output": 0.0, "cache_creation": 0.0}
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
                    "input_cost": cost_by_date.get(r.date, _empty_cost)["input"],
                    "output_cost": cost_by_date.get(r.date, _empty_cost)["output"],
                    "cache_creation_cost": cost_by_date.get(r.date, _empty_cost)["cache_creation"],
                }
                for r in rollups
            ],
        })

    @app.get("/api/breakdown/by-project")
    async def api_breakdown_by_project(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = range_since(range, date.today()) if range != "all" else None

        cwd_by_sid = _grouped_cwd_by_sid(result.sessions)

        buckets = _bucket_turns(
            result.sessions, since,
            key_fn=lambda s, _t: cwd_by_sid.get(s.session_id, "(unknown)"),
        )

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
                "cache_creation": b["cache_creation"],
                "input_cost": b["input_cost"],
                "output_cost": b["output_cost"],
                "cache_creation_cost": b["cache_creation_cost"],
                "cache_hit_rate": hit_rate,
            })
        projects.sort(key=lambda p: p["input"] + p["output"], reverse=True)
        return JSONResponse({"range": range, "projects": projects})

    @app.get("/api/breakdown/by-model")
    async def api_breakdown_by_model(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = range_since(range, date.today()) if range != "all" else None

        buckets = _bucket_turns(
            result.sessions, since,
            key_fn=lambda _s, t: t.model or "(unknown)",
        )

        total_billable = sum(b["input"] + b["output"] for b in buckets.values()) or 1
        total_cost = sum(b["total_cost"] for b in buckets.values())
        models = []
        for name, b in buckets.items():
            billable = b["input"] + b["output"]
            cost_usd = b["total_cost"]
            models.append({
                "model": name,
                "input": b["input"],
                "output": b["output"],
                "share": billable / total_billable,
                "cost_usd": cost_usd,
                "cost_share": (cost_usd / total_cost) if total_cost > 0 else 0,
            })
        models.sort(key=lambda m: m["input"] + m["output"], reverse=True)
        return JSONResponse({"range": range, "models": models})

    @app.get("/api/breakdown/tools")
    async def api_breakdown_tools(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = range_since(range, date.today()) if range != "all" else None

        total: Counter[str] = Counter()
        for t in result.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            total.update(t.tool_names)

        tools = _rank_counter_with_others(total, top_n=10)
        return JSONResponse({"range": range, "tools": tools})

    return app
