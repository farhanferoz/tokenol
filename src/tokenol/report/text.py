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


def _fmt_cost_short(usd: float) -> str:
    """Compact 2-decimal form for wide tables."""
    return f"${usd:.2f}"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_cost_per_kw(cost_usd: float, output_tokens: int) -> str:
    """USD per 1,000 output tokens (cost per kiloword of 'work')."""
    if output_tokens <= 0:
        return "—"
    return f"${cost_usd * 1000 / output_tokens:.3f}"


def _fmt_ratio(numerator: int, denominator: int) -> str:
    """Simple ratio N:1 — e.g. context tokens read per output token."""
    if denominator <= 0:
        return "—"
    return f"{numerator / denominator:.0f}:1"


def _fmt_cache_eff(cache_read: int, cache_creation: int) -> str:
    """Cache reuse efficiency as reads-per-create ratio."""
    if cache_creation <= 0:
        return "—"
    return f"{cache_read / cache_creation:.0f}:1"


def _fmt_hit_rate(cache_read: int, cache_creation: int, input_tokens: int) -> str:
    """% of context served from cache (vs. paid cache-create + fresh input)."""
    denom = cache_read + cache_creation + input_tokens
    if denom <= 0:
        return "—"
    return f"{cache_read / denom * 100:.1f}%"


def _fmt_model(model: str | None) -> str:
    """Shorten full model IDs to a display-friendly form.

    e.g. 'claude-sonnet-4-6-20251015' -> 'sonnet-4.6'
         'claude-opus-4-7-20260101'   -> 'opus-4.7'
    """
    if not model:
        return "—"
    m = model.removeprefix("claude-")
    # Strip trailing -YYYYMMDD date suffix if present.
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        m = parts[0]
    # Convert family-<major>-<minor> -> family-<major>.<minor>
    tokens = m.split("-")
    if len(tokens) >= 3 and tokens[-1].isdigit() and tokens[-2].isdigit():
        m = "-".join(tokens[:-2]) + f"-{tokens[-2]}.{tokens[-1]}"
    return m


def _fmt_duration(td: timedelta) -> str:
    total_secs = int(td.total_seconds())
    if total_secs < 0:
        total_secs = 0
    h = total_secs // 3600
    m = (total_secs % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


_VERDICT_SHORT = {
    BlowUpVerdict.OK: ("ok", "green"),
    BlowUpVerdict.CONTEXT_CREEP: ("ctx-creep", "red"),
    BlowUpVerdict.RUNAWAY_WINDOW: ("runaway", "red"),
    BlowUpVerdict.TOOL_ERROR_STORM: ("tool-errs", "red"),
    BlowUpVerdict.SIDECHAIN_HEAVY: ("sidechain", "yellow"),
}


def _verdict_style(verdict: BlowUpVerdict) -> str:
    label, colour = _VERDICT_SHORT.get(verdict, (verdict.value, "white"))
    return f"[{colour}]{label}[/{colour}]"


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
    tbl.add_column("Date", style="bold", no_wrap=True)
    tbl.add_column("Out", justify="right", no_wrap=True)
    tbl.add_column("CtxRd", justify="right", no_wrap=True)
    tbl.add_column("Cost", justify="right", style="green", no_wrap=True)
    tbl.add_column("$/kW", justify="right", no_wrap=True)
    tbl.add_column("Ctx", justify="right", no_wrap=True)
    tbl.add_column("CacheE", justify="right", no_wrap=True)
    tbl.add_column("Hit%", justify="right", no_wrap=True)

    total_cost = 0.0
    total_out = 0
    total_read = 0
    total_create = 0
    total_input = 0
    total_turns = 0

    for r in rollups:
        tbl.add_row(
            str(r.date),
            _fmt_tokens(r.output_tokens),
            _fmt_tokens(r.cache_read_tokens),
            _fmt_cost_short(r.cost_usd),
            _fmt_cost_per_kw(r.cost_usd, r.output_tokens),
            _fmt_ratio(r.cache_read_tokens, r.output_tokens),
            _fmt_cache_eff(r.cache_read_tokens, r.cache_creation_tokens),
            _fmt_hit_rate(r.cache_read_tokens, r.cache_creation_tokens, r.input_tokens),
        )
        total_cost += r.cost_usd
        total_out += r.output_tokens
        total_read += r.cache_read_tokens
        total_create += r.cache_creation_tokens
        total_input += r.input_tokens
        total_turns += r.turns

    if rollups:
        tbl.add_row(
            "[bold]TOTAL[/bold]",
            _fmt_tokens(total_out),
            _fmt_tokens(total_read),
            _fmt_cost_short(total_cost),
            _fmt_cost_per_kw(total_cost, total_out),
            _fmt_ratio(total_read, total_out),
            _fmt_cache_eff(total_read, total_create),
            _fmt_hit_rate(total_read, total_create, total_input),
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
    tbl.add_column("Session", style="bold", no_wrap=True)
    tbl.add_column("Model", no_wrap=True)
    tbl.add_column("Start", no_wrap=True)
    tbl.add_column("Turns", justify="right", no_wrap=True)
    tbl.add_column("MaxIn", justify="right", no_wrap=True)
    tbl.add_column("Cost", justify="right", style="green", no_wrap=True)
    tbl.add_column("Verdict", no_wrap=True)

    for sr in rollups:
        tbl.add_row(
            sr.session_id[:8],
            _fmt_model(sr.model),
            sr.first_ts.strftime("%m-%d %H:%M"),
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
    tbl.add_column("Model", style="bold", no_wrap=True)
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
            _fmt_model(mr.model),
            str(mr.turns),
            _fmt_tokens(mr.input_tokens),
            _fmt_tokens(mr.output_tokens),
            _fmt_tokens(mr.cache_read_tokens),
            _fmt_cost(mr.cost_usd),
            tool_err_pct,
        )

    c.print(tbl)
    _print_assumptions(c, force=show_assumptions)
