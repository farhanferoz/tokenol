"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.engine.context import UsageContext
from tokenol.engine.filters import UNKNOWN_LABEL, GroupKey, InterruptedPolicy, TurnFilter
from tokenol.engine.queries import ComponentCosts
from tokenol.enums import AttributionMode
from tokenol.metrics.context import cache_hit_rate, cost_share
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.persistence.store import default_path as default_store_path
from tokenol.serve.prefs import Preferences, default_path

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
from tokenol.serve.session_detail import build_session_detail, build_turn_detail
from tokenol.serve.state import (
    VALID_METRICS,
    LegacyTurnView,
    ParseCache,
    SnapshotResult,
    build_breakdown_tools,
    build_daily_panel,
    build_day_detail,
    build_hourly_panel,
    build_model_detail,
    build_models_panel,
    build_project_detail,
    build_recent_activity_panel,
    build_search_results,
    build_skill_breakdown,
    build_skill_detail,
    build_snapshot_full,
    build_tool_detail,
    decode_cwd,
    encode_cwd,
    legacy_turn_view,
    model_price_status,
    range_since,
)
from tokenol.serve.streaming import SnapshotBroadcaster

_WINDOW_MINUTES: dict[str, int] = {"15m": 15, "60m": 60, "4h": 240, "24h": 1440}
_KNOWN_PREFS_KEYS: frozenset[str] = frozenset({"tick_seconds", "reference_usd", "hot_window_days", "thresholds"})
_BREAKDOWN_RANGES: frozenset[str] = frozenset({"7d", "30d", "90d", "all"})
_ATTRIBUTION_MODES: frozenset[str] = frozenset(m.value for m in AttributionMode)


def _validate_breakdown_range(range_: str) -> None:
    if range_ not in _BREAKDOWN_RANGES:
        raise HTTPException(
            status_code=400,
            detail="range must be 7d, 30d, 90d, or all",
        )


def _is_compare_form(param: str) -> bool:
    """Whether a project/model filter value produces multiple series."""
    return param == "compare" or "," in param


STATIC_DIR = Path(__file__).parent / "static"

# Upper bound on the hot_window_days preference (ten years).
_MAX_HOT_WINDOW_DAYS = 3650


def _open_readonly_usage(store_path: Path) -> UsageContext:
    """Open the engine on an existing history store for a plain `serve`, or on nothing.

    Reading the history is what recovers turns the JSONL no longer has, while writing
    runs a flusher thread inside the server. So a plain `tokenol serve` reads whatever
    store is already there and never writes to it; `--persist` is only about keeping it
    up to date.

    Best-effort: a missing, unreadable, or write-locked store just means an empty
    engine fed from the JSONL, never a failed startup.
    """
    from rich.console import Console

    console = Console(stderr=True)
    try:
        size_mb = store_path.stat().st_size / (1024 * 1024)
    except OSError:
        return UsageContext.open(None)
    try:
        usage = UsageContext.open(store_path)
    except Exception as exc:  # noqa: BLE001 - never block startup on the history store
        console.print(f"[yellow]History store at {store_path} ({size_mb:.0f} MB) could not be read: {exc}[/yellow]")
        return UsageContext.open(None)
    console.print(
        f"[green]Reading history store at {store_path} ({size_mb:.0f} MB) — read-only.[/green]\n"
        "[yellow]Not being updated. Pass --persist to keep it current.[/yellow]"
    )
    return usage


@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5
    persist: bool = False


def _build_snapshot(request: Request, period: str = "today") -> SnapshotResult:
    cfg: ServerConfig = request.app.state.config
    prefs: Preferences = request.app.state.prefs
    cache: ParseCache = request.app.state.parse_cache
    return build_snapshot_full(
        cache,
        all_projects=cfg.all_projects,
        reference_usd=prefs.reference_usd,
        tick_seconds=prefs.tick_seconds,
        period=period,
        thresholds=prefs.thresholds,
        history_store=request.app.state.history_store,
        flush_queue=request.app.state.flush_queue,
    )


