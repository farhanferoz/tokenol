"""Unit tests for history.py and thresholds.py."""

from __future__ import annotations

from datetime import date

from tokenol.metrics.history import baseline_median, trailing_median, trailing_stddev
from tokenol.model.pricing import context_window

# ---- trailing_median ---------------------------------------------------

def _series(values: list[float], start_date: date) -> list[dict]:
    from datetime import timedelta
    return [
        {"date": str(start_date + timedelta(days=i)), "cost_usd": v}
        for i, v in enumerate(values)
    ]


def test_trailing_median_basic():
    today = date(2026, 1, 15)
    series = _series([10.0, 20.0, 30.0, 10.0, 20.0, 30.0, 10.0], date(2026, 1, 8))
    result = trailing_median(series, 7, today)
    assert result is not None
    assert result == 20.0


def test_trailing_median_cold_start_too_few():
    today = date(2026, 1, 5)
    series = _series([5.0, 10.0], date(2026, 1, 3))
    result = trailing_median(series, 7, today)
    assert result is None


def test_trailing_median_excludes_today():
    today = date(2026, 1, 10)
    # Make today's value an outlier
    series = _series([10.0] * 6 + [999.0], date(2026, 1, 4))
    assert series[-1]["date"] == "2026-01-10"
    result = trailing_median(series, 7, today)
    assert result is not None
    assert result == 10.0  # today excluded


def test_trailing_median_zero_values_excluded():
    today = date(2026, 1, 10)
    # Zero-cost days (no usage) should not count toward the baseline
    series = [
        {"date": "2026-01-03", "cost_usd": 0.0},
        {"date": "2026-01-04", "cost_usd": 0.0},
        {"date": "2026-01-05", "cost_usd": 10.0},
        {"date": "2026-01-06", "cost_usd": 20.0},
        {"date": "2026-01-07", "cost_usd": 30.0},
    ]
    # Only 3 non-zero days — below days//3=2 minimum (7//3=2, and we need max(3,2)=3)
    result = trailing_median(series, 7, today)
    # 3 non-zero values meets the threshold of max(3, 7//3)=max(3,2)=3
    assert result == 20.0


# ---- trailing_stddev ---------------------------------------------------

def test_trailing_stddev_basic():
    today = date(2026, 1, 15)
    series = _series([10.0, 10.0, 10.0, 20.0, 10.0, 10.0, 10.0], date(2026, 1, 8))
    result = trailing_stddev(series, 7, today)
    assert result is not None
    assert result > 0


def test_trailing_stddev_cold_start():
    today = date(2026, 1, 5)
    series = _series([5.0, 10.0, 15.0], date(2026, 1, 2))
    result = trailing_stddev(series, 7, today)
    assert result is None  # fewer than 4 baseline days


# ---- context_window helper --------------------------------------------

def test_context_window_known_model():
    assert context_window("claude-opus-4-7") == 1_000_000
    assert context_window("claude-haiku-4-5") == 200_000


def test_context_window_unknown_model():
    assert context_window("some-unknown-model-xyz") is None
    assert context_window("") is None


# ---- baseline_median (cold-start fallback) ---------------------------------

def test_baseline_median_cold_fewer_than_3():
    today = date(2026, 1, 5)
    series = _series([10.0, 15.0], date(2026, 1, 3))
    val, label = baseline_median(series, today)
    assert val is None
    assert label == "cold"


def test_baseline_median_3d_branch():
    today = date(2026, 1, 10)
    series = _series([10.0, 20.0, 30.0, 15.0, 25.0], date(2026, 1, 5))
    val, label = baseline_median(series, today)
    assert label == "3d"
    assert val is not None


def test_baseline_median_7d_branch():
    today = date(2026, 1, 15)
    series = _series([10.0, 20.0, 30.0, 10.0, 20.0, 30.0, 10.0, 20.0], date(2026, 1, 7))
    val, label = baseline_median(series, today)
    assert label == "7d"
    assert val is not None


def test_baseline_median_excludes_today():
    today = date(2026, 1, 10)
    series = _series([10.0] * 8 + [999.0], date(2026, 1, 2))
    assert series[-1]["date"] == str(today)
    val, label = baseline_median(series, today)
    assert val is not None and val < 100


def test_baseline_median_excludes_zero_days():
    today = date(2026, 1, 10)
    series = [
        {"date": "2026-01-03", "cost_usd": 0.0},
        {"date": "2026-01-04", "cost_usd": 0.0},
        {"date": "2026-01-05", "cost_usd": 5.0},
    ]
    val, label = baseline_median(series, today)
    assert val is None and label == "cold"
