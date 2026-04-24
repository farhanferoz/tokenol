"""Cost decomposition from billed token fields."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from tokenol.enums import AssumptionTag
from tokenol.model import registry
from tokenol.model.events import Turn, Usage

_M = 1_000_000  # tokens per pricing unit


@dataclass
class TurnCost:
    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_creation_usd: float
    total_usd: float
    assumptions: list[AssumptionTag]


def cost_for_turn(model: str | None, usage: Usage) -> TurnCost:
    tags: list[AssumptionTag] = []

    if model:
        entry, fallback_tags = registry.resolve(model)
        tags.extend(fallback_tags)
    else:
        entry = None
        tags.append(AssumptionTag.UNKNOWN_MODEL_FALLBACK)

    if entry is None:
        return TurnCost(0, 0, 0, 0, 0, tags)

    input_usd = usage.input_tokens * entry["input"] / _M
    output_usd = usage.output_tokens * entry["output"] / _M
    cache_read_usd = usage.cache_read_input_tokens * entry["cache_read"] / _M
    cache_creation_usd = usage.cache_creation_input_tokens * entry["cache_write"] / _M
    total = input_usd + output_usd + cache_read_usd + cache_creation_usd

    return TurnCost(input_usd, output_usd, cache_read_usd, cache_creation_usd, total, tags)


def cache_saved_usd(turns: Iterable[Turn]) -> float:
    """Sum of cache-read counterfactual savings across *turns*, in USD.

    For each turn with a resolvable model: computes what its cache_read tokens
    would have cost at that model's full input price, minus what they actually
    cost at its cache_read price. Turns with model=None, an unknown model, or
    zero cache_read_input_tokens contribute 0.
    """
    total = 0.0
    for turn in turns:
        cache_read = turn.usage.cache_read_input_tokens
        if not turn.model or cache_read == 0:
            continue
        entry, _tags = registry.resolve(turn.model)
        if entry is None:
            continue
        full_input_usd = cache_read * entry["input"] / _M
        actual_cache_usd = cache_read * entry["cache_read"] / _M
        total += full_input_usd - actual_cache_usd
    return total


@dataclass
class DailyRollup:
    date: date
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    interrupted_turns: int


@dataclass
class HourlyRollup:
    hour: datetime
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


def _accumulate_turn(rollup: DailyRollup | HourlyRollup, turn: Turn) -> None:
    rollup.turns += 1
    rollup.input_tokens += turn.usage.input_tokens
    rollup.output_tokens += turn.usage.output_tokens
    rollup.cache_read_tokens += turn.usage.cache_read_input_tokens
    rollup.cache_creation_tokens += turn.usage.cache_creation_input_tokens
    rollup.cost_usd += turn.cost_usd


def rollup_by_date(
    turns: list[Turn],
    since: date | None = None,
    until: date | None = None,
) -> list[DailyRollup]:
    """Bucket turns by date. If *since*/*until* are given, zero-fill missing days."""

    def _empty(d: date) -> DailyRollup:
        return DailyRollup(
            date=d, turns=0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.0, interrupted_turns=0,
        )

    buckets: dict[date, DailyRollup] = {}

    for turn in turns:
        d = turn.timestamp.date()
        if d not in buckets:
            buckets[d] = _empty(d)
        r = buckets[d]
        _accumulate_turn(r, turn)
        if turn.is_interrupted:
            r.interrupted_turns += 1

    if since is not None:
        until = until or date.today()
        cur = since
        while cur <= until:
            if cur not in buckets:
                buckets[cur] = _empty(cur)
            cur += timedelta(days=1)

    return sorted(buckets.values(), key=lambda r: r.date)


def rollup_by_hour(
    turns: list[Turn],
    target_date: date | None = None,
    fill_day: bool = False,
) -> list[HourlyRollup]:
    """Bucket turns by UTC hour. If *fill_day* and *target_date*, zero-fill 0-23."""

    def _empty(h: datetime) -> HourlyRollup:
        return HourlyRollup(
            hour=h, turns=0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.0,
        )

    buckets: dict[datetime, HourlyRollup] = {}

    for turn in turns:
        utc_ts = turn.timestamp.astimezone(timezone.utc)
        if target_date and utc_ts.date() != target_date:
            continue
        hour = utc_ts.replace(minute=0, second=0, microsecond=0)
        if hour not in buckets:
            buckets[hour] = _empty(hour)
        _accumulate_turn(buckets[hour], turn)

    if fill_day and target_date:
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        for h in range(24):
            hour = day_start + timedelta(hours=h)
            buckets.setdefault(hour, _empty(hour))

    return sorted(buckets.values(), key=lambda r: r.hour)
