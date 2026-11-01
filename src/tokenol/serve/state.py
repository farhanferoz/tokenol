"""Server-side parse cache and snapshot builder.

Each tick parses only the JSONL files whose mtime moved, derives the turns the
usage engine does not hold yet (_ingest_tick), hands them to the engine in one
batch, and keeps no reference to them. Every snapshot section is then answered
by the engine's queries (build_snapshot_full).

The old no-store fallback (get_or_parse / get_derived / _build_turns_and_sessions,
which re-derives everything from cached events) is no longer called by the
snapshot; Task 8 deletes it.

Thread-safety invariant: ParseCache._store is mutated only under _lock.
All code paths that call get_or_parse / purge acquire the lock first.
"""

from __future__ import annotations

import base64
import bisect
import itertools
import logging
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tokenol.engine.context import UsageContext
from tokenol.engine.engine import release_free_memory
from tokenol.engine.filters import (
    UNKNOWN_LABEL,
    GroupKey,
    InterruptedPolicy,
    SearchField,
    TurnFilter,
)
from tokenol.engine.queries import (
    BucketTotals,
    LatestTurn,
    SkillCosts,
    ToolCosts,
    Totals,
    UsageQueries,
)
from tokenol.engine.sessions import SessionInfo
from tokenol.enums import AssumptionTag, AttributionMode, BlowUpVerdict, PriceStatus
from tokenol.ingest.discovery import find_jsonl_files, get_config_dirs, select_edge_paths
from tokenol.ingest.parser import (
    SKILL_TOOL,
    UNATTRIBUTED_TOOL,
    UNKNOWN_TOOL,
    dedup_key,
    parse_file,
)
from tokenol.model import registry
from tokenol.persistence.marks import clear_marks, load_marks, save_marks

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
from tokenol.metrics.context import (
    cache_hit_pct,
    cache_hit_rate,
    cache_reuse_n_to_1,
    cost_per_kw,
    cost_share,
    ctx_ratio_n_to_1,
    tool_error_rate,
)
from tokenol.metrics.cost import (
    DailyRollup,
    HourlyRollup,
    TurnCost,
    cost_for_turn,
)
from tokenol.metrics.cost import price_status as model_price_status
from tokenol.metrics.history import baseline_median, trailing_median
from tokenol.metrics.rollups import (
    ModelRollup,
    SessionRollup,
    _rank_dict_with_others,
    build_project_rollups,
)
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.model.events import EMPTY_ASSUMPTIONS, RawEvent, Session, Turn, Usage
from tokenol.model.pricing import FAMILY_FALLBACKS, context_window

log = logging.getLogger(__name__)

# Minimum seconds between two writes of the parse-marks sidecar. Bounds the new
# disk traffic to one small file per minute while transcripts are changing, and
# none at all when they are not. A save additionally requires the flusher to
# report every enqueued turn written; see _ingest_tick.
_MARKS_SAVE_INTERVAL_SECONDS = 60.0


@dataclass
class ParseCache:
    """JSONL parse cache keyed by (path_str, size, mtime_ns).

    All public methods are thread-safe via an internal lock.

    Also memoizes the derived (turns, sessions) tuple keyed on the active set of
    parse keys: when no JSONL file changed since the last build, build_snapshot_full
    can skip the O(total events) re-derivation. The memo is invalidated automatically
    on any get_or_parse miss or any purge that drops a key.

    Scope note: `_store` and the derived memo back the old NO-STORE fallback
    path only, which the snapshot no longer takes (Task 8 deletes it). The tick
    (`_ingest_tick`) parses edge files directly and retains none of their
    events; this instance carries just its bookkeeping as duck-typed attributes
    (`_tick_initialized`, `_known_passthrough_locs`, `_last_mtime_ns_by_path`,
    ...), and `size` stays 0. Retaining events there cost roughly 2.1 GB on a
    real corpus for a reader that did not exist.
    """

    _store: dict[tuple[str, int, int], list[RawEvent]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _derived_keys: frozenset[tuple[str, int, int]] | None = None
    _derived: tuple[list[Turn], list[Session], Counter[AssumptionTag]] | None = None
    # The usage engine and its companions. create_app sets it from the store before any
    # HistoryStore opens; build_snapshot_full opens an empty one when it is still None.
    usage: UsageContext | None = None

    def get_or_parse(self, path: Path) -> tuple[tuple[str, int, int], list[RawEvent]]:
        """Return (cache_key, events). Parses only when (size, mtime_ns) changes."""
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        path_str = str(path)
        with self._lock:
            if key not in self._store:
                self._store[key] = list(parse_file(path))
                # Drop superseded versions of THIS path. The key carries (size,
                # mtime_ns), so every append to a live session file mints a new
                # entry while the old one — the whole event list for the previous
                # size — stays resident. A file has exactly one current version and
                # older ones are never read again, so retaining them is a pure leak
                # that grows with session activity, not with corpus size. purge()
                # used to hide this by clearing on every tick, but it runs only on
                # the non-store derivation path, so store-backed servers never
                # evicted anything.
                for stale in [k for k in self._store if k[0] == path_str and k != key]:
                    del self._store[stale]
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
        if not registry.is_claude(ev.model):
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

        tc = cost_for_turn(ev.model, usage, at=ev.timestamp)
        tags.extend(t for t in tc.assumptions if t not in tags)
        for tag in tags:
            fired[tag] += 1

        key_str = k or ev.uuid or str(id(ev))
        turns.append(
            Turn(
                dedup_key=key_str,
                timestamp=ev.timestamp,
                session_id=ev.session_id,
                model=ev.model,
                usage=usage,
                is_sidechain=ev.is_sidechain,
                stop_reason=ev.stop_reason,
                assumptions=tags if tags else EMPTY_ASSUMPTIONS,
                cost_usd=tc.total_usd,
                is_interrupted=is_interrupted,
                tool_use_count=ev.tool_use_count,
                tool_error_count=ev.tool_error_count,
                tool_names=ev.tool_names,
                tool_costs=ev.tool_costs,
                unattributed_input_tokens=ev.unattributed_input_tokens,
                unattributed_output_tokens=ev.unattributed_output_tokens,
                unattributed_cost_usd=ev.unattributed_cost_usd,
                attribution_skill=ev.attribution_skill,
                skill_names=ev.skill_names,
            )
        )

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
        sessions.append(
            Session(
                session_id=sid,
                source_file=session_source.get(sid, ""),
                is_sidechain=session_sidechain.get(sid, False),
                cwd=cwd_by_session.get(sid),
                turns=t_list,
            )
        )
    sessions.sort(key=lambda s: s.turns[0].timestamp if s.turns else s.session_id)
    return turns, sessions, fired


def derive_delta_turns(
    new_events: list[RawEvent],
    known_keys: Callable[[Iterable[str]], set[str]],
    existing_passthrough_locations: set[tuple[str, int]],
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag], set[tuple[str, int]]]:
    """Build new Turn/Session deltas from events not already represented in memory.

    *known_keys* answers which of the given dedup keys the usage engine already holds
    (``engine.known_keys``; a caller holding a set passes ``known.intersection``). It
    is called once, with every candidate key of the batch, so the engine is asked in
    one statement however many events there are.
    *existing_passthrough_locations* is the set of (source_file, line_number) tuples
    for passthrough turns already emitted (passthroughs lack dedup keys).

    Returns (turns, sessions, fired_assumptions, accepted_passthrough_locs):
    - *turns* and *sessions* are new records to append; never touches existing state.
    - *fired_assumptions* tracks which assumption tags fired during derivation.
    - *accepted_passthrough_locs* is the set of (source_file, line_number) tuples for
      passthroughs that were actually emitted (excluding synthetic models and already-known).
    Within-batch dedup follows the existing last-wins rule.
    """
    candidates: list[tuple[RawEvent, str | None]] = []
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
        if not registry.is_claude(ev.model):
            continue
        candidates.append((ev, dedup_key(ev)))

    known = known_keys([k for _ev, k in candidates if k is not None])
    seen: dict[str, tuple[RawEvent, str]] = {}
    passthroughs: list[tuple[RawEvent, None]] = []
    accepted_passthrough_locs: set[tuple[str, int]] = set()
    for ev, k in candidates:
        if k is None:
            loc = (ev.source_file, ev.line_number)
            if loc in existing_passthrough_locations:
                continue
            passthroughs.append((ev, None))
            accepted_passthrough_locs.add(loc)
        else:
            if k in known:
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

        tc = cost_for_turn(ev.model, usage, at=ev.timestamp)
        tags.extend(t for t in tc.assumptions if t not in tags)
        for tag in tags:
            fired[tag] += 1

        key_str = k or ev.uuid or str(id(ev))
        turns.append(
            Turn(
                dedup_key=key_str,
                timestamp=ev.timestamp,
                session_id=ev.session_id,
                model=ev.model,
                usage=usage,
                is_sidechain=ev.is_sidechain,
                stop_reason=ev.stop_reason,
                assumptions=tags if tags else EMPTY_ASSUMPTIONS,
                cost_usd=tc.total_usd,
                is_interrupted=is_interrupted,
                tool_use_count=ev.tool_use_count,
                tool_error_count=ev.tool_error_count,
                tool_names=ev.tool_names,
                tool_costs=ev.tool_costs,
                unattributed_input_tokens=ev.unattributed_input_tokens,
                unattributed_output_tokens=ev.unattributed_output_tokens,
                unattributed_cost_usd=ev.unattributed_cost_usd,
                attribution_skill=ev.attribution_skill,
                skill_names=ev.skill_names,
            )
        )

    # Build *delta* Session records for any session_id we touched (one Session per
    # new sid, with empty turns list — caller appends turns to its own session map).
    sessions: list[Session] = []
    seen_sids: set[str] = set()
    for t in turns:
        if t.session_id in seen_sids:
            continue
        seen_sids.add(t.session_id)
        sessions.append(
            Session(
                session_id=t.session_id,
                source_file=session_source.get(t.session_id, ""),
                is_sidechain=t.is_sidechain,
                cwd=cwd_by_session.get(t.session_id),
                turns=[],  # caller appends to its own session's turn list
            )
        )

    return turns, sessions, fired, accepted_passthrough_locs


