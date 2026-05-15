"""Server-side parse cache and snapshot builder.

Thread-safety invariant: ParseCache._store is mutated only under _lock.
All code paths that call get_or_parse / purge acquire the lock first.
REST endpoints and the SSE loop both go through build_snapshot_full, which
holds the lock for the duration of a single parse sweep.
"""

from __future__ import annotations

import base64
import bisect
import itertools
import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tokenol.enums import AssumptionTag, BlowUpVerdict
from tokenol.ingest.discovery import find_jsonl_files, get_config_dirs, select_edge_paths
from tokenol.ingest.parser import dedup_key, parse_file

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
from tokenol.metrics.context import (
    cache_hit_pct,
    cache_hit_rate,
    cache_reuse_n_to_1,
    cost_per_kw,
    ctx_ratio_n_to_1,
)
from tokenol.metrics.cost import cost_for_turn, rollup_by_date, rollup_by_hour
from tokenol.metrics.history import baseline_median, trailing_median
from tokenol.metrics.rollups import (
    SessionRollup,
    build_model_rollups,
    build_project_rollups,
    build_session_rollup,
    build_tool_cost_daily,
)
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.metrics.verdicts import compute_verdict
from tokenol.model.events import RawEvent, Session, Turn, Usage
from tokenol.model.pricing import context_window

log = logging.getLogger(__name__)


@dataclass
class ParseCache:
    """JSONL parse cache keyed by (path_str, size, mtime_ns).

    All public methods are thread-safe via an internal lock.

    Also memoizes the derived (turns, sessions) tuple keyed on the active set of
    parse keys: when no JSONL file changed since the last build, build_snapshot_full
    can skip the O(total events) re-derivation. The memo is invalidated automatically
    on any get_or_parse miss or any purge that drops a key.
    """

    _store: dict[tuple[str, int, int], list[RawEvent]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _derived_keys: frozenset[tuple[str, int, int]] | None = None
    _derived: tuple[list[Turn], list[Session], Counter[AssumptionTag]] | None = None

    def get_or_parse(self, path: Path) -> tuple[tuple[str, int, int], list[RawEvent]]:
        """Return (cache_key, events). Parses only when (size, mtime_ns) changes."""
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if key not in self._store:
                self._store[key] = list(parse_file(path))
                # New file content invalidates any derived memo.
                self._derived_keys = None
                self._derived = None
            return key, self._store[key]

    def purge(self, keep_keys: set[tuple[str, int, int]]) -> None:
        """Remove entries not in keep_keys (stale / deleted files)."""
        with self._lock:
            stale = [k for k in self._store if k not in keep_keys]
            for k in stale:
                del self._store[k]
            if stale:
                self._derived_keys = None
                self._derived = None

    def get_derived(
        self,
        keys: frozenset[tuple[str, int, int]],
        builder: Callable[[list[RawEvent]], tuple[list[Turn], list[Session], Counter[AssumptionTag]]],
    ) -> tuple[list[Turn], list[Session], Counter[AssumptionTag]]:
        """Return memoized (turns, sessions, fired_counts) for the given key set; rebuild on miss.

        The memo is keyed on `keys` (the set of active ParseCache entries). When no
        file changed mtime/size since the last build, the cached derivation is
        returned without re-iterating events — this is the dominant per-tick cost.
        """
        with self._lock:
            if self._derived is not None and self._derived_keys == keys:
                return self._derived
            # Build outside the parse-cache invariant by snapshotting events first.
            events: list[RawEvent] = []
            for k in keys:
                bucket = self._store.get(k)
                if bucket is not None:
                    events.extend(bucket)
        # Builder runs without the lock — it's pure CPU over a private list.
        derived = builder(events)
        with self._lock:
            self._derived = derived
            self._derived_keys = keys
        return derived

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)


def _build_turns_and_sessions(
    all_events: list[RawEvent],
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag]]:
    """Build deduplicated turns and sessions from pre-parsed raw events.

    Returns the (turns, sessions, fired-assumption-counts). Fired counts are returned
    rather than written to a global recorder so the result is referentially
    transparent and safe to memoize on ParseCache.
    """
    seen: dict[str, tuple[RawEvent, str]] = {}
    passthroughs: list[tuple[RawEvent, None]] = []
    cwd_by_session: dict[str, str] = {}
    session_source: dict[str, str] = {}
    fired: Counter[AssumptionTag] = Counter()

    for ev in all_events:
        if ev.cwd and ev.session_id not in cwd_by_session:
            cwd_by_session[ev.session_id] = ev.cwd
        if ev.session_id not in session_source:
            session_source[ev.session_id] = ev.source_file
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
        for tag in tags:
            fired[tag] += 1

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
            tool_names=ev.tool_names,
            tool_costs=ev.tool_costs,
            unattributed_input_tokens=ev.unattributed_input_tokens,
            unattributed_output_tokens=ev.unattributed_output_tokens,
            unattributed_cost_usd=ev.unattributed_cost_usd,
        ))

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
    return turns, sessions, fired


def derive_delta_turns(
    new_events: list[RawEvent],
    existing_dedup_keys: set[str],
    existing_passthrough_locations: set[tuple[str, int]],
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag], set[tuple[str, int]]]:
    """Build new Turn/Session deltas from events not already represented in memory.

    *existing_dedup_keys* is the set of dedup keys already in the in-memory hot tier.
    *existing_passthrough_locations* is the set of (source_file, line_number) tuples
    for passthrough turns already emitted (passthroughs lack dedup keys).

    Returns (turns, sessions, fired_assumptions, accepted_passthrough_locs):
    - *turns* and *sessions* are new records to append; never touches existing state.
    - *fired_assumptions* tracks which assumption tags fired during derivation.
    - *accepted_passthrough_locs* is the set of (source_file, line_number) tuples for
      passthroughs that were actually emitted (excluding synthetic models and already-known).
    Within-batch dedup follows the existing last-wins rule.
    """
    seen: dict[str, tuple[RawEvent, str]] = {}
    passthroughs: list[tuple[RawEvent, None]] = []
    accepted_passthrough_locs: set[tuple[str, int]] = set()
    cwd_by_session: dict[str, str] = {}
    session_source: dict[str, str] = {}
    fired: Counter[AssumptionTag] = Counter()

    for ev in new_events:
        if ev.cwd and ev.session_id not in cwd_by_session:
            cwd_by_session[ev.session_id] = ev.cwd
        if ev.session_id not in session_source:
            session_source[ev.session_id] = ev.source_file
        if ev.event_type != "assistant":
            continue
        if ev.model == "<synthetic>":
            continue
        k = dedup_key(ev)
        if k is None:
            loc = (ev.source_file, ev.line_number)
            if loc in existing_passthrough_locations:
                continue
            passthroughs.append((ev, None))
            accepted_passthrough_locs.add(loc)
        else:
            if k in existing_dedup_keys:
                continue
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
        for tag in tags:
            fired[tag] += 1

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
            tool_names=ev.tool_names,
            tool_costs=ev.tool_costs,
            unattributed_input_tokens=ev.unattributed_input_tokens,
            unattributed_output_tokens=ev.unattributed_output_tokens,
            unattributed_cost_usd=ev.unattributed_cost_usd,
        ))

    # Build *delta* Session records for any session_id we touched (one Session per
    # new sid, with empty turns list — caller appends turns to its own session map).
    sessions: list[Session] = []
    seen_sids: set[str] = set()
    for t in turns:
        if t.session_id in seen_sids:
            continue
        seen_sids.add(t.session_id)
        sessions.append(Session(
            session_id=t.session_id,
            source_file=session_source.get(t.session_id, ""),
            is_sidechain=t.is_sidechain,
            cwd=cwd_by_session.get(t.session_id),
            turns=[],  # caller appends to its own session's turn list
        ))

    return turns, sessions, fired, accepted_passthrough_locs


@dataclass
class SnapshotResult:
    payload: dict
    turns: list[Turn]
    sessions: list[Session]