def _usage_after_a_tick(request: Request) -> UsageContext:
    """The usage context every engine-backed endpoint reads, ready to be queried.

    If no tick has run yet (no dashboard connected) run one first: nothing but a snapshot
    build ingests the JSONL, so an endpoint reached before any build would otherwise answer
    from the history store alone. Then push each new session's grouped project into the
    engine (memoised), which a query must not run without: a session added since the last
    call would group under its raw cwd, or as "(unknown)".
    """
    if not getattr(request.app.state.parse_cache, "_tick_initialized", False):
        _build_snapshot(request)
    usage: UsageContext = request.app.state.usage
    usage.index.project_map()
    return usage


def _legacy_turn_view(request: Request) -> LegacyTurnView:
    """TRANSITIONAL: every turn and session as Python objects, for endpoints Task 6-7 migrate."""
    return legacy_turn_view(_usage_after_a_tick(request))


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

    # DE2 order: the engine reads the store file first and lets go of it, then --persist
    # opens it read-write; DuckDB refuses a read-only attach while a writer holds the file.
    store_path = default_store_path()
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

        # Another tokenol holding the file raises StoreLockedError here: refuse to start.
        usage = UsageContext.open(store_path)
        try:
            history_store = _HistoryStore(store_path)
        except Exception:
            usage.close()
            raise
        flush_queue = _FlushQueue(history_store)
        write_pidfile_fn = _write_pidfile
        clear_pidfile_fn = _clear_pidfile
    else:
        # No flusher, but still surface whatever history is already persisted.
        usage = _open_readonly_usage(store_path) if store_path.exists() else UsageContext.open(None)
    parse_cache.usage = usage

    broadcaster = SnapshotBroadcaster(
        parse_cache=parse_cache,
        all_projects=config.all_projects,
        get_reference_usd=lambda: prefs.reference_usd,
        get_tick_seconds=lambda: prefs.tick_seconds,
        get_thresholds=lambda: prefs.thresholds,
        history_store=history_store,
        flush_queue=flush_queue,
        on_forget=None,
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
            usage.close()

    app = FastAPI(title="tokenol", lifespan=lifespan)
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = parse_cache
    app.state.usage = usage
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

    @app.get("/skill/{name}", include_in_schema=False)
    async def skill_page(name: str):
        p = STATIC_DIR / "skill.html"
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
        return JSONResponse(_build_snapshot(request, period=period).payload)

    @app.get("/api/session/{session_id}")
    async def api_session_detail(session_id: str, request: Request):
        result = _legacy_turn_view(request)
        session = next((s for s in result.sessions if s.session_id == session_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return JSONResponse(build_session_detail(session))

    @app.get("/api/session/{session_id}/turn/{turn_idx}")
    async def api_turn_detail(session_id: str, turn_idx: int, request: Request):
        result = _legacy_turn_view(request)
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
        result = _legacy_turn_view(request)
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
        result = _legacy_turn_view(request)
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
        usage = _usage_after_a_tick(request)
        return JSONResponse(build_hourly_panel(usage.queries, parsed, metric, project, model))

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
        usage = _usage_after_a_tick(request)
        today = date.today()
        since = range_since(range, today) if range != "all" else None
        # Fall back silently to the longest available window when the requested range
        # exceeds the data we have — return 200 with a `note` so the UI can caption it.
        # Returning 400 here forced clients to special-case "policy" failures even though
        # the request itself is well-formed.
        effective_range = range
        note: str | None = None
        have_days: int | None = None
        earliest = usage.queries.earliest_date(TurnFilter()) if since is not None else None
        if earliest is not None and earliest > since:
            have_days = (today - earliest).days + 1
            effective_range = "all"
            note = f"Only {have_days} days of history available — showing all data instead of {range}."
        panel = build_daily_panel(usage.queries, effective_range, metric, project, model, today_date=today)
        if note is not None:
            panel["requested_range"] = range
            panel["have_days"] = have_days
            panel["note"] = note
        return JSONResponse(panel)

    @app.get("/api/models")
    async def api_models(request: Request, range: str = "today"):
        usage = _usage_after_a_tick(request)
        since = range_since(range, date.today())
        return JSONResponse(build_models_panel(usage.queries.model_rollups(TurnFilter(since=since)), range))

    @app.get("/api/recent")
    async def api_recent(request: Request, window: str = "60m"):
        if window not in _WINDOW_MINUTES:
            raise HTTPException(status_code=400, detail=f"window must be one of: {list(_WINDOW_MINUTES)}")
        usage = _usage_after_a_tick(request)
        now = datetime.now(tz=timezone.utc)
        return JSONResponse(build_recent_activity_panel(usage.queries, now, _WINDOW_MINUTES[window]))

    def _validate_tool_or_model_name(name: str) -> str:
        # MCP tools have names like `mcp__server__tool`; future Claude Code
        # releases may legitimately use `/` (e.g. `Bash/foo`). The `:path`
        # converter passes those through. Reject only the obviously bogus
        # cases — empty, path-traversal segments, or NULs — and let the
        # detail builder return 404 if the name doesn't resolve.
        if not name or ".." in name.split("/") or "\x00" in name:
            raise HTTPException(status_code=400, detail="Invalid name")
        return name

    @app.get("/api/model/{name:path}")
    async def api_model_detail(name: str, request: Request):
        name = _validate_tool_or_model_name(name)
        result = _legacy_turn_view(request)
        detail = build_model_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Model not found")
        return JSONResponse(detail)

    @app.get("/api/tool/{name:path}")
    async def api_tool_detail(name: str, request: Request):
        name = _validate_tool_or_model_name(name)
        result = _legacy_turn_view(request)
        detail = build_tool_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Tool not found")
        return JSONResponse(detail)

    @app.get("/api/skill/{name:path}")
    async def api_skill_detail(name: str, request: Request):
        name = _validate_tool_or_model_name(name)
        result = _legacy_turn_view(request)
        detail = build_skill_detail(name, result.turns, result.sessions)
        if detail is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return JSONResponse(detail)

    @app.get("/api/search")
    async def api_search(request: Request, q: str = ""):
        if not q.strip():
            return JSONResponse({"hits": [], "query": q})
        result = _legacy_turn_view(request)
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
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= _MAX_HOT_WINDOW_DAYS):
                raise HTTPException(
                    status_code=400,
                    detail=f"hot_window_days must be an integer between 1 and {_MAX_HOT_WINDOW_DAYS} (takes effect on next startup)",
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
        since = range_since(range, date.today()) if range != "all" else None
        # Interrupted turns count here: the turn total is every turn in the range.
        totals = _usage_after_a_tick(request).queries.totals(TurnFilter(since=since))
        return JSONResponse(
            {
                "range": range,
                "sessions": totals.sessions,
                "turns": totals.turns,
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "cache_read_tokens": totals.cache_read_tokens,
                "cache_creation_tokens": totals.cache_creation_tokens,
                "cost_usd": totals.cost_usd,
                "cache_saved_usd": totals.cache_saved_usd,
            }
        )

    @app.get("/api/breakdown/daily-tokens")
    async def api_breakdown_daily_tokens(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        since = range_since(range, date.today()) if range != "all" else None
        queries = _usage_after_a_tick(request).queries
        rollups = queries.daily(TurnFilter(since=since), fill_since=since)
        # Per-component cost per day. Includes cache_read so the visible stack
        # in $ mode sums to cost_usd (cache_read is the largest cost component
        # on heavy-cache days and would otherwise be silently missing).
        components = queries.cost_components_by_date(TurnFilter(since=since, interrupted=InterruptedPolicy.EXCLUDE))
        no_cost = ComponentCosts(0.0, 0.0, 0.0, 0.0)
        return JSONResponse(
            {
                "range": range,
                "days": [
                    {
                        "date": r.date.isoformat(),
                        "input": r.input_tokens,
                        "output": r.output_tokens,
                        "cache_creation": r.cache_creation_tokens,
                        "cache_read": r.cache_read_tokens,
                        "cost_usd": r.cost_usd,
                        "input_cost": components.get(r.date, no_cost).input,
                        "output_cost": components.get(r.date, no_cost).output,
                        "cache_creation_cost": components.get(r.date, no_cost).cache_creation,
                        "cache_read_cost": components.get(r.date, no_cost).cache_read,
                    }
                    for r in rollups
                ],
            }
        )

    @app.get("/api/breakdown/by-project")
    async def api_breakdown_by_project(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        since = range_since(range, date.today()) if range != "all" else None
        queries = _usage_after_a_tick(request).queries
        buckets = queries.bucket(TurnFilter(since=since, interrupted=InterruptedPolicy.EXCLUDE), GroupKey.PROJECT)

        projects = []
        for cwd, b in buckets.items():
            known = cwd != UNKNOWN_LABEL
            projects.append(
                {
                    "project": Path(cwd).name if known else UNKNOWN_LABEL,
                    "cwd": cwd,
                    "cwd_b64": encode_cwd(cwd) if known else None,
                    "input": b.input,
                    "output": b.output,
                    "cache_creation": b.cache_creation,
                    "input_cost": b.input_cost,
                    "output_cost": b.output_cost,
                    "cache_creation_cost": b.cache_creation_cost,
                    "cache_read_cost": b.cache_read_cost,
                    "cache_hit_rate": cache_hit_rate(b.cache_read, b.cache_creation, b.input),
                }
            )
        # Biggest first; ties by label, so the order does not depend on SQL group order.
        projects.sort(key=lambda p: (-(p["input"] + p["output"]), p["cwd"]))
        return JSONResponse({"range": range, "projects": projects})

    @app.get("/api/breakdown/by-model")
    async def api_breakdown_by_model(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        since = range_since(range, date.today()) if range != "all" else None
        queries = _usage_after_a_tick(request).queries
        buckets = queries.bucket(TurnFilter(since=since, interrupted=InterruptedPolicy.EXCLUDE), GroupKey.MODEL)

        total_billable = sum(b.input + b.output for b in buckets.values()) or 1
        cost_shares = cost_share({name: b.total_cost for name, b in buckets.items()})
        models = [
            {
                "model": name,
                "input": b.input,
                "output": b.output,
                "share": (b.input + b.output) / total_billable,
                "cost_usd": b.total_cost,
                "cost_share": cost_shares[name],
                # "known" | "estimated" | "unpriced" — lets the UI flag models
                # whose cost is a fallback estimate or shown as $0 (no price).
                "price_status": model_price_status(name),
            }
            for name, b in buckets.items()
        ]
        models.sort(key=lambda m: (-(m["input"] + m["output"]), m["model"]))
        return JSONResponse({"range": range, "models": models})

    @app.get("/api/breakdown/tools")
    async def api_breakdown_tools(
        request: Request,
        range: str = "30d",
        mode: str = AttributionMode.PRORATA.value,
    ):
        _validate_breakdown_range(range)
        # Unknown `mode` values fall back to prorata silently (in contrast to
        # `range`, which 400s). Forward-compatible: a client that stores a
        # future mode in localStorage degrades gracefully on an older server.
        if mode not in _ATTRIBUTION_MODES:
            mode = AttributionMode.PRORATA.value
        since = range_since(range, datetime.now(tz=timezone.utc).date()) if range != "all" else None
        queries = _usage_after_a_tick(request).queries
        f = TurnFilter(since=since, interrupted=InterruptedPolicy.EXCLUDE)
        tools = build_breakdown_tools(queries.tool_costs(f, AttributionMode(mode)))
        # Billable-token totals so the Tokens view can show what fraction of all
        # tokens ran through tool calls — a different number than the cost share,
        # since $/token varies by model and cache.
        nontool_billable, total_billable = queries.billable_totals(f)
        return JSONResponse(
            {
                "range": range,
                "mode": mode,
                "tools": tools,
                "total_billable_tokens": total_billable,
                "nontool_billable_tokens": nontool_billable,
            }
        )

    @app.get("/api/breakdown/skills")
    async def api_breakdown_skills(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        since = range_since(range, datetime.now(tz=timezone.utc).date()) if range != "all" else None
        queries = _usage_after_a_tick(request).queries
        skill_costs = queries.skill_costs(TurnFilter(since=since, interrupted=InterruptedPolicy.EXCLUDE))
        return JSONResponse({"range": range, **build_skill_breakdown(skill_costs)})

    return app