@dataclass
class SnapshotResult:
    payload: dict


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
    error_rate = tool_error_rate(sr.tool_error_count, sr.tool_use_count) or 0.0

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
        "tool_error_rate": error_rate,
        "cost_per_kw": sr.cost_per_kw_val,
        "ctx_ratio": sr.ctx_ratio_n_to_1,
    }


def _verdict_dist(rollups: list[SessionRollup]) -> dict[str, int]:
    dist = {v.value: 0 for v in BlowUpVerdict}
    for sr in rollups:
        dist[sr.verdict.value] += 1
    return dist


def _sessions_in_window(rollups: list[SessionRollup], since: date, top: int) -> list[dict]:
    filtered = [sr for sr in rollups if sr.last_ts.date() >= since]
    filtered.sort(key=lambda sr: sr.cost_usd, reverse=True)
    return [_session_rollup_to_dict(sr) for sr in filtered[:top]]


def _projects_in_window(rollups: list[SessionRollup], since: date) -> list[dict]:
    scoped = [sr for sr in rollups if sr.last_ts.date() >= since]
    result = []
    for pr in build_project_rollups(scoped):
        result.append(
            {
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
            }
        )
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


def _build_daily_series(rollups: list[DailyRollup]) -> list[dict]:
    result = []
    for r in rollups:
        result.append(
            {
                "date": str(r.date),
                "cost_usd": r.cost_usd,
                "output_tokens": r.output_tokens,
                "turns": r.turns,
                "hit_pct": cache_hit_pct(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens),
                "cost_per_kw": cost_per_kw(r.cost_usd, r.output_tokens),
                "ctx_ratio": ctx_ratio_n_to_1(r.cache_read_tokens, r.output_tokens),
                "cache_reuse": cache_reuse_n_to_1(r.cache_read_tokens, r.cache_creation_tokens),
            }
        )
    first_active = next((i for i, r in enumerate(result) if r["turns"] > 0), len(result))
    return result[first_active:]


def _moving_avg_7d(series: list[dict], key: str) -> list[dict]:
    values = [r.get(key) for r in series]
    result = []
    for i, r in enumerate(series):
        window = [v for v in values[max(0, i - 6) : i + 1] if v is not None]
        result.append({"date": r["date"], "value": sum(window) / len(window) if window else None})
    return result


def _excluding_interrupted(f: TurnFilter) -> TurnFilter:
    """*f* without interrupted turns: the policy of every token and cost sum the tiles and topbar show."""
    return replace(f, interrupted=InterruptedPolicy.EXCLUDE)


def _model_mix(by_model: dict[str, BucketTotals]) -> dict[str, float]:
    """Each model's share of cost, short-named and largest first; the unknown group is left out."""
    model_costs = {m: b.total_cost for m, b in by_model.items() if m != UNKNOWN_LABEL}
    shares = cost_share(model_costs)
    # Ties by name: the SQL group order is arbitrary, and the mix must not depend on it.
    return {_short_model(m): shares[m] for m, _cost in sorted(model_costs.items(), key=lambda x: (-x[1], x[0]))}


def _build_topbar(queries: UsageQueries, today: date) -> dict:
    day = TurnFilter(since=today, until=today)
    everything = queries.totals(day)
    billable = queries.totals(_excluding_interrupted(day))
    by_model = queries.bucket(day, GroupKey.MODEL)
    # The newest turn of ANY group counts, the unknown-model group included.
    last_active = max((b.last_ts for b in by_model.values() if b.last_ts is not None), default=None)
    return {
        "today_cost": everything.cost_usd,
        "sessions_count": everything.sessions,
        "output_tokens": billable.output_tokens,
        "input_tokens": billable.input_tokens,
        "model_mix": _model_mix(by_model),
        "last_active": last_active.isoformat() if last_active else None,
    }


def _tile_metrics(totals: Totals) -> dict[str, float | None]:
    """Compute the 4 headline metrics over the totals of a slice of turns (interrupted ones excluded)."""
    cr, cc = totals.cache_read_tokens, totals.cache_creation_tokens
    return {
        "hit_pct": cache_hit_pct(cr, cc, totals.input_tokens),
        "cost_per_kw": cost_per_kw(totals.cost_usd, totals.output_tokens),
        "ctx_ratio": ctx_ratio_n_to_1(cr, totals.output_tokens),
        "cache_reuse": cache_reuse_n_to_1(cr, cc),
    }


def _build_tiles(
    queries: UsageQueries,
    period_filter: TurnFilter,
    daily_90d: list[dict],
    today_date: date,
    thresholds: dict,
    now: datetime,
) -> dict:
    m = _tile_metrics(queries.totals(_excluding_interrupted(period_filter)))
    hit_val, cpk_val, ctx_val, reuse_val = m["hit_pct"], m["cost_per_kw"], m["ctx_ratio"], m["cache_reuse"]

    # Last-hour slice: shows whether the period aggregate still reflects current
    # behaviour or is dominated by an early-period spike (e.g. cache thrashing
    # at 8am pulls the today-total cache_reuse down and keeps it low even after
    # recovery, because subsequent turns have tiny absolute cache_creation).
    # Shown whenever the hour holds any turn, interrupted ones included.
    last_hour = replace(period_filter, since_ts=now - timedelta(hours=1))
    lh = (
        _tile_metrics(queries.totals(_excluding_interrupted(last_hour)))
        if queries.totals(last_hour).turns > 0
        else {
            "hit_pct": None,
            "cost_per_kw": None,
            "ctx_ratio": None,
            "cache_reuse": None,
        }
    )

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
            return {"severity": "red", "drilldown_href": href, "message": f"Cache reuse dropped to {val:.0f}:1 (normal {val / dr:.0f}:1) — cache thrashing."}

    if av := _active(tiles["hit_pct"]):
        dr, val = av
        if dr < 0.88:
            return {"severity": "amber", "drilldown_href": href, "message": f"Cache hit rate {val:.1f}% (normal {val / dr:.1f}%) — below expected."}

    if av := _active(tiles["cost_per_kw"]):
        dr, val = av
        if dr > 2.5:
            return {"severity": "amber", "drilldown_href": href, "message": f"Cost efficiency ${val:.2f}/kW ({dr:.1f}× baseline) — high relative spend."}

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
            candidates = {cwd: "/".join(cwd.strip("/").split("/")[-segs:]) or "–" for cwd in colliding}
            if len(set(candidates.values())) == len(colliding):
                labels.update(candidates)
                break
            segs += 1
            if segs > 20:  # pathological — fall back to full paths
                labels.update({cwd: cwd for cwd in colliding})
                break
    return labels


