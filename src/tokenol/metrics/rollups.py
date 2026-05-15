"""Per-session, per-project, and per-model rollup dataclasses."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from tokenol.enums import BlowUpVerdict
from tokenol.metrics.context import (
    cache_reuse_n_to_1,
    cache_reuse_ratio,
    context_growth_rate,
    cost_per_kw,
    ctx_ratio_n_to_1,
    ctx_used_latest,
    max_turn_input,
)
from tokenol.metrics.cost import cost_for_turn
from tokenol.metrics.thresholds import (
    CACHE_CREATION_DOMINANCE_AMBER,
    CACHE_HIT_RATE_RED,
    CONTEXT_GROWTH_AMBER,
    TOOL_ERROR_RATE_AMBER,
)
from tokenol.metrics.windows import align_windows
from tokenol.model.events import Session, Turn
from tokenol.model.pricing import context_window


@dataclass
class SessionRollup:
    session_id: str
    source_file: str
    is_sidechain: bool
    cwd: str | None
    first_ts: datetime
    last_ts: datetime
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    max_turn_input: int
    cache_reuse_ratio: float | None
    context_growth_rate_val: float
    tool_use_count: int
    tool_error_count: int
    tool_mix: Counter[str] = field(default_factory=Counter)
    peak_window_cost: float = 0.0
    verdict: BlowUpVerdict = BlowUpVerdict.OK
    model: str | None = None
    ctx_ratio_n_to_1: float | None = None
    cache_reuse_n_to_1: float | None = None
    cost_per_kw_val: float | None = None
    ctx_used_latest_val: float | None = None


@dataclass
class ProjectRollup:
    cwd: str
    sessions: int
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    cache_reuse_ratio: float | None
    cache_hit_rate: float | None = None
    cache_creation_dominance: float | None = None
    avg_context_growth: float | None = None
    tool_error_rate: float | None = None
    interrupted_turn_rate: float | None = None
    peak_window_cost: float = 0.0
    verdict_mix: dict[str, int] | None = None
    flagged: bool = False
    ctx_ratio_n_to_1: float | None = None
    cache_reuse_n_to_1: float | None = None
    cost_per_kw_val: float | None = None
    ctx_used_latest: float | None = None
    model_mix: dict[str, float] = field(default_factory=dict)
    dual_session_conflict: bool = False


@dataclass
class ModelRollup:
    model: str
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    tool_use_count: int
    tool_error_count: int
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_creation_usd: float = 0.0
    sidechain_turns: int = 0
    interrupted_turns: int = 0
    ctx_ratio_n_to_1: float | None = None
    cache_reuse_n_to_1: float | None = None
    cost_per_kw_val: float | None = None
    cost_share: float | None = None


@dataclass
class ToolCostRollup:
    tool_name: str
    invocations: int
    input_tokens: float
    output_tokens: float
    cost_usd: float
    last_active: datetime | None


def build_tool_cost_rollups(turns: list[Turn]) -> list[ToolCostRollup]:
    """Aggregate per-tool cost across *turns*. Skips interrupted turns."""
    buckets: dict[str, ToolCostRollup] = {}
    for turn in turns:
        if turn.is_interrupted:
            continue
        for name, tc in turn.tool_costs.items():
            if name not in buckets:
                buckets[name] = ToolCostRollup(
                    tool_name=name, invocations=0,
                    input_tokens=0.0, output_tokens=0.0, cost_usd=0.0,
                    last_active=None,
                )
            r = buckets[name]
            r.input_tokens += tc.input_tokens
            r.output_tokens += tc.output_tokens
            r.cost_usd += tc.cost_usd
            if name in turn.tool_names:
                r.invocations += turn.tool_names[name]
                if r.last_active is None or turn.timestamp > r.last_active:
                    r.last_active = turn.timestamp
    return sorted(buckets.values(), key=lambda r: r.cost_usd, reverse=True)


@dataclass
class DailyToolCost:
    date: date
    cost_usd: float


def build_tool_cost_daily(
    turns: list[Turn], *, tool_name: str, days: int = 30, today: date | None = None
) -> list[DailyToolCost]:
    """Per-day cost_usd for *tool_name* over the last *days* days, zero-filled."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    buckets: dict[date, float] = {start + timedelta(days=i): 0.0 for i in range(days)}
    for turn in turns:
        if turn.is_interrupted:
            continue
        tc = turn.tool_costs.get(tool_name)
        if not tc:
            continue
        d = turn.timestamp.date()
        if d in buckets:
            buckets[d] += tc.cost_usd
    return [DailyToolCost(date=d, cost_usd=c) for d, c in sorted(buckets.items())]


