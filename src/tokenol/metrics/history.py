"""Trailing-window statistics for daily time-series data."""

from __future__ import annotations

import statistics
from datetime import date


def trailing_median(
    daily_series: list[dict],
    days: int,
    today: date,
    value_key: str = "cost_usd",
) -> float | None:
    """Compute median of *value_key* over the *days* days ending the day before *today*.

    Returns None if the baseline window has fewer than *days* entries with
    non-zero cost (cold-start guard — don't emit noisy deltas when there's
    barely any history).
    """
    cutoff = today.toordinal() - days
    window = [
        r[value_key]
        for r in daily_series
        if r.get("date") and date.fromisoformat(r["date"]).toordinal() > cutoff
        and date.fromisoformat(r["date"]) < today
        and r.get(value_key, 0) > 0
    ]
    if len(window) < max(3, days // 3):
        return None
    return statistics.median(window)


def baseline_median(
    series: list[dict],
    today: date,
    key: str = "cost_usd",
) -> tuple[float | None, str]:
    """Return (median, label) with cold-start fallback.

    - Days 0–2 of history: returns (None, "cold").
    - Days 3–6 of history: median of last 3 non-zero days, label "3d".
    - Day 7+: median of last 7 days excluding today, label "7d".
    """
    past = sorted(
        [r for r in series if r.get("date") and date.fromisoformat(r["date"]) < today and r.get(key, 0) > 0],
        key=lambda r: r["date"],
    )
    n = len(past)
    if n < 3:
        return None, "cold"
    if n < 7:
        return statistics.median(r[key] for r in past[-3:]), "3d"
    return statistics.median(r[key] for r in past[-7:]), "7d"


def trailing_stddev(
    daily_series: list[dict],
    days: int,
    today: date,
    value_key: str = "cost_usd",
) -> float | None:
    """Standard deviation over the same baseline window as trailing_median.

    Returns None if fewer than 4 baseline days available.
    """
    cutoff = today.toordinal() - days
    window = [
        r[value_key]
        for r in daily_series
        if r.get("date") and date.fromisoformat(r["date"]).toordinal() > cutoff
        and date.fromisoformat(r["date"]) < today
        and r.get(value_key, 0) >= 0
    ]
    if len(window) < 4:
        return None
    try:
        return statistics.stdev(window)
    except statistics.StatisticsError:
        return None