def _format_active_entities(cwds: Iterable[str], models: Iterable[str]) -> dict[str, list[dict]]:
    """Distinct cwds and models formatted for a UI dropdown.

    Results are sorted alphabetically by display label so the dropdown is stable and
    scannable. Labels are disambiguated when multiple cwds share a basename. When two
    full model ids share a short name, the first one in *models* is the value.
    """
    cwd_set = set(cwds)
    models_by_short: dict[str, str] = {}
    for model in models:
        models_by_short.setdefault(_short_model(model), model)
    cwd_labels = _disambiguate_cwd_labels(cwd_set)
    projects = sorted(
        ({"label": cwd_labels[c], "value": c} for c in cwd_set),
        key=lambda p: (p["label"].lower(), p["value"]),
    )
    models_out = sorted(
        ({"label": s, "value": m} for s, m in models_by_short.items()),
        key=lambda p: p["label"].lower(),
    )
    return {"active_projects": projects, "active_models": models_out}


def _engine_active_entities(queries: UsageQueries, f: TurnFilter) -> dict[str, list[dict]]:
    """The filter's distinct projects (never the unknown one) and models, formatted for a UI dropdown.

    Models come sorted, so a short name shared by two full ids takes the alphabetically first id.
    """
    cwds = [project for project in queries.bucket(f, GroupKey.PROJECT) if project != UNKNOWN_LABEL]
    return _format_active_entities(cwds, queries.distinct_values(SearchField.MODEL, f))


def _build_hourly(queries: UsageQueries, today_date: date) -> dict:
    day = TurnFilter(since=today_date, until=today_date)
    series = []
    for r in queries.hourly(day, target_date=today_date, fill_day=True):
        series.append(
            {
                "hour": r.hour.isoformat(),
                "cost_usd": r.cost_usd,
                "turns": r.turns,
                "hit_pct": cache_hit_pct(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens),
            }
        )
    earliest = queries.earliest_date(TurnFilter()) or today_date
    return {
        "date": str(today_date),
        "earliest_available": str(earliest),
        "series": series,
        **_engine_active_entities(queries, day),
    }


def _build_daily(
    daily_90d: list[dict],
    period: str,
    today_date: date,
    entities: dict[str, list[dict]],
) -> dict:
    since_30d = today_date - timedelta(days=29)
    series = [r for r in daily_90d if date.fromisoformat(r["date"]) >= since_30d]
    earliest = daily_90d[0]["date"] if daily_90d else str(today_date)
    return {
        "range": period if period != "today" else "30d",
        "earliest_available": earliest,
        "series": series,
        "moving_avg_7d": _moving_avg_7d(series, "hit_pct"),
        **entities,
    }


def _models_payload(rollups: list[ModelRollup], range_label: str) -> dict:
    rows = []
    for mr in rollups:
        cw = context_window(mr.model)
        rows.append(
            {
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
                "tool_error_rate": tool_error_rate(mr.tool_error_count, mr.tool_use_count),
                "input_usd": mr.input_usd,
                "output_usd": mr.output_usd,
                "cache_read_usd": mr.cache_read_usd,
                "cache_creation_usd": mr.cache_creation_usd,
            }
        )
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


