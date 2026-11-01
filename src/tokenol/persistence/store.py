"""DuckDB-backed durable store for tokenol's derived analytics.

The store is single-process, single-writer. The broadcaster owns the write
connection; FastAPI handlers that need warm-tier reads acquire short-lived
read connections via :func:`read_connection`.

Schema is versioned via ``meta.schema_version``. ``HistoryStore.__init__``
applies any missing migrations idempotently, so opening an existing file
either upgrades or no-ops.
"""

from __future__ import annotations

import json as _json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from tokenol.model.events import (
    Session,
    Turn,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# Per-chunk row count for the flush — see :meth:`HistoryStore.flush`.
FLUSH_CHUNK_SIZE = 1000

# DuckDB reports a lock conflict as an IOException whose text carries the
# holding PID, e.g.:
#   Could not set lock on file "...": Conflicting lock is held in
#   /usr/bin/python3.13 (PID 3170792) by user ff235.
_CONFLICT_PID_RE = re.compile(r"\(PID (\d+)\)")


class StoreLockedError(RuntimeError):
    """Another process holds the store's lock.

    DuckDB's lock is exclusive and cross-process, so this is exactly the case
    of a second tokenol being started while one is already running. It is a
    clean refusal, not a data hazard: the second process dies at connect
    having written nothing, and `turns.dedup_key` makes re-flushing the same
    turns a no-op anyway. Raised so callers can print a sentence instead of a
    DuckDB traceback.
    """

    def __init__(self, path: Path, pid: int | None) -> None:
        self.path = path
        self.pid = pid
        who = f"another process (PID {pid})" if pid is not None else "another process"
        super().__init__(
            f"tokenol is already running: {who} holds {path}. "
            f"Stop it first, or start this one without --persist."
        )

def raise_if_lock_conflict(exc: duckdb.IOException, path: Path) -> None:
    """Raise StoreLockedError if *exc* is DuckDB's file-lock conflict; otherwise return.

    Only a lock conflict is translated; a genuine IO failure (full disk, bad
    permissions) must surface unchanged, so the caller re-raises after this returns.
    """
    text = str(exc)
    if "lock" not in text.lower():
        return
    m = _CONFLICT_PID_RE.search(text)
    raise StoreLockedError(path, int(m.group(1)) if m else None) from exc


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

# v2: add per-tool cost attribution columns. ``ALTER … IF NOT EXISTS`` makes
# this idempotent so upgrading from a v1 file is a no-op on the second run.
# Existing rows hydrate with empty tool_costs and zero unattributed floats —
# warm-tier breakdowns for those rows under-report attribution until they age
# out of the window. New flushes write the full per-tool slice.
_MIGRATION_V2 = """
ALTER TABLE turns ADD COLUMN IF NOT EXISTS tool_costs JSON;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS unattributed_input_tokens DOUBLE DEFAULT 0.0;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS unattributed_output_tokens DOUBLE DEFAULT 0.0;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS unattributed_cost_usd DOUBLE DEFAULT 0.0;
"""

# v3: add the Skill cost-dimension columns. ``ALTER … IF NOT EXISTS`` keeps this
# idempotent. Rows written before v3 hydrate with attribution_skill NULL and an
# empty skill_names map — those turns simply don't contribute to the Skill
# dimension until they age out of the window; new flushes write both fields.
_MIGRATION_V3 = """
ALTER TABLE turns ADD COLUMN IF NOT EXISTS attribution_skill VARCHAR;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS skill_names JSON;
"""

# v4: add the 1-hour cache-write tier split, so pricing (2x input, vs 1.25x
# for the 5-minute tier) can be recomputed correctly for warm-tier rows on a
# future flush. Rows written before v4 hydrate with 0 — i.e. entirely
# 5-minute tier — which understates cost for any turn that actually used
# 1-hour caching; those rows' cost_usd was already computed (and persisted)
# under the old formula and does not self-correct. Re-ingest from source
# JSONL (after a `forget`) to recompute affected historical turns.
_MIGRATION_V4 = """
ALTER TABLE turns ADD COLUMN IF NOT EXISTS cache_creation_1h_tokens BIGINT DEFAULT 0;
"""


def default_path() -> Path:
    """Resolve `TOKENOL_HISTORY_PATH` env var or fall back to ``~/.tokenol/history.duckdb``."""
    env = os.environ.get("TOKENOL_HISTORY_PATH")
    if env:
        return Path(env)
    return Path.home() / ".tokenol" / "history.duckdb"


def _turn_row(t: Turn) -> tuple:
    # Strip timezone info before storing in DuckDB to avoid TZ conversion issues
    ts = t.timestamp.replace(tzinfo=None) if t.timestamp.tzinfo else t.timestamp
    # Tool costs use compact one-letter keys so a corpus with thousands of
    # turns × tens of tools doesn't bloat the warm-tier file with verbose JSON.
    tool_costs_json = _json.dumps({name: {"i": tc.input_tokens, "o": tc.output_tokens, "c": tc.cost_usd} for name, tc in t.tool_costs.items()})
    return (
        t.dedup_key,
        ts,
        t.session_id,
        t.model,
        int(t.usage.input_tokens),
        int(t.usage.output_tokens),
        int(t.usage.cache_read_input_tokens),
        int(t.usage.cache_creation_input_tokens),
        int(t.usage.cache_creation_1h_input_tokens),
        float(t.cost_usd),
        bool(t.is_sidechain),
        bool(t.is_interrupted),
        t.stop_reason,
        int(t.tool_use_count),
        int(t.tool_error_count),
        _json.dumps(dict(t.tool_names)),
        _json.dumps([a.value for a in t.assumptions]),
        tool_costs_json,
        float(t.unattributed_input_tokens),
        float(t.unattributed_output_tokens),
        float(t.unattributed_cost_usd),
        t.attribution_skill,
        _json.dumps(dict(t.skill_names)),
    )


def _session_aggregate(turns: Iterable[Turn]) -> dict[str, dict]:
    """Return {session_id: {first_ts, last_ts, count}} for the given turns."""
    agg: dict[str, dict] = {}
    for t in turns:
        # Strip timezone info for consistency with stored values
        ts = t.timestamp.replace(tzinfo=None) if t.timestamp.tzinfo else t.timestamp
        a = agg.setdefault(t.session_id, {"first_ts": ts, "last_ts": ts, "count": 0})
        if ts < a["first_ts"]:
            a["first_ts"] = ts
        if ts > a["last_ts"]:
            a["last_ts"] = ts
        a["count"] += 1
    return agg


class HistoryStore:
    """Owns a single DuckDB write connection and the schema."""

    def __init__(self, path: Path | None = None, read_only: bool = False) -> None:
        """Open the warm tier. With *read_only*, no DDL and no writes are issued.

        Read-only mode exists so the dashboard can surface persisted history
        without running a flusher inside the server process. Reading is the
        half that recovers history; writing is the half that costs CPU and,
        before the connection lock, crashed the process. Keeping them separable
        means a plain `tokenol serve` can show the whole warm tier while the
        write path lives in a separate, single-threaded process.
        """
        self.read_only = read_only
        self.path = path if path is not None else default_path()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A duckdb.DuckDBPyConnection is NOT safe for concurrent use: two
        # threads issuing queries on one connection race inside
        # ClientContext::PendingQuery and segfault the process (confirmed by
        # coredump 2026-09-04 — memcpy in PendingQuery on a flusher thread
        # while the event loop held the GIL). This store is used from several
        # threads at once: the flusher writes from its executor thread while
        # dashboard requests read from theirs. Every use of `_con` is therefore
        # serialised on this lock. RLock because flush() nests _tx() inside
        # already-locked regions.
        self._lock = threading.RLock()
        try:
            self._con = duckdb.connect(str(self.path), read_only=read_only)
        except duckdb.IOException as exc:
            raise_if_lock_conflict(exc, self.path)
            raise
        # DuckDB defaults to 80% of system RAM, which on a 32-GiB box is enough
        # to OOM the process during a large first-run flush. Bounding the pool
        # forces spills to disk instead.
        self._con.execute("SET memory_limit='1GB'")
        temp_dir = tempfile.gettempdir().replace("'", "''")
        self._con.execute(f"SET temp_directory='{temp_dir}'")
        self._con.execute("SET preserve_insertion_order=false")
        if not read_only:
            # Best-effort tighten file mode after open (DuckDB may have created it).
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                log.debug("could not chmod 0600 on %s", self.path)
            self._migrate()

    # Column order MUST match the CREATE TABLE and the engine's staging columns.
    _TURN_COLUMNS: tuple[tuple[str, str], ...] = (
        ("dedup_key", "NULL"), ("ts", "NULL"), ("session_id", "NULL"), ("model", "NULL"),
        ("input_tokens", "0"), ("output_tokens", "0"), ("cache_read_tokens", "0"),
        ("cache_creation_tokens", "0"), ("cache_creation_1h_tokens", "0"),
        ("cost_usd", "0.0"), ("is_sidechain", "FALSE"), ("is_interrupted", "FALSE"),
        ("stop_reason", "NULL"), ("tool_use_count", "0"), ("tool_error_count", "0"),
        ("tool_names", "NULL"), ("assumptions", "NULL"), ("tool_costs", "NULL"),
        ("unattributed_input_tokens", "0.0"), ("unattributed_output_tokens", "0.0"),
        ("unattributed_cost_usd", "0.0"), ("attribution_skill", "NULL"), ("skill_names", "NULL"),
    )

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Wrap a block in BEGIN/COMMIT, with ROLLBACK on any exception.

        Holds `_lock` for the transaction's lifetime so no reader can issue a
        query on the shared connection mid-transaction. Scoped per transaction
        rather than per flush() call so a large flush yields the connection
        between chunks instead of stalling the dashboard for its duration.
        """
        with self._lock:
            self._con.begin()
            try:
                yield
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def _migrate(self) -> None:
        # Apply schema (DDL is idempotent via IF NOT EXISTS / IF NOT EXISTS).
        self._con.execute(_SCHEMA_V1)
        self._con.execute(_MIGRATION_V2)
        self._con.execute(_MIGRATION_V3)
        self._con.execute(_MIGRATION_V4)
        # Upsert schema_version. Existing v1/v2 files get bumped here.
        self._con.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [str(SCHEMA_VERSION)],
        )

    def close(self) -> None:
        try:
            with self._lock:
                self._con.close()
        except Exception:
            log.debug("error closing DuckDB connection", exc_info=True)

    def flush(self, turns: list[Turn], sessions: list[Session]) -> None:
        """Insert *turns* (idempotent on dedup_key) and UPSERT *sessions*.

        Turns are committed in chunks of `FLUSH_CHUNK_SIZE` rows, each in its
        own transaction, because a single ~90k-row INSERT … ON CONFLICT DO
        NOTHING with JSON columns blows past DuckDB's default 80%-of-RAM
        memory pool and OOMs the process. Per-chunk commits release the page
        cache between chunks; dedup_key collisions make any partially-applied
        chunk a no-op on retry.

        After all turn chunks land, denormalized session metadata is computed
        in a single GROUP BY over the affected ids — so a crash between turn
        commits and the session UPSERT is self-healing on next flush, and
        steady-state flushes don't issue a SELECT per session.
        """
        if self.read_only:
            raise RuntimeError("HistoryStore opened read-only; flush() is not available")
        if not turns and not sessions:
            return

        sessions_by_id = {s.session_id: s for s in sessions}
        # Ensure every session whose turns we're inserting has a row, even
        # when the caller didn't pass an explicit Session.
        for sid in _session_aggregate(turns):
            if sid not in sessions_by_id:
                sessions_by_id[sid] = Session(session_id=sid, source_file="", is_sidechain=False, cwd=None, turns=[])

        if turns:
            insert_sql = """
                INSERT INTO turns (
                    dedup_key, ts, session_id, model,
                    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cache_creation_1h_tokens,
                    cost_usd, is_sidechain, is_interrupted, stop_reason,
                    tool_use_count, tool_error_count, tool_names, assumptions,
                    tool_costs, unattributed_input_tokens,
                    unattributed_output_tokens, unattributed_cost_usd,
                    attribution_skill, skill_names
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (dedup_key) DO NOTHING
            """
            for i in range(0, len(turns), FLUSH_CHUNK_SIZE):
                chunk = turns[i : i + FLUSH_CHUNK_SIZE]
                rows = [_turn_row(t) for t in chunk]
                with self._tx():
                    self._con.executemany(insert_sql, rows)

        sids = list(sessions_by_id)
        placeholders = ",".join(["?"] * len(sids))
        with self._lock:
            agg_rows = self._con.execute(
                f"SELECT session_id, MIN(ts), MAX(ts), COUNT(*) FROM turns WHERE session_id IN ({placeholders}) GROUP BY session_id",
                sids,
            ).fetchall()
        actual_by_sid = {sid: (mn, mx, c) for sid, mn, mx, c in agg_rows}

        with self._tx():
            for sid, s in sessions_by_id.items():
                actual = actual_by_sid.get(sid)
                if actual is None:
                    continue  # session has no turns in the store yet
                first_ts, last_ts, count = actual
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
                        updated_at  = EXCLUDED.updated_at
                    """,
                    [sid, s.source_file or None, s.cwd, s.is_sidechain, first_ts, last_ts, count],
                )

    def last_ts_by_session(self) -> dict[str, datetime]:
        """High-water marks per session_id (UTC datetimes)."""
        with self._lock:
            rows = self._con.execute("SELECT session_id, last_ts FROM sessions").fetchall()
        return {sid: ts.replace(tzinfo=timezone.utc) for sid, ts in rows}

    def is_empty(self) -> bool:
        """True when the store holds no turns at all."""
        with self._lock:
            return self._con.execute("SELECT 1 FROM turns LIMIT 1").fetchone() is None

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
        if self.read_only:
            # Same guard as flush(). Without it a read-only store fails deep inside
            # DuckDB instead of at the boundary, and the caller's broad except turns
            # a refused delete into a silent no-op.
            raise RuntimeError("HistoryStore opened read-only; forget() is not available")
        with self._lock:
            specified = sum(
                1
                for x in (
                    session_ids is not None,
                    cwd is not None,
                    older_than is not None,
                    bool(all),
                )
                if x
            )
            if specified != 1:
                raise ValueError("forget requires exactly one of: session_ids, cwd, older_than, all")

            with self._tx():
                if all:
                    t_dropped = self._con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                    s_dropped = self._con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                    self._con.execute("DELETE FROM turns")
                    self._con.execute("DELETE FROM sessions")
                    return s_dropped, t_dropped

                if session_ids is not None:
                    if not session_ids:
                        return 0, 0
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
                    return s_dropped, t_dropped

                if cwd is not None:
                    sids = [r[0] for r in self._con.execute("SELECT session_id FROM sessions WHERE cwd = ?", [cwd]).fetchall()]
                    if not sids:
                        return 0, 0
                    placeholders = ",".join(["?"] * len(sids))
                    t_dropped = self._con.execute(
                        f"SELECT COUNT(*) FROM turns WHERE session_id IN ({placeholders})",
                        sids,
                    ).fetchone()[0]
                    self._con.execute(f"DELETE FROM turns WHERE session_id IN ({placeholders})", sids)
                    self._con.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", sids)
                    return len(sids), t_dropped

                # older_than (per-turn semantics)
                cutoff_naive = older_than.replace(tzinfo=None)
                t_dropped = self._con.execute("SELECT COUNT(*) FROM turns WHERE ts < ?", [cutoff_naive]).fetchone()[0]
                affected_sids = [r[0] for r in self._con.execute("SELECT DISTINCT session_id FROM turns WHERE ts < ?", [cutoff_naive]).fetchall()]
                self._con.execute("DELETE FROM turns WHERE ts < ?", [cutoff_naive])

                s_dropped = 0
                for sid in affected_sids:
                    agg = self._con.execute(
                        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM turns WHERE session_id = ?",
                        [sid],
                    ).fetchone()
                    if agg[2] == 0:
                        self._con.execute("DELETE FROM sessions WHERE session_id = ?", [sid])
                        s_dropped += 1
                    else:
                        self._con.execute(
                            "UPDATE sessions SET first_ts = ?, last_ts = ?, turn_count = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                            [agg[0], agg[1], agg[2], sid],
                        )
                return s_dropped, t_dropped


@contextmanager
def read_connection(path: Path | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Short-lived read-only connection. Use from FastAPI handlers via run_in_executor."""
    p = path if path is not None else default_path()
    con = duckdb.connect(str(p), read_only=True)
    try:
        yield con
    finally:
        con.close()