def encode_cwd(cwd: str) -> str:
    """Encode a filesystem path to a URL-safe base64 string (no padding)."""
    return base64.urlsafe_b64encode(cwd.encode()).rstrip(b"=").decode()


def decode_cwd(cwd_b64: str) -> str:
    """Decode a base64-urlsafe cwd string (no padding) to the original path."""
    padding = 4 - len(cwd_b64) % 4
    padded = cwd_b64 + "=" * (padding % 4)
    return base64.urlsafe_b64decode(padded).decode()


# ---------------------------------------------------------------------------
# Helpers retained for drilldown endpoints (build_project_detail, build_day_detail)
# ---------------------------------------------------------------------------


def _session_rollup_to_dict(sr: SessionRollup) -> dict:
    hit = cache_hit_rate(sr.cache_read_tokens, sr.cache_creation_tokens, sr.input_tokens)
    tool_error_rate = sr.tool_error_count / sr.tool_use_count if sr.tool_use_count > 0 else 0.0

    return {
        "id": sr.session_id,
        "model": sr.model,
        "first_ts": sr.first_ts.isoformat(),
        "last_ts": sr.last_ts.isoformat(),
        "cost_usd": sr.cost_usd,
        "turns": sr.turns,
        "verdict": sr.verdict.value,
        "cwd": sr.cwd or "",
        "context_growth_rate": sr.context_growth_rate_val,
        "cache_hit_rate": hit if hit is not None else 0.0,
        "tool_error_rate": tool_error_rate,
        "cost_per_kw": sr.cost_per_kw_val,
        "ctx_ratio": sr.ctx_ratio_n_to_1,
    }


def _verdict_dist(rollups: list[SessionRollup]) -> dict[str, int]:
    dist = {v.value: 0 for v in BlowUpVerdict}
    for sr in rollups:
        dist[sr.verdict.value] += 1
    return dist


def _sessions_in_window(
    rollups: list[SessionRollup], since: date, top: int
) -> list[dict]:
    filtered = [sr for sr in rollups if sr.last_ts.date() >= since]
    filtered.sort(key=lambda sr: sr.cost_usd, reverse=True)
    return [_session_rollup_to_dict(sr) for sr in filtered[:top]]


def _projects_in_window(rollups: list[SessionRollup], since: date) -> list[dict]:
    scoped = [sr for sr in rollups if sr.last_ts.date() >= since]
    result = []
    for pr in build_project_rollups(scoped):
        result.append({
            "cwd": pr.cwd,
            "cwd_b64": encode_cwd(pr.cwd),
            "cost_usd": pr.cost_usd,
            "sessions": pr.sessions,
            "turns": pr.turns,
            "cache_reuse_ratio": pr.cache_reuse_ratio,
            "cache_hit_rate": pr.cache_hit_rate,
            "avg_context_growth": pr.avg_context_growth,
            "tool_error_rate": pr.tool_error_rate,
            "interrupted_turn_rate": pr.interrupted_turn_rate,
            "verdict_mix": pr.verdict_mix,
            "flagged": pr.flagged,
        })
    return result


def _models_in_window(turns: list[Turn], since: date) -> list[dict]:
    scoped = [t for t in turns if t.timestamp.date() >= since]
    result = []
    for mr in build_model_rollups(scoped):
        sidechain_ratio = mr.sidechain_turns / mr.turns if mr.turns > 0 else 0.0
        interrupted_turn_rate = mr.interrupted_turns / mr.turns if mr.turns > 0 else 0.0
        result.append({
            "model": mr.model,
            "cost_usd": mr.cost_usd,
            "turns": mr.turns,
            "input_tokens": mr.input_tokens,
            "output_tokens": mr.output_tokens,
            "cache_read_tokens": mr.cache_read_tokens,
            "cache_creation_tokens": mr.cache_creation_tokens,
            "tool_error_rate": mr.tool_error_count / mr.tool_use_count if mr.tool_use_count > 0 else 0.0,
            "sidechain_ratio": sidechain_ratio,
            "interrupted_turn_rate": interrupted_turn_rate,
            "cost_breakdown": {
                "input_usd": mr.input_usd,
                "output_usd": mr.output_usd,
                "cache_read_usd": mr.cache_read_usd,
                "cache_creation_usd": mr.cache_creation_usd,
            },
        })
    return result





def _short_model(model: str | None) -> str:
    """Shorten a full model ID: "claude-opus-4-7-20250514" → "opus-4-7"."""
    if not model:
        return "(unknown)"
    parts = model.split("-")
    if parts and parts[0] == "claude":
        parts = parts[1:]
    if parts and len(parts[-1]) >= 8 and parts[-1].isdigit():
        parts = parts[:-1]
    return "-".join(parts) if parts else model


def _series_for_key(series: list[dict], key: str) -> list[dict]:
    """Filter *series* to entries where *key* is non-None (for baseline_median)."""
    return [{"date": r["date"], key: r[key]} for r in series if r.get(key) is not None]


def _build_daily_series(turns: list[Turn], since: date) -> list[dict]:
    result = []
    for r in rollup_by_date(turns, since=since):
        result.append({
            "date": str(r.date),
            "cost_usd": r.cost_usd,
            "output_tokens": r.output_tokens,
            "turns": r.turns,
            "hit_pct": cache_hit_pct(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens),
            "cost_per_kw": cost_per_kw(r.cost_usd, r.output_tokens),
            "ctx_ratio": ctx_ratio_n_to_1(r.cache_read_tokens, r.output_tokens),
            "cache_reuse": cache_reuse_n_to_1(r.cache_read_tokens, r.cache_creation_tokens),
        })
    first_active = next((i for i, r in enumerate(result) if r["turns"] > 0), len(result))
    return result[first_active:]


def _moving_avg_7d(series: list[dict], key: str) -> list[dict]:
    values = [r.get(key) for r in series]
    result = []
    for i, r in enumerate(series):
        window = [v for v in values[max(0, i - 6):i + 1] if v is not None]
        result.append({"date": r["date"], "value": sum(window) / len(window) if window else None})
    return result


def _build_topbar(today_turns: list[Turn]) -> dict:
    model_costs: defaultdict[str, float] = defaultdict(float)
    session_ids: set[str] = set()
    today_cost = 0.0
    billable_out = billable_inp = 0
    last_active: datetime | None = None
    for t in today_turns:
        today_cost += t.cost_usd
        session_ids.add(t.session_id)
        if t.model:
            model_costs[t.model] += t.cost_usd
        if not t.is_interrupted:
            billable_out += t.usage.output_tokens
            billable_inp += t.usage.input_tokens
        if last_active is None or t.timestamp > last_active:
            last_active = t.timestamp
    total_mc = sum(model_costs.values()) or 1.0
    model_mix = {
        _short_model(m): c / total_mc
        for m, c in sorted(model_costs.items(), key=lambda x: -x[1])
    }
    return {
        "today_cost": today_cost,
        "sessions_count": len(session_ids),
        "output_tokens": billable_out,
        "input_tokens": billable_inp,
        "model_mix": model_mix,
        "last_active": last_active.isoformat() if last_active else None,
    }


def _tile_metrics(turns: list[Turn]) -> dict[str, float | None]:
    """Compute the 4 headline metrics over a slice of turns."""
    cr = cc = inp = out = 0
    cost = 0.0
    for t in turns:
        if t.is_interrupted:
            continue
        u = t.usage
        cr += u.cache_read_input_tokens
        cc += u.cache_creation_input_tokens
        inp += u.input_tokens
        out += u.output_tokens
        cost += t.cost_usd
    return {
        "hit_pct":     cache_hit_pct(cr, cc, inp),
        "cost_per_kw": cost_per_kw(cost, out),
        "ctx_ratio":   ctx_ratio_n_to_1(cr, out),
        "cache_reuse": cache_reuse_n_to_1(cr, cc),
    }


