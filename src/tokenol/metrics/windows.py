"""5-hour rate-limit window alignment and burn-rate projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tokenol import assumptions as assumption_recorder
from tokenol.enums import AssumptionTag
from tokenol.metrics.context import context_tokens
from tokenol.model.events import Turn

WINDOW_DURATION = timedelta(hours=5)


@dataclass
class Window:
    """One 5-hour wall-clock rate-limit window."""

    start: datetime              # first event timestamp
    end: datetime                # start + 5h (half-open: [start, end))
    turns: list[Turn] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns)

    @property
    def total_tokens(self) -> int:
        return sum(context_tokens(t) for t in self.turns)


def align_windows(turns: list[Turn]) -> list[Window]:
    """Partition turns into 5-hour wall-clock windows.

    Only billable turns (not interrupted) open new windows; interrupted
    turns are attached to whichever window contains their timestamp,
    or dropped if before the first window.

    Records AssumptionTag.WINDOW_BOUNDARY_HEURISTIC once per call.
    """
    assumption_recorder.record([AssumptionTag.WINDOW_BOUNDARY_HEURISTIC])

    sorted_turns = sorted(turns, key=lambda t: t.timestamp)
    windows: list[Window] = []
    active: Window | None = None

    for turn in sorted_turns:
        ts = turn.timestamp

        if not turn.is_interrupted:
            # Billable turn: open a new window if none active or current expired
            if active is None or ts >= active.end:
                active = Window(start=ts, end=ts + WINDOW_DURATION)
                windows.append(active)
            active.turns.append(turn)
        else:
            # Interrupted turn: attach to window containing its timestamp, if any
            if active is not None and active.start <= ts < active.end:
                active.turns.append(turn)
            # else: drop (before first window or after active window ends)

    return windows


def project_window(active: Window, now: datetime, lookback: timedelta) -> dict:
    """Extrapolate usage to window.end using the last *lookback* burn rate.

    Returns a dict with keys:
        elapsed_in_window, remaining_in_window,
        recent_cost, burn_rate_usd_per_hour,
        projected_window_cost, over_reference
    """
    elapsed = now - active.start
    remaining_raw = active.end - now
    remaining = remaining_raw if remaining_raw.total_seconds() > 0 else timedelta(0)

    cutoff = now - lookback
    recent_turns = [t for t in active.turns if t.timestamp >= cutoff]
    recent_cost = sum(t.cost_usd for t in recent_turns)

    lookback_hours = lookback.total_seconds() / 3600
    burn_rate_usd_per_hour = recent_cost / lookback_hours if lookback_hours > 0 else 0.0

    projected_window_cost = (
        active.cost_usd + burn_rate_usd_per_hour * remaining.total_seconds() / 3600
    )

    over_reference = projected_window_cost > 50.0

    return {
        "elapsed_in_window": elapsed,
        "remaining_in_window": remaining,
        "recent_cost": recent_cost,
        "burn_rate_usd_per_hour": burn_rate_usd_per_hour,
        "projected_window_cost": projected_window_cost,
        "over_reference": over_reference,
    }
