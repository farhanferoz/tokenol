# Persistent History — PR 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-file DuckDB store of derived `Turn`/`Session` rows that backs the in-memory dashboard model, so JSONL deletion no longer drops history and cold start is bounded by the configurable hot window — without shipping any new CLI commands. (PR 2 ships `tokenol forget` and `tokenol recompute-costs` against the infrastructure landed here.)

**Architecture:** New `tokenol/persistence/` subpackage owns the DuckDB connection (`store.py`), an async batch flusher (`flusher.py`), and a pidfile + request-file handshake for live forget operations (`forget_handoff.py`). The broadcaster (`serve/streaming.py`) gains a flush queue and a per-tick forget probe. `serve/state.py`'s `_build_turns_and_sessions` is refactored so it can run on a delta of events and append to the in-memory hot tier. `serve/app.py` opens the store, hydrates the hot tier on startup, writes the pidfile, and force-flushes on shutdown. `serve/prefs.py` gains `hot_window_days` (default 90).

**Tech Stack:** DuckDB ≥0.10 (already a project dep), FastAPI, asyncio, pytest, pytest-asyncio.

**Spec source:** `docs/superpowers/specs/2026-05-02-persistent-history-design.md` (commit `ac23ba5`).

---

## Scope

**In scope for this PR:**

- `tokenol/persistence/store.py` — `HistoryStore` (schema, hydrate, flush, query, forget, migrate) + `ReadConnection` context manager.
- `tokenol/persistence/flusher.py` — `FlushQueue` + async drain task.
- `tokenol/persistence/forget_handoff.py` — pidfile + request file.
- `tokenol/ingest/discovery.py` — `select_edge_paths` helper.
- `tokenol/serve/state.py` — `derive_delta_turns` (incremental builder); `build_snapshot_full` integrated with store.
- `tokenol/serve/streaming.py` — broadcaster integrates flush queue and per-tick forget probe.
- `tokenol/serve/app.py` — store init, hot-tier hydration, pidfile, lifespan flush, warm-tier path for `range=all` on `/api/daily` and `/api/project/...`.
- `tokenol/serve/prefs.py` — `hot_window_days` field.
- `tokenol/model/events.py` — `archived: bool` field on `Session`.
- `tokenol/serve/static/session.html` + `session.js` — small badge when archived.
- Python tests for every component + an end-to-end test that verifies JSONL deletion preserves the snapshot.

**Out of scope (deferred to PR 2):**

- `tokenol forget` typer command.
- `tokenol recompute-costs` typer command.
- UI for editing `hot_window_days` (the field is exposed via `/api/prefs` but no widget is added in this PR).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/tokenol/persistence/__init__.py` | Create | Empty package marker. |
| `src/tokenol/persistence/store.py` | Create | `HistoryStore` (write conn + schema + flush/hydrate/query/forget/migrate) and `ReadConnection`. |
| `src/tokenol/persistence/flusher.py` | Create | `FlushQueue` + async drain loop with 30s/100-turn trigger. |
| `src/tokenol/persistence/forget_handoff.py` | Create | Pidfile management, request-file IO, `ForgetRequest` dataclass. |
| `src/tokenol/ingest/discovery.py` | Modify | Add `select_edge_paths(paths, last_ts_by_session) -> list[Path]`. |
| `src/tokenol/serve/state.py` | Modify | Add `derive_delta_turns`; refactor `build_snapshot_full` to use store-backed model and edge-only parsing. |
| `src/tokenol/serve/streaming.py` | Modify | Wire `FlushQueue` into broadcaster; per-tick forget-request probe; per-tick high-water mark refresh. |
| `src/tokenol/serve/app.py` | Modify | Open store, hydrate hot tier, attach to `app.state`, write pidfile, lifespan force-flush + close, warm-tier handlers for `range=all`. |
| `src/tokenol/serve/prefs.py` | Modify | Add `hot_window_days: int = 90` (validated 1..3650). |
| `src/tokenol/model/events.py` | Modify | Add `archived: bool = False` to `Session`. |
| `src/tokenol/serve/static/session.html` | Modify | Add archived-badge container in session header. |
| `src/tokenol/serve/static/session.js` | Modify | Render badge from `session.archived`. |
| `tests/test_persistence_store.py` | Create | `HistoryStore` schema/migrate/flush/hydrate/query/forget tests. |
| `tests/test_persistence_flusher.py` | Create | `FlushQueue` count/time triggers + force-flush. |
| `tests/test_persistence_forget_handoff.py` | Create | Pidfile staleness + atomic request file. |
| `tests/test_ingest_discovery.py` | Create | `select_edge_paths` unit tests. |
| `tests/test_serve_state.py` | Modify | Add `derive_delta_turns` tests + store-backed snapshot equivalence test. |
| `tests/test_serve_streaming.py` | Modify | Add broadcaster-with-flusher integration test + live-forget propagation test. |
| `tests/test_serve_app.py` | Modify | Add app-startup hydration test + warm-tier path equivalence test for `range=all`. |
| `tests/test_serve_prefs.py` | Modify | Add `hot_window_days` round-trip + bounds tests. |
| `tests/test_serve_archived_session.py` | Create | End-to-end: delete JSONL, snapshot identical except `archived=True`. |

`tests/` stays flat (matches existing convention).

---

## Pre-implementation

- [ ] **Step 0a: Verify the duckdb dep is installed**

```bash
cd /home/ff235/dev/claude_rate_limit
uv run python -c "import duckdb; print(duckdb.__version__)"
```

Expected: a version ≥0.10. If you get `ModuleNotFoundError`, run `uv sync` first.

- [ ] **Step 0b: Run the existing test suite to capture the baseline**

```bash
uv run pytest -q
```

Expected: green. Note any pre-existing skips. If anything is already red, stop and surface it before starting work.

---

## Task 1: `HistoryStore` foundation — schema, migrate, init/close

A single class that owns one DuckDB write connection plus the schema. Supports opening at a given path (creating the file + parent dir with mode 0700/0600 if missing) and applying schema migrations idempotently.

**Files:**
- Create: `src/tokenol/persistence/__init__.py`
- Create: `src/tokenol/persistence/store.py`
- Create: `tests/test_persistence_store.py`

- [ ] **Step 1: Create the empty package marker**

```bash
mkdir -p src/tokenol/persistence
```

Write `src/tokenol/persistence/__init__.py` with a single empty line.

- [ ] **Step 2: Write failing schema tests**

Create `tests/test_persistence_store.py`:

```python
"""Tests for tokenol.persistence.store.HistoryStore."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tokenol.persistence.store import HistoryStore


def test_open_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    store = HistoryStore(db_path)
    try:
        # File and parent dir created
        assert db_path.exists()
        # Tables present
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            tables = {row[0] for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()}
        finally:
            con.close()
        assert {"meta", "sessions", "turns"} <= tables
    finally:
        store.close()


def test_open_existing_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    HistoryStore(db_path).close()
    HistoryStore(db_path).close()  # must not raise


def test_schema_version_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "history.duckdb"
    store = HistoryStore(db_path)
    try:
        rows = store._con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchall()
        assert rows == [("1",)]
    finally:
        store.close()
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: ImportError on `tokenol.persistence.store`.

- [ ] **Step 4: Implement `HistoryStore` foundation**

Write `src/tokenol/persistence/store.py`:

```python
"""DuckDB-backed durable store for tokenol's derived analytics.

The store is single-process, single-writer. The broadcaster owns the write
connection; FastAPI handlers that need warm-tier reads acquire short-lived
read connections via :class:`ReadConnection`.

Schema is versioned via ``meta.schema_version``. ``HistoryStore.__init__``
applies any missing migrations idempotently, so opening an existing file
either upgrades or no-ops.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   VARCHAR PRIMARY KEY,
    source_file  VARCHAR,
    cwd          VARCHAR,
    is_sidechain BOOLEAN NOT NULL,
    first_ts     TIMESTAMP NOT NULL,
    last_ts      TIMESTAMP NOT NULL,
    turn_count   INTEGER NOT NULL,
    inserted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd     ON sessions(cwd);

CREATE TABLE IF NOT EXISTS turns (
    dedup_key             VARCHAR PRIMARY KEY,
    ts                    TIMESTAMP NOT NULL,
    session_id            VARCHAR NOT NULL,
    model                 VARCHAR,
    input_tokens          BIGINT NOT NULL,
    output_tokens         BIGINT NOT NULL,
    cache_read_tokens     BIGINT NOT NULL,
    cache_creation_tokens BIGINT NOT NULL,
    cost_usd              DOUBLE  NOT NULL,
    is_sidechain          BOOLEAN NOT NULL,
    is_interrupted        BOOLEAN NOT NULL,
    stop_reason           VARCHAR,
    tool_use_count        INTEGER NOT NULL,
    tool_error_count      INTEGER NOT NULL,
    tool_names            JSON,
    assumptions           JSON,
    inserted_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_turns_ts      ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
"""


def default_path() -> Path:
    """Resolve `TOKENOL_HISTORY_PATH` env var or fall back to ``~/.tokenol/history.duckdb``."""
    env = os.environ.get("TOKENOL_HISTORY_PATH")
    if env:
        return Path(env)
    return Path.home() / ".tokenol" / "history.duckdb"


class HistoryStore:
    """Owns a single DuckDB write connection and the schema."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._con = duckdb.connect(str(self.path))
        # Best-effort tighten file mode after open (DuckDB may have created it).
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            log.debug("could not chmod 0600 on %s", self.path)
        self._migrate()

    def _migrate(self) -> None:
        # Apply schema (DDL is idempotent via IF NOT EXISTS).
        self._con.execute(_SCHEMA_V1)
        # Record schema_version if not present.
        self._con.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO NOTHING",
            [str(SCHEMA_VERSION)],
        )

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            log.debug("error closing DuckDB connection", exc_info=True)


