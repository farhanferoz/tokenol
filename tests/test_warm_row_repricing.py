"""Persisted rows must be priced by today's table, not the day they were written.

A turn row stores the cost computed when it was flushed, and `_row_to_turn`
returned that number verbatim — so every later pricing correction stopped at the
store boundary. The Opus 5 and Fable 5.1 entries added on 2026-09-04 would never
have reached a single persisted turn, and Fable 5.1 reads cache at a quarter of
the rate its family fallback assumed, so the error is not small.

Schema v4 stores the 5m/1h cache-creation split, so the recompute has the same
inputs the live path does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import Session, Turn, Usage


def _write(db: Path, *, model: str, stored_cost: float) -> Usage:
    from tokenol.persistence.store import HistoryStore

    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_input_tokens=20_000,
        cache_creation_input_tokens=4_000,
        cache_creation_1h_input_tokens=1_000,
    )
    turn = Turn(
        dedup_key="row-1",
        timestamp=datetime.now(tz=timezone.utc) - timedelta(days=10),
        session_id="s-1",
        model=model,
        usage=usage,
        is_sidechain=False,
        stop_reason="end_turn",
        cost_usd=stored_cost,          # deliberately wrong: a stale price
    )
    store = HistoryStore(db)
    store.flush([turn], [Session(session_id="s-1", source_file="", is_sidechain=False, cwd="/x", turns=[turn])])
    store.close()
    return usage


def test_stale_stored_cost_is_recomputed_on_read(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "history.duckdb"
    usage = _write(db, model="claude-opus-4-8", stored_cost=999.0)

    store = HistoryStore(db, read_only=True)
    try:
        turns, _sessions = store.hydrate_hot(window_days=3650)
    finally:
        store.close()

    assert len(turns) == 1
    expected = cost_for_turn("claude-opus-4-8", usage).total_usd
    assert turns[0].cost_usd == pytest.approx(expected)
    assert turns[0].cost_usd != pytest.approx(999.0), "stale stored cost was returned unchanged"


def test_the_1h_cache_split_survives_the_round_trip(tmp_path: Path) -> None:
    """Re-pricing is only correct if the split it prices from is preserved."""
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "history.duckdb"
    _write(db, model="claude-opus-4-8", stored_cost=0.0)

    store = HistoryStore(db, read_only=True)
    try:
        turns, _ = store.hydrate_hot(window_days=3650)
    finally:
        store.close()

    assert turns[0].usage.cache_creation_1h_input_tokens == 1_000
    assert turns[0].usage.cache_creation_input_tokens == 4_000


def test_a_persisted_non_claude_row_is_not_read_back(tmp_path: Path) -> None:
    """Non-Claude models are excluded, not priced at 0.

    Before the live path filtered them, their turns were persisted, so existing
    stores hold such rows. Reading one back as a $0 turn is what diluted every
    blended cost-per-token figure.
    """
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "history.duckdb"
    _write(db, model="deepseek-v4-pro", stored_cost=12.34)

    store = HistoryStore(db, read_only=True)
    try:
        hot, hot_sessions = store.hydrate_hot(window_days=3650)
        warm, warm_sessions = store.hydrate_before(datetime.now(tz=timezone.utc))
        queried = store.query_turns()
        session = store.query_session("s-1")
    finally:
        store.close()

    assert (hot, hot_sessions) == ([], [])
    assert (warm, warm_sessions) == ([], [])
    assert queried == []
    assert session is not None and session.turns == []


def test_per_tool_split_is_repriced_on_read(tmp_path: Path) -> None:
    """The per-tool split must follow the repriced total, not the day it was stored.

    Repricing only `cost_usd` left `tool_costs` and the unattributed residual at
    the rates in force when the row was flushed, so the Tools view kept every
    stale price after a correction reached the headline total. Opus 5.5 is the
    case that exposed it: persisted at Opus 5 rates, where cache reads cost 2.5x.
    The stored per-tool token counts are the turn's pools times each tool's byte
    share, so the shares -- and therefore the current-rate split -- are exact.
    """
    pytest.importorskip("duckdb")
    from tokenol.model.events import ToolCost
    from tokenol.persistence.store import HistoryStore

    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_input_tokens=20_000,
        cache_creation_input_tokens=4_000,
        cache_creation_1h_input_tokens=1_000,
    )
    pool = usage.input_token_pool
    in_share = {"Bash": 0.5, "Read": 0.25}
    out_share = {"Bash": 0.6, "Read": 0.0}
    stale = cost_for_turn("claude-opus-5", usage)   # what the fallback priced it at
    stale_in_pool = stale.input_usd + stale.cache_read_usd + stale.cache_creation_usd
    turn = Turn(
        dedup_key="row-1",
        timestamp=datetime.now(tz=timezone.utc) - timedelta(days=10),
        session_id="s-1",
        model="claude-opus-5-5",
        usage=usage,
        is_sidechain=False,
        stop_reason="end_turn",
        cost_usd=stale.total_usd,
        tool_costs={
            n: ToolCost(
                tool_name=n,
                input_tokens=pool * in_share[n],
                output_tokens=usage.output_tokens * out_share[n],
                cost_usd=stale_in_pool * in_share[n] + stale.output_usd * out_share[n],
            )
            for n in in_share
        },
        unattributed_input_tokens=pool * 0.25,
        unattributed_output_tokens=usage.output_tokens * 0.4,
        unattributed_cost_usd=stale_in_pool * 0.25 + stale.output_usd * 0.4,
    )
    db = tmp_path / "history.duckdb"
    store = HistoryStore(db)
    store.flush([turn], [Session(session_id="s-1", source_file="", is_sidechain=False, cwd="/x", turns=[turn])])
    store.close()

    store = HistoryStore(db, read_only=True)
    try:
        (got,), _ = store.hydrate_hot(window_days=3650)
    finally:
        store.close()

    now = cost_for_turn("claude-opus-5-5", usage)
    now_in_pool = now.input_usd + now.cache_read_usd + now.cache_creation_usd
    for n in in_share:
        expected = now_in_pool * in_share[n] + now.output_usd * out_share[n]
        assert got.tool_costs[n].cost_usd == pytest.approx(expected), n
    assert got.unattributed_cost_usd == pytest.approx(now_in_pool * 0.25 + now.output_usd * 0.4)
    split_total = sum(tc.cost_usd for tc in got.tool_costs.values()) + got.unattributed_cost_usd
    assert split_total == pytest.approx(got.cost_usd), "split no longer adds up to the turn total"