def build_session_rollup(session: Session) -> SessionRollup:
    """Compute a SessionRollup from a Session."""
    billable_turns = [t for t in session.turns if not t.is_interrupted]

    if not session.turns:
        first_ts = last_ts = datetime.min
    else:
        first_ts = session.turns[0].timestamp
        last_ts = session.turns[-1].timestamp

    input_tokens = sum(t.usage.input_tokens for t in billable_turns)
    output_tokens = sum(t.usage.output_tokens for t in billable_turns)
    cache_read = sum(t.usage.cache_read_input_tokens for t in billable_turns)
    cache_creation = sum(t.usage.cache_creation_input_tokens for t in billable_turns)
    cost = sum(t.cost_usd for t in billable_turns)
    tool_use_count = sum(t.tool_use_count for t in session.turns)
    tool_error_count = sum(t.tool_error_count for t in session.turns)
    tool_mix: Counter[str] = Counter()
    for t in session.turns:
        tool_mix.update(t.tool_names)

    mti = max_turn_input(billable_turns)
    crr = cache_reuse_ratio(billable_turns)
    cgr = context_growth_rate(billable_turns)

    windows = align_windows(session.turns)
    peak_window_cost = max((w.cost_usd for w in windows), default=0.0)

    model_counter = Counter(t.model for t in session.turns if t.model)
    dominant_model = model_counter.most_common(1)[0][0] if model_counter else None

    crn = cache_reuse_n_to_1(cache_read, cache_creation)
    cxr = ctx_ratio_n_to_1(cache_read, output_tokens)
    cpk = cost_per_kw(cost, output_tokens)

    latest_billable = billable_turns[-1] if billable_turns else None
    cul = ctx_used_latest(latest_billable, context_window(dominant_model or "")) if latest_billable else None

    return SessionRollup(
        session_id=session.session_id,
        source_file=session.source_file,
        is_sidechain=session.is_sidechain,
        cwd=session.cwd,
        first_ts=first_ts,
        last_ts=last_ts,
        turns=len(session.turns),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=cost,
        max_turn_input=mti,
        cache_reuse_ratio=crr,
        context_growth_rate_val=cgr,
        tool_use_count=tool_use_count,
        tool_error_count=tool_error_count,
        tool_mix=tool_mix,
        peak_window_cost=peak_window_cost,
        verdict=BlowUpVerdict.OK,
        model=dominant_model,
        ctx_ratio_n_to_1=cxr,
        cache_reuse_n_to_1=crn,
        cost_per_kw_val=cpk,
        ctx_used_latest_val=cul,
    )


def build_project_rollups(
    session_rollups: list[SessionRollup],
    conflicted_cwds: set[str] | None = None,
) -> list[ProjectRollup]:
    """Aggregate SessionRollups by cwd."""
    conflicted = conflicted_cwds or set()
    buckets: dict[str, list[SessionRollup]] = {}

    for sr in session_rollups:
        key = sr.cwd or "(unknown)"
        buckets.setdefault(key, []).append(sr)

    result: list[ProjectRollup] = []
    for cwd, srs in buckets.items():
        reads = sum(sr.cache_read_tokens for sr in srs)
        creates = sum(sr.cache_creation_tokens for sr in srs)
        inputs = sum(sr.input_tokens for sr in srs)
        outputs = sum(sr.output_tokens for sr in srs)
        total_cost = sum(sr.cost_usd for sr in srs)

        cache_denom = reads + creates
        crr = reads / cache_denom if cache_denom > 0 else None

        hit_denom = reads + creates + inputs
        cache_hit_rate = reads / hit_denom if hit_denom > 0 else None
        cache_creation_dominance = creates / max(reads, 1) if creates > 0 else 0.0

        growths = [sr.context_growth_rate_val for sr in srs if sr.context_growth_rate_val != 0.0]
        avg_context_growth = sum(growths) / len(growths) if growths else 0.0

        total_tool_uses = sum(sr.tool_use_count for sr in srs)
        total_tool_errors = sum(sr.tool_error_count for sr in srs)
        tool_error_rate = total_tool_errors / total_tool_uses if total_tool_uses > 0 else 0.0

        peak_window_cost = max((sr.peak_window_cost for sr in srs), default=0.0)

        verdict_mix = {v.value: 0 for v in BlowUpVerdict}
        for sr in srs:
            verdict_mix[sr.verdict.value] += 1

        flagged = bool(
            (cache_hit_rate is not None and cache_hit_rate < CACHE_HIT_RATE_RED)
            or cache_creation_dominance > CACHE_CREATION_DOMINANCE_AMBER
            or avg_context_growth > CONTEXT_GROWTH_AMBER
            or tool_error_rate > TOOL_ERROR_RATE_AMBER
            or any(v != BlowUpVerdict.OK.value and cnt > 0 for v, cnt in verdict_mix.items())
        )

        cxr = ctx_ratio_n_to_1(reads, outputs)
        crn = cache_reuse_n_to_1(reads, creates)
        cpk = cost_per_kw(total_cost, outputs)

        most_recent = max(srs, key=lambda s: s.last_ts)
        cul = most_recent.ctx_used_latest_val

        model_costs: defaultdict[str, float] = defaultdict(float)
        for sr in srs:
            if sr.model:
                model_costs[sr.model] += sr.cost_usd
        cost_denom = sum(model_costs.values()) or 1.0
        model_mix = {m: c / cost_denom for m, c in sorted(model_costs.items(), key=lambda x: -x[1])}

        result.append(
            ProjectRollup(
                cwd=cwd,
                sessions=len(srs),
                turns=sum(sr.turns for sr in srs),
                input_tokens=inputs,
                output_tokens=outputs,
                cache_read_tokens=reads,
                cache_creation_tokens=creates,
                cost_usd=total_cost,
                cache_reuse_ratio=crr,
                cache_hit_rate=cache_hit_rate,
                cache_creation_dominance=cache_creation_dominance,
                avg_context_growth=avg_context_growth,
                tool_error_rate=tool_error_rate,
                interrupted_turn_rate=None,
                peak_window_cost=peak_window_cost,
                verdict_mix=verdict_mix,
                flagged=flagged,
                ctx_ratio_n_to_1=cxr,
                cache_reuse_n_to_1=crn,
                cost_per_kw_val=cpk,
                ctx_used_latest=cul,
                model_mix=model_mix,
                dual_session_conflict=(cwd in conflicted),
            )
        )

    result.sort(key=lambda r: r.cost_usd, reverse=True)
    return result