def _build_tiles(
    period_turns: list[Turn],
    daily_90d: list[dict],
    today_date: date,
    thresholds: dict,
    now: datetime,
) -> dict:
    m = _tile_metrics(period_turns)
    hit_val, cpk_val, ctx_val, reuse_val = m["hit_pct"], m["cost_per_kw"], m["ctx_ratio"], m["cache_reuse"]

    # Last-hour slice: shows whether the period aggregate still reflects current
    # behaviour or is dominated by an early-period spike (e.g. cache thrashing
    # at 8am pulls the today-total cache_reuse down and keeps it low even after
    # recovery, because subsequent turns have tiny absolute cache_creation).
    last_hour_cutoff = now - timedelta(hours=1)
    last_hour_turns = [t for t in period_turns if t.timestamp >= last_hour_cutoff]
    lh = _tile_metrics(last_hour_turns) if last_hour_turns else {
        "hit_pct": None, "cost_per_kw": None, "ctx_ratio": None, "cache_reuse": None,
    }

    b_hit, hit_lbl = baseline_median(_series_for_key(daily_90d, "hit_pct"), today_date, key="hit_pct")
    b_cpk, cpk_lbl = baseline_median(_series_for_key(daily_90d, "cost_per_kw"), today_date, key="cost_per_kw")
    b_ctx, ctx_lbl = baseline_median(_series_for_key(daily_90d, "ctx_ratio"), today_date, key="ctx_ratio")
    b_reuse, reuse_lbl = baseline_median(_series_for_key(daily_90d, "cache_reuse"), today_date, key="cache_reuse")

    def _delta(val: float | None, base: float | None) -> float | None:
        if val is None or base is None or base == 0:
            return None
        return val / base

    return {
        "hit_pct": {
            "value": hit_val,
            "last_hour_value": lh["hit_pct"],
            "delta_ratio": _delta(hit_val, b_hit),
            "baseline_label": hit_lbl,
            "goal": {"good_gte": thresholds["hit_rate_good_pct"], "red_lt": thresholds["hit_rate_red_pct"]},
        },
        "cost_per_kw": {
            "value": cpk_val,
            "last_hour_value": lh["cost_per_kw"],
            "delta_ratio": _delta(cpk_val, b_cpk),
            "baseline_label": cpk_lbl,
            "goal": {"good_lte": thresholds["cost_per_kw_good"], "red_gt": thresholds["cost_per_kw_red"]},
        },
        "ctx_ratio": {
            "value": ctx_val,
            "last_hour_value": lh["ctx_ratio"],
            "delta_ratio": _delta(ctx_val, b_ctx),
            "baseline_label": ctx_lbl,
            "goal": {"red_gt": thresholds["ctx_ratio_red"]},
        },
        "cache_reuse": {
            "value": reuse_val,
            "last_hour_value": lh["cache_reuse"],
            "delta_ratio": _delta(reuse_val, b_reuse),
            "baseline_label": reuse_lbl,
            "goal": {"good_gte": thresholds["cache_reuse_good"], "red_lt": thresholds["cache_reuse_red"]},
        },
    }


def _build_anomaly(tiles: dict, today_date: date) -> dict | None:
    href = f"/day/{today_date}"

    def _active(tile: dict) -> tuple[float, float] | None:
        dr, val = tile.get("delta_ratio"), tile.get("value")
        if dr is None or val is None or tile.get("baseline_label") == "cold":
            return None
        return (dr, val)

    if av := _active(tiles["cache_reuse"]):
        dr, val = av
        if dr < 0.4:
            return {"severity": "red", "drilldown_href": href,
                    "message": f"Cache reuse dropped to {val:.0f}:1 (normal {val / dr:.0f}:1) — cache thrashing."}

    if av := _active(tiles["hit_pct"]):
        dr, val = av
        if dr < 0.88:
            return {"severity": "amber", "drilldown_href": href,
                    "message": f"Cache hit rate {val:.1f}% (normal {val / dr:.1f}%) — below expected."}

    if av := _active(tiles["cost_per_kw"]):
        dr, val = av
        if dr > 2.5:
            return {"severity": "amber", "drilldown_href": href,
                    "message": f"Cost efficiency ${val:.2f}/kW ({dr:.1f}× baseline) — high relative spend."}

    return None


def _disambiguate_cwd_labels(cwds: set[str] | list[str]) -> dict[str, str]:
    """Return {cwd: label} where each label is the shortest path suffix unique among *cwds*.

    Non-colliding cwds get the plain basename. Colliding ones get parent segments
    prepended until each is globally unique — e.g. ``/.../mercor/agentic-bench-gh8nb``
    and ``/.../agentic-bench-gh8nb`` resolve to ``mercor/agentic-bench-gh8nb`` and
    ``dev/agentic-bench-gh8nb`` respectively.
    """
    labels = {cwd: _cwd_basename(cwd) for cwd in cwds}
    groups: dict[str, list[str]] = {}
    for cwd, lbl in labels.items():
        groups.setdefault(lbl, []).append(cwd)
    for colliding in groups.values():
        if len(colliding) < 2:
            continue
        segs = 2
        while True:
            candidates = {
                cwd: "/".join(cwd.strip("/").split("/")[-segs:]) or "–"
                for cwd in colliding
            }
            if len(set(candidates.values())) == len(colliding):
                labels.update(candidates)
                break
            segs += 1
            if segs > 20:  # pathological — fall back to full paths
                labels.update({cwd: cwd for cwd in colliding})
                break
    return labels


def _active_entities(turns: list[Turn], cwd_by_sid: dict[str, str]) -> dict[str, list[dict]]:
    """Distinct cwds and models active across *turns*, formatted for a UI dropdown.

    Excludes the '(unknown)' cwd (turns whose session isn't indexed) — compare views
    treat those as noise. Results are sorted alphabetically by display label so the
    dropdown is stable and scannable. Labels are disambiguated when multiple cwds
    share a basename.
    """
    cwds: set[str] = set()
    models_by_short: dict[str, str] = {}
    for t in turns:
        cwd = cwd_by_sid.get(t.session_id, "(unknown)")
        if cwd != "(unknown)":
            cwds.add(cwd)
        if t.model:
            models_by_short.setdefault(_short_model(t.model), t.model)
    cwd_labels = _disambiguate_cwd_labels(cwds)
    projects = sorted(
        ({"label": cwd_labels[c], "value": c} for c in cwds),
        key=lambda p: (p["label"].lower(), p["value"]),
    )
    models = sorted(
        ({"label": s, "value": m} for s, m in models_by_short.items()),
        key=lambda p: p["label"].lower(),
    )
    return {"active_projects": projects, "active_models": models}


def _build_hourly(
    today_turns: list[Turn],
    today_date: date,
    all_turns: list[Turn],
    cwd_by_sid: dict[str, str],
) -> dict:
    series = []
    for r in rollup_by_hour(today_turns, target_date=today_date, fill_day=True):
        series.append({
            "hour": r.hour.isoformat(),
            "cost_usd": r.cost_usd,
            "turns": r.turns,
            "hit_pct": cache_hit_pct(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens),
        })
    earliest = min((t.timestamp.date() for t in all_turns), default=today_date)
    return {
        "date": str(today_date),
        "earliest_available": str(earliest),
        "series": series,
        **_active_entities(today_turns, cwd_by_sid),
    }


def _build_daily(
    daily_90d: list[dict],
    period: str,
    today_date: date,
    range_turns: list[Turn],
    cwd_by_sid: dict[str, str],
) -> dict:
    since_30d = today_date - timedelta(days=29)
    series = [r for r in daily_90d if date.fromisoformat(r["date"]) >= since_30d]
    earliest = daily_90d[0]["date"] if daily_90d else str(today_date)
    return {
        "range": period if period != "today" else "30d",
        "earliest_available": earliest,
        "series": series,
        "moving_avg_7d": _moving_avg_7d(series, "hit_pct"),
        **_active_entities(range_turns, cwd_by_sid),
    }


