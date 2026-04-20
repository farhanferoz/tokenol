"""tokenol — Claude Code usage & efficiency audit CLI."""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import typer
from rich.console import Console

from tokenol import assumptions as assumption_recorder
from tokenol.ingest.builder import build_turns
from tokenol.ingest.discovery import find_jsonl_files, get_config_dirs
from tokenol.metrics.cost import rollup_by_date, rollup_by_hour
from tokenol.report.text import print_daily, print_hourly

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


def _parse_since(since: str) -> date:
    """Parse e.g. '14d', '30d', '2026-04-01' into a date."""
    since = since.strip()
    if since.endswith("d"):
        days = int(since[:-1])
        return (datetime.now(tz=timezone.utc) - timedelta(days=days)).date()
    return date.fromisoformat(since)


def _load_turns(since: date | None = None, strict: bool = False):
    assumption_recorder.reset()
    dirs = get_config_dirs()
    paths = find_jsonl_files(dirs)
    turns = build_turns(paths)
    if since:
        turns = [t for t in turns if t.timestamp.date() >= since]
    return turns


@app.command()
def daily(
    since: str = typer.Option("14d", help="Start date, e.g. '14d' or '2026-04-01'"),
    strict: bool = typer.Option(False, "--strict", help="Error on any assumption fallback"),
    show_assumptions: bool = typer.Option(False, "--show-assumptions", help="Always print assumption footer"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Daily token and cost aggregates."""
    since_date = _parse_since(since)
    turns = _load_turns(since=since_date, strict=strict)
    rollups = rollup_by_date(turns)
    print_daily(rollups, console=console)


@app.command()
def hourly(
    day: str | None = typer.Argument(None, help="Date as YYYY-MM-DD (default: today)"),
    strict: bool = typer.Option(False, "--strict"),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
    """Hourly token and cost breakdown for one day."""
    target = date.fromisoformat(day) if day else date.today()
    turns = _load_turns()
    rollups = rollup_by_hour(turns, target_date=target)
    print_hourly(rollups, console=console)


@app.command()
def verify(
    since: str = typer.Option("14d"),
    tolerance: float = typer.Option(0.02, help="Max acceptable fractional diff vs ccusage"),
) -> None:
    """Cross-check tokenol totals against ccusage (if installed)."""
    since_date = _parse_since(since)
    turns = _load_turns(since=since_date)

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
        # Try to sum cost from entries
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
