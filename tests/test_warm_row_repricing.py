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


def test_an_unpriced_model_still_reads_zero(tmp_path: Path) -> None:
    """Non-Claude models price at 0 by design; re-pricing must not invent a number."""
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "history.duckdb"
    _write(db, model="deepseek-v4-pro", stored_cost=12.34)

    store = HistoryStore(db, read_only=True)
    try:
        turns, _ = store.hydrate_hot(window_days=3650)
    finally:
        store.close()

    assert turns[0].cost_usd == pytest.approx(0.0)
