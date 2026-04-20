"""5-hour window alignment and burn-rate projection tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tokenol import assumptions as assumption_recorder
from tokenol.enums import AssumptionTag
from tokenol.metrics.windows import Window, align_windows, project_window
from tokenol.model.events import Turn, Usage


def _make_turn(
    offset_hours: float,
    base: datetime | None = None,
    cost: float = 1.0,
    is_interrupted: bool = False,
) -> Turn:
    if base is None:
        base = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)
    ts = base + timedelta(hours=offset_hours)
    usage = Usage(input_tokens=1000) if not is_interrupted else Usage()
    return Turn(
        dedup_key=f"k-{offset_hours}",
        timestamp=ts,
        session_id="sess-test",
        model="claude-opus-4-7",
        usage=usage,
        is_sidechain=False,
        stop_reason=None if is_interrupted else "end_turn",
        cost_usd=0.0 if is_interrupted else cost,
        is_interrupted=is_interrupted,
    )


BASE = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)


def test_three_turns_two_windows():
    """T, T+2h, T+6h → window 1 has T and T+2h; window 2 has T+6h."""
    t0 = _make_turn(0, BASE, cost=1.0)
    t2 = _make_turn(2, BASE, cost=2.0)
    t6 = _make_turn(6, BASE, cost=3.0)

    assumption_recorder.reset()
    windows = align_windows([t0, t2, t6])

    assert len(windows) == 2

    w1 = windows[0]
    assert w1.start == BASE
    assert w1.end == BASE + timedelta(hours=5)
    assert len(w1.turns) == 2
    assert abs(w1.cost_usd - 3.0) < 1e-9

    w2 = windows[1]
    assert w2.start == BASE + timedelta(hours=6)
    assert len(w2.turns) == 1
    assert abs(w2.cost_usd - 3.0) < 1e-9


def test_window_boundary_assumption_recorded():
    """align_windows records WINDOW_BOUNDARY_HEURISTIC exactly once."""
    assumption_recorder.reset()
    align_windows([_make_turn(0, BASE)])
    fired = assumption_recorder.fired()
    assert AssumptionTag.WINDOW_BOUNDARY_HEURISTIC in fired
    assert fired[AssumptionTag.WINDOW_BOUNDARY_HEURISTIC] == 1


def test_half_open_interval():
    """A turn at exactly window.end starts a new window."""
    t0 = _make_turn(0, BASE)
    t5 = _make_turn(5, BASE)  # exactly at end of first window

    windows = align_windows([t0, t5])
    assert len(windows) == 2
    assert windows[1].start == BASE + timedelta(hours=5)


def test_interrupted_turn_attached_to_window():
    """An interrupted turn within a window's [start, end) is attached."""
    t0 = _make_turn(0, BASE, cost=1.0)
    t1_int = _make_turn(1, BASE, is_interrupted=True)

    windows = align_windows([t0, t1_int])
    assert len(windows) == 1
    assert len(windows[0].turns) == 2


def test_interrupted_turn_before_first_window_dropped():
    """An interrupted turn before any billable turn is dropped."""
    t_int = _make_turn(0, BASE, is_interrupted=True)
    t1 = _make_turn(1, BASE, cost=1.0)

    windows = align_windows([t_int, t1])
    assert len(windows) == 1
    assert len(windows[0].turns) == 1  # only billable turn


def test_empty_turns():
    windows = align_windows([])
    assert windows == []


def test_project_window():
    """Burn-rate projection with known values."""
    t0 = _make_turn(0, BASE, cost=5.0)
    t1 = _make_turn(1, BASE, cost=3.0)
    window = Window(start=BASE, end=BASE + timedelta(hours=5), turns=[t0, t1])

    now = BASE + timedelta(hours=2)
    lookback = timedelta(minutes=60)

    result = project_window(window, now, lookback)

    assert result["elapsed_in_window"] == timedelta(hours=2)
    assert result["remaining_in_window"] == timedelta(hours=3)

    # recent_turns: turns within [now - 60m, now) = [T+1h, T+2h)
    # Only t1 (at T+1h) qualifies: recent_cost = 3.0
    assert abs(result["recent_cost"] - 3.0) < 1e-9

    # burn_rate = 3.0 / 1.0 hr = 3.0 $/hr
    assert abs(result["burn_rate_usd_per_hour"] - 3.0) < 1e-9

    # projected = (5.0 + 3.0) + 3.0 * 3 = 8.0 + 9.0 = 17.0
    assert abs(result["projected_window_cost"] - 17.0) < 1e-9
    assert result["over_reference"] is False


def test_project_window_over_reference():
    """over_reference is True when projected > $50."""
    turns = [_make_turn(0, BASE, cost=10.0)]
    window = Window(start=BASE, end=BASE + timedelta(hours=5), turns=turns)

    now = BASE + timedelta(minutes=30)
    lookback = timedelta(minutes=30)

    result = project_window(window, now, lookback)

    # recent_cost = 10.0, burn_rate = 10 / 0.5 = 20 $/hr
    # remaining = 4.5 hr
    # projected = 10 + 20 * 4.5 = 100 > 50 -> over_reference
    assert result["over_reference"] is True