def _build_recent_activity(queries: UsageQueries, now: datetime, window_minutes: int = 60) -> dict:
    window = TurnFilter(since_ts=now - timedelta(minutes=window_minutes))
    billable = _excluding_interrupted(window)

    latest = queries.latest_turn_per_project(window)
    billable_by_project = queries.bucket(billable, GroupKey.PROJECT)
    window_totals = queries.totals(window)
    billable_totals = queries.totals(billable)

    def project_cost(turn: LatestTurn) -> float:
        b = billable_by_project.get(turn.project)  # absent when every turn of the project was interrupted
        return b.total_cost if b else 0.0

    rows = []
    # Most expensive first; ties by project, so the order does not depend on arrival.
    for turn in sorted(latest, key=lambda t: (-project_cost(t), t.project)):
        b = billable_by_project.get(turn.project)
        cr, cc, inp, out, cost = (
            (b.cache_read, b.cache_creation, b.input, b.output, b.total_cost) if b else (0, 0, 0, 0, 0.0)
        )
        cw = context_window(turn.model or "")
        visible = turn.pool
        rows.append(
            {
                "cwd": turn.project,
                "cwd_b64": encode_cwd(turn.project),
                "model_primary": _short_model(turn.primary_model) if turn.primary_model else "(unknown)",
                "last_turn_at": turn.timestamp.isoformat(),
                "latest_session_id": turn.session_id,
                "turns": turn.turns,
                "output": out,
                "ctx_used": visible / cw if cw else None,
                "ctx_used_abs": {"visible": visible, "window": cw} if cw else None,
                "cost_per_kw": cost_per_kw(cost, out),
                "ctx_ratio": ctx_ratio_n_to_1(cr, out),
                "cache_reuse": cache_reuse_n_to_1(cr, cc),
                "hit_pct": cache_hit_pct(cr, cc, inp),
                "verdict": BlowUpVerdict.OK.value,
            }
        )

    return {
        "window": f"{window_minutes}m",
        "aggregate": {
            "projects": len(latest),
            "turns": window_totals.turns,
            "output": billable_totals.output_tokens,
            "cost": billable_totals.cost_usd,
            "model_mix": _model_mix(queries.bucket(window, GroupKey.MODEL)),
            "hit_pct": cache_hit_pct(
                billable_totals.cache_read_tokens, billable_totals.cache_creation_tokens, billable_totals.input_tokens
            ),
            "cost_per_kw": cost_per_kw(billable_totals.cost_usd, billable_totals.output_tokens),
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Store-backed derivation helper
# ---------------------------------------------------------------------------


def _ingest_tick(
    parse_cache: ParseCache,
    paths: list[Path],
    history_store: HistoryStore | None,
    flush_queue: FlushQueue | None,
    mtime_by_path: dict[Path, int] | None = None,
) -> Counter[AssumptionTag]:
    """Parse the files that changed, hand their new turns to the engine, keep no reference to them.

    Per edge file: parse, derive the turns the engine does not hold. Once per tick: add
    them to the engine and the session index, drop the rollups of the sessions they
    touched, queue them for the store under --persist, and record which JSONL files are
    present. The bookkeeping that survives ticks (marks, passthrough locations, assumption
    counts) lives as duck-typed attributes on the ParseCache, beside its JSONL parse cache.
    """
    usage = parse_cache.usage
    assert usage is not None, "build_snapshot_full opens the usage context before the first tick"
    if not getattr(parse_cache, "_tick_initialized", False):
        parse_cache._known_passthrough_locs = set()
        # Marks persist across restarts ONLY under --persist (a flush queue
        # exists). Plain `serve` has no writer, so a skipped file's turns would
        # exist nowhere; there the marks start empty and every file is parsed.
        parse_cache._last_mtime_ns_by_path = load_marks() if flush_queue is not None else {}
        # A mark asserts "this file's turns are already in the store". An EMPTY
        # store with non-empty marks is that assertion contradicted — the store
        # was deleted or replaced while the sidecar survived — and honouring the
        # marks there would skip every transcript and serve an empty dashboard
        # for ever, silently. The marks lose. (forget --all clears them on its
        # own path; this covers the store going away behind our back.)
        if parse_cache._last_mtime_ns_by_path and history_store is not None and history_store.is_empty():
            log.warning("history store is empty but parse marks exist; ignoring the marks and re-reading every transcript")
            parse_cache._last_mtime_ns_by_path = {}
            clear_marks()
        parse_cache._marks_dirty = False
        # None, not 0.0: the interval below is a difference against
        # time.monotonic(), whose origin is arbitrary (uptime on Linux). A 0.0
        # sentinel therefore means "never saved" on a long-running box and
        # "saved just now" on one that booted a minute ago, which delayed the
        # first save by a whole interval there. An explicit None cannot.
        parse_cache._marks_saved_at = None
        parse_cache._fired = Counter()
        parse_cache._tick_initialized = True

    # Persist the marks at the START of a tick, before this tick enqueues
    # anything: if the flusher reports everything written now, every mark
    # currently held describes a file whose turns are all in the store. A crash
    # at any later point then loses at most this tick's work, which the still-old
    # on-disk mark makes re-parseable on the next start.
    if (
        flush_queue is not None
        and parse_cache._marks_dirty
        and (parse_cache._marks_saved_at is None or time.monotonic() - parse_cache._marks_saved_at >= _MARKS_SAVE_INTERVAL_SECONDS)
        and flush_queue.all_written()
    ):
        save_marks(dict(parse_cache._last_mtime_ns_by_path))
        parse_cache._marks_dirty = False
        parse_cache._marks_saved_at = time.monotonic()

    # Drop marks for files that are gone, so the sidecar tracks the corpus
    # rather than growing forever with pruned transcripts.
    present = mtime_by_path.keys() if mtime_by_path is not None else set(paths)
    if len(parse_cache._last_mtime_ns_by_path) > len(present):
        parse_cache._last_mtime_ns_by_path = {p: m for p, m in parse_cache._last_mtime_ns_by_path.items() if p in present}
        parse_cache._marks_dirty = True

    # Filter to edge paths (mtime_ns differs from persisted high-water mark).
    marks = parse_cache._last_mtime_ns_by_path
    if mtime_by_path is not None and marks:
        # Same rule as select_edge_paths, but off mtimes the caller already read.
        edge_paths = [p for p, m in mtime_by_path.items() if marks.get(p) != m]
    else:
        edge_paths = select_edge_paths(paths, marks)

    # Parse only edge files; record current mtime_ns for next tick's gate.
    #
    # Parsed directly, NOT through parse_cache.get_or_parse: on this path a
    # file's events are consumed exactly once. A changed file is re-parsed under
    # a new (path, size, mtime_ns) key and derive_delta_turns skips what the engine
    # already holds by dedup_key, and session drill-down re-opens the JSONL from
    # disk. Caching the events retained every RawEvent of the whole corpus for
    # the life of the process — on the first tick every file is an edge file —
    # for a reader that does not exist.
    #
    # Derived PER FILE, one at a time. Nothing here needs the whole tick's
    # events at once, but accumulating them did cost that: on the first tick
    # every file is an edge file, and holding every RawEvent of the corpus
    # simultaneously peaked at ~1.4-2 GB, of which the allocator kept about
    # half for the life of the process (measured 2026-09-07: 1,000 files ->
    # 275 MB held, 139 MB still resident after gc and malloc_trim). Per-file
    # batches give the same answer, because a dedup_key never spans two files
    # (0 of 56,626 keys over a 1,000-file sample) and within-file last-wins is
    # unchanged. Deltas are still ENQUEUED once per tick: the flusher's count
    # threshold must see them as one batch, not one per file.
    tick_turns: list[Turn] = []
    tick_sessions: list[Session] = []
    for p in edge_paths:
        try:
            file_events = list(parse_file(p))
            # Prefer the caller's already-read mtime over a fresh stat().
            parse_cache._last_mtime_ns_by_path[p] = (
                mtime_by_path[p] if mtime_by_path is not None and p in mtime_by_path else p.stat().st_mtime_ns
            )
            parse_cache._marks_dirty = True
        except OSError:
            continue
        if not file_events:
            continue

        delta_turns, delta_sessions, fired, accepted_passthrough_locs = derive_delta_turns(
            file_events,
            usage.engine.known_keys,
            parse_cache._known_passthrough_locs,
        )
        del file_events
        # Track passthrough locations from the SAME source of truth as derive_delta_turns
        # (which knows about the synthetic-model and already-known filters).
        parse_cache._known_passthrough_locs.update(accepted_passthrough_locs)
        parse_cache._fired.update(fired)
        tick_turns.extend(delta_turns)
        tick_sessions.extend(delta_sessions)

    # First-seen metadata wins, as the hot tier's session map did.
    new_infos: dict[str, SessionInfo] = {}
    for s in tick_sessions:
        if s.session_id not in new_infos and usage.index.get(s.session_id) is None:
            new_infos[s.session_id] = SessionInfo(s.session_id, s.source_file, s.cwd, s.is_sidechain)
    touched_sids = list(dict.fromkeys(t.session_id for t in tick_turns))
    if tick_turns or new_infos:
        usage.engine.add_turns(tick_turns, new_infos.values())
    if new_infos:
        usage.index.add(new_infos.values())
    usage.rollups.invalidate(touched_sids)

    # Queue this tick's deltas for background flush, as one batch.
    if flush_queue is not None and tick_turns:
        sessions_to_flush = []
        for sid in touched_sids:
            info = usage.index.get(sid)
            if info is not None:
                sessions_to_flush.append(
                    Session(
                        session_id=sid,
                        source_file=info.source_file,
                        is_sidechain=info.is_sidechain,
                        cwd=info.cwd,
                        turns=[],
                    )
                )
        flush_queue.enqueue(tick_turns, sessions_to_flush)

    # Sessions whose JSONL is no longer on disk are archived.
    usage.index.set_live_paths(paths)

    return Counter(parse_cache._fired)


# ---------------------------------------------------------------------------
# Main snapshot builder
# ---------------------------------------------------------------------------


def _usage_context(parse_cache: ParseCache) -> UsageContext:
    """The cache's usage context, opening an empty one (no store) the first time it is needed."""
    with parse_cache._lock:
        if parse_cache.usage is None:
            parse_cache.usage = UsageContext.open(None)
        return parse_cache.usage


def build_snapshot_full(
    parse_cache: ParseCache,
    all_projects: bool = False,
    reference_usd: float = 50.0,
    tick_seconds: int = 5,
    period: str = "today",
    thresholds: dict | None = None,
    history_store: HistoryStore | None = None,
    flush_queue: FlushQueue | None = None,
    active_keys: frozenset[tuple[str, int, int]] | None = None,
) -> SnapshotResult:
    """Build the dashboard snapshot.

    Every call first runs a tick (``_ingest_tick``): the JSONL files that changed are
    parsed and their new turns go into the usage engine, and onto *flush_queue* if
    present. Each section of the payload is then a query over the engine.

    *history_store* is the writable store under --persist, else None; the tick uses it
    only for the empty-store check on its first run.
    """
    now = datetime.now(tz=timezone.utc)
    today_date = now.date()
    since_90d = today_date - timedelta(days=89)

    if active_keys is not None:
        # The SSE gate has already globbed every JSONL and stat'ed it to decide
        # whether this tick needs a build at all. Recomputing that here globbed
        # ~3,900 files a second time and stat'ed each a third time inside
        # select_edge_paths — around a fifth of server CPU, nearly all of it in
        # glob._iterdir. The key carries (path, size, mtime_ns), so the mtimes the
        # edge selection needs are already in hand and cost no further I/O.
        mtime_by_path: dict[Path, int] | None = {Path(k[0]): k[2] for k in active_keys}
        paths = sorted(mtime_by_path)
    else:
        mtime_by_path = None
        dirs = get_config_dirs(all_projects=all_projects)
        paths = find_jsonl_files(dirs)

    usage = _usage_context(parse_cache)
    _fired = _ingest_tick(parse_cache, paths, history_store, flush_queue, mtime_by_path)
    queries = usage.queries
    # Before the first query: pushes each new session's grouped project into the engine.
    # Without it a session added this tick would group under its raw cwd, or as "(unknown)".
    usage.index.project_map()

    daily_90d = _build_daily_series(queries.daily(TurnFilter(since=since_90d), fill_since=since_90d))

    period_filter = TurnFilter(since=range_since(period, today_date))

    thresholds = thresholds if thresholds is not None else dict(DEFAULTS)

    tiles = _build_tiles(queries, period_filter, daily_90d, today_date, thresholds, now)
    topbar = _build_topbar(queries, today_date)
    anomaly = _build_anomaly(tiles, today_date)
    # Daily default is a 30-day window regardless of global period.
    daily_default_since = today_date - timedelta(days=29)
    hourly = _build_hourly(queries, today_date)
    daily = _build_daily(
        daily_90d, period, today_date, _engine_active_entities(queries, TurnFilter(since=daily_default_since))
    )
    models = _models_payload(queries.model_rollups(period_filter), period)
    recent_activity = _build_recent_activity(queries, now)

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
        "estimated_models": estimated_models(queries.distinct_values(SearchField.MODEL, TurnFilter())),
    }

    # Each build leaves the allocator holding the memory its queries used. On the real store RSS
    # climbed 587 -> 915 MB over 16 builds and reached ~970 MB over 50, and malloc_trim returned
    # it to ~450 MB. Trimming after every build holds RSS at 380-420 MB over 50 builds for a median
    # 11-13 ms against a 215 ms build; capping the arenas instead (mallopt) left 499 MB and slowed
    # builds by 60 %.
    before, after = release_free_memory()
    log.debug("snapshot build trimmed the heap: RSS %s -> %s bytes", before, after)

    return SnapshotResult(payload=payload)


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


_COMPARE_TOP_N = 8
# Tools and skills ranked past this many collapse into one 'other' row.
_BREAKDOWN_TOP_N = 10


def _parse_filter(param: str) -> str | list[str]:
    """Parse a project/model query-param value.

    Returns either the literal 'all'/'compare' sentinel, or a list of explicit values.
    A single-element list is treated as a singleton filter downstream.
    """
    if param in ("all", "compare"):
        return param
    return [s.strip() for s in param.split(",") if s.strip()]


def _is_compare(spec: str | list[str]) -> bool:
    return spec == "compare" or (isinstance(spec, list) and len(spec) > 1)


def _scoped(f: TurnFilter, key: GroupKey, spec: str | list[str]) -> TurnFilter:
    """*f* limited, on the *key* dimension, to the explicit values in *spec* (no limit for a sentinel)."""
    values = tuple(spec) if isinstance(spec, list) else ()
    return replace(f, projects=values) if key is GroupKey.PROJECT else replace(f, models=values)


def _build_series(
    queries: UsageQueries,
    base: TurnFilter,
    points: TurnFilter,
    project: str,
    model: str,
    points_fn: Callable[[TurnFilter], list[dict]],
) -> list[dict]:
    """Build series list for timeline endpoints.

    project/model may each be: 'all', 'compare' (top-N by cost), a single value, or
    a comma-separated list of values (explicit compare). The "(unknown)" cwd is
    dropped from compare results.

    *base* is the set compare mode ranks and explicit lists are checked against (the
    series a chart can offer); *points* is the set each series' points cover, and may
    be narrower. A ranked or listed label with no turn in *points* still gets its
    series, all zeros, from *points_fn* over that empty selection.
    """
    spec = {GroupKey.PROJECT: _parse_filter(project), GroupKey.MODEL: _parse_filter(model)}
    for key, other in ((GroupKey.PROJECT, GroupKey.MODEL), (GroupKey.MODEL, GroupKey.PROJECT)):
        if not _is_compare(spec[key]):
            continue
        # The other dimension's explicit list narrows what is ranked and what is plotted.
        ranked_over = _scoped(base, other, spec[other])
        if spec[key] == "compare":
            labels = queries.top_keys_by_cost(ranked_over, key, _COMPARE_TOP_N)
        else:
            # Explicit list: keep the user's order, skip silently entries with no turns.
            listed = spec[key]
            present = set(queries.top_keys_by_cost(_scoped(ranked_over, key, listed), key, len(listed)))
            labels = [value for value in listed if value in present]
        plotted = _scoped(points, other, spec[other])
        return [{"label": label, "points": points_fn(_scoped(plotted, key, [label]))} for label in labels]

    # Single-filter or all/all mode. project and model are either 'all' or a length-1 list here.
    plotted = _scoped(_scoped(points, GroupKey.PROJECT, spec[GroupKey.PROJECT]), GroupKey.MODEL, spec[GroupKey.MODEL])
    p, m = spec[GroupKey.PROJECT], spec[GroupKey.MODEL]
    label = p[0] if isinstance(p, list) else (m[0] if isinstance(m, list) else "all")
    return [{"label": label, "points": points_fn(plotted)}]


def build_hourly_panel(
    queries: UsageQueries,
    target_date: date,
    metric: str = "hit_pct",
    project: str = "all",
    model: str = "all",
) -> dict:
    day = TurnFilter(since=target_date, until=target_date)

    def _points(f: TurnFilter) -> list[dict]:
        return [
            {"hour": r.hour.isoformat(), "value": _extract_metric(r, metric), "turns": r.turns}
            for r in queries.hourly(f, target_date=target_date, fill_day=True)
        ]

    return {
        "date": str(target_date),
        "metric": metric,
        "y_unit": _METRIC_Y_UNIT.get(metric, "unknown"),
        "series": _build_series(queries, day, day, project, model, _points),
        **_engine_active_entities(queries, day),
    }


def build_daily_panel(
    queries: UsageQueries,
    range_str: str,
    metric: str = "hit_pct",
    project: str = "all",
    model: str = "all",
    today_date: date | None = None,
) -> dict:
    today_date = today_date or date.today()
    since = range_since(range_str, today_date)
    earliest_date = queries.earliest_date(TurnFilter()) or today_date
    # For range=all, anchor zero-fill at the earliest active day so step-plot gaps
    # render as broken lines instead of last-value-extending across inactive stretches.
    fill_since = since if since is not None else earliest_date

    def _points(f: TurnFilter) -> list[dict]:
        return [
            {"date": str(r.date), "value": _extract_metric(r, metric), "turns": r.turns}
            for r in queries.daily(f, fill_since=fill_since, fill_until=today_date)
        ]

    return {
        "range": range_str,
        "metric": metric,
        "y_unit": _METRIC_Y_UNIT.get(metric, "unknown"),
        "earliest_available": str(earliest_date),
        "series": _build_series(
            queries, TurnFilter(), TurnFilter(since=fill_since, until=today_date), project, model, _points
        ),
        **_engine_active_entities(queries, TurnFilter(since=fill_since)),
    }


def build_models_panel(rollups: list[ModelRollup], range_label: str) -> dict:
    return _models_payload(rollups, range_label)


def build_recent_activity_panel(queries: UsageQueries, now: datetime, window_minutes: int = 60) -> dict:
    return _build_recent_activity(queries, now, window_minutes)


def _by_tool_rows(tool_costs: ToolCosts, *, with_last_active: bool) -> list[dict]:
    """Per-tool rows of a model or project page: cost and calls, costliest first (ties by name).

    ``last_active`` is the newest turn with either a cost entry or a call (``last_seen``).
    """
    rows = []
    for name in set(tool_costs.cost_by_tool) | set(tool_costs.calls_by_tool):
        row = {
            "name": name,
            "cost_usd": tool_costs.cost_by_tool.get(name, 0.0),
            "invocations": tool_costs.calls_by_tool.get(name, 0),
        }
        if with_last_active:
            seen = tool_costs.last_seen.get(name)
            row["last_active"] = seen.isoformat() if seen else None
        rows.append(row)
    rows.sort(key=lambda r: (-r["cost_usd"], r["name"]))
    return rows


def _by_skill_rows(skill_costs: SkillCosts, *, with_last_active: bool) -> list[dict]:
    """Per-skill rows of a model or project page: attributed cost and calls, costliest first (ties by name)."""
    rows = []
    for name, cost in skill_costs.cost_by_skill.items():
        row = {"name": name, "cost_usd": cost, "invocations": skill_costs.invocations_by_skill.get(name, 0)}
        if with_last_active:
            seen = skill_costs.last_active.get(name)
            row["last_active"] = seen.isoformat() if seen else None
        rows.append(row)
    rows.sort(key=lambda r: (-r["cost_usd"], r["name"]))
    return rows


def build_model_detail(name: str, queries: UsageQueries) -> dict | None:
    """Build the model drill-down payload for GET /api/model/{name}.

    *name* is a full model id or its short name; every full id it matches is one model here.
    """
    models = tuple(m for m in queries.distinct_values(SearchField.MODEL, TurnFilter()) if name in (m, _short_model(m)))
    if not models:
        return None
    f = TurnFilter(models=models)
    # Only the costliest model's split is shown when a short name matches several full ids.
    mr = queries.model_rollups(f)[0]
    totals = queries.totals(f)

    projects = sorted(
        (
            {
                "cwd": cwd,
                "cwd_b64": encode_cwd(cwd),
                "cost": b.total_cost,
                "turns": b.turns,
                "last_active": b.last_ts.isoformat() if b.last_ts else None,
            }
            for cwd, b in queries.bucket(f, GroupKey.PROJECT).items()
        ),
        key=lambda p: (-p["cost"], p["cwd"]),
    )

    return {
        "name": name,
        "total_cost": totals.cost_usd,
        "total_turns": totals.turns,
        "cost_breakdown": {
            "input_usd": mr.input_usd,
            "output_usd": mr.output_usd,
            "cache_read_usd": mr.cache_read_usd,
            "cache_creation_usd": mr.cache_creation_usd,
        },
        "projects_using_model": projects,
        "by_tool": _by_tool_rows(queries.tool_costs(f, AttributionMode.PRORATA), with_last_active=False),
        "by_skill": _by_skill_rows(queries.skill_costs(_excluding_interrupted(f)), with_last_active=False),
    }


def _recompute_excl_cache_read(
    turn: Turn,
    turn_cost: TurnCost | None = None,
) -> dict[str, float]:
    """Per-tool cost under the 'exclude cache_read' attribution mode.

    `cache_read_usd` is dropped from the per-tool input pool entirely; its share
    that would have gone to each tool flows into the non-tool residual computed
    by the caller. `turn_cost` may be passed in by a caller that already priced
    the turn (avoiding a second `cost_for_turn` call); otherwise it's computed
    here.
    """
    usage = turn.usage
    input_token_pool = usage.input_token_pool
    output_token_count = usage.output_tokens
    if turn_cost is None:
        turn_cost = cost_for_turn(turn.model, usage, at=turn.timestamp)
    input_pool_excl = turn_cost.input_usd + turn_cost.cache_creation_usd

    out: dict[str, float] = {}
    for name, tc in turn.tool_costs.items():
        in_share = (tc.input_tokens / input_token_pool) if input_token_pool else 0.0
        out_share = (tc.output_tokens / output_token_count) if output_token_count else 0.0
        out[name] = in_share * input_pool_excl + out_share * turn_cost.output_usd
    return out


def build_breakdown_tools(tool_costs: ToolCosts) -> list[dict]:
    """Build the ranked tool list for GET /api/breakdown/tools.

    *tool_costs* holds the per-tool cost in the requested attribution mode, the call
    counts, last-active times and the residual no tool accounts for (the engine
    computes all of it). This ranks them: top-N tools by cost, an 'other' row for the
    tail, then a ``__unattributed__`` row. The caller wraps it in the response envelope.
    """
    ranked = _rank_dict_with_others(tool_costs.cost_by_tool, top_n=_BREAKDOWN_TOP_N)
    head_names = {row["name"] for row in ranked if row["name"] != "other"}
    # The "other" row collapses tail tools — its `count` (used as the bar
    # value in tokens mode) must be the sum of those tools' invocations,
    # not the *number* of collapsed tools. `tool_count` (set inside
    # `_rank_dict_with_others`) preserves the latter for the UI label.
    tail_call_sum = sum(c for n, c in tool_costs.calls_by_tool.items() if n not in head_names)
    for row in ranked:
        name = row["name"]
        if name == "other":
            row["count"] = tail_call_sum
        elif name in tool_costs.calls_by_tool:
            row["count"] = tool_costs.calls_by_tool[name]
        if name in tool_costs.last_active:
            row["last_active"] = tool_costs.last_active[name].isoformat()
        row["cost_usd"] = row.pop("value")
    ranked.append({"name": UNATTRIBUTED_TOOL, "cost_usd": tool_costs.unattributed_usd})

    return ranked


def _rank_skill_costs(
    cost_by_skill: dict[str, float],
    inv_by_skill: dict[str, int],
    last_active: dict[str, datetime],
    *,
    top_n: int = _BREAKDOWN_TOP_N,
) -> list[dict]:
    """Rank pre-accumulated skill costs into the Skill Mix rows (no turn walk)."""
    # Skill Mix ranks by cost, so a skill that was triggered but accrued no
    # attributed cost (invocations only) intentionally doesn't get a row. The
    # `other` tally therefore sums invocations of the collapsed *cost-bearing*
    # tail only — not those orphan invocation-only skills, which would otherwise
    # inflate the "other" count with skills that have no bar.
    ranked = _rank_dict_with_others(cost_by_skill, top_n=top_n)
    head_names = {r["name"] for r in ranked if r["name"] != "other"}
    tail_inv_sum = sum(inv_by_skill.get(n, 0) for n in cost_by_skill if n not in head_names)
    for row in ranked:
        name = row["name"]
        if name == "other":
            row["invocations"] = tail_inv_sum
        else:
            row["invocations"] = inv_by_skill.get(name, 0)
            if name in last_active:
                row["last_active"] = last_active[name].isoformat()
        row["cost_usd"] = row.pop("value")
    return ranked


def build_skill_breakdown(skill_costs: SkillCosts, top_n: int = _BREAKDOWN_TOP_N) -> dict:
    """Full GET /api/breakdown/skills payload from the engine's per-skill sums.

    Returns ``{skills, total_cost, total_billable_tokens, skill_billable_tokens,
    invoked_no_cost}``: the ranked rows, the spend totals, and the skills that were
    started but had no cost billed to them.
    """
    return {
        "skills": _rank_skill_costs(
            skill_costs.cost_by_skill, skill_costs.invocations_by_skill, skill_costs.last_active, top_n=top_n
        ),
        "total_cost": skill_costs.total_cost,
        "total_billable_tokens": skill_costs.total_billable_tokens,
        "skill_billable_tokens": skill_costs.skill_billable_tokens,
        "invoked_no_cost": _invoked_without_cost(skill_costs.cost_by_skill, skill_costs.invocations_by_skill),
    }


def estimated_models(models: Iterable[str]) -> list[dict]:
    """Models among *models* that are missing from the price list, and what each is priced as.

    Feeds the dashboard banner. A missing model is priced as its family's
    fallback until the table gains an entry, at which point every stored turn
    is repriced on read, so the banner is what prompts that correction.
    """
    out = []
    for model in sorted({m for m in models if m}):
        if model_price_status(model) is PriceStatus.ESTIMATED:
            entry, _ = registry.resolve(model)
            out.append({"model": model, "priced_as": FAMILY_FALLBACKS[entry["family"]]})
    return out


def _invoked_without_cost(
    cost_by_skill: dict[str, float],
    inv_by_skill: dict[str, int],
) -> dict:
    """Skills that were started but had no cost billed specifically to them.

    Returns ``{"skills": <distinct count>, "uses": <total invocations>}`` for
    skills that show up as a Skill-tool invocation but never as an attributed
    cost. The Skill Mix ranks by cost, so these would otherwise vanish; the
    panel shows "+N started with no separate cost" instead of dropping them.
    Works off the already-accumulated dicts so the combined breakdown builder
    doesn't have to walk the turns again.
    """
    no_cost = {n: c for n, c in inv_by_skill.items() if c > 0 and n not in cost_by_skill}
    return {"skills": len(no_cost), "uses": sum(no_cost.values())}


def _cwd_label(cwd: str) -> str:
    return cwd.rsplit("/", 1)[-1] if cwd != UNKNOWN_LABEL else UNKNOWN_LABEL


def _detail_lists(rows: Iterable[tuple[str, str, float, int, datetime]]) -> tuple[list[dict], list[dict]]:
    """The ``by_project`` and ``by_model`` lists of a tool or skill page, costliest first, uncapped.

    *rows* are ``(project, model, cost, invocations, last_active)``, one per (project, model) pair.
    """
    by_cwd: dict[str, tuple[float, int, datetime]] = {}
    by_model: dict[str, tuple[float, int]] = {}
    for cwd, model, cost, invocations, last in rows:
        old_cost, old_invs, old_last = by_cwd.get(cwd, (0.0, 0, last))
        by_cwd[cwd] = (old_cost + cost, old_invs + invocations, max(old_last, last))
        old_cost, old_invs = by_model.get(model, (0.0, 0))
        by_model[model] = (old_cost + cost, old_invs + invocations)
    by_project = sorted(
        (
            {
                "cwd_b64": encode_cwd(cwd) if cwd != UNKNOWN_LABEL else None,
                "project_label": _cwd_label(cwd),
                "cost_usd": cost,
                "invocations": invocations,
                "last_active": last.isoformat(),
            }
            for cwd, (cost, invocations, last) in by_cwd.items()
        ),
        key=lambda r: (-r["cost_usd"], r["project_label"], r["cwd_b64"] or ""),
    )
    by_model_rows = sorted(
        ({"name": m, "cost_usd": cost, "invocations": invs} for m, (cost, invs) in by_model.items()),
        key=lambda r: (-r["cost_usd"], r["name"]),
    )
    return by_project, by_model_rows


def _top_project(by_project: list[dict], total_cost: float) -> dict:
    """The costliest project of a tool or skill page, with its share of that tool's or skill's cost."""
    if not by_project:
        return {"name": "—", "cost_usd": 0.0, "share": 0.0}
    top = by_project[0]
    return {
        "name": top["project_label"] if top["cwd_b64"] else "—",
        "cost_usd": top["cost_usd"],
        "share": top["cost_usd"] / total_cost if total_cost > 0 else 0.0,
    }


def _detail_window_start() -> datetime:
    """Start (UTC midnight) of the last seven days, today included: the "calls in 7 days" window."""
    today = datetime.now(tz=timezone.utc).date()
    return datetime.combine(today - timedelta(days=_RECENT_DAYS - 1), datetime.min.time(), tzinfo=timezone.utc)


def build_tool_detail(name: str, queries: UsageQueries) -> dict | None:
    """Build the tool drill-down payload for GET /api/tool/{name}."""
    if name in (UNATTRIBUTED_TOOL, UNKNOWN_TOOL, SKILL_TOOL):
        return None
    billable = TurnFilter(interrupted=InterruptedPolicy.EXCLUDE)
    rows = queries.tool_detail_rows(billable, name, recent_since=_detail_window_start())
    if not rows:
        return None

    total_cost = sum(r.cost_usd for r in rows)
    total_invocations = sum(r.invocations for r in rows)
    by_project, by_model = _detail_lists(
        (r.project, r.model, r.cost_usd, r.invocations, r.last_active) for r in rows
    )
    grand_total_cost = queries.totals(billable).cost_usd
    daily = queries.tool_cost_daily(billable, name)

    return {
        "name": name,
        "total_invocations": total_invocations,
        "scorecards": {
            "cost_usd": total_cost,
            "output_tokens": sum(r.output_tokens for r in rows),
            "invocations": total_invocations,
            "invocations_7d": sum(r.recent_invocations for r in rows),
            "share_of_total": (total_cost / grand_total_cost) if grand_total_cost else 0.0,
            "top_project": _top_project(by_project, total_cost),
        },
        "daily_cost": [{"date": d.date.isoformat(), "cost_usd": d.cost_usd} for d in daily],
        # Capped so a user with many projects or model variants keeps a bounded payload; rows
        # folded into an "other" footer would lose their click-through, so the tail is dropped.
        "by_project": by_project[:_DETAIL_LIST_LIMIT],
        "by_model": by_model[:_DETAIL_LIST_LIMIT],
    }


def build_skill_detail(name: str, queries: UsageQueries) -> dict | None:
    """Build the skill drill-down payload for GET /api/skill/{name}.

    Cost, tokens, by_model, by_project and the daily series come from the turns attributed to
    the skill (turn-level: each turn's full cost). Invocations come from Skill-tool trigger
    blocks across all turns. ``split`` partitions the attributed cost into inline (main thread)
    and sub-agent (sidechain), the dimension's headline.
    """
    billable = TurnFilter(interrupted=InterruptedPolicy.EXCLUDE)
    detail = queries.skill_detail(billable, name, recent_since=_detail_window_start())
    if not detail.rows and detail.invocations == 0:
        return None

    total_cost = sum((r.cost_usd for r in detail.rows), 0.0)
    # A skill's per-pair rows carry no invocations: its calls sit on turns it is not attributed to.
    by_project, by_model = _detail_lists((r.project, r.model, r.cost_usd, 0, r.last_active) for r in detail.rows)
    grand_total_cost = queries.totals(billable).cost_usd
    daily = queries.skill_cost_daily(billable, name)

    return {
        "name": name,
        "total_invocations": detail.invocations,
        "scorecards": {
            "cost_usd": total_cost,
            "output_tokens": float(sum(r.output_tokens for r in detail.rows)),
            "invocations": detail.invocations,
            "invocations_7d": detail.recent_invocations,
            "share_of_total": (total_cost / grand_total_cost) if grand_total_cost else 0.0,
            "top_project": _top_project(by_project, total_cost),
        },
        "split": {
            "inline_usd": sum((r.inline_usd for r in detail.rows), 0.0),
            "subagent_usd": sum((r.subagent_usd for r in detail.rows), 0.0),
        },
        "daily_cost": [{"date": d.date.isoformat(), "cost_usd": d.cost_usd} for d in daily],
        "by_project": by_project[:_DETAIL_LIST_LIMIT],
        "by_model": by_model[:_DETAIL_LIST_LIMIT],
    }


def _str_score(target: str, query: str) -> float:
    if not query:
        return 0.0
    if target.startswith(query):
        return 1.0
    if query in target:
        return 0.7
    return 0.0


def build_search_results(query: str, usage: UsageContext) -> dict:
    q = query.lower().strip()
    hits: list[dict] = []
    sessions = usage.index.all()

    if q.startswith("session:"):
        val = q[8:].strip()
        for s in sessions:
            score = _str_score(s.session_id.lower(), val)
            if score > 0:
                cwd = s.cwd or UNKNOWN_LABEL
                hits.append(
                    {
                        "kind": "session",
                        "label": f"{cwd.split('/')[-1]} — {s.session_id[:8]}",
                        "href": f"/session/{s.session_id}",
                        "score": score,
                    }
                )

    elif q.startswith("cwd:"):
        val = q[4:].strip()
        seen: set[str] = set()
        cwd_by_sid = usage.index.project_map()
        for s in sessions:
            cwd = cwd_by_sid.get(s.session_id, UNKNOWN_LABEL)
            if cwd in seen:
                continue
            score = _str_score(cwd.lower(), val)
            if score > 0:
                seen.add(cwd)
                hits.append(
                    {
                        "kind": "project",
                        "label": _cwd_basename(cwd),
                        "href": f"/project/{encode_cwd(cwd)}",
                        "score": score,
                    }
                )

    elif q.startswith("model:"):
        val = q[6:].strip()
        for model in usage.queries.distinct_values(SearchField.MODEL, TurnFilter()):
            score = _str_score(model.lower(), val)
            if score == 0:
                score = _str_score(_short_model(model).lower(), val)
            if score > 0:
                short = _short_model(model)
                hits.append({"kind": "model", "label": short, "href": f"/model/{short}", "score": score})

    elif q.startswith("date:"):
        val = q[5:].strip()
        for day in usage.queries.distinct_values(SearchField.DATE, TurnFilter()):
            score = _str_score(day, val)
            if score > 0:
                hits.append({"kind": "day", "label": day, "href": f"/day/{day}", "score": score})

    elif q.startswith("verdict:"):
        val = q[8:].strip()
        # Every session's verdict: the first call computes and caches them all.
        rollups = usage.rollups.rollups([s.session_id for s in sessions])
        for s, sr in zip(sessions, rollups, strict=True):
            if _str_score(sr.verdict.value.lower(), val) > 0:
                cwd = s.cwd or UNKNOWN_LABEL
                hits.append(
                    {
                        "kind": "session",
                        "label": f"{cwd.split('/')[-1]} — {sr.verdict.value}",
                        "href": f"/session/{s.session_id}",
                        "score": 1.0,
                    }
                )

    else:
        seen_cwds: set[str] = set()
        for s in sessions:
            cwd = s.cwd or UNKNOWN_LABEL
            score_cwd = _str_score(cwd.lower(), q)
            score_sid = _str_score(s.session_id.lower(), q)
            score = max(score_cwd, score_sid)
            if score > 0:
                hits.append(
                    {
                        "kind": "session",
                        "label": f"{cwd.split('/')[-1]} — {s.session_id[:8]}",
                        "href": f"/session/{s.session_id}",
                        "score": score,
                    }
                )
                if cwd not in seen_cwds and score_cwd > 0:
                    seen_cwds.add(cwd)
                    hits.append(
                        {
                            "kind": "project",
                            "label": _cwd_basename(cwd),
                            "href": f"/project/{encode_cwd(cwd)}",
                            "score": score_cwd,
                        }
                    )

    hits.sort(key=lambda h: -h["score"])
    return {"hits": hits[:_SEARCH_HIT_LIMIT], "query": query}


# ---------------------------------------------------------------------------
# Drilldown builders
# ---------------------------------------------------------------------------


# Page sizes and windows of the drill-down payloads.
_TOP_TURNS_PER_PROJECT = 20
_DETAIL_LIST_LIMIT = 50  # by_project / by_model rows on a tool or skill page
_RECENT_DAYS = 7  # the "calls in 7 days" window, today included
_SEARCH_HIT_LIMIT = 20
_DAY_TREND_DAYS = 90  # the daily series the day page's 7-day median reads
_DAY_ANOMALY_RATIO = 2.0  # a day costing this many times the trailing median is flagged
_DAY_TOP_SESSIONS = 20
_DAY_TOP_PROJECTS = 10

_RANGE_DAYS: dict[str, int | None] = {
    "1d": 0,
    "7d": 6,
    "14d": 13,
    "30d": 29,
    "all": None,
}


def _trend_point(unit_start: str, r: DailyRollup | HourlyRollup) -> dict:
    return {
        "date": unit_start,
        "hit_rate": cache_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens) or 0.0,
        "cost_usd": r.cost_usd,
    }


