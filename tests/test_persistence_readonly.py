"""Reading the warm tier must not require running a flusher.

Reading persisted history and writing it are separate concerns with very
different risk. Reading is what recovers history the JSONL no longer has;
writing runs a flusher thread inside the server process, which is where the
CPU cost and (before the connection lock) the segfaults came from. So a plain
`tokenol serve` opens whatever store exists read-only and surfaces it, and
`--persist` is only about keeping that store up to date.

The schema-compatibility half matters just as much: a store opened read-only
cannot be migrated, and the oldest stores — the ones holding history worth
recovering — predate the columns the read path selects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SEEDED_TURNS = 30
SEEDED_SESSIONS = 1
HTTP_OK = 200

# Columns added by migrations v2-v4. A v1-era store has none of them.
POST_V1_COLUMNS = (
    "tool_costs",
    "unattributed_input_tokens",
    "unattributed_output_tokens",
    "unattributed_cost_usd",
    "attribution_skill",
    "skill_names",
    "cache_creation_1h_tokens",
)


def _seed(db_path: Path, n: int = SEEDED_TURNS) -> float:
    from tokenol.metrics.cost import cost_for_turn
    from tokenol.model.events import Session, Turn, Usage
    from tokenol.persistence.store import HistoryStore

    ts = datetime.now(tz=timezone.utc) - timedelta(days=200)
    usage = Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    turns = [
        Turn(
            dedup_key=f"ro-{i}",
            timestamp=ts + timedelta(seconds=i),
            session_id="ro-sess",
            model="claude-opus-4-8",
            usage=usage,
            is_sidechain=False,
            stop_reason="end_turn",
            cost_usd=cost_for_turn("claude-opus-4-8", usage).total_usd,
        )
        for i in range(n)
    ]
    store = HistoryStore(db_path)
    store.flush(turns, [Session(session_id="ro-sess", source_file="", is_sidechain=False, cwd="/dev/old", turns=turns)])
    store.close()
    return sum(t.cost_usd for t in turns)


def _downgrade_to_v1(db_path: Path) -> None:
    """Strip the v2-v4 columns so the file looks like a pre-migration store.

    Indexes on `turns` depend on the table, so DuckDB refuses the column drops
    until they are gone. They are not recreated: this fixture is about which
    columns exist, and index presence has no bearing on that.
    """
    import duckdb

    con = duckdb.connect(str(db_path))
    for (name,) in con.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'turns'").fetchall():
        con.execute(f"DROP INDEX IF EXISTS {name}")
    for col in POST_V1_COLUMNS:
        con.execute(f"ALTER TABLE turns DROP COLUMN IF EXISTS {col}")
    con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    con.close()


def test_readonly_store_reads_a_v1_file_without_migrating_it(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    import duckdb

    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "h.duckdb"
    expected = _seed(db)
    _downgrade_to_v1(db)

    store = HistoryStore(db, read_only=True)
    turns = store.query_turns()
    assert len(turns) == SEEDED_TURNS
    assert sum(t.cost_usd for t in turns) == pytest.approx(expected)
    # Missing columns hydrate to the defaults the migration would have supplied.
    assert turns[0].usage.cache_creation_1h_input_tokens == 0
    assert turns[0].attribution_skill is None
    hot_turns, hot_sessions = store.hydrate_hot(window_days=100_000)
    assert len(hot_turns) == SEEDED_TURNS
    assert len(hot_sessions) == SEEDED_SESSIONS
    store.close()

    # The file must be untouched: still v1, still missing the columns.
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    cols = {r[0] for r in con.execute("DESCRIBE turns").fetchall()}
    assert not (cols & set(POST_V1_COLUMNS)), "read-only open migrated the file"
    con.close()


def test_readonly_store_refuses_to_flush(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from tokenol.persistence.store import HistoryStore

    db = tmp_path / "h.duckdb"
    _seed(db, n=1)
    store = HistoryStore(db, read_only=True)
    with pytest.raises(RuntimeError, match="read-only"):
        store.flush([], [])
    store.close()


@pytest.mark.asyncio
async def test_serve_without_persist_still_surfaces_the_warm_tier(tmp_path: Path, monkeypatch) -> None:
    """The whole point: `tokenol serve` with no --persist shows persisted history."""
    pytest.importorskip("duckdb")
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    db = tmp_path / ".tokenol" / "history.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    warm_cost = _seed(db, n=SEEDED_TURNS)

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    assert app.state.history_store is None, "no flusher may be constructed without --persist"
    assert app.state.warm_store is not None, "an existing store must still be read"
    assert app.state.warm_store.read_only is True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        merged = (await client.get("/api/breakdown/summary?range=all")).json()
    assert merged["turns"] >= SEEDED_TURNS
    assert merged["cost_usd"] > warm_cost * 0.99


@pytest.mark.asyncio
async def test_serve_without_a_store_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    """No store file: no warm tier, no error, hot tier exactly as before."""
    from httpx import ASGITransport, AsyncClient

    from tokenol.serve.app import ServerConfig, create_app

    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "sess-001.jsonl").write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_state_mod, "get_config_dirs", lambda all_projects=False: [tmp_path])

    app = create_app(ServerConfig(persist=False), prefs_path=tmp_path / "prefs.json")
    assert app.state.history_store is None
    assert app.state.warm_store is None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/api/breakdown/summary?range=all")
    assert resp.status_code == HTTP_OK
    assert resp.json()["turns"] > 0