@contextmanager
def read_connection(path: Path | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Short-lived read-only connection. Use from FastAPI handlers via run_in_executor."""
    p = path if path is not None else default_path()
    con = duckdb.connect(str(p), read_only=True)
    try:
        yield con
    finally:
        con.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: all three tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/persistence/__init__.py src/tokenol/persistence/store.py tests/test_persistence_store.py
git commit -m "feat(persistence): HistoryStore foundation with DuckDB schema v1"
```

---

## Task 2: `HistoryStore.flush()` — batched turn insert + session UPSERT

One transaction per flush: idempotent INSERT for turns (skip on `dedup_key` collision), UPSERT for sessions (refresh `source_file`, `last_ts`, `turn_count`, `updated_at`).

**Files:**
- Modify: `src/tokenol/persistence/store.py`
- Modify: `tests/test_persistence_store.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_persistence_store.py`:

```python
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage


def _turn(
    key: str,
    sid: str,
    *,
    ts: datetime | None = None,
    model: str = "claude-sonnet-4-6",
    cost: float = 0.01,
) -> Turn:
    return Turn(
        dedup_key=key,
        timestamp=ts or datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        session_id=sid,
        model=model,
        usage=Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=20, cache_creation_input_tokens=10),
        is_sidechain=False,
        stop_reason="end_turn",
        cost_usd=cost,
        is_interrupted=False,
        tool_use_count=2,
        tool_error_count=0,
        tool_names=Counter({"Read": 1, "Bash": 1}),
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
    )


def _session(sid: str, *, source: str = "/tmp/x.jsonl", cwd: str = "/tmp/proj") -> Session:
    return Session(
        session_id=sid,
        source_file=source,
        is_sidechain=False,
        cwd=cwd,
        turns=[],
    )


def test_flush_inserts_turns_and_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        t1 = _turn("k1", "sess-1")
        t2 = _turn("k2", "sess-1", ts=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))
        store.flush(turns=[t1, t2], sessions=[s])

        rows = store._con.execute(
            "SELECT dedup_key, session_id, cost_usd FROM turns ORDER BY dedup_key"
        ).fetchall()
        assert rows == [("k1", "sess-1", 0.01), ("k2", "sess-1", 0.01)]

        srows = store._con.execute(
            "SELECT session_id, source_file, cwd, turn_count, first_ts, last_ts FROM sessions"
        ).fetchall()
        assert srows == [(
            "sess-1", "/tmp/x.jsonl", "/tmp/proj", 2,
            datetime(2026, 5, 1, 12, 0),
            datetime(2026, 5, 1, 13, 0),
        )]
    finally:
        store.close()


def test_flush_idempotent_on_dedup_key(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        store.flush([_turn("k1", "sess-1")], [s])
        # Re-flushing the same key is a no-op (no error, no duplication).
        store.flush([_turn("k1", "sess-1", cost=999.0)], [s])
        rows = store._con.execute("SELECT cost_usd FROM turns").fetchall()
        assert rows == [(0.01,)]  # original value retained, not overwritten
    finally:
        store.close()


def test_flush_upserts_session_metadata(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1", source="/old/path.jsonl", cwd="/tmp/old")
        store.flush([_turn("k1", "sess-1")], [s])

        s2 = _session("sess-1", source="/new/path.jsonl", cwd="/tmp/new")
        t2 = _turn("k2", "sess-1", ts=datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc))
        store.flush([t2], [s2])

        srows = store._con.execute(
            "SELECT source_file, cwd, turn_count, last_ts FROM sessions"
        ).fetchall()
        assert srows == [(
            "/new/path.jsonl", "/tmp/new", 2,
            datetime(2026, 5, 1, 14, 0),
        )]
    finally:
        store.close()


def test_flush_serializes_tool_names_and_assumptions_as_json(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        s = _session("sess-1")
        t = _turn("k1", "sess-1")
        store.flush([t], [s])
        row = store._con.execute(
            "SELECT tool_names, assumptions FROM turns WHERE dedup_key = 'k1'"
        ).fetchone()
        assert json.loads(row[0]) == {"Read": 1, "Bash": 1}
        assert json.loads(row[1]) == ["UNKNOWN_MODEL_FALLBACK"]
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: the four new tests fail with `AttributeError: HistoryStore has no attribute 'flush'`.

- [ ] **Step 3: Implement `flush`**

Append to `src/tokenol/persistence/store.py`:

```python
import json as _json
from collections.abc import Iterable

from tokenol.model.events import Session, Turn


def _turn_row(t: Turn) -> tuple:
    return (
        t.dedup_key,
        t.timestamp,
        t.session_id,
        t.model,
        int(t.usage.input_tokens),
        int(t.usage.output_tokens),
        int(t.usage.cache_read_input_tokens),
        int(t.usage.cache_creation_input_tokens),
        float(t.cost_usd),
        bool(t.is_sidechain),
        bool(t.is_interrupted),
        t.stop_reason,
        int(t.tool_use_count),
        int(t.tool_error_count),
        _json.dumps(dict(t.tool_names)),
        _json.dumps([a.value for a in t.assumptions]),
    )


def _session_aggregate(turns: Iterable[Turn]) -> dict[str, dict]:
    """Return {session_id: {first_ts, last_ts, count}} for the given turns."""
    agg: dict[str, dict] = {}
    for t in turns:
        a = agg.setdefault(t.session_id, {"first_ts": t.timestamp, "last_ts": t.timestamp, "count": 0})
        if t.timestamp < a["first_ts"]:
            a["first_ts"] = t.timestamp
        if t.timestamp > a["last_ts"]:
            a["last_ts"] = t.timestamp
        a["count"] += 1
    return agg


# ---- HistoryStore methods (add inside the class) -----------------------------

def flush(self, turns: list[Turn], sessions: list[Session]) -> None:
    """Insert *turns* (idempotent on dedup_key) and UPSERT *sessions* in one tx.

    The session metadata is refreshed from the new turns: first_ts/last_ts/turn_count
    are recomputed against the union of (existing rows for that session_id, new turns)
    so the denormalized totals stay accurate after each batch.
    """
    if not turns and not sessions:
        return

    new_agg = _session_aggregate(turns)
    sessions_by_id = {s.session_id: s for s in sessions}
    # Always touch every session whose turns we're inserting, even if the caller
    # didn't pass a Session object (defensive — keeps denormalized counts honest).
    for sid in new_agg:
        if sid not in sessions_by_id:
            sessions_by_id[sid] = Session(
                session_id=sid, source_file="", is_sidechain=False, cwd=None, turns=[]
            )

    self._con.begin()
    try:
        # Turns: bulk INSERT … ON CONFLICT DO NOTHING.
        if turns:
            rows = [_turn_row(t) for t in turns]
            self._con.executemany(
                """
                INSERT INTO turns (
                    dedup_key, ts, session_id, model,
                    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, is_sidechain, is_interrupted, stop_reason,
                    tool_use_count, tool_error_count, tool_names, assumptions
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (dedup_key) DO NOTHING
                """,
                rows,
            )

        # Sessions: refresh aggregates from the union of stored + new turns.
        for sid, s in sessions_by_id.items():
            existing = self._con.execute(
                "SELECT first_ts, last_ts, turn_count FROM sessions WHERE session_id = ?",
                [sid],
            ).fetchone()
            new_a = new_agg.get(sid)
            if existing is None and new_a is None:
                continue
            if existing is None:
                first_ts = new_a["first_ts"]
                last_ts = new_a["last_ts"]
                count = new_a["count"]
            elif new_a is None:
                first_ts, last_ts, count = existing
            else:
                first_ts = min(existing[0], new_a["first_ts"])
                last_ts = max(existing[1], new_a["last_ts"])
                # turn_count is *additive* — dedup_key conflicts mean some inserts
                # were no-ops, so re-derive from the actual row count.
                count = self._con.execute(
                    "SELECT COUNT(*) FROM turns WHERE session_id = ?", [sid]
                ).fetchone()[0]

            self._con.execute(
                """
                INSERT INTO sessions (
                    session_id, source_file, cwd, is_sidechain,
                    first_ts, last_ts, turn_count, updated_at
                ) VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
                ON CONFLICT (session_id) DO UPDATE SET
                    source_file = COALESCE(EXCLUDED.source_file, sessions.source_file),
                    cwd         = COALESCE(EXCLUDED.cwd,         sessions.cwd),
                    is_sidechain = EXCLUDED.is_sidechain,
                    first_ts    = LEAST(sessions.first_ts, EXCLUDED.first_ts),
                    last_ts     = GREATEST(sessions.last_ts, EXCLUDED.last_ts),
                    turn_count  = EXCLUDED.turn_count,
                    updated_at  = CURRENT_TIMESTAMP
                """,
                [sid, s.source_file or None, s.cwd, s.is_sidechain,
                 first_ts, last_ts, count],
            )

        self._con.commit()
    except Exception:
        self._con.rollback()
        raise
```

Add the `flush` method to the `HistoryStore` class (it's defined inside `class HistoryStore:`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: all seven tests PASS. If `test_flush_upserts_session_metadata` fails on `turn_count == 2`, double-check the `COUNT(*)` re-derivation block.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/store.py tests/test_persistence_store.py
git commit -m "feat(persistence): HistoryStore.flush — idempotent turns + session UPSERT"
```

---

## Task 3: `hydrate_hot()` and `last_ts_by_session()`

Reads back what `flush` wrote: hydrate the in-memory hot tier (last N days of turns + their sessions), and a separate dict of per-session high-water marks used by the edge-parse filter.

**Files:**
- Modify: `src/tokenol/persistence/store.py`
- Modify: `tests/test_persistence_store.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_persistence_store.py`:

```python
from datetime import timedelta


def test_hydrate_hot_returns_recent_turns_only(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        now = datetime.now(tz=timezone.utc)
        old = _turn("old", "sess-1", ts=now - timedelta(days=120))
        recent = _turn("recent", "sess-2", ts=now - timedelta(days=10))
        store.flush(
            [old, recent],
            [_session("sess-1"), _session("sess-2")],
        )

        turns, sessions = store.hydrate_hot(window_days=90)
        keys = {t.dedup_key for t in turns}
        assert keys == {"recent"}
        sids = {s.session_id for s in sessions}
        # Sessions tied to hot-window turns are returned. Sess-1 has no
        # hot-window turn so it is not hydrated into memory.
        assert sids == {"sess-2"}
    finally:
        store.close()


def test_hydrate_hot_reconstructs_turn_fields(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("k1", "sess-1")], [_session("sess-1")])
        turns, _ = store.hydrate_hot(window_days=365)
        assert len(turns) == 1
        t = turns[0]
        assert t.dedup_key == "k1"
        assert t.session_id == "sess-1"
        assert t.usage.input_tokens == 100
        assert t.usage.cache_read_input_tokens == 20
        assert dict(t.tool_names) == {"Read": 1, "Bash": 1}
        assert [a.value for a in t.assumptions] == ["UNKNOWN_MODEL_FALLBACK"]
    finally:
        store.close()


def test_last_ts_by_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "sess-1", ts=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)),
                _turn("b", "sess-1", ts=datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)),
                _turn("c", "sess-2", ts=datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)),
            ],
            [_session("sess-1"), _session("sess-2")],
        )
        marks = store.last_ts_by_session()
        # DuckDB returns naive datetimes from TIMESTAMP columns; we tag them UTC on read.
        assert marks["sess-1"] == datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
        assert marks["sess-2"] == datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_store.py::test_hydrate_hot_returns_recent_turns_only tests/test_persistence_store.py::test_hydrate_hot_reconstructs_turn_fields tests/test_persistence_store.py::test_last_ts_by_session -v
```

Expected: failures with `AttributeError`.

- [ ] **Step 3: Implement `hydrate_hot` and `last_ts_by_session`**

Add to `HistoryStore` (inside the class, after `flush`):

```python
from datetime import datetime, timedelta, timezone


def hydrate_hot(self, window_days: int) -> tuple[list[Turn], list[Session]]:
    """Load Turn rows whose ts is within `window_days` of now, plus their sessions.

    Returns ([], []) if the store is empty.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    turn_rows = self._con.execute(
        """
        SELECT dedup_key, ts, session_id, model,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
               cost_usd, is_sidechain, is_interrupted, stop_reason,
               tool_use_count, tool_error_count, tool_names, assumptions
        FROM turns
        WHERE ts >= ?
        ORDER BY ts
        """,
        [cutoff.replace(tzinfo=None)],  # DuckDB TIMESTAMP is tz-naive UTC
    ).fetchall()

    turns = [_row_to_turn(r) for r in turn_rows]

    if not turns:
        return [], []

    session_ids = {t.session_id for t in turns}
    placeholders = ",".join(["?"] * len(session_ids))
    session_rows = self._con.execute(
        f"SELECT session_id, source_file, cwd, is_sidechain "
        f"FROM sessions WHERE session_id IN ({placeholders})",
        list(session_ids),
    ).fetchall()

    turns_by_sid: dict[str, list[Turn]] = {}
    for t in turns:
        turns_by_sid.setdefault(t.session_id, []).append(t)

    sessions: list[Session] = []
    for sid, src, cwd, sidechain in session_rows:
        sessions.append(Session(
            session_id=sid,
            source_file=src or "",
            is_sidechain=bool(sidechain),
            cwd=cwd,
            turns=turns_by_sid.get(sid, []),
        ))

    return turns, sessions


def last_ts_by_session(self) -> dict[str, datetime]:
    """High-water marks per session_id (UTC datetimes)."""
    rows = self._con.execute(
        "SELECT session_id, last_ts FROM sessions"
    ).fetchall()
    return {sid: ts.replace(tzinfo=timezone.utc) for sid, ts in rows}
```

Also add a module-level `_row_to_turn` helper (place above the `HistoryStore` class):

```python
def _row_to_turn(r: tuple) -> Turn:
    """Reconstruct a Turn from a turns-table row (column order must match SELECT)."""
    from tokenol.enums import AssumptionTag

    (dedup_key, ts, sid, model, inp, out, cr, cc, cost, sidechain, interrupted,
     stop_reason, tu, te, tool_names_json, assumptions_json) = r
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    tool_names = Counter(_json.loads(tool_names_json) if tool_names_json else {})
    assumption_values = _json.loads(assumptions_json) if assumptions_json else []
    assumptions = [AssumptionTag(v) for v in assumption_values]
    return Turn(
        dedup_key=dedup_key,
        timestamp=ts,
        session_id=sid,
        model=model,
        usage=Usage(
            input_tokens=inp, output_tokens=out,
            cache_read_input_tokens=cr, cache_creation_input_tokens=cc,
        ),
        is_sidechain=bool(sidechain),
        stop_reason=stop_reason,
        cost_usd=float(cost),
        is_interrupted=bool(interrupted),
        tool_use_count=int(tu),
        tool_error_count=int(te),
        tool_names=tool_names,
        assumptions=assumptions,
    )
```

Add the missing imports to the top of `store.py`:

```python
from collections import Counter
from tokenol.model.events import Session, Turn, Usage
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/store.py tests/test_persistence_store.py
git commit -m "feat(persistence): hydrate_hot + last_ts_by_session"
```

---

## Task 4: `query_turns()` and `query_session()` — warm-tier reads

Used by handlers that ask for `range=all` when the requested window exceeds the in-memory hot tier. Returns hydrated `Turn` / `Session` objects so handler code stays uniform.

**Files:**
- Modify: `src/tokenol/persistence/store.py`
- Modify: `tests/test_persistence_store.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_persistence_store.py`:

```python
from datetime import date


def test_query_turns_filters_by_date_range(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "s1", ts=datetime(2026, 4, 1, tzinfo=timezone.utc)),
                _turn("b", "s1", ts=datetime(2026, 5, 1, tzinfo=timezone.utc)),
                _turn("c", "s1", ts=datetime(2026, 6, 1, tzinfo=timezone.utc)),
            ],
            [_session("s1")],
        )
        rows = store.query_turns(since=date(2026, 5, 1), until=date(2026, 5, 31))
        assert {t.dedup_key for t in rows} == {"b"}
    finally:
        store.close()


