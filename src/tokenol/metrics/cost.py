"""Cost decomposition from billed token fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

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


def rollup_by_date(turns: list[Turn]) -> list[DailyRollup]:
    buckets: dict[date, DailyRollup] = {}

    for turn in turns:
        d = turn.timestamp.date()
        if d not in buckets:
            buckets[d] = DailyRollup(
                date=d, turns=0, input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0,
                cost_usd=0.0, interrupted_turns=0,
            )
        r = buckets[d]
        _accumulate_turn(r, turn)
        if turn.is_interrupted:
            r.interrupted_turns += 1

    return sorted(buckets.values(), key=lambda r: r.date)


def rollup_by_hour(turns: list[Turn], target_date: date | None = None) -> list[HourlyRollup]:
    buckets: dict[datetime, HourlyRollup] = {}

    for turn in turns:
        utc_ts = turn.timestamp.astimezone(timezone.utc)
        if target_date and utc_ts.date() != target_date:
            continue
        hour = utc_ts.replace(minute=0, second=0, microsecond=0)
        if hour not in buckets:
            buckets[hour] = HourlyRollup(
                hour=hour, turns=0, input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.0,
            )
        _accumulate_turn(buckets[hour], turn)

    return sorted(buckets.values(), key=lambda r: r.hour)
