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


def test_stored_rows_reprice_at_the_rate_of_their_own_day(tmp_path: Path) -> None:
    """History on disk must follow dated and prompt-length pricing, not just today's flat rate.

    Sonnet 5.5 rows were persisted at $0.20 cache reads; Anthropic cut that to $0.10
    on 2026-10-07, so rows after the cut must drop and rows before it must not. A
    Haiku 5.5 row whose prompt is over 100k tokens must reprice at the long-prompt
    rates. Each row's per-tool split must still add up to its repriced total.
    """
    pytest.importorskip("duckdb")
    from tokenol.model.events import ToolCost
    from tokenol.persistence.store import HistoryStore

    cut = datetime(2026, 10, 7, tzinfo=timezone.utc)
    usage = Usage(input_tokens=1_000, output_tokens=500, cache_read_input_tokens=200_000, cache_creation_input_tokens=4_000)
    rows = {
        "sonnet-before": ("claude-sonnet-5-5", cut - timedelta(hours=1)),
        "sonnet-after": ("claude-sonnet-5-5", cut + timedelta(hours=1)),
        "haiku-long": ("claude-haiku-5-5", cut + timedelta(days=10)),
    }
    turns = [
        Turn(
            dedup_key=key,
            timestamp=ts,
            session_id="s-1",
            model=model,
            usage=usage,
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=999.0,  # deliberately stale
            tool_costs={"Bash": ToolCost(tool_name="Bash", input_tokens=usage.input_token_pool / 2, output_tokens=500, cost_usd=999.0)},
            unattributed_input_tokens=usage.input_token_pool / 2,
            unattributed_cost_usd=999.0,
        )
        for key, (model, ts) in rows.items()
    ]
    db = tmp_path / "history.duckdb"
    store = HistoryStore(db)
    store.flush(turns, [Session(session_id="s-1", source_file="", is_sidechain=False, cwd="/x", turns=turns)])
    store.close()

    store = HistoryStore(db, read_only=True)
    try:
        got, _ = store.hydrate_hot(window_days=3650)
    finally:
        store.close()

    by_key = {t.dedup_key: t for t in got}
    assert by_key.keys() == rows.keys()
    for key, (model, ts) in rows.items():
        t = by_key[key]
        assert t.cost_usd == pytest.approx(cost_for_turn(model, usage, at=ts).total_usd), key
        split = sum(tc.cost_usd for tc in t.tool_costs.values()) + t.unattributed_cost_usd
        assert split == pytest.approx(t.cost_usd), key
    reads = usage.cache_read_input_tokens
    assert by_key["sonnet-before"].cost_usd - by_key["sonnet-after"].cost_usd == pytest.approx(reads * (0.20 - 0.10) / 1e6)
    # Haiku 5.5 over the line: every rate is the long-prompt one ($0.50 in, $2.50 out,
    # $0.625 5-minute write, $0.05 read).
    expected_haiku = (1_000 * 0.50 + 500 * 2.50 + 4_000 * 0.625 + reads * 0.05) / 1e6
    assert by_key["haiku-long"].cost_usd == pytest.approx(expected_haiku)
