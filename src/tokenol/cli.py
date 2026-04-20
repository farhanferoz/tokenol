"""tokenol — Claude Code usage & efficiency audit CLI."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import typer
from rich.console import Console

from tokenol import assumptions as assumption_recorder
from tokenol.ingest.builder import build_sessions, build_turns
from tokenol.ingest.discovery import find_jsonl_files, get_config_dirs
from tokenol.metrics.cost import rollup_by_date, rollup_by_hour
from tokenol.metrics.rollups import (
    build_model_rollups,
    build_project_rollups,
    build_session_rollup,
)
from tokenol.metrics.verdicts import compute_verdict
from tokenol.metrics.windows import align_windows, project_window
from tokenol.report.text import (
    print_daily,
    print_hourly,
    print_live_full,
    print_models,
    print_projects,
    print_sessions,
)

app = typer.Typer(
    name="tokenol",
    help="Audit Claude Code JSONL logs: cost, cache health, blow-up detection.",
    add_completion=False,
)

console = Console(stderr=False)
err = Console(stderr=True)


class LogLevel(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"


class SortKey(str, Enum):
    cost = "cost"
    input = "input"
    output = "output"
    cache_read = "cache_read"
    turns = "turns"
    max_input = "max_input"
    duration = "duration"


def _parse_since(since: str) -> date:
    """Parse e.g. '14d', '30d', '2026-04-01' into a date."""
    since = since.strip()
    if since.endswith("d"):
        days = int(since[:-1])
        return (datetime.now(tz=timezone.utc) - timedelta(days=days)).date()
    return date.fromisoformat(since)


def _parse_last(last: str) -> timedelta:
    """Parse lookback duration like '20m', '2h', '30s'. Rejects bare numbers."""
    m = re.fullmatch(r"(\d+)([mhs])", last.strip())
    if not m:
        raise typer.BadParameter(
            f"Invalid duration '{last}'. Use format like '20m', '2h', '30s'."
        )
    value = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    # unit == "s"
    return timedelta(seconds=value)


def _configure_logging(log_level: LogLevel) -> None:
    level = getattr(logging, log_level.value.upper(), logging.INFO)
    logging.basicConfig(level=level)


def _load_turns(since: date | None = None):
    assumption_recorder.reset()
    dirs = get_config_dirs()
    paths = find_jsonl_files(dirs)
    turns = build_turns(paths)
    if since:
        turns = [t for t in turns if t.timestamp.date() >= since]
    return turns, paths


def _load_turns_and_sessions(since: date | None = None):
    assumption_recorder.reset()
    dirs = get_config_dirs()
    paths = find_jsonl_files(dirs)
    turns = build_turns(paths)
    sessions = build_sessions(turns, paths=paths)
    if since:
        turns = [t for t in turns if t.timestamp.date() >= since]
        sessions = [
            s for s in sessions
            if s.turns and s.turns[-1].timestamp.date() >= since
        ]
    return turns, sessions, paths


@app.command()
def daily(
    since: str = typer.Option("14d", help="Start date, e.g. '14d' or '2026-04-01'"),
    strict: bool = typer.Option(False, "--strict", help="Error on any assumption fallback"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions", help="Always print assumption footer"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Daily token and cost aggregates."""
    _configure_logging(log_level)
    since_date = _parse_since(since)
    turns, paths = _load_turns(since=since_date)
    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")
    rollups = rollup_by_date(turns)
    print_daily(rollups, console=console, show_assumptions=show_assumptions)