def _project_cache_trend(
    queries: UsageQueries, f: TurnFilter, today: date, *, hourly: bool
) -> tuple[list[dict], str]:
    """The project page's cache-efficiency series and its unit.

    A one-day range collapses a daily series to one or two points (a flat line or a dot), so it
    gets hourly buckets instead. Any other range is daily and zero-filled from its first day
    (for "all": the project's first turn day, not the epoch) to today.
    """
    if hourly:
        return [_trend_point(r.hour.isoformat(), r) for r in queries.hourly(f, target_date=today)], "hour"
    first_day = f.since or queries.earliest_date(f)
    return [_trend_point(str(r.date), r) for r in queries.daily(f, fill_since=first_day)], "day"


def build_project_detail(cwd: str, usage: UsageContext, range_key: str = "14d") -> dict | None:
    """Build the project drill-down payload for GET /api/project/{cwd_b64}."""
    if range_key not in _RANGE_DAYS:
        raise ValueError(f"Unknown range: {range_key!r}")
    queries = usage.queries

    today_date = datetime.now(tz=timezone.utc).date()
    days_back = _RANGE_DAYS[range_key]
    since = (today_date - timedelta(days=days_back)) if days_back is not None else None

    cwd_by_sid = usage.index.project_map()
    project_ids = [s.session_id for s in usage.index.all() if cwd_by_sid.get(s.session_id) == cwd]
    if not project_ids:
        return None

    # Scope rollups to the sessions that were last active in the window.
    scoped_rollups = [
        sr for sr in usage.rollups.rollups(project_ids) if since is None or sr.last_ts.date() >= since
    ]
    if not scoped_rollups:
        return None

    cwd_b64 = encode_cwd(cwd)
    prs = build_project_rollups(scoped_rollups)
    pr = prs[0] if prs else None

    f = TurnFilter(projects=(cwd,), since=since)
    cache_trend, cache_trend_unit = _project_cache_trend(queries, f, today_date, hourly=range_key == "1d")

    growths = [sr.context_growth_rate_val for sr in scoped_rollups]
    _GROWTH_EDGES = [500, 1000, 2000, 5000, 10000]
    labels = ["<500", "500-1k", "1k-2k", "2k-5k", "5k-10k", "10k+"]
    histogram = [{"label": lbl, "count": 0} for lbl in labels]
    for g in growths:
        histogram[bisect.bisect_right(_GROWTH_EDGES, g)]["count"] += 1

    verdict_dist = _verdict_dist(scoped_rollups)

    # Interrupted turns carry no cost and are not listed.
    top_turns = queries.top_turns_by_cost(_excluding_interrupted(f), _TOP_TURNS_PER_PROJECT)

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
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "cache_read_tokens": t.cache_read_tokens,
                "cache_creation_tokens": t.cache_creation_tokens,
                "hit_rate": cache_hit_rate(t.cache_read_tokens, t.cache_creation_tokens, t.input_tokens),
                "cost_per_kw": cost_per_kw(t.cost_usd, t.output_tokens),
                "ctx_ratio": ctx_ratio_n_to_1(t.cache_read_tokens, t.output_tokens),
            }
            for t in top_turns
        ],
        "by_tool": _by_tool_rows(queries.tool_costs(f, AttributionMode.PRORATA), with_last_active=True),
        "by_skill": _by_skill_rows(queries.skill_costs(_excluding_interrupted(f)), with_last_active=True),
    }