def _build_models(period_turns: list[Turn], range_label: str) -> dict:
    rollups = build_model_rollups(period_turns)
    rows = []
    for mr in rollups:
        cw = context_window(mr.model)
        rows.append({
            "model": mr.model,
            "short_name": _short_model(mr.model),
            "context_window_k": cw // 1000 if cw else None,
            "cost_usd": mr.cost_usd,
            "cost_share": mr.cost_share,
            "turns": mr.turns,
            "output_tokens": mr.output_tokens,
            "hit_pct": cache_hit_pct(mr.cache_read_tokens, mr.cache_creation_tokens, mr.input_tokens),
            "cost_per_kw": mr.cost_per_kw_val,
            "ctx_ratio": mr.ctx_ratio_n_to_1,
            "cache_reuse": mr.cache_reuse_n_to_1,
            "tool_error_rate": mr.tool_error_count / mr.tool_use_count if mr.tool_use_count > 0 else None,
            "input_usd": mr.input_usd,
            "output_usd": mr.output_usd,
            "cache_read_usd": mr.cache_read_usd,
            "cache_creation_usd": mr.cache_creation_usd,
        })
    dominant = rollups[0] if rollups else None
    return {
        "range": range_label,
        "rows": rows,
        "aggregate": {
            "active_count": len(rollups),
            "dominant": _short_model(dominant.model) if dominant else None,
            "dominant_share": dominant.cost_share if dominant else None,
            "cost_split": {_short_model(mr.model): mr.cost_share for mr in rollups if mr.cost_share is not None},
        },
    }


