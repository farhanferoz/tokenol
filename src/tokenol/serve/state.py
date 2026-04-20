"""Server-side parse cache and snapshot builder."""

from __future__ import annotations

import heapq
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tokenol import assumptions as assumption_recorder
from tokenol.enums import AssumptionTag
from tokenol.ingest.discovery import find_jsonl_files, get_config_dirs
from tokenol.ingest.parser import dedup_key, parse_file
from tokenol.metrics.cost import cost_for_turn, rollup_by_date, rollup_by_hour
from tokenol.metrics.rollups import (
    SessionRollup,
    build_model_rollups,
    build_project_rollups,
    build_session_rollup,
)
from tokenol.metrics.verdicts import compute_verdict
from tokenol.metrics.windows import align_windows, project_window
from tokenol.model.events import RawEvent, Session, Turn, Usage


@dataclass
class ParseCache:
    """JSONL parse cache keyed by (path_str, size, mtime_ns)."""

    _store: dict[tuple[str, int, int], list[RawEvent]] = field(default_factory=dict)

    def get_or_parse(self, path: Path) -> tuple[tuple[str, int, int], list[RawEvent]]:
        """Return (cache_key, events). Parses only when (size, mtime_ns) changes."""
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._store:
            self._store[key] = list(parse_file(path))
        return key, self._store[key]

    def purge(self, keep_keys: set[tuple[str, int, int]]) -> None:
        """Remove entries not in keep_keys (stale / deleted files)."""
        stale = [k for k in self._store if k not in keep_keys]
        for k in stale:
            del self._store[k]

    @property
    def size(self) -> int:
        return len(self._store)


def _build_turns_and_sessions(
    all_events: list[RawEvent], paths: list[Path]
) -> tuple[list[Turn], list[Session]]:
    """Build deduplicated turns and sessions from pre-parsed raw events."""
    seen: dict[str, tuple[RawEvent, str]] = {}
    passthroughs: list[tuple[RawEvent, None]] = []
    cwd_by_session: dict[str, str] = {}

    for ev in all_events:
        if ev.event_type == "system" and ev.cwd and ev.session_id not in cwd_by_session:
            cwd_by_session[ev.session_id] = ev.cwd
        if ev.event_type != "assistant":
            continue
        if ev.model == "<synthetic>":
            continue
        k = dedup_key(ev)
        if k is None:
            passthroughs.append((ev, None))
        else:
            seen[k] = (ev, k)

    turns: list[Turn] = []
    for ev, k in itertools.chain(passthroughs, seen.values()):
        is_interrupted = ev.usage is None
        usage = ev.usage if ev.usage is not None else Usage()

        tags: list[AssumptionTag] = []
        if k is None:
            tags.append(AssumptionTag.DEDUP_PASSTHROUGH)
        if is_interrupted:
            tags.append(AssumptionTag.INTERRUPTED_TURN_SKIPPED)

        tc = cost_for_turn(ev.model, usage)
        tags.extend(t for t in tc.assumptions if t not in tags)
        assumption_recorder.record(tags)

        key_str = k or ev.uuid or str(id(ev))
        turns.append(Turn(
            dedup_key=key_str,
            timestamp=ev.timestamp,
            session_id=ev.session_id,
            model=ev.model,
            usage=usage,
            is_sidechain=ev.is_sidechain,
            stop_reason=ev.stop_reason,
            assumptions=tags,
            cost_usd=tc.total_usd,
            is_interrupted=is_interrupted,
            tool_use_count=ev.tool_use_count,
            tool_error_count=ev.tool_error_count,
        ))

    session_source: dict[str, str] = {p.stem: str(p) for p in paths}

    session_turns: dict[str, list[Turn]] = defaultdict(list)
    session_sidechain: dict[str, bool] = {}
    for turn in turns:
        sid = turn.session_id
        session_turns[sid].append(turn)
        if sid not in session_sidechain:
            session_sidechain[sid] = turn.is_sidechain

    sessions: list[Session] = []
    for sid, t_list in session_turns.items():
        t_list.sort(key=lambda t: t.timestamp)
        sessions.append(Session(
            session_id=sid,
            source_file=session_source.get(sid, ""),
            is_sidechain=session_sidechain.get(sid, False),
            cwd=cwd_by_session.get(sid),
            turns=t_list,
        ))
    sessions.sort(key=lambda s: s.turns[0].timestamp if s.turns else s.session_id)
    return turns, sessions


@dataclass
class SnapshotResult:
    payload: dict
    turns: list[Turn]
    sessions: list[Session]


def _session_rollup_to_dict(sr: SessionRollup) -> dict:
    return {
        "id": sr.session_id,
        "model": sr.model,
        "first_ts": sr.first_ts.isoformat(),
        "last_ts": sr.last_ts.isoformat(),
        "cost_usd": sr.cost_usd,
        "turns": sr.turns,
        "max_input": sr.max_turn_input,
        "verdict": sr.verdict.value,
        "cwd": sr.cwd or "",
    }