def test_query_turns_filters_by_project_and_model(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [
                _turn("a", "s1", model="claude-opus-4-7"),
                _turn("b", "s2", model="claude-sonnet-4-6"),
            ],
            [_session("s1", cwd="/proj/a"), _session("s2", cwd="/proj/b")],
        )
        rows = store.query_turns(project="/proj/a")
        assert {t.dedup_key for t in rows} == {"a"}
        rows = store.query_turns(model="claude-sonnet-4-6")
        assert {t.dedup_key for t in rows} == {"b"}
    finally:
        store.close()


def test_query_session_returns_full_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s1", ts=datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc))],
            [_session("s1", cwd="/proj/x")],
        )
        s = store.query_session("s1")
        assert s is not None
        assert s.session_id == "s1"
        assert s.cwd == "/proj/x"
        assert {t.dedup_key for t in s.turns} == {"a", "b"}

    finally:
        store.close()


def test_query_session_returns_none_for_missing(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        assert store.query_session("nope") is None
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_store.py::test_query_turns_filters_by_date_range tests/test_persistence_store.py::test_query_turns_filters_by_project_and_model tests/test_persistence_store.py::test_query_session_returns_full_session tests/test_persistence_store.py::test_query_session_returns_none_for_missing -v
```

Expected: failures with AttributeError.

- [ ] **Step 3: Implement `query_turns` and `query_session`**

Add to `HistoryStore`:

```python
from datetime import date as _date


def query_turns(
    self,
    since: _date | None = None,
    until: _date | None = None,
    project: str | None = None,
    model: str | None = None,
) -> list[Turn]:
    """Return matching turns from the warm tier, hydrated into Turn objects.

    `since`/`until` are inclusive bounds on the date portion of `ts`.
    `project` matches `sessions.cwd` exactly. `model` matches `turns.model` exactly.
    """
    where: list[str] = []
    params: list = []
    join_sessions = project is not None
    if since is not None:
        where.append("turns.ts >= ?")
        params.append(datetime.combine(since, datetime.min.time()))
    if until is not None:
        # End-inclusive: include the entire `until` day.
        where.append("turns.ts < ?")
        params.append(datetime.combine(until, datetime.min.time()) + timedelta(days=1))
    if model is not None:
        where.append("turns.model = ?")
        params.append(model)
    if project is not None:
        where.append("sessions.cwd = ?")
        params.append(project)

    join_clause = "JOIN sessions USING (session_id)" if join_sessions else ""
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT turns.dedup_key, turns.ts, turns.session_id, turns.model,
               turns.input_tokens, turns.output_tokens, turns.cache_read_tokens,
               turns.cache_creation_tokens, turns.cost_usd, turns.is_sidechain,
               turns.is_interrupted, turns.stop_reason, turns.tool_use_count,
               turns.tool_error_count, turns.tool_names, turns.assumptions
        FROM turns {join_clause} {where_clause}
        ORDER BY turns.ts
    """
    rows = self._con.execute(sql, params).fetchall()
    return [_row_to_turn(r) for r in rows]


def query_session(self, session_id: str) -> Session | None:
    """Return a Session with all its persisted turns, or None if unknown."""
    srow = self._con.execute(
        "SELECT session_id, source_file, cwd, is_sidechain "
        "FROM sessions WHERE session_id = ?",
        [session_id],
    ).fetchone()
    if srow is None:
        return None
    sid, src, cwd, sidechain = srow
    turns = self.query_turns()
    turns = [t for t in turns if t.session_id == sid]
    return Session(
        session_id=sid,
        source_file=src or "",
        is_sidechain=bool(sidechain),
        cwd=cwd,
        turns=turns,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/store.py tests/test_persistence_store.py
git commit -m "feat(persistence): query_turns + query_session warm-tier reads"
```

---

## Task 5: `HistoryStore.forget()` — supports all four kinds

Implements per-turn semantics for `--older-than` (re-derive `first_ts`/`turn_count` for surviving sessions in the same transaction). Returns `(sessions_dropped, turns_dropped)` for caller-side logging.

**Files:**
- Modify: `src/tokenol/persistence/store.py`
- Modify: `tests/test_persistence_store.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_persistence_store.py`:

```python
def test_forget_session_drops_turns_and_session(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s2")],
            [_session("s1"), _session("s2")],
        )
        dropped = store.forget(session_ids=["s1"])
        assert dropped == (1, 1)
        assert store._con.execute(
            "SELECT session_id FROM sessions ORDER BY session_id"
        ).fetchall() == [("s2",)]
        assert store._con.execute("SELECT dedup_key FROM turns").fetchall() == [("b",)]
    finally:
        store.close()


def test_forget_project_drops_all_matching(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush(
            [_turn("a", "s1"), _turn("b", "s2"), _turn("c", "s3")],
            [_session("s1", cwd="/proj/x"), _session("s2", cwd="/proj/x"), _session("s3", cwd="/proj/y")],
        )
        dropped = store.forget(cwd="/proj/x")
        assert dropped == (2, 2)
        assert store._con.execute("SELECT cwd FROM sessions").fetchall() == [("/proj/y",)]
    finally:
        store.close()


def test_forget_older_than_drops_old_turns_only(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recent = datetime(2026, 5, 1, tzinfo=timezone.utc)
        store.flush(
            [
                _turn("old1", "s1", ts=old),
                _turn("old2", "s2", ts=old),
                _turn("recent", "s1", ts=recent),
            ],
            [_session("s1"), _session("s2")],
        )
        cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
        dropped = store.forget(older_than=cutoff)
        assert dropped == (1, 2)  # s2 fully gone (1 session, 2 turns total dropped)

        # s1 retained with only its recent turn; first_ts/turn_count refreshed.
        srows = store._con.execute(
            "SELECT session_id, first_ts, turn_count FROM sessions"
        ).fetchall()
        assert srows == [("s1", datetime(2026, 5, 1), 1)]
        assert store._con.execute(
            "SELECT dedup_key FROM turns ORDER BY dedup_key"
        ).fetchall() == [("recent",)]
    finally:
        store.close()


def test_forget_all_wipes_store(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        store.flush([_turn("a", "s1")], [_session("s1")])
        dropped = store.forget(all=True)
        assert dropped == (1, 1)
        assert store._con.execute("SELECT COUNT(*) FROM turns").fetchone() == (0,)
        assert store._con.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        # Schema/meta retained.
        assert store._con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
    finally:
        store.close()


def test_forget_requires_exactly_one_kind(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        with pytest.raises(ValueError, match="exactly one"):
            store.forget()
        with pytest.raises(ValueError, match="exactly one"):
            store.forget(session_ids=["x"], cwd="/p")
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: failures with AttributeError on `forget`.

- [ ] **Step 3: Implement `forget`**

Add to `HistoryStore`:

```python
def forget(
    self,
    *,
    session_ids: list[str] | None = None,
    cwd: str | None = None,
    older_than: datetime | None = None,
    all: bool = False,
) -> tuple[int, int]:
    """Delete persisted history. Returns (sessions_dropped, turns_dropped).

    Exactly one of *session_ids*, *cwd*, *older_than*, *all* must be supplied.

    Per-turn semantics for *older_than*: turns with `ts < older_than` are deleted.
    Sessions with no remaining turns are also dropped. Surviving sessions have
    their denormalized `first_ts` and `turn_count` re-derived from remaining turns.
    """
    specified = sum(x is not None and x is not False for x in
                    [session_ids, cwd, older_than, all if all else None])
    if specified != 1:
        raise ValueError("forget requires exactly one of: session_ids, cwd, older_than, all")

    self._con.begin()
    try:
        if all:
            t_dropped = self._con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            s_dropped = self._con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            self._con.execute("DELETE FROM turns")
            self._con.execute("DELETE FROM sessions")
            self._con.commit()
            return s_dropped, t_dropped

        if session_ids:
            placeholders = ",".join(["?"] * len(session_ids))
            t_dropped = self._con.execute(
                f"SELECT COUNT(*) FROM turns WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchone()[0]
            s_dropped = self._con.execute(
                f"SELECT COUNT(*) FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchone()[0]
            self._con.execute(
                f"DELETE FROM turns WHERE session_id IN ({placeholders})",
                session_ids,
            )
            self._con.execute(
                f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            )
            self._con.commit()
            return s_dropped, t_dropped

        if cwd is not None:
            sids = [r[0] for r in self._con.execute(
                "SELECT session_id FROM sessions WHERE cwd = ?", [cwd]
            ).fetchall()]
            if not sids:
                self._con.commit()
                return 0, 0
            placeholders = ",".join(["?"] * len(sids))
            t_dropped = self._con.execute(
                f"SELECT COUNT(*) FROM turns WHERE session_id IN ({placeholders})",
                sids,
            ).fetchone()[0]
            self._con.execute(
                f"DELETE FROM turns WHERE session_id IN ({placeholders})", sids
            )
            self._con.execute(
                f"DELETE FROM sessions WHERE session_id IN ({placeholders})", sids
            )
            self._con.commit()
            return len(sids), t_dropped

        # older_than (per-turn semantics)
        cutoff_naive = older_than.replace(tzinfo=None)
        t_dropped = self._con.execute(
            "SELECT COUNT(*) FROM turns WHERE ts < ?", [cutoff_naive]
        ).fetchone()[0]
        affected_sids = [r[0] for r in self._con.execute(
            "SELECT DISTINCT session_id FROM turns WHERE ts < ?", [cutoff_naive]
        ).fetchall()]
        self._con.execute("DELETE FROM turns WHERE ts < ?", [cutoff_naive])

        s_dropped = 0
        for sid in affected_sids:
            agg = self._con.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM turns WHERE session_id = ?",
                [sid],
            ).fetchone()
            if agg[2] == 0:
                self._con.execute(
                    "DELETE FROM sessions WHERE session_id = ?", [sid]
                )
                s_dropped += 1
            else:
                self._con.execute(
                    "UPDATE sessions SET first_ts = ?, last_ts = ?, turn_count = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE session_id = ?",
                    [agg[0], agg[1], agg[2], sid],
                )
        self._con.commit()
        return s_dropped, t_dropped
    except Exception:
        self._con.rollback()
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_store.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/store.py tests/test_persistence_store.py
git commit -m "feat(persistence): HistoryStore.forget with per-turn older-than semantics"
```

---

## Task 6: `discovery.select_edge_paths`

Filter discovered JSONLs to those whose mtime is newer than the persisted high-water mark for their session_id (filename stem). Sessions with no recorded mark are kept (they're either new or never persisted yet). Returns the input unchanged when the marks dict is empty.

**Files:**
- Modify: `src/tokenol/ingest/discovery.py`
- Create: `tests/test_ingest_discovery.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_ingest_discovery.py`:

```python
"""Tests for tokenol.ingest.discovery.select_edge_paths."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from tokenol.ingest.discovery import select_edge_paths


def _touch(p: Path, *, seconds_ago: float) -> None:
    p.write_text("")
    ts = (datetime.now(tz=timezone.utc).timestamp()) - seconds_ago
    os.utime(p, (ts, ts))


def test_returns_all_paths_when_marks_empty(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"; _touch(a, seconds_ago=10)
    b = tmp_path / "b.jsonl"; _touch(b, seconds_ago=10)
    assert sorted(select_edge_paths([a, b], {})) == sorted([a, b])


def test_keeps_paths_with_no_persisted_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"; _touch(a, seconds_ago=10)
    new = tmp_path / "new.jsonl"; _touch(new, seconds_ago=10)
    marks = {"a": datetime.now(tz=timezone.utc)}
    # 'new' has no mark → kept; 'a' is older than mark → dropped.
    assert select_edge_paths([a, new], marks) == [new]


def test_keeps_paths_newer_than_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"; _touch(a, seconds_ago=10)
    marks = {"a": datetime.now(tz=timezone.utc).replace(microsecond=0) - __import__("datetime").timedelta(hours=1)}
    assert select_edge_paths([a], marks) == [a]


def test_drops_paths_older_than_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"; _touch(a, seconds_ago=3600)  # 1 hour old file
    marks = {"a": datetime.now(tz=timezone.utc)}            # mark from now
    assert select_edge_paths([a], marks) == []


def test_session_id_is_filename_stem(tmp_path: Path) -> None:
    p = tmp_path / "abc-123.jsonl"; _touch(p, seconds_ago=10)
    # Mark keyed by 'abc-123' (the stem) drops the file.
    marks = {"abc-123": datetime.now(tz=timezone.utc)}
    assert select_edge_paths([p], marks) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_ingest_discovery.py -v
```

Expected: failures with `ImportError`.

- [ ] **Step 3: Implement `select_edge_paths`**

Append to `src/tokenol/ingest/discovery.py`:

```python
from datetime import datetime, timezone


def select_edge_paths(
    paths: list[Path],
    last_ts_by_session: dict[str, datetime],
) -> list[Path]:
    """Return the subset of *paths* worth re-parsing this tick.

    A path is kept when:
    - The marks dict is empty (no warm tier — caller falls back to "all"), or
    - The file's session_id (filename stem) has no mark, or
    - The file's mtime is greater than the persisted mark.

    Paths whose stat() fails are dropped silently.
    """
    if not last_ts_by_session:
        return list(paths)

    kept: list[Path] = []
    for p in paths:
        sid = p.stem
        mark = last_ts_by_session.get(sid)
        if mark is None:
            kept.append(p)
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime > mark:
            kept.append(p)
    return kept
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_ingest_discovery.py -v
```

Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/ingest/discovery.py tests/test_ingest_discovery.py
git commit -m "feat(ingest): select_edge_paths filter by per-session high-water mark"
```

---

## Task 7: `derive_delta_turns` — incremental builder

Refactor the existing `_build_turns_and_sessions` so it can run on a *delta* of events without re-deriving from the full corpus. Output is the new turns + their session metadata, deduped against caller-supplied "already known" sets.

**Files:**
- Modify: `src/tokenol/serve/state.py`
- Modify: `tests/test_serve_state.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_state.py`:

```python
from tokenol.model.events import RawEvent, Usage
from tokenol.serve.state import derive_delta_turns


def _ev(
    *,
    sid: str,
    msg_id: str | None,
    req_id: str | None,
    ts: datetime,
    source: str = "/tmp/x.jsonl",
    line: int = 1,
    cwd: str = "/proj",
    model: str = "claude-sonnet-4-6",
) -> RawEvent:
    return RawEvent(
        source_file=source,
        line_number=line,
        event_type="assistant",
        session_id=sid,
        request_id=req_id,
        message_id=msg_id,
        uuid=f"u-{line}",
        timestamp=ts,
        usage=Usage(input_tokens=10, output_tokens=5),
        model=model,
        is_sidechain=False,
        stop_reason="end_turn",
        cwd=cwd,
    )


def test_derive_delta_turns_skips_known_dedup_keys() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev(sid="s", msg_id="m1", req_id="r1", ts=ts),
        _ev(sid="s", msg_id="m2", req_id="r2", ts=ts, line=2),
    ]
    turns, _, _ = derive_delta_turns(
        events,
        existing_dedup_keys={"m1:r1"},
        existing_passthrough_locations=set(),
    )
    assert {t.dedup_key for t in turns} == {"m2:r2"}


def test_derive_delta_turns_skips_known_passthroughs() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=4),
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=7),
    ]
    turns, _, _ = derive_delta_turns(
        events,
        existing_dedup_keys=set(),
        existing_passthrough_locations={("/x.jsonl", 4)},
    )
    # Line 4 known → skipped; line 7 emitted as a new passthrough turn.
    assert len(turns) == 1
    assert turns[0].dedup_key  # passthroughs use uuid or id() as fallback


def test_derive_delta_turns_emits_session_metadata_for_new_sids() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [_ev(sid="brand-new", msg_id="m1", req_id="r1", ts=ts, cwd="/proj/new")]
    _, sessions, _ = derive_delta_turns(
        events,
        existing_dedup_keys=set(),
        existing_passthrough_locations=set(),
    )
    assert len(sessions) == 1
    assert sessions[0].session_id == "brand-new"
    assert sessions[0].cwd == "/proj/new"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_serve_state.py -v -k derive_delta
```

Expected: failures with ImportError.

- [ ] **Step 3: Implement `derive_delta_turns`**

In `src/tokenol/serve/state.py`, add a new function (after `_build_turns_and_sessions`):

```python
def derive_delta_turns(
    new_events: list[RawEvent],
    existing_dedup_keys: set[str],
    existing_passthrough_locations: set[tuple[str, int]],
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag]]:
    """Build new Turn/Session deltas from events not already represented in memory.

    *existing_dedup_keys* is the set of dedup keys already in the in-memory hot tier.
    *existing_passthrough_locations* is the set of (source_file, line_number) tuples
    for passthrough turns already emitted (passthroughs lack dedup keys).

    Returns *only* the new turns and sessions to append; never touches existing state.
    Within-batch dedup follows the existing last-wins rule.
    """
    seen: dict[str, tuple[RawEvent, str]] = {}
    passthroughs: list[tuple[RawEvent, None]] = []
    cwd_by_session: dict[str, str] = {}
    session_source: dict[str, str] = {}
    fired: Counter[AssumptionTag] = Counter()

    for ev in new_events:
        if ev.cwd and ev.session_id not in cwd_by_session:
            cwd_by_session[ev.session_id] = ev.cwd
        if ev.session_id not in session_source:
            session_source[ev.session_id] = ev.source_file
        if ev.event_type != "assistant":
            continue
        if ev.model == "<synthetic>":
            continue
        k = dedup_key(ev)
        if k is None:
            loc = (ev.source_file, ev.line_number)
            if loc in existing_passthrough_locations:
                continue
            passthroughs.append((ev, None))
        else:
            if k in existing_dedup_keys:
                continue
            seen[k] = (ev, k)

    turns: list[Turn] = []
    for ev, k in itertools.chain(passthroughs, seen.values()):
        is_interrupted = ev.usage is None
        usage = ev.usage if ev.usage is not None else Usage()

        tags: list[AssumptionTag] = []
        if k is None:
            tags.append(AssumptionTag.DEDUP_PASSTHROUGH)
        if is_interrupted:
            tags.append(AssumptionTag.INTERRUPTED_TURN_SKIPPED)

        tc = cost_for_turn(ev.model, usage)
        tags.extend(t for t in tc.assumptions if t not in tags)
        for tag in tags:
            fired[tag] += 1

        key_str = k or ev.uuid or str(id(ev))
        turns.append(Turn(
            dedup_key=key_str,
            timestamp=ev.timestamp,
            session_id=ev.session_id,
            model=ev.model,
            usage=usage,
            is_sidechain=ev.is_sidechain,
            stop_reason=ev.stop_reason,
            assumptions=tags,
            cost_usd=tc.total_usd,
            is_interrupted=is_interrupted,
            tool_use_count=ev.tool_use_count,
            tool_error_count=ev.tool_error_count,
            tool_names=ev.tool_names,
        ))

    # Build *delta* Session records for any session_id we touched.
    sessions: list[Session] = []
    seen_sids: set[str] = set()
    for t in turns:
        if t.session_id in seen_sids:
            continue
        seen_sids.add(t.session_id)
        sessions.append(Session(
            session_id=t.session_id,
            source_file=session_source.get(t.session_id, ""),
            is_sidechain=t.is_sidechain,
            cwd=cwd_by_session.get(t.session_id),
            turns=[],  # caller appends to its own session's turn list
        ))

    return turns, sessions, fired
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_serve_state.py -v -k derive_delta
```

Expected: all three tests PASS. The pre-existing tests in this file should still pass — run the full file too:

```bash
uv run pytest tests/test_serve_state.py -v
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/state.py tests/test_serve_state.py
git commit -m "feat(state): derive_delta_turns — incremental Turn/Session derivation"
```

---

## Task 8: `build_snapshot_full` integrates with the store

Replace the always-full re-derivation with: hydrate hot tier on first call (cached on `app.state`), then per-tick, parse only edge JSONLs, derive deltas, append to in-memory hot tier, and queue for flush. Keeps the existing snapshot payload identical — the change is *how* turns get into memory.

**Files:**
- Modify: `src/tokenol/serve/state.py`
- Modify: `tests/test_serve_state.py`

- [ ] **Step 1: Write failing test (snapshot equivalence)**

Append to `tests/test_serve_state.py`:

```python
from tokenol.persistence.store import HistoryStore


def test_snapshot_equivalence_via_store(tmp_path: Path) -> None:
    """Snapshot built from JSONLs == snapshot built from store-only after deletion."""
    proj = tmp_path / "claude" / "projects" / "p1"
    proj.mkdir(parents=True)
    _write_session(proj, "sid-A", "/proj/a", "claude-sonnet-4-6", "2026-05-01T12:00:00Z", "1")
    _write_session(proj, "sid-B", "/proj/b", "claude-opus-4-7", "2026-05-01T13:00:00Z", "2")

    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        with _mock_dirs(tmp_path / "claude"):
            cache = ParseCache()
            r1 = build_snapshot_full(cache, history_store=store)
        # Now delete the JSONLs and rebuild — store-backed snapshot must match.
        for f in proj.glob("*.jsonl"):
            f.unlink()
        with _mock_dirs(tmp_path / "claude"):
            cache2 = ParseCache()
            r2 = build_snapshot_full(cache2, history_store=store)

        # Quantitative payload fields are identical.
        for k in ("topbar_summary", "tiles", "models", "recent_activity"):
            assert r1.payload[k] == r2.payload[k], f"divergence in {k}"
        # Sessions are preserved (just no live source file).
        assert {s.session_id for s in r1.sessions} == {s.session_id for s in r2.sessions}
    finally:
        store.close()
```

`_mock_dirs` already exists in `tests/test_serve_state.py` (it patches `get_config_dirs`). Verify it's still there before running.

- [ ] **Step 2: Run the new test to verify it fails**

```bash
uv run pytest tests/test_serve_state.py::test_snapshot_equivalence_via_store -v
```

Expected: failure — `build_snapshot_full` doesn't accept `history_store=` yet.

- [ ] **Step 3: Modify `build_snapshot_full` to accept and use the store**

In `src/tokenol/serve/state.py`, replace the `build_snapshot_full` function (currently lines 742-825). Only the **first ~25 lines** change — the period-turn derivation, panel builders, and payload assembly (lines ~778-825 of the current file) are kept verbatim and pasted into the new function after the `all_turns/all_sessions` assignment.

The new function:

```python
def build_snapshot_full(
    parse_cache: ParseCache,
    all_projects: bool = False,
    reference_usd: float = 50.0,
    tick_seconds: int = 5,
    period: str = "today",
    thresholds: dict | None = None,
    history_store: "HistoryStore | None" = None,
    flush_queue: "FlushQueue | None" = None,
) -> SnapshotResult:
    """Build the dashboard snapshot, store-backed when *history_store* is given.

    When a store is supplied:
    - On first call (parse_cache is empty), the in-memory model is seeded from
      `history_store.hydrate_hot(window_days=…)`.
    - Subsequent calls parse only edge JSONLs (mtime > persisted mark) and append
      derived deltas to the in-memory model. New turns are queued on *flush_queue*
      if present.

    When no store is supplied, falls back to today's full re-derivation behavior so
    every existing test that doesn't care about persistence keeps passing.
    """
    now = datetime.now(tz=timezone.utc)
    today_date = now.date()
    since_90d = today_date - timedelta(days=89)

    dirs = get_config_dirs(all_projects=all_projects)
    paths = find_jsonl_files(dirs)

    if history_store is not None:
        all_turns, all_sessions, _fired = _store_backed_derivation(
            parse_cache, paths, history_store, flush_queue
        )
    else:
        active_keys: set[tuple[str, int, int]] = set()
        for path in paths:
            try:
                key, _events = parse_cache.get_or_parse(path)
                active_keys.add(key)
            except OSError:
                pass
        parse_cache.purge(active_keys)
        all_turns, all_sessions, _fired = parse_cache.get_derived(
            frozenset(active_keys), _build_turns_and_sessions
        )

    # ↓↓↓ PASTE LINES 778-825 OF THE EXISTING build_snapshot_full HERE VERBATIM ↓↓↓
    # (turns_90d / daily_90d / cwd_by_sid / period_since / period_turns /
    #  today_turns / thresholds resolution / tile/topbar/anomaly/hourly/
    #  daily/models/recent_activity construction / payload dict / return)
    # ↑↑↑ END VERBATIM PASTE ↑↑↑
```

Add a new helper function above `build_snapshot_full`:

```python
def _store_backed_derivation(
    parse_cache: ParseCache,
    paths: list[Path],
    history_store: "HistoryStore",
    flush_queue: "FlushQueue | None",
) -> tuple[list[Turn], list[Session], Counter[AssumptionTag]]:
    """Hot-tier-aware variant of the corpus build.

    The first call hydrates the hot tier from DuckDB into `parse_cache._hot_*`.
    Each call parses only edge paths and appends derived deltas. The dedup-key
    and passthrough-location sets live on the parse_cache so they survive between
    ticks within a single process.
    """
    # Lazy initialization of the hot-tier caches on the parse_cache instance.
    if not getattr(parse_cache, "_hot_initialized", False):
        from tokenol.persistence.store import HistoryStore  # noqa: F401 (type hint resolution)
        # Window comes from prefs; default 90 if unset (caller passes via history_store).
        hot_turns, hot_sessions = history_store.hydrate_hot(
            window_days=getattr(history_store, "_hot_window_days", 90)
        )
        parse_cache._hot_turns = hot_turns
        parse_cache._hot_sessions_by_id = {s.session_id: s for s in hot_sessions}
        parse_cache._known_dedup_keys = {t.dedup_key for t in hot_turns}
        parse_cache._known_passthrough_locs = set()  # passthroughs aren't persisted; in-process only
        parse_cache._last_ts_by_session = history_store.last_ts_by_session()
        parse_cache._fired = Counter()
        parse_cache._hot_initialized = True

    edge_paths = select_edge_paths(paths, parse_cache._last_ts_by_session)
    new_events: list[RawEvent] = []
    for p in edge_paths:
        try:
            _key, evs = parse_cache.get_or_parse(p)
            new_events.extend(evs)
        except OSError:
            continue

    if new_events:
        delta_turns, delta_sessions, fired = derive_delta_turns(
            new_events,
            parse_cache._known_dedup_keys,
            parse_cache._known_passthrough_locs,
        )
        # Append to hot tier and update bookkeeping.
        for t in delta_turns:
            parse_cache._hot_turns.append(t)
            parse_cache._known_dedup_keys.add(t.dedup_key)
        for s in delta_sessions:
            parse_cache._hot_sessions_by_id.setdefault(s.session_id, s)
        # Track passthrough locations to avoid re-emission on subsequent ticks.
        for ev in new_events:
            if dedup_key(ev) is None and ev.event_type == "assistant":
                parse_cache._known_passthrough_locs.add((ev.source_file, ev.line_number))
        # Refresh per-session high-water marks.
        for t in delta_turns:
            cur = parse_cache._last_ts_by_session.get(t.session_id)
            if cur is None or t.timestamp > cur:
                parse_cache._last_ts_by_session[t.session_id] = t.timestamp
        # Update Session.turns lists in-place so the in-memory model is coherent.
        for t in delta_turns:
            sess = parse_cache._hot_sessions_by_id.get(t.session_id)
            if sess is not None:
                sess.turns.append(t)
        parse_cache._fired.update(fired)
        # Queue for flush if a queue is wired in.
        if flush_queue is not None:
            flush_queue.enqueue(
                delta_turns,
                [parse_cache._hot_sessions_by_id[s.session_id] for s in delta_sessions],
            )

    # Mark sessions whose JSONL is no longer on disk as archived. live_paths is
    # the set of session_ids backed by a current JSONL.
    live_sids = {p.stem for p in paths}
    for sid, sess in parse_cache._hot_sessions_by_id.items():
        sess.archived = sid not in live_sids

    return (
        list(parse_cache._hot_turns),
        list(parse_cache._hot_sessions_by_id.values()),
        Counter(parse_cache._fired),
    )
```

Add the missing imports at the top of `state.py`:

```python
from tokenol.ingest.discovery import select_edge_paths
```

(Inside `_store_backed_derivation`, the `FlushQueue` reference is forward-only — Task 10 creates the class. Use a string annotation `"FlushQueue | None"` and import lazily inside the function body if needed, but no runtime import is required because the queue is passed as an argument.)

- [ ] **Step 4: Run the equivalence test**

```bash
uv run pytest tests/test_serve_state.py::test_snapshot_equivalence_via_store -v
```

Expected: PASS.

- [ ] **Step 5: Run the full state test file to confirm no regression**

```bash
uv run pytest tests/test_serve_state.py -v
```

Expected: green. The non-store tests still go through the legacy path because `history_store=None`.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/state.py tests/test_serve_state.py
git commit -m "feat(state): store-backed snapshot path with edge-only parsing"
```

---

## Task 9: `forget_handoff` — pidfile + request file

A small module that lets `tokenol forget` (PR 2) coordinate with a live `tokenol serve` so deletes apply without a restart. The broadcaster (next task) will poll `take_forget_request()` once per tick.

**Files:**
- Create: `src/tokenol/persistence/forget_handoff.py`
- Create: `tests/test_persistence_forget_handoff.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_persistence_forget_handoff.py`:

```python
"""Tests for tokenol.persistence.forget_handoff."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tokenol.persistence.forget_handoff import (
    ForgetRequest,
    clear_pidfile,
    pidfile_path,
    read_live_pid,
    request_path,
    submit_forget_request,
    take_forget_request,
    write_pidfile,
)


def test_pidfile_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    write_pidfile()
    assert pidfile_path().exists()
    assert read_live_pid() == os.getpid()
    clear_pidfile()
    assert not pidfile_path().exists()


def test_read_live_pid_returns_none_when_pid_dead(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    # Write a PID that almost certainly isn't running.
    pidfile_path().write_text("9999999")
    assert read_live_pid() is None


def test_submit_and_take_forget_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    req = ForgetRequest(kind="session", value="sess-abc",
                        submitted_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    submit_forget_request(req)
    assert request_path().exists()

    taken = take_forget_request()
    assert taken == req
    assert not request_path().exists()  # consumed


def test_take_forget_request_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    assert take_forget_request() is None


def test_submit_is_atomic_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    submit_forget_request(ForgetRequest(kind="all", value=None,
                                         submitted_at=datetime.now(tz=timezone.utc)))
    # Verify no leftover .tmp file.
    leftovers = list(tmp_path.glob("pending-forget*.tmp"))
    assert leftovers == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_forget_handoff.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `forget_handoff`**

Create `src/tokenol/persistence/forget_handoff.py`:

```python
"""Pidfile + request-file handshake between `tokenol forget` and a live serve.

The CLI uses `read_live_pid()` to detect a running serve; if found it writes
the request via `submit_forget_request(...)` and exits. The serve broadcaster
calls `take_forget_request()` once per tick to consume any pending requests.
Both the pidfile and the request file live under `~/.tokenol/` (or the path
indicated by the `TOKENOL_HISTORY_DIR` env var, used by tests).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


def base_dir() -> Path:
    env = os.environ.get("TOKENOL_HISTORY_DIR")
    if env:
        return Path(env)
    return Path.home() / ".tokenol"


def pidfile_path() -> Path:
    return base_dir() / "serve.pid"


def request_path() -> Path:
    return base_dir() / "pending-forget.json"


@dataclass(frozen=True)
class ForgetRequest:
    kind: Literal["session", "project", "older_than", "all"]
    value: str | None  # session_id / cwd / ISO duration string / None for "all"
    submitted_at: datetime


# ---- pidfile -----------------------------------------------------------------

def write_pidfile() -> None:
    """Write current PID to the pidfile, creating the directory if needed."""
    p = pidfile_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(str(os.getpid()))


def clear_pidfile() -> None:
    with contextlib.suppress(FileNotFoundError):
        pidfile_path().unlink()


def read_live_pid() -> int | None:
    """Return PID from the pidfile if it points to a live process; else None.

    A stale pidfile (e.g. from a crashed serve) is treated as no-live-serve.
    """
    p = pidfile_path()
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


# ---- request file ------------------------------------------------------------

def submit_forget_request(req: ForgetRequest) -> None:
    """Atomically write the request via tempfile + rename."""
    p = request_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {**asdict(req), "submitted_at": req.submitted_at.isoformat()}
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="pending-forget", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        Path(tmp).replace(p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def take_forget_request() -> ForgetRequest | None:
    """Read + delete the pending-forget file; returns None if absent or unparsable."""
    p = request_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        req = ForgetRequest(
            kind=data["kind"],
            value=data.get("value"),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
        )
    except (OSError, ValueError, KeyError):
        # Malformed request — drop it so a misformed file doesn't wedge the loop.
        with contextlib.suppress(OSError):
            p.unlink()
        return None
    with contextlib.suppress(OSError):
        p.unlink()
    return req
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_forget_handoff.py -v
```

Expected: all five tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/forget_handoff.py tests/test_persistence_forget_handoff.py
git commit -m "feat(persistence): forget_handoff — pidfile + atomic request file"
```

---

## Task 10: `FlushQueue` and async drain task

The flush queue accumulates `(turns, sessions)` deltas from the broadcaster. A drain coroutine wakes on a 30s interval (or sooner via an asyncio.Event when ≥100 turns accumulate) and calls `HistoryStore.flush(...)` via `run_in_executor`. A `force_drain()` empties the queue synchronously for graceful shutdown.

**Files:**
- Create: `src/tokenol/persistence/flusher.py`
- Create: `tests/test_persistence_flusher.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_persistence_flusher.py`:

```python
"""Tests for tokenol.persistence.flusher.FlushQueue."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage
from tokenol.persistence.flusher import FlushQueue
from tokenol.persistence.store import HistoryStore


def _turn(key: str, sid: str) -> Turn:
    return Turn(
        dedup_key=key,
        timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        session_id=sid,
        model="claude-sonnet-4-6",
        usage=Usage(input_tokens=100, output_tokens=50,
                    cache_read_input_tokens=20, cache_creation_input_tokens=10),
        is_sidechain=False, stop_reason="end_turn", cost_usd=0.01, is_interrupted=False,
        tool_use_count=0, tool_error_count=0, tool_names=Counter(),
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
    )


def _session(sid: str) -> Session:
    return Session(
        session_id=sid, source_file="/tmp/x.jsonl",
        is_sidechain=False, cwd="/tmp/proj", turns=[],
    )


@pytest.mark.asyncio
async def test_drains_at_count_threshold(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=3, interval_seconds=60)
    try:
        await q.start()
        for i in range(3):
            q.enqueue([_turn(f"k{i}", "s")], [_session("s")])
        # The threshold trigger fires the drain immediately.
        await asyncio.wait_for(q.drained.wait(), timeout=2.0)
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (3,)
    finally:
        await q.stop()
        store.close()


@pytest.mark.asyncio
async def test_drains_at_time_interval(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=1000, interval_seconds=0.2)
    try:
        await q.start()
        q.enqueue([_turn("k1", "s")], [_session("s")])
        # Below threshold, but interval is short — drain should fire on the timer.
        await asyncio.sleep(0.5)
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (1,)
    finally:
        await q.stop()
        store.close()


@pytest.mark.asyncio
async def test_force_drain_on_shutdown(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.duckdb")
    q = FlushQueue(store, count_threshold=1000, interval_seconds=60)
    try:
        await q.start()
        q.enqueue([_turn("k1", "s")], [_session("s")])
        await q.stop()  # must drain before returning
        rows = store._con.execute("SELECT COUNT(*) FROM turns").fetchone()
        assert rows == (1,)
    finally:
        store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_persistence_flusher.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `FlushQueue`**

Create `src/tokenol/persistence/flusher.py`:

```python
"""Async batch flusher: drains pending Turn/Session deltas to HistoryStore.

Flush triggers:
- Count threshold: ≥100 queued turns → wake immediately.
- Time interval: every 30 seconds → wake regardless.

The drain runs `HistoryStore.flush(...)` in a background executor to keep the
asyncio event loop free. `stop()` cancels the loop and force-drains any pending
turns before returning so graceful shutdown loses nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tokenol.model.events import Session, Turn
    from tokenol.persistence.store import HistoryStore

log = logging.getLogger(__name__)

DEFAULT_COUNT_THRESHOLD = 100
DEFAULT_INTERVAL_SECONDS = 30.0


class FlushQueue:
    """Thread-safe enqueue side; asyncio drain side."""

    def __init__(
        self,
        store: "HistoryStore",
        count_threshold: int = DEFAULT_COUNT_THRESHOLD,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._count_threshold = count_threshold
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._pending_turns: list[Turn] = []
        self._pending_sessions: dict[str, Session] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.drained = asyncio.Event()  # set after each successful drain (test aid)

    def enqueue(self, turns: list["Turn"], sessions: list["Session"]) -> None:
        if not turns and not sessions:
            return
        with self._lock:
            self._pending_turns.extend(turns)
            for s in sessions:
                self._pending_sessions[s.session_id] = s
            count = len(self._pending_turns)
        if count >= self._count_threshold:
            try:
                self._wake.set()
            except RuntimeError:
                # Loop not running yet — drain will pick up on next start.
                pass

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="history-flusher")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Synchronous force-drain in case anything was added between the last
        # async drain and stop().
        await self._drain_once()

    async def _run(self) -> None:
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                if self._stopping:
                    break
                await self._drain_once()
        except asyncio.CancelledError:
            return

    async def _drain_once(self) -> None:
        with self._lock:
            turns = self._pending_turns
            sessions = list(self._pending_sessions.values())
            self._pending_turns = []
            self._pending_sessions = {}
        if not turns and not sessions:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._store.flush, turns, sessions)
        except Exception:
            log.exception("flush failed — re-queuing %d turns", len(turns))
            with self._lock:
                self._pending_turns[:0] = turns
                for s in sessions:
                    self._pending_sessions.setdefault(s.session_id, s)
            return
        self.drained.set()
```

The `drained` `asyncio.Event` is a test aid that the drain method sets after each successful flush. Tests can `await q.drained.wait()` to know the drain happened.

Add `pytest-asyncio` to the test command if it's not already installed: it's in the existing `dev` extras (pyproject.toml shows `pytest-asyncio>=0.24`).

The `tests/test_persistence_flusher.py` file uses `@pytest.mark.asyncio`. The repo's `pyproject.toml` already configures pytest-asyncio. Make sure the test file is picked up; if pytest complains about the marker, add `asyncio_mode = "auto"` or annotate with `@pytest.mark.asyncio` (already done).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_persistence_flusher.py -v
```

Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/persistence/flusher.py tests/test_persistence_flusher.py
git commit -m "feat(persistence): FlushQueue with count + time triggers"
```

---

## Task 11: Broadcaster integration — flusher + per-tick forget probe

Hook the `FlushQueue` into the broadcaster's lifespan and add a per-tick `take_forget_request()` probe that applies any pending forget to the store and evicts matching session_ids from the in-memory hot tier.

**Files:**
- Modify: `src/tokenol/serve/streaming.py`
- Modify: `tests/test_serve_streaming.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_serve_streaming.py`:

```python
import pytest
from collections import Counter
from datetime import datetime, timezone
from tokenol.enums import AssumptionTag
from tokenol.model.events import Session, Turn, Usage
from tokenol.persistence.flusher import FlushQueue
from tokenol.persistence.forget_handoff import ForgetRequest, submit_forget_request
from tokenol.persistence.store import HistoryStore
from tokenol.serve.state import ParseCache


def _local_turn(key: str, sid: str) -> Turn:
    return Turn(
        dedup_key=key,
        timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        session_id=sid,
        model="claude-sonnet-4-6",
        usage=Usage(input_tokens=100, output_tokens=50,
                    cache_read_input_tokens=20, cache_creation_input_tokens=10),
        is_sidechain=False, stop_reason="end_turn", cost_usd=0.01, is_interrupted=False,
        tool_use_count=0, tool_error_count=0, tool_names=Counter(),
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
    )


def _local_session(sid: str) -> Session:
    return Session(
        session_id=sid, source_file="/tmp/x.jsonl",
        is_sidechain=False, cwd="/tmp/proj", turns=[],
    )


@pytest.mark.asyncio
async def test_broadcaster_applies_pending_forget(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        # Pre-populate the store with a session and prime an in-memory hot tier.
        store.flush([_local_turn("k1", "sess-X")], [_local_session("sess-X")])

        parse_cache = ParseCache()
        # Force the hot tier to hydrate from the store.
        parse_cache._hot_initialized = False

        flush_queue = FlushQueue(store, count_threshold=1000, interval_seconds=60)
        await flush_queue.start()

        broadcaster = SnapshotBroadcaster(
            parse_cache=parse_cache,
            all_projects=False,
            get_reference_usd=lambda: 50.0,
            get_tick_seconds=lambda: 1,
            get_thresholds=lambda: {},
            history_store=store,
            flush_queue=flush_queue,
        )
        # Submit a forget request for the pre-populated session.
        submit_forget_request(ForgetRequest(
            kind="session", value="sess-X",
            submitted_at=datetime.now(tz=timezone.utc),
        ))

        # Trigger one tick (process_pending_forget is the broadcaster's per-tick hook).
        await broadcaster.process_pending_forget()

        # Store row gone.
        rows = store._con.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 'sess-X'"
        ).fetchone()
        assert rows == (0,)
        # In-memory hot tier evicted.
        assert "sess-X" not in parse_cache._hot_sessions_by_id

        await flush_queue.stop()
    finally:
        store.close()
```

The existing `tests/test_serve_streaming.py` should already import `SnapshotBroadcaster` — check imports at the top of the file. If not, add:

```python
from tokenol.serve.streaming import SnapshotBroadcaster
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_serve_streaming.py::test_broadcaster_applies_pending_forget -v
```

Expected: TypeError on `history_store=` / AttributeError on `process_pending_forget`.

- [ ] **Step 3: Modify `SnapshotBroadcaster`**

In `src/tokenol/serve/streaming.py`, change `SnapshotBroadcaster.__init__` to accept `history_store` and `flush_queue` (both optional, defaulting to `None` to keep existing tests working):

```python
def __init__(
    self,
    parse_cache: ParseCache,
    all_projects: bool,
    get_reference_usd: Callable[[], float],
    get_tick_seconds: Callable[[], int],
    get_thresholds: Callable[[], dict],
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    history_store: "HistoryStore | None" = None,
    flush_queue: "FlushQueue | None" = None,
) -> None:
    self._parse_cache = parse_cache
    self._all_projects = all_projects
    self._get_reference_usd = get_reference_usd
    self._get_tick_seconds = get_tick_seconds
    self._get_thresholds = get_thresholds
    self._heartbeat_s = heartbeat_s
    self._history_store = history_store
    self._flush_queue = flush_queue
    self._groups: dict[str, _Group] = {}
    self._lock = asyncio.Lock()
    self._latest_result: SnapshotResult | None = None
```

Update `_build_payload` to thread the store and flush queue through:

```python
def _build_payload(self, period: str) -> dict:
    result: SnapshotResult = build_snapshot_full(
        self._parse_cache,
        all_projects=self._all_projects,
        reference_usd=self._get_reference_usd(),
        tick_seconds=int(self._get_tick_seconds()),
        period=period,
        thresholds=self._get_thresholds(),
        history_store=self._history_store,
        flush_queue=self._flush_queue,
    )
    self._latest_result = result
    return result.payload
```

Add the per-tick forget hook as a public method:

```python
async def process_pending_forget(self) -> None:
    """Consume any pending forget request and apply it to the store + hot tier.

    Called once per broadcaster tick. Cheap when no request is pending (a single
    `os.stat` via `take_forget_request`).
    """
    from tokenol.persistence.forget_handoff import take_forget_request

    req = take_forget_request()
    if req is None or self._history_store is None:
        return

    loop = asyncio.get_running_loop()
    try:
        evicted_sids: list[str] = []
        if req.kind == "session" and req.value:
            evicted_sids = [req.value]
            await loop.run_in_executor(
                None, lambda: self._history_store.forget(session_ids=[req.value])
            )
        elif req.kind == "project" and req.value:
            # We need the session_ids for hot-tier eviction; query before deletion.
            evicted_sids = list(self._parse_cache._hot_sessions_by_id) if hasattr(
                self._parse_cache, "_hot_sessions_by_id"
            ) else []
            evicted_sids = [
                sid for sid in evicted_sids
                if (s := self._parse_cache._hot_sessions_by_id.get(sid)) and s.cwd == req.value
            ]
            await loop.run_in_executor(
                None, lambda: self._history_store.forget(cwd=req.value)
            )
        elif req.kind == "older_than" and req.value:
            from datetime import datetime, timezone
            cutoff = datetime.fromisoformat(req.value)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            await loop.run_in_executor(
                None, lambda: self._history_store.forget(older_than=cutoff)
            )
            # Hot-tier eviction: drop turns older than cutoff.
            if hasattr(self._parse_cache, "_hot_turns"):
                self._parse_cache._hot_turns = [
                    t for t in self._parse_cache._hot_turns if t.timestamp >= cutoff
                ]
        elif req.kind == "all":
            await loop.run_in_executor(
                None, lambda: self._history_store.forget(all=True)
            )
            if hasattr(self._parse_cache, "_hot_turns"):
                self._parse_cache._hot_turns = []
                self._parse_cache._hot_sessions_by_id = {}
                self._parse_cache._known_dedup_keys = set()
                self._parse_cache._known_passthrough_locs = set()
                self._parse_cache._last_ts_by_session = {}

        # Evict the hot tier rows for explicit-session deletions.
        if evicted_sids and hasattr(self._parse_cache, "_hot_sessions_by_id"):
            for sid in evicted_sids:
                self._parse_cache._hot_sessions_by_id.pop(sid, None)
                self._parse_cache._last_ts_by_session.pop(sid, None)
            self._parse_cache._hot_turns = [
                t for t in self._parse_cache._hot_turns if t.session_id not in set(evicted_sids)
            ]
            self._parse_cache._known_dedup_keys = {
                t.dedup_key for t in self._parse_cache._hot_turns
            }
    except Exception:
        log.exception("processing forget request failed")
```

Wire `process_pending_forget` into the producer loop. Find `_Group.run` (around line 82) and update its loop body so that *after* the snapshot builder runs (or after the idle skip), it calls `process_pending_forget` on the parent broadcaster. Easiest: add a `forget_hook: Callable[[], Awaitable[None]] | None = None` to `_Group.__init__`, pass `self.process_pending_forget` from `subscribe`, and call it once per loop iteration:

```python
# In _Group.__init__:
self._forget_hook = forget_hook  # add new param

# In _Group.run, near the top of the while loop:
if self._forget_hook is not None:
    try:
        await self._forget_hook()
    except Exception:
        log.exception("forget hook failed — continuing tick")
```

And in `SnapshotBroadcaster.subscribe`, pass it:

```python
grp = _Group(
    period,
    self._build_payload,
    self._compute_active_keys,
    self._get_tick_seconds,
    heartbeat_s=self._heartbeat_s,
    forget_hook=self.process_pending_forget,
)
```

- [ ] **Step 4: Run the new test**

```bash
uv run pytest tests/test_serve_streaming.py::test_broadcaster_applies_pending_forget -v
```

Expected: PASS.

- [ ] **Step 5: Run the full streaming test file**

```bash
uv run pytest tests/test_serve_streaming.py -v
```

Expected: green. Existing tests still pass because `history_store` and `flush_queue` default to `None`.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/streaming.py tests/test_serve_streaming.py
git commit -m "feat(broadcaster): integrate FlushQueue + per-tick forget probe"
```

---

## Task 12: `Preferences.hot_window_days`

Add the new validated field; expose via the existing `/api/prefs` endpoint with no UI changes.

**Files:**
- Modify: `src/tokenol/serve/prefs.py`
- Modify: `src/tokenol/serve/app.py` (validator block in `api_prefs_post`)
- Modify: `tests/test_serve_prefs.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_prefs.py`:

```python
def test_hot_window_days_default(tmp_path: Path) -> None:
    p = tmp_path / "prefs.json"
    prefs = Preferences.load(p)
    assert prefs.hot_window_days == 90


def test_hot_window_days_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "prefs.json"
    prefs = Preferences()
    prefs.hot_window_days = 30
    prefs.save(p)
    loaded = Preferences.load(p)
    assert loaded.hot_window_days == 30


def test_hot_window_days_to_dict(tmp_path: Path) -> None:
    prefs = Preferences()
    prefs.hot_window_days = 45
    assert prefs.to_dict()["hot_window_days"] == 45
```

(`Preferences` must already be imported at the top of the file — if not, add `from tokenol.serve.prefs import Preferences`.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_serve_prefs.py -v
```

Expected: failures with AttributeError.

- [ ] **Step 3: Add the field**

In `src/tokenol/serve/prefs.py`, modify the dataclass:

```python
@dataclass
class Preferences:
    tick_seconds: int = 5
    reference_usd: float = 50.0
    hot_window_days: int = 90
    thresholds: dict = field(default_factory=lambda: dict(DEFAULTS))
```

Update `load` to honor it:

```python
if "hot_window_days" in data:
    prefs.hot_window_days = int(data["hot_window_days"])
```

(Insert after the existing `if "reference_usd" in data:` block.)

Update `save` to include it in the payload:

```python
payload = {
    "tick_seconds": self.tick_seconds,
    "reference_usd": self.reference_usd,
    "hot_window_days": self.hot_window_days,
    "thresholds": self.thresholds,
}
```

- [ ] **Step 4: Add validator in `api_prefs_post`**

In `src/tokenol/serve/app.py`, locate `_KNOWN_PREFS_KEYS` and add the new key:

```python
_KNOWN_PREFS_KEYS: frozenset[str] = frozenset(
    {"tick_seconds", "reference_usd", "thresholds", "hot_window_days"}
)
```

In `api_prefs_post`, add a validator block alongside the existing `tick_seconds` / `reference_usd` blocks:

```python
if "hot_window_days" in body:
    v = body["hot_window_days"]
    if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 3650):
        raise HTTPException(
            status_code=400,
            detail="hot_window_days must be an integer between 1 and 3650 (takes effect on next startup)",
        )
    prefs.hot_window_days = v
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_serve_prefs.py -v
uv run pytest tests/test_serve_app.py -v -k prefs
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/prefs.py src/tokenol/serve/app.py tests/test_serve_prefs.py
git commit -m "feat(prefs): hot_window_days field (default 90) with validation"
```

---

## Task 13: `app.create_app` — wire store, hydrate, pidfile, lifespan flush

Open the `HistoryStore`, attach it (and the `FlushQueue`) to `app.state`, write the pidfile on startup, force-flush the queue and clear the pidfile on shutdown. Pass the store and flush queue through to the `SnapshotBroadcaster`.

**Files:**
- Modify: `src/tokenol/serve/app.py`
- Modify: `tests/test_serve_app.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_serve_app.py`:

```python
def test_create_app_attaches_store_and_writes_pidfile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("TOKENOL_HISTORY_PATH", str(tmp_path / "h.duckdb"))

    from tokenol.serve.app import ServerConfig, create_app

    app = create_app(ServerConfig())
    # Store attached
    assert app.state.history_store is not None
    # Flush queue attached (started by lifespan, but the object exists)
    assert app.state.flush_queue is not None


def test_lifespan_starts_and_stops_flusher(tmp_path, monkeypatch) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("TOKENOL_HISTORY_PATH", str(tmp_path / "h.duckdb"))

    from tokenol.serve.app import ServerConfig, create_app

    app = create_app(ServerConfig())

    async def go():
        async with app.router.lifespan_context(app):
            # Inside lifespan: pidfile written, flusher started.
            from tokenol.persistence.forget_handoff import pidfile_path
            assert pidfile_path().exists()
        # Outside: pidfile cleared.
        from tokenol.persistence.forget_handoff import pidfile_path
        assert not pidfile_path().exists()

    asyncio.run(go())
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_serve_app.py -v -k "store_and_writes_pidfile or lifespan_starts_and_stops"
```

Expected: AttributeError on `app.state.history_store`.

- [ ] **Step 3: Wire the store in `create_app`**

In `src/tokenol/serve/app.py`, modify `create_app`:

```python
from tokenol.persistence.flusher import FlushQueue
from tokenol.persistence.forget_handoff import clear_pidfile, write_pidfile
from tokenol.persistence.store import HistoryStore


def create_app(
    config: ServerConfig | None = None,
    prefs_path: Path | None = None,
) -> FastAPI:
    if config is None:
        config = ServerConfig()
    _prefs_path = prefs_path or default_path()
    prefs = Preferences.load(_prefs_path)

    parse_cache = ParseCache()
    history_store = HistoryStore()
    history_store._hot_window_days = prefs.hot_window_days  # consumed by store-backed derivation
    flush_queue = FlushQueue(history_store)

    broadcaster = SnapshotBroadcaster(
        parse_cache=parse_cache,
        all_projects=config.all_projects,
        get_reference_usd=lambda: prefs.reference_usd,
        get_tick_seconds=lambda: prefs.tick_seconds,
        get_thresholds=lambda: prefs.thresholds,
        history_store=history_store,
        flush_queue=flush_queue,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        write_pidfile()
        await flush_queue.start()
        try:
            yield
        finally:
            await broadcaster.shutdown()
            await flush_queue.stop()
            history_store.close()
            clear_pidfile()

    app = FastAPI(title="tokenol", lifespan=lifespan)
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = parse_cache
    app.state.snapshot_result = None
    app.state.broadcaster = broadcaster
    app.state.history_store = history_store
    app.state.flush_queue = flush_queue

    # ... rest of create_app body unchanged (route registrations) ...
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_serve_app.py -v
```

Expected: green. The pre-existing `test_serve_app.py` tests should continue to work because the new lifespan additions (pidfile, flusher) only kick in when `lifespan_context` is entered — the unit tests that just call `create_app` won't notice.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(serve): wire HistoryStore + FlushQueue + pidfile into app lifespan"
```

---

## Task 14: `Session.archived` field + warm-tier path for `range=all`

Add the `archived` field on `Session`. Make `/api/daily?range=all` and `/api/project/{cwd}?range=all` delegate to `HistoryStore.query_turns(...)` when the requested window exceeds the hot tier, and feed the result through the same panel builders.

**Files:**
- Modify: `src/tokenol/model/events.py`
- Modify: `src/tokenol/serve/app.py`
- Modify: `tests/test_serve_app.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_app.py`:

```python
def test_session_dataclass_has_archived_field() -> None:
    from tokenol.model.events import Session
    s = Session(session_id="x", source_file="", is_sidechain=False)
    assert s.archived is False


def test_daily_range_all_uses_warm_tier(tmp_path, monkeypatch) -> None:
    """range=all surfaces rows from the warm tier when older than the hot window."""
    from collections import Counter
    from datetime import datetime, timezone
    from tokenol.enums import AssumptionTag
    from tokenol.model.events import Session, Turn, Usage

    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("TOKENOL_HISTORY_PATH", str(tmp_path / "h.duckdb"))
    from tokenol.serve.app import ServerConfig, create_app
    from fastapi.testclient import TestClient

    app = create_app(ServerConfig())
    store = app.state.history_store
    # Insert one turn well outside any reasonable hot window.
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.flush(
        [Turn(
            dedup_key="old", timestamp=old_ts, session_id="s1",
            model="claude-sonnet-4-6",
            usage=Usage(input_tokens=10, output_tokens=5,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
            is_sidechain=False, stop_reason="end_turn", cost_usd=0.001,
            is_interrupted=False, tool_use_count=0, tool_error_count=0,
            tool_names=Counter(), assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
        )],
        [Session(session_id="s1", source_file="", is_sidechain=False, cwd="/proj/old")],
    )

    with TestClient(app) as client:
        resp = client.get("/api/daily?range=all")
    assert resp.status_code == 200
    payload = resp.json()
    # earliest_available is a stable top-level field that reflects the oldest turn
    # the handler considered, so it's the cleanest assertion regardless of series shape.
    assert payload["earliest_available"] <= "2026-01-01"
```

(The exact shape of the daily payload is established in `state.build_daily_panel`; this test asserts on `earliest_available` because that field is stable.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_serve_app.py -v -k "archived_field or range_all_uses_warm_tier"
```

Expected: failures.

- [ ] **Step 3: Add `archived` field on `Session`**

In `src/tokenol/model/events.py`, modify the `Session` dataclass:

```python
@dataclass
class Session:
    """All turns from one JSONL file (one sessionId)."""

    session_id: str
    source_file: str
    is_sidechain: bool
    cwd: str | None = None
    turns: list[Turn] = field(default_factory=list)
    archived: bool = False
    # ... existing properties unchanged ...
```

- [ ] **Step 4: Add warm-tier path for `/api/daily?range=all`**

In `src/tokenol/serve/app.py`'s `api_daily` handler, before the `panel = build_daily_panel(...)` call, insert:

```python
# Warm-tier query when the requested range exceeds the in-memory hot tier.
if range == "all" and request.app.state.history_store is not None:
    prefs: Preferences = request.app.state.prefs
    earliest_in_hot = (
        min((t.timestamp.date() for t in result.turns), default=date.today())
        if result.turns
        else date.today()
    )
    hot_cutoff = date.today() - timedelta(days=prefs.hot_window_days)
    # If the warm tier might hold turns older than the hot window, refresh result.turns
    # with the union of warm + hot.
    loop = asyncio.get_running_loop()
    warm_turns = await loop.run_in_executor(
        None,
        lambda: request.app.state.history_store.query_turns(until=hot_cutoff),
    )
    if warm_turns:
        existing_keys = {t.dedup_key for t in result.turns}
        merged = list(result.turns) + [t for t in warm_turns if t.dedup_key not in existing_keys]
        merged.sort(key=lambda t: t.timestamp)
        # Replace result.turns with the merged superset (handler-local; doesn't mutate hot tier).
        from dataclasses import replace
        result = replace(result, turns=merged)
```

(Add `from datetime import timedelta` at the top of `app.py` if not already imported. The existing import already includes `from datetime import date, datetime, timezone`.)

- [ ] **Step 5: Apply the same warm-tier path to `/api/project/{cwd_b64}?range=all`**

In `api_project_detail`, near the top after `cwd = decode_cwd(cwd_b64)`:

```python
if range == "all" and request.app.state.history_store is not None:
    loop = asyncio.get_running_loop()
    warm_turns = await loop.run_in_executor(
        None,
        lambda: request.app.state.history_store.query_turns(project=cwd),
    )
    if warm_turns:
        existing_keys = {t.dedup_key for t in result.turns}
        merged_turns = list(result.turns) + [t for t in warm_turns if t.dedup_key not in existing_keys]
        merged_turns.sort(key=lambda t: t.timestamp)
        # Build a superset of sessions: existing + warm-tier sessions for this cwd.
        warm_sids = {t.session_id for t in warm_turns}
        existing_sids = {s.session_id for s in result.sessions}
        missing_sids = warm_sids - existing_sids
        warm_sessions: list = []
        for sid in missing_sids:
            s = await loop.run_in_executor(
                None, lambda: request.app.state.history_store.query_session(sid)
            )
            if s is not None:
                s.archived = True
                warm_sessions.append(s)
        from dataclasses import replace
        result = replace(result, turns=merged_turns, sessions=list(result.sessions) + warm_sessions)
```

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/test_serve_app.py -v
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/tokenol/model/events.py src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(serve): Session.archived field + warm-tier path for range=all"
```

---

## Task 15: Frontend — archived badge in session detail

When `Session.archived` is true, the session detail UI shows a small badge and the per-turn modal omits the text-snippet block. Backend exposure: include `archived` in `build_session_detail`'s payload (already serializable since the field is on the dataclass, but the existing builder doesn't include it). Frontend reads the flag and conditionally renders.

**Files:**
- Modify: `src/tokenol/serve/session_detail.py`
- Modify: `src/tokenol/serve/static/session.html`
- Modify: `src/tokenol/serve/static/session.js`
- Modify: `tests/test_session_detail.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_session_detail.py`:

```python
def test_build_session_detail_includes_archived_flag() -> None:
    from tokenol.serve.session_detail import build_session_detail
    from tokenol.model.events import Session

    s = Session(session_id="sx", source_file="/dev/null", is_sidechain=False, archived=True)
    payload = build_session_detail(s)
    assert payload["archived"] is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_session_detail.py::test_build_session_detail_includes_archived_flag -v
```

Expected: KeyError on "archived".

- [ ] **Step 3: Add `archived` to the session detail payload**

In `src/tokenol/serve/session_detail.py`'s `build_session_detail` function, add the field to the returned dict:

```python
return {
    "session_id": session.session_id,
    "source_file": session.source_file,
    "model": sr.model,
    "cwd": session.cwd or "",
    "verdict": verdict.value,
    "first_ts": first_ts,
    "last_ts": last_ts,
    "archived": session.archived,
    # ... rest unchanged ...
}
```

Also add it to `build_turn_detail`'s return dict so the modal can hide the snippet block:

```python
return {
    "session_id": session.session_id,
    "turn_idx": turn_idx,
    "archived": session.archived,
    # ... rest unchanged ...
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_session_detail.py -v
```

Expected: green.

- [ ] **Step 5: Add badge to `session.html`**

In `src/tokenol/serve/static/session.html`, find the session header element (likely an `<h1>` or `<header>` near the top of the page body — search for `session-id` or similar). Add an empty `<span id="archived-badge" hidden></span>` next to it.

For the per-turn modal (search for `turn-modal` or the snippet block element ID), wrap the snippet section in a container with an ID like `turn-snippet-block` so the JS can hide it.

If you cannot identify the existing markup, the precise edit is:

```html
<!-- Inside the session header section -->
<span id="session-archived-badge" hidden
      style="margin-left: 8px; padding: 2px 8px; border-radius: 4px;
             background: var(--color-amber-50); color: var(--color-amber-900);
             font-size: 12px;">
  Archived — text snippets unavailable
</span>
```

- [ ] **Step 6: Wire it in `session.js`**

Find the session-detail render function (search for `archived` — it doesn't yet exist, or for `session_id` to find the render entrypoint). Add:

```javascript
// After the session payload is fetched and rendered:
const badge = document.getElementById('session-archived-badge');
if (badge) badge.hidden = !payload.archived;
```

In the per-turn modal render path (search for `assistant_preview` or `user_prompt`), wrap the snippet section render with a check:

```javascript
const snippetBlock = document.getElementById('turn-snippet-block');
if (snippetBlock) {
  snippetBlock.hidden = !!turnPayload.archived;
}
```

If `turn-snippet-block` doesn't exist as an ID, you may need to add it around the existing snippet markup.

- [ ] **Step 7: Visual smoke check**

Start the server and load a known live session vs. a session whose JSONL has been deleted (use `mv ~/.claude/projects/<proj>/<sid>.jsonl /tmp/` to simulate). Verify the badge appears for the deleted-JSONL session and the snippet block is hidden in its turn modal.

```bash
uv run tokenol serve --port 8787
```

Then in a browser, navigate to the dashboard, click into a session for both cases. Confirm the badge appears for the archived one only.

(Restore the moved JSONL after testing.)

- [ ] **Step 8: Commit**

```bash
git add src/tokenol/serve/session_detail.py src/tokenol/serve/static/session.html src/tokenol/serve/static/session.js tests/test_session_detail.py
git commit -m "feat(ui): archived badge on session detail; hide snippet block for archived turns"
```

---

## Task 16: End-to-end — JSONL deletion preserves the snapshot

Black-box integration test: spin up `create_app`, force the broadcaster to build a snapshot, delete a JSONL, build again, and verify the snapshot is identical in every quantitative payload field plus the affected session is `archived=True`.

**Files:**
- Create: `tests/test_serve_archived_session.py`

- [ ] **Step 1: Write the test**

Create `tests/test_serve_archived_session.py`:

```python
"""End-to-end: JSONL deletion preserves dashboard data via the persistent store."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tokenol.ingest.discovery as _disc_mod
import tokenol.serve.state as _state_mod
from tokenol.serve.state import ParseCache, build_snapshot_full
from tokenol.persistence.store import HistoryStore


def _write_session(proj_dir: Path, sid: str, cwd: str, model: str, ts_iso: str, uid: str) -> None:
    sys_ev = json.dumps({
        "type": "system", "timestamp": ts_iso, "sessionId": sid,
        "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
    })
    asst_ev = json.dumps({
        "type": "assistant", "timestamp": ts_iso, "sessionId": sid,
        "requestId": f"req-{uid}", "uuid": f"evt-{uid}", "isSidechain": False,
        "model": model,
        "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
    })
    (proj_dir / f"{sid}.jsonl").write_text(sys_ev + "\n" + asst_ev + "\n")


@contextmanager
def _mock_dirs(claude_root: Path):
    original_gcd = _state_mod.get_config_dirs
    original_disc = _disc_mod.get_config_dirs
    _state_mod.get_config_dirs = lambda all_projects=False: [claude_root]
    _disc_mod.get_config_dirs = lambda all_projects=False: [claude_root]
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original_gcd
        _disc_mod.get_config_dirs = original_disc


def test_jsonl_deletion_preserves_snapshot(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude"
    proj = claude_root / "projects" / "p1"
    proj.mkdir(parents=True)
    _write_session(proj, "sid-A", "/proj/a", "claude-sonnet-4-6", "2026-05-01T12:00:00Z", "1")
    _write_session(proj, "sid-B", "/proj/b", "claude-opus-4-7", "2026-05-01T13:00:00Z", "2")

    store = HistoryStore(tmp_path / "h.duckdb")
    store._hot_window_days = 365  # capture both turns in hot tier

    try:
        with _mock_dirs(claude_root):
            cache = ParseCache()
            r1 = build_snapshot_full(cache, history_store=store)
            # Force a flush of any queued turns by re-creating store interaction.
            store.flush(turns=cache._hot_turns, sessions=list(cache._hot_sessions_by_id.values()))

        # Delete one JSONL.
        (proj / "sid-A.jsonl").unlink()

        with _mock_dirs(claude_root):
            cache2 = ParseCache()
            r2 = build_snapshot_full(cache2, history_store=store)

        # All quantitative fields must match.
        for k in ("topbar_summary", "tiles", "models", "recent_activity"):
            assert r1.payload[k] == r2.payload[k], f"divergence in {k}"

        # sid-A is archived; sid-B is not.
        sids = {s.session_id: s for s in r2.sessions}
        assert sids["sid-A"].archived is True
        assert sids["sid-B"].archived is False
    finally:
        store.close()
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/test_serve_archived_session.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the entire suite**

```bash
uv run pytest -q
```

Expected: green. Note any flaky tests.

- [ ] **Step 4: Run ruff**

```bash
uv run ruff check src tests
```

Expected: no errors. Per the project's release-gate convention, ruff must be green before push.

- [ ] **Step 5: Commit**

```bash
git add tests/test_serve_archived_session.py
git commit -m "test(serve): end-to-end — JSONL deletion preserves snapshot via store"
```

---

## Final verification

- [ ] **Step 1: Cold start with a populated store works**

Manually verify that re-running `tokenol serve` after some live use produces a usable dashboard within a second or two:

```bash
# First run — let it ingest some live JSONLs and write to the store.
uv run tokenol serve --port 8787 --tick 5s
# (browse to http://127.0.0.1:8787, let it run ~30s, then Ctrl-C)

# Second run — should hydrate from the store immediately.
uv run tokenol serve --port 8787 --tick 5s
```

Confirm the dashboard renders historical data without re-parsing every JSONL.

- [ ] **Step 2: JSONL deletion smoke test**

```bash
# With serve running:
mv ~/.claude/projects/<some-proj>/<some-sid>.jsonl /tmp/
# Wait one tick (~5s). Check the dashboard — that session's data is still there.
# Restore:
mv /tmp/<some-sid>.jsonl ~/.claude/projects/<some-proj>/
```

- [ ] **Step 3: Final ruff + pytest gate**

```bash
uv run ruff check src tests
uv run pytest -q
```

Both must be green.

- [ ] **Step 4: Inspect the store size**

```bash
ls -lh ~/.tokenol/history.duckdb
```

Sanity check: well under 50 MB for typical use after the first session of running the new build.

---

## Self-review notes

- **Spec coverage:** Tasks 1-5 cover `HistoryStore` (schema, flush, hydrate, query, forget). Task 6 covers `select_edge_paths`. Tasks 7-8 cover the incremental builder and store-backed `build_snapshot_full`. Task 9 covers `forget_handoff`. Task 10 covers `FlushQueue`. Task 11 covers broadcaster integration including the per-tick forget probe. Task 12 covers `Preferences.hot_window_days`. Task 13 covers `create_app` wiring (store, hot-tier hydration, pidfile, lifespan force-flush). Task 14 covers `Session.archived` and the warm-tier path for `range=all`. Task 15 covers the UI badge. Task 16 is the end-to-end equivalence test from the spec's testing section.
- **Skipped from spec for PR 1:** `tokenol forget` and `tokenol recompute-costs` CLIs (PR 2 scope).
- **Open follow-ups for PR 2:** wire the CLI commands; add `tests/test_cli_forget.py` and `tests/test_cli_recompute_costs.py`.
- **Type consistency:** `HistoryStore.flush(turns, sessions)`, `hydrate_hot(window_days)`, `last_ts_by_session()`, `query_turns(since, until, project, model)`, `query_session(session_id)`, `forget(*, session_ids|cwd|older_than|all)` — referenced consistently across tasks.
