"""Shared test fixtures.

All `tests/fixtures/*.jsonl` files carry a hardcoded absolute event timestamp
(currently: most at 2026-04-14, `per_tool_basic.jsonl` at 2026-05-15,
`skills.jsonl` at 2026-06-10) — they do NOT move with wall-clock time.

This has caused two separate CI breaks (2026-06-05, 2026-07-13): a test hit a
windowed endpoint/range (default range, `7d`/`14d`/`30d`/`90d`/`today`, or any
`since`-cutoff computation from `date.today()`) against one of these fixtures,
passed for weeks, then started failing/emptying out once the real calendar
date moved past the window relative to the fixture's fixed date.

When writing a NEW test that pairs a static fixture with range/window logic:
  - If the test only cares about panel *shape* (not that data is present),
    a stale fixture is harmless.
  - If the test asserts data IS present, either pass `range=all` (or the
    endpoint's all-time equivalent) so the fixed date can't age out, or build
    the event(s) with a timestamp relative to `date.today()` (see
    `test_daily_insufficient_history` / `test_project_detail_default_range_14d`
    for the pattern) instead of reusing a fixture file.
  - If the test asserts data is ABSENT/empty for being outside the window
    (e.g. `test_breakdown_tools_empty_window`), a stale fixture is safe by
    construction — it only gets more clearly outside the window over time.
"""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def seed_history_store(db_path: Path, *, days_ago: int, turns: int, session_id: str = "warm-sess", cwd: str = "/dev/archived") -> float:
    """Write *turns* persisted turns dated *days_ago* back. Returns their total cost.

    Shared because four separate test modules had grown their own near-identical
    copy of this. Timestamps are built relative to now, not from a fixture file,
    so a caller pairing this with a windowed range does not become a time bomb
    (see the module docstring above).
    """
    from datetime import datetime, timedelta, timezone

    from tokenol.metrics.cost import cost_for_turn
    from tokenol.model.events import Session, Turn, Usage
    from tokenol.persistence.store import HistoryStore

    ts = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    usage = Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    unit = cost_for_turn("claude-opus-4-8", usage).total_usd
    made = [
        Turn(
            # Keyed on session_id, not just the index: turns.dedup_key is a
            # PRIMARY KEY and flush() skips conflicts, so two seed calls on one
            # store with different sessions would silently drop the second.
            dedup_key=f"{session_id}-{i}",
            timestamp=ts + timedelta(seconds=i),
            session_id=session_id,
            model="claude-opus-4-8",
            usage=usage,
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=unit,
        )
        for i in range(turns)
    ]
    session = Session(session_id=session_id, source_file="", is_sidechain=False, cwd=cwd, turns=made)
    store = HistoryStore(db_path)
    store.flush(made, [session])
    store.close()
    return unit * turns


@pytest.fixture(autouse=True)
def _isolate_history_store(tmp_path_factory, monkeypatch):
    """Point every test at a private, non-existent history store.

    `tokenol serve` reads any store it finds at `TOKENOL_HISTORY_PATH` (or
    `~/.tokenol/history.duckdb`) read-only, so without this the suite silently
    merges the *developer's own* warm tier into fixture-based assertions —
    observed as tests failing with real project paths and 88M-token counts that
    no fixture contains.

    Isolating HOME rather than forcing TOKENOL_HISTORY_PATH keeps the existing
    convention working: a test that wants a store sets HOME itself (monkeypatch
    applies in order, so its setenv wins) and writes to `$HOME/.tokenol`.
    """
    monkeypatch.delenv("TOKENOL_HISTORY_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
