"""Per-session, per-project, and per-model rollup dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tokenol.enums import BlowUpVerdict
from tokenol.metrics.context import cache_reuse_ratio, context_growth_rate, max_turn_input
from tokenol.metrics.windows import align_windows
from tokenol.model.events import Session, Turn


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
    peak_window_cost: float = 0.0
    verdict: BlowUpVerdict = BlowUpVerdict.OK
    model: str | None = None  # most-used model in session


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


def build_session_rollup(session: Session) -> SessionRollup:
    """Compute a SessionRollup from a Session."""
    billable_turns = [t for t in session.turns if not t.is_interrupted]

    if not session.turns:
        first_ts = last_ts = datetime.min
    else:
        first_ts = session.turns[0].timestamp
        last_ts = session.turns[-1].timestamp

    # Token sums over billable turns
    input_tokens = sum(t.usage.input_tokens for t in billable_turns)
    output_tokens = sum(t.usage.output_tokens for t in billable_turns)
    cache_read = sum(t.usage.cache_read_input_tokens for t in billable_turns)
    cache_creation = sum(t.usage.cache_creation_input_tokens for t in billable_turns)
    cost = sum(t.cost_usd for t in billable_turns)
    tool_use_count = sum(t.tool_use_count for t in session.turns)
    tool_error_count = sum(t.tool_error_count for t in session.turns)

    # Context metrics over billable turns
    mti = max_turn_input(billable_turns)
    crr = cache_reuse_ratio(billable_turns)
    cgr = context_growth_rate(billable_turns)

    # Peak 5h window cost
    windows = align_windows(session.turns)
    peak_window_cost = max((w.cost_usd for w in windows), default=0.0)

    # Most common model
    model_counts: dict[str, int] = {}
    for t in session.turns:
        if t.model:
            model_counts[t.model] = model_counts.get(t.model, 0) + 1
    model = max(model_counts, key=lambda m: model_counts[m]) if model_counts else None

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
        peak_window_cost=peak_window_cost,
        verdict=BlowUpVerdict.OK,
        model=model,
    )


def build_project_rollups(session_rollups: list[SessionRollup]) -> list[ProjectRollup]:
    """Aggregate SessionRollups by cwd."""
    buckets: dict[str, dict] = {}

    for sr in session_rollups:
        key = sr.cwd or "(unknown)"
        if key not in buckets:
            buckets[key] = {
                "sessions": 0,
                "turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
                "cache_reads": 0,
                "cache_creates": 0,
            }
        b = buckets[key]
        b["sessions"] += 1
        b["turns"] += sr.turns
        b["input_tokens"] += sr.input_tokens
        b["output_tokens"] += sr.output_tokens
        b["cache_read_tokens"] += sr.cache_read_tokens
        b["cache_creation_tokens"] += sr.cache_creation_tokens
        b["cost_usd"] += sr.cost_usd
        b["cache_reads"] += sr.cache_read_tokens
        b["cache_creates"] += sr.cache_creation_tokens

    result: list[ProjectRollup] = []
    for cwd, b in buckets.items():
        reads = b["cache_reads"]
        creates = b["cache_creates"]
        denom = reads + creates
        crr = reads / denom if denom > 0 else None
        result.append(
            ProjectRollup(
                cwd=cwd,
                sessions=b["sessions"],
                turns=b["turns"],
                input_tokens=b["input_tokens"],
                output_tokens=b["output_tokens"],
                cache_read_tokens=b["cache_read_tokens"],
                cache_creation_tokens=b["cache_creation_tokens"],
                cost_usd=b["cost_usd"],
                cache_reuse_ratio=crr,
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
            }
        b = buckets[key]
        b["turns"] += 1
        if not turn.is_interrupted:
            b["input_tokens"] += turn.usage.input_tokens
            b["output_tokens"] += turn.usage.output_tokens
            b["cache_read_tokens"] += turn.usage.cache_read_input_tokens
            b["cache_creation_tokens"] += turn.usage.cache_creation_input_tokens
            b["cost_usd"] += turn.cost_usd
        b["tool_use_count"] += turn.tool_use_count
        b["tool_error_count"] += turn.tool_error_count

    result: list[ModelRollup] = []
    for model, b in buckets.items():
        result.append(
            ModelRollup(
                model=model,
                turns=b["turns"],
                input_tokens=b["input_tokens"],
                output_tokens=b["output_tokens"],
                cache_read_tokens=b["cache_read_tokens"],
                cache_creation_tokens=b["cache_creation_tokens"],
                cost_usd=b["cost_usd"],
                tool_use_count=b["tool_use_count"],
                tool_error_count=b["tool_error_count"],
            )
        )

    result.sort(key=lambda r: r.cost_usd, reverse=True)
    return result
