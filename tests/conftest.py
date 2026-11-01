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

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokenol.model.events import Turn

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Shared by the usage-engine tests (ingest round trip and aggregation parity).
BASE = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
HOSTILE_CWD = '/home/ünï/"quo,te"\nnext line'
HOSTILE_TOOL = 'mcp__sé,rver__"do\nit"'
HOSTILE_SKILL = 'skill,"é"\nx'
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"  # sorts before SONNET: a position-based model id would shift


def _priced_turn(key: str, sid: str, at: datetime, **fields) -> Turn:
    """A turn priced and attributed exactly as the parser would."""
    from tokenol.ingest.parser import _attribute_cost
    from tokenol.metrics.cost import cost_for_turn
    from tokenol.model.events import EMPTY_TOOL_COSTS

    usage = fields.pop("usage")
    model = fields.pop("model", SONNET)
    out_shares = fields.pop("out_shares", {})
    in_shares = fields.pop("in_shares", {})
    cost = cost_for_turn(model, usage, at=at)
    tools, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        model, usage, out_shares, in_shares, cost, at
    )
    return Turn(
        dedup_key=key,
        timestamp=at,
        session_id=sid,
        model=model,
        usage=usage,
        cost_usd=cost.total_usd,
        tool_costs=dict(tools) if tools else EMPTY_TOOL_COSTS,
        unattributed_input_tokens=unattr_in,
        unattributed_output_tokens=unattr_out,
        unattributed_cost_usd=unattr_cost,
        **fields,
    )


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


def usage_from_sessions(sessions):
    """A mode-(c) usage context holding *sessions* (their turns and metadata), ready to query.

    The engine prices the turns itself from their usage, so a test states token counts and
    models, not dollar amounts. Project grouping is pushed before returning, as the server does.
    """
    from tokenol.engine import SessionInfo, UsageContext

    usage = UsageContext.open(None)
    infos = [SessionInfo(s.session_id, s.source_file, s.cwd, s.is_sidechain) for s in sessions]
    usage.engine.add_turns([t for s in sessions for t in s.turns], infos)
    usage.index.add(infos)
    usage.index.project_map()
    return usage


def engine_sessions(usage):
    """Every session of *usage* as ``Session`` objects holding its engine turns (the oracle tests compare against)."""
    infos = usage.index.all()
    turns_by_sid = usage.engine.session_turns_many(i.session_id for i in infos)
    return [usage.index.session(i.session_id, turns_by_sid.get(i.session_id, [])) for i in infos]


def engine_turns(usage):
    """Every turn of *usage* in time order."""
    return sorted((t for s in engine_sessions(usage) for t in s.turns), key=lambda t: t.timestamp)


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