def build_model_rollups(turns: list[Turn]) -> list[ModelRollup]:
    """Aggregate turns by model string."""
    buckets: dict[str, dict] = {}

    for turn in turns:
        key = turn.model or "(unknown)"
        if key not in buckets:
            buckets[key] = {
                "turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
                "tool_use_count": 0,
                "tool_error_count": 0,
                "input_usd": 0.0,
                "output_usd": 0.0,
                "cache_read_usd": 0.0,
                "cache_creation_usd": 0.0,
                "sidechain_turns": 0,
                "interrupted_turns": 0,
            }
        b = buckets[key]
        b["turns"] += 1
        if turn.is_sidechain:
            b["sidechain_turns"] += 1
        if turn.is_interrupted:
            b["interrupted_turns"] += 1
        else:
            b["input_tokens"] += turn.usage.input_tokens
            b["output_tokens"] += turn.usage.output_tokens
            b["cache_read_tokens"] += turn.usage.cache_read_input_tokens
            b["cache_creation_tokens"] += turn.usage.cache_creation_input_tokens
            b["cost_usd"] += turn.cost_usd
            tc = cost_for_turn(turn.model, turn.usage)
            b["input_usd"] += tc.input_usd
            b["output_usd"] += tc.output_usd
            b["cache_read_usd"] += tc.cache_read_usd
            b["cache_creation_usd"] += tc.cache_creation_usd
        b["tool_use_count"] += turn.tool_use_count
        b["tool_error_count"] += turn.tool_error_count

    total_cost = sum(b["cost_usd"] for b in buckets.values()) or 1.0

    result: list[ModelRollup] = []
    for model, b in buckets.items():
        reads = b["cache_read_tokens"]
        creates = b["cache_creation_tokens"]
        outputs = b["output_tokens"]
        cost = b["cost_usd"]
        result.append(
            ModelRollup(
                model=model,
                turns=b["turns"],
                input_tokens=b["input_tokens"],
                output_tokens=outputs,
                cache_read_tokens=reads,
                cache_creation_tokens=creates,
                cost_usd=cost,
                tool_use_count=b["tool_use_count"],
                tool_error_count=b["tool_error_count"],
                input_usd=b["input_usd"],
                output_usd=b["output_usd"],
                cache_read_usd=b["cache_read_usd"],
                cache_creation_usd=b["cache_creation_usd"],
                sidechain_turns=b["sidechain_turns"],
                interrupted_turns=b["interrupted_turns"],
                ctx_ratio_n_to_1=ctx_ratio_n_to_1(reads, outputs),
                cache_reuse_n_to_1=cache_reuse_n_to_1(reads, creates),
                cost_per_kw_val=cost_per_kw(cost, outputs),
                cost_share=cost / total_cost,
            )
        )

    result.sort(key=lambda r: r.cost_usd, reverse=True)
    return result


def _rank_counter_with_others(total: Counter[str], top_n: int) -> list[dict]:
    """Rank a Counter and emit `[{tool, count}]` with tail collapsed to 'others'.

    Returns `[]` if `total` is empty. Otherwise emits up to `top_n` top entries
    ranked by `Counter.most_common()` (insertion-order tie-break), and appends
    a single `{"tool": "others", "count": <sum of tail>}` row when more than
    `top_n` distinct keys exist.
    """
    if not total:
        return []
    ranked = total.most_common()
    head = ranked[:top_n]
    tail = ranked[top_n:]
    rows = [{"tool": name, "count": count} for name, count in head]
    if tail:
        rows.append({"tool": "others", "count": sum(c for _, c in tail)})
    return rows


def build_tool_mix(
    session_rollups: list[SessionRollup],
    top_n: int = 10,
) -> list[dict]:
    """Rank tool-name call counts across all sessions.

    Returns `[{tool, count}]` sorted count-desc. If more than `top_n` distinct
    tools are present, the tail collapses into a single `{tool: "others",
    count: <sum of tail>}` row. Returns `[]` on empty input.
    """
    total: Counter[str] = Counter()
    for sr in session_rollups:
        total.update(sr.tool_mix)
    return _rank_counter_with_others(total, top_n)