@app.command()
def hourly(
    day: str | None = typer.Argument(None, help="Date as YYYY-MM-DD (default: today)"),
    strict: bool = typer.Option(False, "--strict"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Hourly token and cost breakdown for one day."""
    _configure_logging(log_level)
    target = date.fromisoformat(day) if day else date.today()
    turns, paths = _load_turns()
    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")
    rollups = rollup_by_hour(turns, target_date=target)
    print_hourly(rollups, console=console, show_assumptions=show_assumptions)


@app.command()
def live(
    last: str = typer.Option(..., "--last", help="Lookback duration, e.g. '20m', '2h', '30s'"),
    strict: bool = typer.Option(False, "--strict"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Live burn-rate view for the active 5h window."""
    _configure_logging(log_level)
    lookback = _parse_last(last)

    turns, paths = _load_turns()
    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")

    now = datetime.now(tz=timezone.utc)

    # Only consider turns within a reasonable past window (last 10h) to keep it fast
    recent_all = [t for t in turns if t.timestamp >= now - timedelta(hours=10)]
    windows = align_windows(recent_all)

    if not windows:
        console.print("[yellow]No active window found.[/yellow]")
        raise typer.Exit(code=0)

    active = windows[-1]

    # Check if active window is actually current (within 5h of now)
    if active.end < now:
        console.print("[yellow]No active window found (all windows have ended).[/yellow]")
        raise typer.Exit(code=0)

    proj = project_window(active, now=now, lookback=lookback)

    # Count recent turns
    cutoff = now - lookback
    recent_turns_count = sum(1 for t in active.turns if t.timestamp >= cutoff)

    # Format the last label nicely
    m = re.fullmatch(r"(\d+)([mhs])", last.strip())
    if m:
        val, unit = m.group(1), m.group(2)
        last_label = f"{val}{'min' if unit == 'm' else ('hr' if unit == 'h' else 'sec')}"
    else:
        last_label = last

    print_live_full(
        active_window=active,
        projection=proj,
        recent_turns_count=recent_turns_count,
        last_label=last_label,
        console=console,
    )

    if proj["over_reference"]:
        raise typer.Exit(code=1)


@app.command()
def sessions(
    top: int = typer.Option(10, "--top", help="Number of sessions to show"),
    sort: SortKey = typer.Option(SortKey.cost, "--sort", help="Sort key"),  # noqa: B008
    since: str = typer.Option("14d", help="Start date, e.g. '14d' or '2026-04-01'"),
    strict: bool = typer.Option(False, "--strict"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Per-session detail table sorted by a chosen metric."""
    _configure_logging(log_level)
    since_date = _parse_since(since)
    turns, session_list, paths = _load_turns_and_sessions(since=since_date)

    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")

    # Build rollups and compute verdicts
    rollups = []
    for session in session_list:
        sr = build_session_rollup(session)
        sr.verdict = compute_verdict(sr)
        rollups.append(sr)

    # Sort
    def _sort_key(sr):  # type: ignore[name-defined]
        if sort == SortKey.cost:
            return sr.cost_usd
        if sort == SortKey.input:
            return sr.input_tokens
        if sort == SortKey.output:
            return sr.output_tokens
        if sort == SortKey.cache_read:
            return sr.cache_read_tokens
        if sort == SortKey.turns:
            return sr.turns
        if sort == SortKey.max_input:
            return sr.max_turn_input
        if sort == SortKey.duration:
            return (sr.last_ts - sr.first_ts).total_seconds()
        return sr.cost_usd

    rollups.sort(key=_sort_key, reverse=True)
    rollups = rollups[:top]

    print_sessions(rollups, console=console, show_assumptions=show_assumptions)


@app.command()
def projects(
    since: str = typer.Option("14d", help="Start date, e.g. '14d' or '2026-04-01'"),
    strict: bool = typer.Option(False, "--strict"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Per-project rollup (grouped by cwd)."""
    _configure_logging(log_level)
    since_date = _parse_since(since)
    turns, session_list, paths = _load_turns_and_sessions(since=since_date)

    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")

    session_rollups = [build_session_rollup(s) for s in session_list]
    project_rollups = build_project_rollups(session_rollups)
    total_cost = sum(pr.cost_usd for pr in project_rollups)

    print_projects(project_rollups, total_cost=total_cost, console=console, show_assumptions=show_assumptions)


@app.command()
def models(
    since: str = typer.Option("14d", help="Start date, e.g. '14d' or '2026-04-01'"),
    strict: bool = typer.Option(False, "--strict"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Per-model rollup."""
    _configure_logging(log_level)
    since_date = _parse_since(since)
    turns, paths = _load_turns(since=since_date)

    if strict and assumption_recorder.fired():
        raise typer.BadParameter("Assumptions fired and --strict is set.")

    model_rollups = build_model_rollups(turns)
    print_models(model_rollups, console=console, show_assumptions=show_assumptions)


@app.command()
def verify(
    since: str = typer.Option("14d"),
    tolerance: float = typer.Option(0.02, help="Max acceptable fractional diff vs ccusage"),
) -> None:
    """Cross-check tokenol totals against ccusage (if installed)."""
    since_date = _parse_since(since)
    turns, paths = _load_turns(since=since_date)

    our_cost = sum(t.cost_usd for t in turns)

    # Try ccusage
    try:
        result = subprocess.run(
            ["ccusage", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err.print("[yellow]ccusage returned non-zero exit — skipping comparison[/yellow]")
            console.print(f"tokenol total: ${our_cost:.4f}")
            return
        import json
        data = json.loads(result.stdout)
        # ccusage JSON structure: list of day objects or a totals key
        ccusage_cost = 0.0
        if isinstance(data, list):
            for row in data:
                ccusage_cost += row.get("cost", 0) or row.get("totalCost", 0)
        elif isinstance(data, dict):
            ccusage_cost = data.get("totalCost", data.get("cost", 0))

        diff = abs(our_cost - ccusage_cost) / max(ccusage_cost, 1e-9)
        status = "OK" if diff <= tolerance else "FAIL"
        console.print(f"tokenol: ${our_cost:.4f}  ccusage: ${ccusage_cost:.4f}  diff: {diff:.1%}  [{status}]")
        if status == "FAIL":
            raise typer.Exit(code=1)

    except FileNotFoundError:
        err.print("[dim]ccusage not on PATH — showing tokenol total only[/dim]")
        console.print(f"tokenol total: ${our_cost:.4f}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