def _build_recent_activity(
    all_turns: list[Turn],
    cwd_by_sid: dict[str, str],
    now: datetime,
    window_minutes: int = 60,
) -> dict:
    window_since = now - timedelta(minutes=window_minutes)
    window_turns = [t for t in all_turns if t.timestamp >= window_since]

    proj_turns: dict[str, list[Turn]] = defaultdict(list)
    for t in window_turns:
        proj_turns[cwd_by_sid.get(t.session_id, "(unknown)")].append(t)

    cr_all = cc_all = inp_all = out_all = 0
    cost_all = 0.0
    model_costs_all: defaultdict[str, float] = defaultdict(float)
    for t in window_turns:
        if not t.is_interrupted:
            cr_all   += t.usage.cache_read_input_tokens
            cc_all   += t.usage.cache_creation_input_tokens
            inp_all  += t.usage.input_tokens
            out_all  += t.usage.output_tokens
            cost_all += t.cost_usd
        if t.model:
            model_costs_all[t.model] += t.cost_usd
    total_mc = sum(model_costs_all.values()) or 1.0
    model_mix = {
        _short_model(m): c / total_mc
        for m, c in sorted(model_costs_all.items(), key=lambda x: -x[1])
    }

    proj_stats: dict[str, dict] = {}
    for cwd, turns in proj_turns.items():
        cr = cc = inp = out = 0
        cost = 0.0
        model_ctr: Counter = Counter()
        last_turn: Turn | None = None
        for t in turns:
            if not t.is_interrupted:
                cr += t.usage.cache_read_input_tokens
                cc += t.usage.cache_creation_input_tokens
                inp += t.usage.input_tokens
                out += t.usage.output_tokens
                cost += t.cost_usd
            if t.model:
                model_ctr[t.model] += 1
            if last_turn is None or t.timestamp > last_turn.timestamp:
                last_turn = t
        proj_stats[cwd] = {
            "turns": turns, "cr": cr, "cc": cc, "inp": inp, "out": out, "cost": cost,
            "model_ctr": model_ctr, "last_turn": last_turn,
        }

    rows = []
    for cwd, s in sorted(proj_stats.items(), key=lambda x: -x[1]["cost"]):
        turns = s["turns"]
        cr, cc, inp, out, cost = s["cr"], s["cc"], s["inp"], s["out"], s["cost"]
        model_ctr = s["model_ctr"]
        last_turn = s["last_turn"]
        cw = context_window(last_turn.model or "")
        visible = (
            last_turn.usage.input_tokens
            + last_turn.usage.cache_read_input_tokens
            + last_turn.usage.cache_creation_input_tokens
        )
        rows.append({
            "cwd": cwd,
            "cwd_b64": encode_cwd(cwd),
            "model_primary": _short_model(model_ctr.most_common(1)[0][0]) if model_ctr else "(unknown)",
            "last_turn_at": last_turn.timestamp.isoformat(),
            "latest_session_id": last_turn.session_id,
            "turns": len(turns),
            "output": out,
            "ctx_used": visible / cw if cw else None,
            "ctx_used_abs": {"visible": visible, "window": cw} if cw else None,
            "cost_per_kw": cost_per_kw(cost, out),
            "ctx_ratio": ctx_ratio_n_to_1(cr, out),
            "cache_reuse": cache_reuse_n_to_1(cr, cc),
            "hit_pct": cache_hit_pct(cr, cc, inp),
            "verdict": BlowUpVerdict.OK.value,
        })

    return {
        "window": f"{window_minutes}m",
        "aggregate": {
            "projects": len(proj_turns),
            "turns": len(window_turns),
            "output": out_all,
            "cost": cost_all,
            "model_mix": model_mix,
            "hit_pct": cache_hit_pct(cr_all, cc_all, inp_all),
            "cost_per_kw": cost_per_kw(cost_all, out_all),
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Store-backed derivation helper
# ---------------------------------------------------------------------------


def _store_backed_derivation(
    parse_cache: ParseCache,
    paths: list[Path],
    history_store: HistoryStore,
    flush_queue: FlushQueue | None,
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag]]:
    """Hot-tier-aware derivation: hydrate from store on first call, then append deltas.

    The hot-tier caches live as duck-typed attributes on the ParseCache instance so
    they survive across ticks within a single process. They are intentionally NOT
    declared on ParseCache itself (the dataclass invariants there are about the
    JSONL parse cache; we layer hot-tier state on top without altering that).
    """
    if not getattr(parse_cache, "_hot_initialized", False):
        window_days = getattr(history_store, "_hot_window_days", 90)
        hot_turns, hot_sessions = history_store.hydrate_hot(window_days=window_days)
        parse_cache._hot_turns = hot_turns
        parse_cache._hot_sessions_by_id = {s.session_id: s for s in hot_sessions}
        parse_cache._known_dedup_keys = {t.dedup_key for t in hot_turns}
        parse_cache._known_passthrough_locs = set()
        parse_cache._last_ts_by_session = history_store.last_ts_by_session()
        parse_cache._last_mtime_ns_by_path = {}  # populated below as files are parsed
        parse_cache._fired = Counter()
        parse_cache._hot_initialized = True

    # Filter to edge paths (mtime_ns differs from persisted high-water mark).
    edge_paths = select_edge_paths(paths, parse_cache._last_mtime_ns_by_path)

    # Parse only edge files; record current mtime_ns for next tick's gate.
    new_events: list[RawEvent] = []
    for p in edge_paths:
        try:
            _key, evs = parse_cache.get_or_parse(p)
            parse_cache._last_mtime_ns_by_path[p] = p.stat().st_mtime_ns
            new_events.extend(evs)
        except OSError:
            continue

    if new_events:
        delta_turns, delta_sessions, fired, accepted_passthrough_locs = derive_delta_turns(
            new_events,
            parse_cache._known_dedup_keys,
            parse_cache._known_passthrough_locs,
        )
        # Append new turns to hot tier and update bookkeeping.
        for t in delta_turns:
            parse_cache._hot_turns.append(t)
            parse_cache._known_dedup_keys.add(t.dedup_key)
        # Add new session metadata for previously-unseen session_ids.
        for s in delta_sessions:
            if s.session_id not in parse_cache._hot_sessions_by_id:
                parse_cache._hot_sessions_by_id[s.session_id] = s
        # Track passthrough locations from the SAME source of truth as derive_delta_turns
        # (which knows about the synthetic-model and already-known filters).
        parse_cache._known_passthrough_locs.update(accepted_passthrough_locs)
        # Refresh per-session high-water marks from the new turns.
        for t in delta_turns:
            cur = parse_cache._last_ts_by_session.get(t.session_id)
            if cur is None or t.timestamp > cur:
                parse_cache._last_ts_by_session[t.session_id] = t.timestamp
        # Attach new turns to their in-memory Session objects so drilldowns work.
        for t in delta_turns:
            sess = parse_cache._hot_sessions_by_id.get(t.session_id)
            if sess is not None:
                sess.turns.append(t)
        parse_cache._fired.update(fired)
        # Queue deltas for background flush.
        if flush_queue is not None:
            sessions_to_flush = [
                parse_cache._hot_sessions_by_id[s.session_id]
                for s in delta_sessions
                if s.session_id in parse_cache._hot_sessions_by_id
            ]
            flush_queue.enqueue(delta_turns, sessions_to_flush)

    # Mark sessions whose JSONL is no longer on disk as archived.
    live_sids = {p.stem for p in paths}
    for sid, sess in parse_cache._hot_sessions_by_id.items():
        sess.archived = sid not in live_sids

    return (
        list(parse_cache._hot_turns),
        list(parse_cache._hot_sessions_by_id.values()),
        Counter(parse_cache._fired),
    )


# ---------------------------------------------------------------------------
# Main snapshot builder
# ---------------------------------------------------------------------------


def build_snapshot_full(
    parse_cache: ParseCache,
    all_projects: bool = False,
    reference_usd: float = 50.0,
    tick_seconds: int = 5,
    period: str = "today",
    thresholds: dict | None = None,
    history_store: HistoryStore | None = None,
    flush_queue: FlushQueue | None = None,
) -> SnapshotResult:
    """Build the dashboard snapshot.

    When *history_store* is supplied:
    - First call: hydrate hot tier from DuckDB.
    - Every call: parse only edge JSONLs (mtime > persisted mark) and append
      derived deltas to the in-memory hot tier. New turns are queued on
      *flush_queue* if present.

    When *history_store* is None (CLI report tools, existing tests):
    - Falls back to today's full re-derivation behavior. Backwards-compatible.
    """
    now = datetime.now(tz=timezone.utc)
    today_date = now.date()
    since_90d = today_date - timedelta(days=89)

    dirs = get_config_dirs(all_projects=all_projects)
    paths = find_jsonl_files(dirs)

    if history_store is not None:
        all_turns, all_sessions, _fired = _store_backed_derivation(
            parse_cache, paths, history_store, flush_queue
        )
    else:
        active_keys: set[tuple[str, int, int]] = set()
        for path in paths:
            try:
                key, _events = parse_cache.get_or_parse(path)
                active_keys.add(key)
            except OSError:
                pass
        parse_cache.purge(active_keys)
        all_turns, all_sessions, _fired = parse_cache.get_derived(
            frozenset(active_keys), _build_turns_and_sessions
        )

    turns_90d = [t for t in all_turns if t.timestamp.date() >= since_90d]
    daily_90d = _build_daily_series(turns_90d, since_90d)

    cwd_by_sid = _grouped_cwd_by_sid(all_sessions)

    period_since = range_since(period, today_date)
    period_turns = (
        [t for t in all_turns if t.timestamp.date() >= period_since]
        if period_since is not None
        else list(all_turns)
    )
    today_turns = [t for t in all_turns if t.timestamp.date() == today_date]

    thresholds = thresholds if thresholds is not None else dict(DEFAULTS)

    tiles = _build_tiles(period_turns, daily_90d, today_date, thresholds, now)
    topbar = _build_topbar(today_turns)
    anomaly = _build_anomaly(tiles, today_date)
    # Daily default is a 30-day window regardless of global period.
    daily_default_since = today_date - timedelta(days=29)
    daily_default_turns = [t for t in turns_90d if t.timestamp.date() >= daily_default_since]
    hourly = _build_hourly(today_turns, today_date, all_turns, cwd_by_sid)
    daily = _build_daily(daily_90d, period, today_date, daily_default_turns, cwd_by_sid)
    models = _build_models(period_turns, period)
    recent_activity = _build_recent_activity(all_turns, cwd_by_sid, now)

    payload = {
        "generated_at": now.isoformat(),
        "config": {"reference_usd": reference_usd, "tick_seconds": tick_seconds},
        "thresholds": thresholds,
        "period": period,
        "topbar_summary": topbar,
        "tiles": tiles,
        "anomaly": anomaly,
        "hourly_today": hourly,
        "daily": daily,
        "models": models,
        "recent_activity": recent_activity,
        "assumptions_summary": {
            "window_boundary_heuristic": _fired.get(AssumptionTag.WINDOW_BOUNDARY_HEURISTIC, 0),
            "unknown_model_fallback": _fired.get(AssumptionTag.UNKNOWN_MODEL_FALLBACK, 0),
            "dedup_passthrough": _fired.get(AssumptionTag.DEDUP_PASSTHROUGH, 0),
            "interrupted_turn_skipped": _fired.get(AssumptionTag.INTERRUPTED_TURN_SKIPPED, 0),
            "gemini_unpriced": _fired.get(AssumptionTag.GEMINI_UNPRICED, 0),
        },
    }

    return SnapshotResult(payload=payload, turns=all_turns, sessions=all_sessions)


def compute_active_keys(all_projects: bool) -> frozenset[tuple[str, int, int]]:
    """Stat all active JSONL files and return their (path, size, mtime_ns) keys.

    Cheap alternative to a full build_snapshot_full when the only question is
    "did anything change since the last build?". The SSE broadcaster uses this as
    an idle gate so unchanged ticks skip the full snapshot assembly entirely.
    """
    dirs = get_config_dirs(all_projects=all_projects)
    paths = find_jsonl_files(dirs)
    keys: set[tuple[str, int, int]] = set()
    for p in paths:
        try:
            s = p.stat()
            keys.add((str(p), s.st_size, s.st_mtime_ns))
        except OSError:
            pass
    return frozenset(keys)


# ---------------------------------------------------------------------------
# Endpoint panel builders
# ---------------------------------------------------------------------------

_METRIC_Y_UNIT: dict[str, str] = {
    "hit_pct": "percent",
    "cost_per_kw": "usd",
    "ctx_ratio": "ratio",
    "cache_reuse": "ratio",
    "output": "tokens",
    "cost": "usd",
}

VALID_METRICS: frozenset[str] = frozenset(_METRIC_Y_UNIT)


def range_since(range_str: str, today: date) -> date | None:
    """Map a period/range string to the start date; None means all time."""
    if range_str == "today":
        return today
    if range_str == "7d":
        return today - timedelta(days=6)
    if range_str == "30d":
        return today - timedelta(days=29)
    if range_str == "90d":
        return today - timedelta(days=89)
    return None


def _extract_metric(r: object, metric: str) -> float | None:
    cr = r.cache_read_tokens  # type: ignore[attr-defined]
    cc = r.cache_creation_tokens  # type: ignore[attr-defined]
    inp = r.input_tokens  # type: ignore[attr-defined]
    out = r.output_tokens  # type: ignore[attr-defined]
    c = r.cost_usd  # type: ignore[attr-defined]
    if metric == "hit_pct":
        return cache_hit_pct(cr, cc, inp)
    if metric == "cost_per_kw":
        return cost_per_kw(c, out)
    if metric == "ctx_ratio":
        return ctx_ratio_n_to_1(cr, out)
    if metric == "cache_reuse":
        return cache_reuse_n_to_1(cr, cc)
    if metric == "output":
        return float(out)
    return c  # "cost"


def _cwd_basename(cwd: str) -> str:
    return cwd.split("/")[-1] if cwd else "–"


def _grouped_cwd_by_sid(sessions: list[Session]) -> dict[str, str]:
    """Return {session_id: canonical_cwd}, where nested cwds roll up to their
    shortest active ancestor.

    Example: if both "/dev/proj" and "/dev/proj/backend" appear as cwds, every
    session in "/dev/proj/backend" is remapped to "/dev/proj". Sibling or
    unrelated cwds stay separate. The "(unknown)" sentinel is preserved as-is.
    """
    raw = {s.session_id: (s.cwd or "(unknown)") for s in sessions}
    cwds = {cwd for cwd in raw.values() if cwd != "(unknown)"}
    remap: dict[str, str] = {}
    for cwd in cwds:
        ancestors = [o for o in cwds if len(o) < len(cwd) and cwd.startswith(o + "/")]
        remap[cwd] = min(ancestors, key=len) if ancestors else cwd
    return {sid: remap.get(cwd, cwd) for sid, cwd in raw.items()}


_COMPARE_TOP_N = 8


def _top_n_by_cost(
    buckets: dict[str, list[Turn]],
    n: int = _COMPARE_TOP_N,
) -> list[tuple[str, list[Turn]]]:
    """Return the N buckets with the highest summed cost, sorted cost-desc."""
    by_cost = sorted(buckets.items(), key=lambda kv: -sum(t.cost_usd for t in kv[1]))
    return by_cost[:n]


def _parse_filter(param: str) -> str | list[str]:
    """Parse a project/model query-param value.

    Returns either the literal 'all'/'compare' sentinel, or a list of explicit values.
    A single-element list is treated as a singleton filter downstream.
    """
    if param in ("all", "compare"):
        return param
    return [s.strip() for s in param.split(",") if s.strip()]


def _build_series(
    turns: list[Turn],
    cwd_by_sid: dict[str, str],
    project: str,
    model: str,
    points_fn: Callable[[list[Turn]], list[dict]],
) -> list[dict]:
    """Build series list for timeline endpoints.

    project/model may each be: 'all', 'compare' (top-N by cost), a single value, or
    a comma-separated list of values (explicit compare). The "(unknown)" cwd is
    dropped from compare results.
    """
    p = _parse_filter(project)
    m = _parse_filter(model)

    p_is_compare = p == "compare" or (isinstance(p, list) and len(p) > 1)
    m_is_compare = m == "compare" or (isinstance(m, list) and len(m) > 1)

    if p_is_compare:
        by_cwd: defaultdict[str, list[Turn]] = defaultdict(list)
        for t in turns:
            cwd = cwd_by_sid.get(t.session_id, "(unknown)")
            if cwd == "(unknown)":
                continue
            if isinstance(p, list) and cwd not in p:
                continue
            if isinstance(m, list) and t.model not in m:
                continue
            by_cwd[cwd].append(t)
        if p == "compare":
            return [{"label": cwd, "points": points_fn(sub)} for cwd, sub in _top_n_by_cost(by_cwd)]
        # Explicit list: preserve user-supplied order, skip silently missing entries.
        return [{"label": cwd, "points": points_fn(by_cwd[cwd])} for cwd in p if cwd in by_cwd]

    if m_is_compare:
        by_model: defaultdict[str, list[Turn]] = defaultdict(list)
        for t in turns:
            if not t.model:
                continue
            if isinstance(p, list) and cwd_by_sid.get(t.session_id, "(unknown)") not in p:
                continue
            if isinstance(m, list) and t.model not in m:
                continue
            by_model[t.model].append(t)
        if m == "compare":
            return [{"label": name, "points": points_fn(sub)} for name, sub in _top_n_by_cost(by_model)]
        return [{"label": name, "points": points_fn(by_model[name])} for name in m if name in by_model]

    # Single-filter or all/all mode. p and m are either 'all' or a length-1 list here.
    filtered = turns
    if isinstance(p, list):
        filtered = [t for t in filtered if cwd_by_sid.get(t.session_id, "(unknown)") == p[0]]
    if isinstance(m, list):
        filtered = [t for t in filtered if t.model == m[0]]
    label = p[0] if isinstance(p, list) else (m[0] if isinstance(m, list) else "all")
    return [{"label": label, "points": points_fn(filtered)}]


def build_hourly_panel(
    target_date: date,
    turns: list[Turn],
    sessions: list[Session],
    metric: str = "hit_pct",
    project: str = "all",
    model: str = "all",
) -> dict:
    cwd_by_sid = _grouped_cwd_by_sid(sessions)
    date_turns = [t for t in turns if t.timestamp.date() == target_date]

    def _points(sub: list[Turn]) -> list[dict]:
        return [
            {"hour": r.hour.isoformat(), "value": _extract_metric(r, metric), "turns": r.turns}
            for r in rollup_by_hour(sub, target_date=target_date, fill_day=True)
        ]

    return {
        "date": str(target_date),
        "metric": metric,
        "y_unit": _METRIC_Y_UNIT.get(metric, "unknown"),
        "series": _build_series(date_turns, cwd_by_sid, project, model, _points),
        **_active_entities(date_turns, cwd_by_sid),
    }


def build_daily_panel(
    turns: list[Turn],
    sessions: list[Session],
    range_str: str,
    metric: str = "hit_pct",
    project: str = "all",
    model: str = "all",
    today_date: date | None = None,
) -> dict:
    today_date = today_date or date.today()
    since = range_since(range_str, today_date)
    cwd_by_sid = _grouped_cwd_by_sid(sessions)
    earliest_date = min((t.timestamp.date() for t in turns), default=today_date)
    earliest = str(earliest_date)
    # For range=all, anchor zero-fill at the earliest active day so step-plot gaps
    # render as broken lines instead of last-value-extending across inactive stretches.
    fill_since = since if since is not None else earliest_date

    def _points(sub: list[Turn]) -> list[dict]:
        return [
            {"date": str(r.date), "value": _extract_metric(r, metric), "turns": r.turns}
            for r in rollup_by_date(sub, since=fill_since, until=today_date)
        ]

    range_turns = [t for t in turns if t.timestamp.date() >= fill_since]
    return {
        "range": range_str,
        "metric": metric,
        "y_unit": _METRIC_Y_UNIT.get(metric, "unknown"),
        "earliest_available": earliest,
        "series": _build_series(turns, cwd_by_sid, project, model, _points),
        **_active_entities(range_turns, cwd_by_sid),
    }


def build_models_panel(turns: list[Turn], range_label: str) -> dict:
    return _build_models(turns, range_label)


def build_recent_activity_panel(
    all_turns: list[Turn],
    all_sessions: list[Session],
    now: datetime,
    window_minutes: int = 60,
) -> dict:
    return _build_recent_activity(all_turns, _grouped_cwd_by_sid(all_sessions), now, window_minutes)


def build_model_detail(
    name: str,
    turns: list[Turn],
    sessions: list[Session],
) -> dict | None:
    model_turns = [t for t in turns if t.model == name or _short_model(t.model) == name]
    if not model_turns:
        return None

    rollups = build_model_rollups(model_turns)
    mr = rollups[0] if rollups else None

    cwd_by_sid = _grouped_cwd_by_sid(sessions)
    proj_costs: defaultdict[str, float] = defaultdict(float)
    proj_turn_counts: defaultdict[str, int] = defaultdict(int)
    proj_last_turn: dict[str, Turn] = {}
    for t in model_turns:
        cwd = cwd_by_sid.get(t.session_id, "(unknown)")
        proj_costs[cwd] += t.cost_usd
        proj_turn_counts[cwd] += 1
        if cwd not in proj_last_turn or t.timestamp > proj_last_turn[cwd].timestamp:
            proj_last_turn[cwd] = t

    projects = sorted(
        [{
            "cwd": cwd,
            "cwd_b64": encode_cwd(cwd),
            "cost": proj_costs[cwd],
            "turns": proj_turn_counts[cwd],
            "last_active": proj_last_turn[cwd].timestamp.isoformat() if cwd in proj_last_turn else None,
        } for cwd in proj_costs],
        key=lambda x: -x["cost"],
    )

    return {
        "name": name,
        "total_cost": sum(t.cost_usd for t in model_turns),
        "total_turns": len(model_turns),
        "cost_breakdown": {
            "input_usd": mr.input_usd,
            "output_usd": mr.output_usd,
            "cache_read_usd": mr.cache_read_usd,
            "cache_creation_usd": mr.cache_creation_usd,
        } if mr else None,
        "projects_using_model": projects,
    }


def build_tool_detail(
    name: str,
    turns: list[Turn],
    sessions: list[Session],
) -> dict | None:
    """Build the tool drill-down payload for GET /api/tool/{name}."""
    tool_turns = [
        t for t in turns
        if not t.is_interrupted and t.tool_names.get(name, 0) > 0
    ]
    if not tool_turns:
        return None

    cwd_by_sid = _grouped_cwd_by_sid(sessions)

    total_cost = 0.0
    total_output_tokens = 0.0
    total_invocations = 0
    proj_cost: defaultdict[str, float] = defaultdict(float)
    proj_invs: defaultdict[str, int] = defaultdict(int)
    proj_last: dict[str, datetime] = {}
    model_cost: defaultdict[str, float] = defaultdict(float)
    model_invs: defaultdict[str, int] = defaultdict(int)

    for t in tool_turns:
        tc = t.tool_costs.get(name)
        if tc:
            total_cost += tc.cost_usd
            total_output_tokens += tc.output_tokens
        invs = t.tool_names.get(name, 0)
        total_invocations += invs
        cwd = cwd_by_sid.get(t.session_id, "(unknown)")
        proj_cost[cwd] += tc.cost_usd if tc else 0.0
        proj_invs[cwd] += invs
        if cwd not in proj_last or t.timestamp > proj_last[cwd]:
            proj_last[cwd] = t.timestamp
        model = t.model or "(unknown)"
        model_cost[model] += tc.cost_usd if tc else 0.0
        model_invs[model] += invs

    grand_total_cost = sum(tt.cost_usd for tt in turns if not tt.is_interrupted) or 1.0

    top_cwd = max(proj_cost.items(), key=lambda kv: kv[1], default=("(unknown)", 0.0))
    top_project = {
        "name": top_cwd[0].rsplit("/", 1)[-1] if top_cwd[0] != "(unknown)" else "—",
        "cost_usd": top_cwd[1],
        "share": top_cwd[1] / total_cost if total_cost > 0 else 0.0,
    }

    today = date.today()
    seven_days_ago_ts = datetime.combine(
        today - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc
    )
    invs_7d = sum(
        t.tool_names.get(name, 0) for t in tool_turns if t.timestamp >= seven_days_ago_ts
    )

    daily = build_tool_cost_daily(turns, tool_name=name, days=30)

    by_project = sorted(
        [{
            "cwd_b64": encode_cwd(cwd) if cwd != "(unknown)" else None,
            "project_label": cwd.rsplit("/", 1)[-1] if cwd != "(unknown)" else "(unknown)",
            "cost_usd": proj_cost[cwd],
            "invocations": proj_invs[cwd],
            "last_active": proj_last[cwd].isoformat(),
        } for cwd in proj_cost],
        key=lambda r: -r["cost_usd"],
    )
    by_model = sorted(
        [{"name": m, "cost_usd": model_cost[m], "invocations": model_invs[m]}
         for m in model_cost],
        key=lambda r: -r["cost_usd"],
    )

    return {
        "name": name,
        "total_invocations": total_invocations,
        "scorecards": {
            "cost_usd": total_cost,
            "output_tokens": total_output_tokens,
            "invocations": total_invocations,
            "invocations_7d": invs_7d,
            "share_of_total": total_cost / grand_total_cost,
            "top_project": top_project,
        },
        "daily_cost": [{"date": d.date.isoformat(), "cost_usd": d.cost_usd} for d in daily],
        "by_project": by_project,
        "by_model": by_model,
    }


def _str_score(target: str, query: str) -> float:
    if not query:
        return 0.0
    if target.startswith(query):
        return 1.0
    if query in target:
        return 0.7
    return 0.0


def build_search_results(
    query: str,
    turns: list[Turn],
    sessions: list[Session],
) -> dict:
    q = query.lower().strip()
    hits: list[dict] = []

    if q.startswith("session:"):
        val = q[8:].strip()
        for s in sessions:
            score = _str_score(s.session_id.lower(), val)
            if score > 0:
                cwd = s.cwd or "(unknown)"
                hits.append({
                    "kind": "session",
                    "label": f"{cwd.split('/')[-1]} — {s.session_id[:8]}",
                    "href": f"/session/{s.session_id}",
                    "score": score,
                })

    elif q.startswith("cwd:"):
        val = q[4:].strip()
        seen: set[str] = set()
        cwd_by_sid = _grouped_cwd_by_sid(sessions)
        for s in sessions:
            cwd = cwd_by_sid.get(s.session_id, "(unknown)")
            if cwd in seen:
                continue
            score = _str_score(cwd.lower(), val)
            if score > 0:
                seen.add(cwd)
                hits.append({
                    "kind": "project",
                    "label": _cwd_basename(cwd),
                    "href": f"/project/{encode_cwd(cwd)}",
                    "score": score,
                })

    elif q.startswith("model:"):
        val = q[6:].strip()
        seen = set()
        for t in turns:
            if not t.model or t.model in seen:
                continue
            score = _str_score(t.model.lower(), val)
            if score == 0:
                score = _str_score(_short_model(t.model).lower(), val)
            if score > 0:
                seen.add(t.model)
                short = _short_model(t.model)
                hits.append({
                    "kind": "model",
                    "label": short,
                    "href": f"/model/{short}",
                    "score": score,
                })

    elif q.startswith("date:"):
        val = q[5:].strip()
        seen = set()
        for t in turns:
            d = str(t.timestamp.date())
            if d in seen:
                continue
            score = _str_score(d, val)
            if score > 0:
                seen.add(d)
                hits.append({
                    "kind": "day",
                    "label": d,
                    "href": f"/day/{d}",
                    "score": score,
                })

    elif q.startswith("verdict:"):
        val = q[8:].strip()
        for s in sessions:
            sr = build_session_rollup(s)
            v = compute_verdict(sr)
            if _str_score(v.value.lower(), val) > 0:
                cwd = s.cwd or "(unknown)"
                hits.append({
                    "kind": "session",
                    "label": f"{cwd.split('/')[-1]} — {v.value}",
                    "href": f"/session/{s.session_id}",
                    "score": 1.0,
                })

    else:
        seen_cwds: set[str] = set()
        for s in sessions:
            cwd = s.cwd or "(unknown)"
            score_cwd = _str_score(cwd.lower(), q)
            score_sid = _str_score(s.session_id.lower(), q)
            score = max(score_cwd, score_sid)
            if score > 0:
                hits.append({
                    "kind": "session",
                    "label": f"{cwd.split('/')[-1]} — {s.session_id[:8]}",
                    "href": f"/session/{s.session_id}",
                    "score": score,
                })
                if cwd not in seen_cwds and score_cwd > 0:
                    seen_cwds.add(cwd)
                    hits.append({
                        "kind": "project",
                        "label": _cwd_basename(cwd),
                        "href": f"/project/{encode_cwd(cwd)}",
                        "score": score_cwd,
                    })

    hits.sort(key=lambda h: -h["score"])
    return {"hits": hits[:20], "query": query}


# ---------------------------------------------------------------------------
# Drilldown builders
# ---------------------------------------------------------------------------


_RANGE_DAYS: dict[str, int | None] = {
    "1d": 0, "7d": 6, "14d": 13, "30d": 29, "all": None,
}


def build_project_detail(
    cwd: str,
    all_sessions: list[Session],
    range_key: str = "14d",
) -> dict | None:
    """Build the project drill-down payload for GET /api/project/{cwd_b64}."""
    if range_key not in _RANGE_DAYS:
        raise ValueError(f"Unknown range: {range_key!r}")

    today_date = datetime.now(tz=timezone.utc).date()
    days_back = _RANGE_DAYS[range_key]
    since = (today_date - timedelta(days=days_back)) if days_back is not None else date.min

    cwd_by_sid = _grouped_cwd_by_sid(all_sessions)
    project_sessions = [s for s in all_sessions if cwd_by_sid.get(s.session_id) == cwd]
    if not project_sessions:
        return None

    rollups = []
    for s in project_sessions:
        sr = build_session_rollup(s)
        sr.verdict = compute_verdict(sr)
        rollups.append(sr)

    # Scope rollups and turns to the selected window.
    scoped_rollups = [sr for sr in rollups if sr.last_ts.date() >= since]
    if not scoped_rollups:
        return None

    cwd_b64 = encode_cwd(cwd)
    prs = build_project_rollups(scoped_rollups)
    pr = prs[0] if prs else None

    project_turns = [
        t for s in project_sessions for t in s.turns
        if t.timestamp.date() >= since
    ]
    # Daily buckets collapse to 1-2 points on range=1d, which makes the cache-
    # efficiency trend chart unusable (straight flat line or a single dot).
    # Switch to hourly buckets for 1d so the reader sees the intraday curve.
    cache_trend = []
    cache_trend_unit = "day"
    if range_key == "1d":
        cache_trend_unit = "hour"
        hourly_rollups = rollup_by_hour(project_turns, target_date=today_date, fill_day=False)
        for r in hourly_rollups:
            cache_trend.append({
                "date": r.hour.isoformat(),
                "hit_rate": cache_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens) or 0.0,
                "cost_usd": r.cost_usd,
            })
    else:
        daily_rollups = rollup_by_date(project_turns, since=since)
        for r in daily_rollups:
            cache_trend.append({
                "date": str(r.date),
                "hit_rate": cache_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens) or 0.0,
                "cost_usd": r.cost_usd,
            })

    growths = [sr.context_growth_rate_val for sr in scoped_rollups]
    _GROWTH_EDGES = [500, 1000, 2000, 5000, 10000]
    labels = ["<500", "500-1k", "1k-2k", "2k-5k", "5k-10k", "10k+"]
    histogram = [{"label": lbl, "count": 0} for lbl in labels]
    for g in growths:
        histogram[bisect.bisect_right(_GROWTH_EDGES, g)]["count"] += 1

    verdict_dist = _verdict_dist(scoped_rollups)

    top_turns = sorted(
        [t for t in project_turns if not t.is_interrupted],
        key=lambda t: t.cost_usd,
        reverse=True,
    )[:20]

    def _turn_hit_rate(t: Turn) -> float | None:
        u = t.usage
        return cache_hit_rate(u.cache_read_input_tokens, u.cache_creation_input_tokens, u.input_tokens)

    tool_cost: defaultdict[str, float] = defaultdict(float)
    tool_invs: defaultdict[str, int] = defaultdict(int)
    tool_last: dict[str, datetime] = {}
    for t in project_turns:
        for tname, tc in t.tool_costs.items():
            tool_cost[tname] += tc.cost_usd
        for tname, count in t.tool_names.items():
            tool_invs[tname] += count
            if tname not in tool_last or t.timestamp > tool_last[tname]:
                tool_last[tname] = t.timestamp

    by_tool = sorted(
        [{
            "name": tname,
            "cost_usd": tool_cost[tname],
            "invocations": tool_invs[tname],
            "last_active": tool_last[tname].isoformat(),
        } for tname in tool_invs],
        key=lambda r: -r["cost_usd"],
    )

    return {
        "cwd": cwd,
        "cwd_b64": cwd_b64,
        "range_key": range_key,
        "total_cost": pr.cost_usd if pr else 0.0,
        "session_count": len(scoped_rollups),
        "flagged": pr.flagged if pr else False,
        "verdict_distribution": verdict_dist,
        "sessions": [_session_rollup_to_dict(sr) for sr in scoped_rollups],
        "cache_trend": cache_trend,
        "cache_trend_unit": cache_trend_unit,
        "context_growth_histogram": histogram,
        "top_turns_by_cost": [
            {
                "ts": t.timestamp.isoformat(),
                "session_id": t.session_id,
                "model": t.model,
                "cost_usd": t.cost_usd,
                "input_tokens": t.usage.input_tokens,
                "output_tokens": t.usage.output_tokens,
                "cache_read_tokens": t.usage.cache_read_input_tokens,
                "cache_creation_tokens": t.usage.cache_creation_input_tokens,
                "hit_rate": _turn_hit_rate(t),
                "cost_per_kw": cost_per_kw(t.cost_usd, t.usage.output_tokens),
                "ctx_ratio": ctx_ratio_n_to_1(t.usage.cache_read_input_tokens, t.usage.output_tokens),
            }
            for t in top_turns
        ],
        "by_tool": by_tool,
    }


