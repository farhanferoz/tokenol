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
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from tokenol.model.events import Session, Turn, Usage

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


def _turn_row(t: Turn) -> tuple:
    # Strip timezone info before storing in DuckDB to avoid TZ conversion issues
    ts = t.timestamp.replace(tzinfo=None) if t.timestamp.tzinfo else t.timestamp
    return (
        t.dedup_key,
        ts,
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
        # Strip timezone info for consistency with stored values
        ts = t.timestamp.replace(tzinfo=None) if t.timestamp.tzinfo else t.timestamp
        a = agg.setdefault(t.session_id, {"first_ts": ts, "last_ts": ts, "count": 0})
        if ts < a["first_ts"]:
            a["first_ts"] = ts
        if ts > a["last_ts"]:
            a["last_ts"] = ts
        a["count"] += 1
    return agg


def _row_to_turn(r: tuple) -> Turn:
    """Reconstruct a Turn from a turns-table row.

    Column order MUST match the SELECT in hydrate_hot / query_turns.
    """
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

    def flush(self, turns: list[Turn], sessions: list[Session]) -> None:
        """Insert *turns* (idempotent on dedup_key) and UPSERT *sessions* in one tx.

        Session metadata is refreshed from the union of (already-stored rows for
        that session_id) ∪ (new turns) so denormalized totals stay accurate.
        """
        if not turns and not sessions:
            return

        new_agg = _session_aggregate(turns)
        sessions_by_id = {s.session_id: s for s in sessions}
        # Defensive: ensure every session whose turns we're inserting has a
        # corresponding sessions-table row, even if the caller didn't pass one.
        for sid in new_agg:
            if sid not in sessions_by_id:
                sessions_by_id[sid] = Session(
                    session_id=sid, source_file="", is_sidechain=False, cwd=None, turns=[]
                )

        self._con.begin()
        try:
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
                    # Re-derive from actual rows because some inserts may have
                    # been no-ops on dedup_key collision.
                    count = self._con.execute(
                        "SELECT COUNT(*) FROM turns WHERE session_id = ?", [sid]
                    ).fetchone()[0]

                now_ts = datetime.now()
                self._con.execute(
                    """
                    INSERT INTO sessions (
                        session_id, source_file, cwd, is_sidechain,
                        first_ts, last_ts, turn_count, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        source_file = COALESCE(EXCLUDED.source_file, sessions.source_file),
                        cwd         = COALESCE(EXCLUDED.cwd,         sessions.cwd),
                        is_sidechain = EXCLUDED.is_sidechain,
                        first_ts    = LEAST(sessions.first_ts, EXCLUDED.first_ts),
                        last_ts     = GREATEST(sessions.last_ts, EXCLUDED.last_ts),
                        turn_count  = EXCLUDED.turn_count,
                        updated_at  = EXCLUDED.updated_at
                    """,
                    [sid, s.source_file or None, s.cwd, s.is_sidechain,
                     first_ts, last_ts, count, now_ts],
                )

            self._con.commit()
        except Exception:
            self._con.rollback()
            raise

    def hydrate_hot(self, window_days: int) -> tuple[list[Turn], list[Session]]:
        """Load Turn rows whose ts is within `window_days` of now, plus their sessions.

        Returns ([], []) if the store is empty or no turns fall within the window.
        Each Session's `turns` list is populated with its corresponding Turns from
        the same hot-window query — callers can iterate sessions and treat them
        as fully-hydrated.
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
            [cutoff.replace(tzinfo=None)],
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

    def query_turns(
        self,
        since: date | None = None,
        until: date | None = None,
        project: str | None = None,
        model: str | None = None,
    ) -> list[Turn]:
        """Return matching turns from the warm tier, hydrated into Turn objects.

        `since` / `until` are inclusive bounds on the date portion of `ts`.
        `project` matches `sessions.cwd` exactly (joins through sessions table).
        `model` matches `turns.model` exactly.
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
        # Direct query for this session's turns (avoids loading the full warm tier).
        turn_rows = self._con.execute(
            """
            SELECT dedup_key, ts, session_id, model,
                   input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                   cost_usd, is_sidechain, is_interrupted, stop_reason,
                   tool_use_count, tool_error_count, tool_names, assumptions
            FROM turns WHERE session_id = ? ORDER BY ts
            """,
            [sid],
        ).fetchall()
        turns = [_row_to_turn(r) for r in turn_rows]
        return Session(
            session_id=sid,
            source_file=src or "",
            is_sidechain=bool(sidechain),
            cwd=cwd,
            turns=turns,
        )


@contextmanager
def read_connection(path: Path | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """Short-lived read-only connection. Use from FastAPI handlers via run_in_executor."""
    p = path if path is not None else default_path()
    con = duckdb.connect(str(p), read_only=True)
    try:
        yield con
    finally:
        con.close()