def _sessions_in_window(
    rollups: list[SessionRollup], since: date, top: int
) -> list[dict]:
    """Filter rollups to those active since *since*, sort by cost desc, top N."""
    filtered = [sr for sr in rollups if sr.last_ts.date() >= since]
    filtered.sort(key=lambda sr: sr.cost_usd, reverse=True)
    return [_session_rollup_to_dict(sr) for sr in filtered[:top]]


def _projects_in_window(rollups: list[SessionRollup], since: date) -> list[dict]:
    scoped = [sr for sr in rollups if sr.last_ts.date() >= since]
    return [
        {
            "cwd": pr.cwd,
            "cost_usd": pr.cost_usd,
            "sessions": pr.sessions,
            "turns": pr.turns,
            "cache_reuse_ratio": pr.cache_reuse_ratio,
        }
        for pr in build_project_rollups(scoped)
    ]


def _models_in_window(turns: list[Turn], since: date) -> list[dict]:
    scoped = [t for t in turns if t.timestamp.date() >= since]
    return [
        {
            "model": mr.model,
            "cost_usd": mr.cost_usd,
            "turns": mr.turns,
            "input_tokens": mr.input_tokens,
            "output_tokens": mr.output_tokens,
            "cache_read_tokens": mr.cache_read_tokens,
            "tool_error_rate": mr.tool_error_count / mr.tool_use_count if mr.tool_use_count > 0 else 0.0,
        }
        for mr in build_model_rollups(scoped)
    ]