def build_day_detail(target_date: date, usage: UsageContext) -> dict | None:
    """Build the day drill-down payload for GET /api/day/{date}."""
    queries = usage.queries
    today_date = datetime.now(tz=timezone.utc).date()
    since_trend = today_date - timedelta(days=_DAY_TREND_DAYS - 1)
    daily_90d_raw: list[dict] = []
    for r in queries.daily(TurnFilter(since=since_trend), fill_since=since_trend):
        kw = cost_per_kw(r.cost_usd, r.output_tokens) or 0.0
        daily_90d_raw.append(
            {
                "date": str(r.date),
                "cost_usd": r.cost_usd,
                "cost_per_kw": kw,
                "hit_rate": cache_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens) or 0.0,
            }
        )

    cost_7d_median = trailing_median(daily_90d_raw, 7, target_date + timedelta(days=1), "cost_usd")

    hours = queries.hourly(TurnFilter(), target_date=target_date, fill_day=True)
    day_turns = sum(r.turns for r in hours)
    if not day_turns and target_date != today_date:
        return None

    # Per hour: the turns of each model (models with a turn that hour only).
    mix: dict[int, dict[str, int]] = defaultdict(dict)
    for model, model_hours in queries.hourly_by(TurnFilter(), GroupKey.MODEL, target_date=target_date).items():
        for r in model_hours:
            mix[r.hour.hour][model] = r.turns
    hourly = [
        {"hour": r.hour.hour, "cost_usd": r.cost_usd, "turns": r.turns, "model_mix": mix.get(r.hour.hour, {})}
        for r in hours
    ]

    touching = set(queries.session_ids(TurnFilter(since=target_date, until=target_date)))
    day_ids = [s.session_id for s in usage.index.all() if s.session_id in touching]
    day_rollups = usage.rollups.rollups(day_ids)
    day_rollups.sort(key=lambda sr: sr.cost_usd, reverse=True)

    verdict_dist = _verdict_dist(day_rollups)

    day_cost = sum(r.cost_usd for r in hours)
    cost_ratio = day_cost / cost_7d_median if cost_7d_median and cost_7d_median > 0 else None
    anomaly_flags = ["cost_2sigma"] if cost_ratio and cost_ratio >= _DAY_ANOMALY_RATIO else []

    return {
        "date": str(target_date),
        "total_cost": day_cost,
        "delta_vs_7d_median": cost_ratio,
        "anomaly_flags": anomaly_flags,
        "hourly": hourly,
        "top_sessions": [_session_rollup_to_dict(sr) for sr in day_rollups[:_DAY_TOP_SESSIONS]],
        "top_projects": _projects_in_window(day_rollups, target_date)[:_DAY_TOP_PROJECTS],
        "verdict_distribution": verdict_dist,
    }
