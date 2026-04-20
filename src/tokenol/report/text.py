"""ANSI/plain text tables for daily, hourly, live, sessions, projects, and model reports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from rich.console import Console
from rich.table import Table

from tokenol import assumptions as assumption_recorder
from tokenol.enums import BlowUpVerdict
from tokenol.metrics.cost import DailyRollup, HourlyRollup
from tokenol.metrics.rollups import ModelRollup, ProjectRollup, SessionRollup
from tokenol.metrics.windows import Window


def _fmt_cost(usd: float) -> str:
    return f"${usd:.4f}"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_duration(td: timedelta) -> str:
    total_secs = int(td.total_seconds())
    if total_secs < 0:
        total_secs = 0
    h = total_secs // 3600
    m = (total_secs % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _verdict_style(verdict: BlowUpVerdict) -> str:
    if verdict == BlowUpVerdict.OK:
        return "[green]OK[/green]"
    if verdict == BlowUpVerdict.CONTEXT_CREEP:
        return "[red]CONTEXT_CREEP[/red]"
    if verdict == BlowUpVerdict.RUNAWAY_WINDOW:
        return "[red]RUNAWAY_WINDOW[/red]"
    if verdict == BlowUpVerdict.TOOL_ERROR_STORM:
        return "[red]TOOL_ERROR_STORM[/red]"
    if verdict == BlowUpVerdict.SIDECHAIN_HEAVY:
        return "[yellow]SIDECHAIN_HEAVY[/yellow]"
    return verdict.value


def _print_assumptions(c: Console, force: bool = False) -> None:
    lines = assumption_recorder.summary_lines()
    if lines or force:
        for line in lines:
            c.print(f"[dim]{line}[/dim]")


def print_daily(
    rollups: Sequence[DailyRollup],
    console: Console | None = None,
    show_assumptions: bool = False,
) -> None:
    c = console or Console()

    tbl = Table(title="Daily usage", show_lines=False)
    tbl.add_column("Date", style="bold")
    tbl.add_column("Turns", justify="right")
    tbl.add_column("Input", justify="right")
    tbl.add_column("Output", justify="right")
    tbl.add_column("Cache read", justify="right")
    tbl.add_column("Cache write", justify="right")
    tbl.add_column("Cost", justify="right", style="green")

    for r in rollups:
        tbl.add_row(
            str(r.date),
            str(r.turns),
            _fmt_tokens(r.input_tokens),
            _fmt_tokens(r.output_tokens),
            _fmt_tokens(r.cache_read_tokens),
            _fmt_tokens(r.cache_creation_tokens),
            _fmt_cost(r.cost_usd),
        )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)


def print_hourly(
    rollups: Sequence[HourlyRollup],
    console: Console | None = None,
    show_assumptions: bool = False,
) -> None:
    c = console or Console()

    tbl = Table(title="Hourly usage", show_lines=False)
    tbl.add_column("Hour (UTC)", style="bold")
    tbl.add_column("Turns", justify="right")
    tbl.add_column("Input", justify="right")
    tbl.add_column("Output", justify="right")
    tbl.add_column("Cache read", justify="right")
    tbl.add_column("Cost", justify="right", style="green")

    for r in rollups:
        tbl.add_row(
            r.hour.strftime("%Y-%m-%d %H:00"),
            str(r.turns),
            _fmt_tokens(r.input_tokens),
            _fmt_tokens(r.output_tokens),
            _fmt_tokens(r.cache_read_tokens),
            _fmt_cost(r.cost_usd),
        )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)


def print_live_full(
    active_window: Window | None,
    projection: dict | None,
    recent_turns_count: int,
    last_label: str,
    console: Console | None = None,
) -> None:
    """Print the live burn-rate view with accurate recent turn count."""
    c = console or Console()

    if active_window is None:
        c.print("[yellow]No active window found in the lookback period.[/yellow]")
        return

    assert projection is not None

    elapsed = _fmt_duration(projection["elapsed_in_window"])
    remaining = _fmt_duration(projection["remaining_in_window"])
    window_start = active_window.start.strftime("%Y-%m-%d %H:%M UTC")

    c.print(
        f"Active 5h window: started {window_start}, "
        f"{elapsed} elapsed, {remaining} remaining"
    )
    c.print(
        f"Last {last_label}:   "
        f"{recent_turns_count} turns,  "
        f"${projection['recent_cost']:.2f} spent,  "
        f"burn rate ${projection['burn_rate_usd_per_hour']:.2f}/hr"
    )
    c.print(
        f"Window:     ${active_window.cost_usd:.2f} spent,  "
        f"projected ${projection['projected_window_cost']:.2f} at end of window"
    )


def print_sessions(
    rollups: Sequence[SessionRollup],
    console: Console | None = None,
    show_assumptions: bool = False,
) -> None:
    c = console or Console()

    tbl = Table(title="Sessions", show_lines=False)
    tbl.add_column("Session", style="bold")
    tbl.add_column("Model")
    tbl.add_column("Start")
    tbl.add_column("Turns", justify="right")
    tbl.add_column("Max input", justify="right")
    tbl.add_column("Cost", justify="right", style="green")
    tbl.add_column("Verdict")

    for sr in rollups:
        tbl.add_row(
            sr.session_id[:8],
            sr.model or "—",
            sr.first_ts.strftime("%Y-%m-%d %H:%M"),
            str(sr.turns),
            _fmt_tokens(sr.max_turn_input),
            _fmt_cost(sr.cost_usd),
            _verdict_style(sr.verdict),
        )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)


def print_projects(
    rollups: Sequence[ProjectRollup],
    total_cost: float,
    console: Console | None = None,
    show_assumptions: bool = False,
) -> None:
    c = console or Console()

    tbl = Table(title="Projects", show_lines=False)
    tbl.add_column("Project (cwd)", style="bold")
    tbl.add_column("Sessions", justify="right")
    tbl.add_column("Turns", justify="right")
    tbl.add_column("Tokens", justify="right")
    tbl.add_column("Cost", justify="right", style="green")
    tbl.add_column("Cache reuse %", justify="right")

    for pr in rollups:
        total_tokens = (
            pr.input_tokens + pr.output_tokens + pr.cache_read_tokens + pr.cache_creation_tokens
        )
        crr_str = f"{pr.cache_reuse_ratio * 100:.1f}%" if pr.cache_reuse_ratio is not None else "—"
        tbl.add_row(
            pr.cwd,
            str(pr.sessions),
            str(pr.turns),
            _fmt_tokens(total_tokens),
            _fmt_cost(pr.cost_usd),
            crr_str,
        )

    # Trailing totals row
    total_sessions = sum(pr.sessions for pr in rollups)
    total_turns = sum(pr.turns for pr in rollups)
    total_tok = sum(
        pr.input_tokens + pr.output_tokens + pr.cache_read_tokens + pr.cache_creation_tokens
        for pr in rollups
    )
    tbl.add_row(
        "[bold]TOTAL[/bold]",
        str(total_sessions),
        str(total_turns),
        _fmt_tokens(total_tok),
        _fmt_cost(total_cost),
        "100.0%",
    )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)


def print_models(
    rollups: Sequence[ModelRollup],
    console: Console | None = None,
    show_assumptions: bool = False,
) -> None:
    c = console or Console()

    tbl = Table(title="Models", show_lines=False)
    tbl.add_column("Model", style="bold")
    tbl.add_column("Turns", justify="right")
    tbl.add_column("Input", justify="right")
    tbl.add_column("Output", justify="right")
    tbl.add_column("Cache read", justify="right")
    tbl.add_column("Cost", justify="right", style="green")
    tbl.add_column("Tool-error %", justify="right")

    for mr in rollups:
        if mr.tool_use_count > 0:
            tool_err_pct = f"{mr.tool_error_count / mr.tool_use_count * 100:.1f}%"
        else:
            tool_err_pct = "—"
        tbl.add_row(
            mr.model,
            str(mr.turns),
            _fmt_tokens(mr.input_tokens),
            _fmt_tokens(mr.output_tokens),
            _fmt_tokens(mr.cache_read_tokens),
            _fmt_cost(mr.cost_usd),
            tool_err_pct,
        )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)