def build_snapshot_full(
    parse_cache: ParseCache,
    all_projects: bool = False,
    reference_usd: float = 50.0,
    tick_seconds: int = 5,
) -> SnapshotResult:
    """Build the full dashboard snapshot, returning payload + raw data for drill-down."""
    assumption_recorder.reset()
    now = datetime.now(tz=timezone.utc)
    today_date = now.date()
    since_90d = today_date - timedelta(days=89)
    since_14d = today_date - timedelta(days=13)
    since_7d = today_date - timedelta(days=6)
    since_24h = today_date  # "last 24h" ≈ today's UTC date

    dirs = get_config_dirs(all_projects=all_projects)
    paths = find_jsonl_files(dirs)

    all_raw_events: list[RawEvent] = []
    active_keys: set[tuple[str, int, int]] = set()
    for path in paths:
        try:
            key, events = parse_cache.get_or_parse(path)
            active_keys.add(key)
            all_raw_events.extend(events)
        except OSError:
            pass

    parse_cache.purge(active_keys)

    all_turns, all_sessions = _build_turns_and_sessions(all_raw_events, paths)

    recent_turns_10h = [t for t in all_turns if t.timestamp >= now - timedelta(hours=10)]
    windows_10h = align_windows(recent_turns_10h)
    active_win = windows_10h[-1] if windows_10h else None
    active_window_data: dict | None = None

    if active_win and active_win.end > now:
        lookback_map = {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "60m": timedelta(hours=1),
        }
        burn_rates: dict[str, float] = {}
        projected_cost = 0.0
        for label, lb in lookback_map.items():
            p = project_window(active_win, now=now, lookback=lb)
            burn_rates[f"burn_rate_usd_per_hour_{label}"] = p["burn_rate_usd_per_hour"]
            if label == "5m":
                projected_cost = p["projected_window_cost"]

        series_start = now - timedelta(hours=1)
        minute_costs: dict[int, float] = defaultdict(float)
        for t in active_win.turns:
            offset_seconds = (t.timestamp - series_start).total_seconds()
            bucket_idx = int(offset_seconds // 60)
            if 0 <= bucket_idx < 60:
                minute_costs[bucket_idx] += t.cost_usd

        burn_series = []
        for i in range(60):
            bucket_start = (series_start + timedelta(minutes=i)).replace(second=0, microsecond=0)
            burn_series.append({
                "t": bucket_start.isoformat(),
                "usd_per_hour": round(minute_costs.get(i, 0.0) * 60, 4),
            })

        active_window_data = {
            "start": active_win.start.isoformat(),
            "end": active_win.end.isoformat(),
            "elapsed_seconds": max(0, int((now - active_win.start).total_seconds())),
            "remaining_seconds": max(0, int((active_win.end - now).total_seconds())),
            "cost_usd": active_win.cost_usd,
            "projected_window_cost": projected_cost,
            "over_reference": projected_cost > reference_usd,
            **burn_rates,
            "burn_rate_series": burn_series,
        }

    today_turns = [t for t in all_turns if t.timestamp.date() == today_date]
    today_hourly_rollups = rollup_by_hour(today_turns, target_date=today_date)
    billable_today = [t for t in today_turns if not t.is_interrupted]
    today_cost = sum(t.cost_usd for t in today_turns)
    today_output = sum(t.usage.output_tokens for t in billable_today)
    today_cache_read = sum(t.usage.cache_read_input_tokens for t in billable_today)
    today_cache_creation = sum(t.usage.cache_creation_input_tokens for t in billable_today)
    today_input = sum(t.usage.input_tokens for t in billable_today)
    today_denom = today_cache_read + today_cache_creation + today_input
    today_data = {
        "date": str(today_date),
        "cost_usd": today_cost,
        "output_tokens": today_output,
        "cache_read_tokens": today_cache_read,
        "hit_rate": today_cache_read / today_denom if today_denom > 0 else 0.0,
        "cost_per_kw": today_cost * 1000 / today_output if today_output > 0 else 0.0,
        "turns": len(today_turns),
        "hourly": [
            {"hour": r.hour.isoformat(), "cost_usd": r.cost_usd, "turns": r.turns}
            for r in today_hourly_rollups
        ],
    }

    turns_90d = [t for t in all_turns if t.timestamp.date() >= since_90d]
    daily_rollups = rollup_by_date(turns_90d, since=since_90d)
    daily_90d = []
    for r in daily_rollups:
        denom = r.cache_read_tokens + r.cache_creation_tokens + r.input_tokens
        daily_90d.append({
            "date": str(r.date),
            "cost_usd": r.cost_usd,
            "output_tokens": r.output_tokens,
            "cost_per_kw": r.cost_usd * 1000 / r.output_tokens if r.output_tokens > 0 else 0.0,
            "hit_rate": r.cache_read_tokens / denom if denom > 0 else 0.0,
        })

    turns_14d = [t for t in all_turns if t.timestamp.date() >= since_14d]

    sessions_14d_objs = [
        s for s in all_sessions
        if s.turns and s.turns[-1].timestamp.date() >= since_14d
    ]
    all_rollups_14d = []
    for s in sessions_14d_objs:
        sr = build_session_rollup(s)
        sr.verdict = compute_verdict(sr)
        all_rollups_14d.append(sr)

    sessions_by_range = {
        "24h": _sessions_in_window(all_rollups_14d, since_24h, top=50),
        "7d":  _sessions_in_window(all_rollups_14d, since_7d, top=50),
        "14d": _sessions_in_window(all_rollups_14d, since_14d, top=50),
    }
    projects_by_range = {
        "24h": _projects_in_window(all_rollups_14d, since_24h),
        "7d":  _projects_in_window(all_rollups_14d, since_7d),
        "14d": _projects_in_window(all_rollups_14d, since_14d),
    }
    models_by_range = {
        "24h": _models_in_window(turns_14d, since_24h),
        "7d":  _models_in_window(turns_14d, since_7d),
        "14d": _models_in_window(turns_14d, since_14d),
    }

    heatmap_dates = [since_14d + timedelta(days=i) for i in range(14)]
    cell_costs: dict[tuple[date, int], float] = defaultdict(float)
    for turn in turns_14d:
        utc = turn.timestamp.astimezone(timezone.utc)
        cell_costs[(utc.date(), utc.hour)] += turn.cost_usd
    heatmap_14d = {
        "dates": [str(d) for d in heatmap_dates],
        "hours": list(range(24)),
        "cells": [
            [round(cell_costs.get((d, h), 0.0), 4) for h in range(24)]
            for d in heatmap_dates
        ],
    }

    recent_turns_list = [
        {
            "ts": t.timestamp.isoformat(),
            "session_id": t.session_id,
            "model": t.model,
            "cost_usd": t.cost_usd,
            "input_tokens": t.usage.input_tokens,
            "output_tokens": t.usage.output_tokens,
            "cache_read_tokens": t.usage.cache_read_input_tokens,
            "is_sidechain": t.is_sidechain,
            "tool_use_count": t.tool_use_count,
            "tool_error_count": t.tool_error_count,
        }
        for t in heapq.nlargest(20, all_turns, key=lambda t: t.timestamp)
    ]

    assumptions_fired = {
        tag.value: count for tag, count in assumption_recorder.fired().items()
    }

    payload = {
        "generated_at": now.isoformat(),
        "config": {
            "reference_usd": reference_usd,
            "all_projects": all_projects,
            "tick_seconds": tick_seconds,
        },
        "active_window": active_window_data,
        "today": today_data,
        "daily_90d": daily_90d,
        "sessions": sessions_by_range,
        "projects": projects_by_range,
        "models": models_by_range,
        "heatmap_14d": heatmap_14d,
        "recent_turns": recent_turns_list,
        "assumptions_fired": assumptions_fired,
    }

    return SnapshotResult(payload=payload, turns=all_turns, sessions=all_sessions)