def build_day_detail(
    target_date: date,
    all_turns: list[Turn],
    all_sessions: list[Session],
) -> dict | None:
    """Build the day drill-down payload for GET /api/day/{date}."""
    today_date = datetime.now(tz=timezone.utc).date()
    since_90d = today_date - timedelta(days=89)
    turns_90d = [t for t in all_turns if t.timestamp.date() >= since_90d]
    daily_90d_raw: list[dict] = []
    for r in rollup_by_date(turns_90d, since=since_90d):
        kw = cost_per_kw(r.cost_usd, r.output_tokens) or 0.0
        daily_90d_raw.append({
            "date": str(r.date),
            "cost_usd": r.cost_usd,
            "cost_per_kw": kw,
            "hit_rate": cache_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens) or 0.0,
        })

    cost_7d_median = trailing_median(daily_90d_raw, 7, target_date + timedelta(days=1), "cost_usd")

    day_turns = [t for t in all_turns if t.timestamp.date() == target_date]
    if not day_turns and target_date != today_date:
        return None

    hourly_buckets: dict[int, dict] = {}
    for t in day_turns:
        h = t.timestamp.astimezone(timezone.utc).hour
        if h not in hourly_buckets:
            hourly_buckets[h] = {"cost_usd": 0.0, "turns": 0, "model_counts": defaultdict(int)}
        hourly_buckets[h]["cost_usd"] += t.cost_usd
        hourly_buckets[h]["turns"] += 1
        if t.model:
            hourly_buckets[h]["model_counts"][t.model] += 1

    hourly = []
    for h in range(24):
        b = hourly_buckets.get(h, {})
        hourly.append({
            "hour": h,
            "cost_usd": b.get("cost_usd", 0.0),
            "turns": b.get("turns", 0),
            "model_mix": dict(b.get("model_counts", {})),
        })

    day_session_ids = {t.session_id for t in day_turns}
    day_sessions = [s for s in all_sessions if s.session_id in day_session_ids]
    day_rollups = []
    for s in day_sessions:
        sr = build_session_rollup(s)
        sr.verdict = compute_verdict(sr)
        day_rollups.append(sr)
    day_rollups.sort(key=lambda sr: sr.cost_usd, reverse=True)

    verdict_dist = _verdict_dist(day_rollups)

    day_cost = sum(t.cost_usd for t in day_turns)
    cost_ratio = day_cost / cost_7d_median if cost_7d_median and cost_7d_median > 0 else None
    anomaly_flags = ["cost_2sigma"] if cost_ratio and cost_ratio >= 2.0 else []

    return {
        "date": str(target_date),
        "total_cost": day_cost,
        "delta_vs_7d_median": cost_ratio,
        "anomaly_flags": anomaly_flags,
        "hourly": hourly,
        "top_sessions": [_session_rollup_to_dict(sr) for sr in day_rollups[:20]],
        "top_projects": _projects_in_window(day_rollups, target_date)[:10],
        "verdict_distribution": verdict_dist,
    }
