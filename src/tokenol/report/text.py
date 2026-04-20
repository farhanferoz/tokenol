"""ANSI/plain text tables for daily and hourly reports."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.table import Table

from tokenol import assumptions as assumption_recorder
from tokenol.metrics.cost import DailyRollup, HourlyRollup


def _fmt_cost(usd: float) -> str:
    return f"${usd:.4f}"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def print_daily(rollups: Sequence[DailyRollup], console: Console | None = None) -> None:
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

    for line in assumption_recorder.summary_lines():
        c.print(f"[dim]{line}[/dim]")


def print_hourly(rollups: Sequence[HourlyRollup], console: Console | None = None) -> None:
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

    for line in assumption_recorder.summary_lines():
        c.print(f"[dim]{line}[/dim]")
