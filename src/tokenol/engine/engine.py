"""The usage engine: dictionary-coded fact tables in DuckDB memory.

One ``UsageEngine`` holds every turn as columns (about 120 MB for 711 k turns)
instead of as Python objects. It is loaded from the history store in SQL, fed
live turns through CSV staging, and materialises one session's turns on demand.

Three connection modes share one API:

* ``PERSIST``: ``--persist`` has a writable ``HistoryStore``. The engine attaches
  an in-memory catalog inside the store's own connection and loads from the
  store's catalog (a second process cannot open the file, so there is no other way).
* ``READ_ONLY_STORE``: a store file exists but nothing holds it for writing. The
  engine opens its own in-memory database, attaches the file read-only, loads,
  then detaches it so the file cache is freed.
* ``MEMORY``: no store; every turn arrives through ``add_turns``.

Searched before writing: ``grep -rn malloc_trim src`` found only a comment in
``serve/state.py``, so ``release_free_memory`` is new here.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import duckdb

from tokenol.engine.codes import decode_assumptions
from tokenol.engine.ingest import (
    BULK_INGEST_ROWS,
    StoreLoader,
    TurnStager,
    TurnTableWriter,
    naive_utc,
)
from tokenol.engine.pricing import PRICED_TURN_TOOLS_VIEW, PRICED_TURNS_VIEW, TURNS_TABLE
from tokenol.engine.schema import (
    ENGINE_CATALOG,
    FACT_TABLES_CHILD_FIRST,
    NAMES_TABLE,
    SESSIONS_TABLE,
    SKILL_CALLS_TABLE,
    TOOL_CALLS_TABLE,
    ModelCatalog,
    NameKind,
    create_schema,
)
from tokenol.engine.sessions import SessionInfo
from tokenol.model.events import (
    EMPTY_ASSUMPTIONS,
    EMPTY_SKILL_NAMES,
    EMPTY_TOOL_COSTS,
    EMPTY_TOOL_NAMES,
    ToolCost,
    Turn,
    Usage,
)
from tokenol.model.registry import is_claude
from tokenol.persistence.store import HistoryStore

log = logging.getLogger(__name__)

_STORE_ALIAS = "tokenol_store"
_PROC_STATM = Path("/proc/self/statm")
_BYTES_PER_MB = 1024 * 1024


class EngineMode(str, Enum):
    PERSIST = "persist"
    READ_ONLY_STORE = "read_only_store"
    MEMORY = "memory"


def _rss_bytes() -> int | None:
    """Resident set size of this process, or None where /proc is absent."""
    try:
        pages = int(_PROC_STATM.read_text().split()[1])
        return pages * _page_size()
    except (OSError, ValueError, IndexError):
        return None


def _page_size() -> int:
    return os.sysconf("SC_PAGE_SIZE")


def release_free_memory() -> tuple[int | None, int | None]:
    """Ask glibc to return freed heap to the OS; (rss_before, rss_after) in bytes.

    A no-op where glibc has no ``malloc_trim`` (macOS, musl, Windows).
    """
    before = _rss_bytes()
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return before, before
    trim(0)
    return before, _rss_bytes()


def _mb(value: int | None) -> str:
    return "n/a" if value is None else f"{value / _BYTES_PER_MB:.0f} MB"


class UsageEngine:
    """Owns the engine's tables, its connection and the ingest and forget paths."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        connection_lock: threading.RLock | threading.Lock,
        mode: EngineMode,
    ) -> None:
        self.mode = mode
        self._con = connection
        # Guards cursor creation only: a connection is not safe for two threads at once.
        self._connection_lock = connection_lock
        # Serialises ingest and forget; reads never take it.
        self._write_lock = threading.Lock()
        self._models = ModelCatalog()
        self._writer = TurnTableWriter(release_free_memory)
        self._session_ix: dict[str, int] = {}
        self._con.execute(f"ATTACH ':memory:' AS {ENGINE_CATALOG}")
        with self.cursor() as cur:
            create_schema(cur)

    @classmethod
    def open(cls, store: HistoryStore | None, store_path: Path | None) -> UsageEngine:
        """Pick the connection mode from what exists, load, and release build memory."""
        started = time.monotonic()
        if store is not None and not store.read_only:
            with store.connection_lock:
                con = store.engine_connection()
                source = con.execute("SELECT current_database()").fetchone()[0]
                engine = cls(con, store.connection_lock, EngineMode.PERSIST)
            engine._load(source)
        else:
            path = store_path if store_path is not None else (store.path if store else None)
            if path is not None and path.exists():
                con = duckdb.connect()
                con.execute(f"ATTACH '{path}' AS {_STORE_ALIAS} (READ_ONLY)")
                engine = cls(con, threading.Lock(), EngineMode.READ_ONLY_STORE)
                engine._load(_STORE_ALIAS)
                con.execute(f"DETACH {_STORE_ALIAS}")
            else:
                engine = cls(duckdb.connect(), threading.Lock(), EngineMode.MEMORY)
        log.info("usage engine open in %s mode after %.1f s", engine.mode.value, time.monotonic() - started)
        return engine

    def close(self) -> None:
        """Free the engine's tables (and, when it owns it, the connection)."""
        with self._connection_lock:
            if self.mode is EngineMode.PERSIST:
                self._con.execute(f"DETACH {ENGINE_CATALOG}")
            else:
                self._con.close()

    @contextmanager
    def cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A cursor whose default catalog is the engine's. Reads go through here."""
        with self._connection_lock:  # creating the cursor is the only step under the lock
            cur = self._con.cursor()
        try:
            cur.execute(f"USE {ENGINE_CATALOG}")
            yield cur
        finally:
            cur.close()

    @contextmanager
    def _transaction(self, cur: duckdb.DuckDBPyConnection) -> Iterator[None]:
        cur.begin()
        try:
            yield
        except BaseException:
            cur.rollback()
            raise
        cur.commit()

    # -- loading and ingest --------------------------------------------------

    def _load(self, source_catalog: str) -> None:
        started = time.monotonic()
        with self._write_lock, self.cursor() as cur:
            has_turns = cur.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? AND table_name = 'turns'",
                [source_catalog],
            ).fetchone()[0]
            rows = 0
            if has_turns:
                # No transaction: a load into an empty engine either completes or
                # aborts open(); inside one, the child inserts peaked at 2.7 GB.
                rows = StoreLoader(self._writer, self._models).load(cur, source_catalog)
                self._models.mark_written()
        self._refresh_session_index()
        self._compact()
        before, after = release_free_memory()
        log.info(
            "usage engine loaded %d turns in %.1f s (resident %s -> %s)",
            rows, time.monotonic() - started, _mb(before), _mb(after),
        )

    def add_turns(self, turns: Sequence[Turn], sessions: Iterable[SessionInfo]) -> int:
        """Add turns the engine does not hold yet, upserting *sessions*; return rows added.

        Non-Claude turns and keys already held (or repeated in the batch) are skipped.
        """
        started = time.monotonic()
        with self._write_lock:
            claude = [t for t in turns if is_claude(t.model)]
            known = self.known_keys(t.dedup_key for t in claude)
            fresh: list[Turn] = []
            for turn in claude:
                if turn.dedup_key not in known:
                    known.add(turn.dedup_key)
                    fresh.append(turn)
            infos = {s.session_id: s for s in sessions}
            for turn in fresh:
                if turn.session_id not in self._session_ix:
                    infos.setdefault(
                        turn.session_id, SessionInfo(turn.session_id, "", None, turn.is_sidechain)
                    )
            allocated = dict(self._session_ix)
            indexed: list[tuple[int, SessionInfo]] = []
            for sid, info in infos.items():
                if sid not in allocated:
                    allocated[sid] = max(allocated.values(), default=-1) + 1
                indexed.append((allocated[sid], info))
            stager = TurnStager(self._writer, self._models)
            with self.cursor() as cur, self._transaction(cur):
                stager.upsert_sessions(cur, indexed)
                rows = stager.add_turns(cur, fresh)
            self._models.mark_written()
            self._session_ix = allocated
        if rows > BULK_INGEST_ROWS:
            self._compact()
            before, after = release_free_memory()
            log.info(
                "usage engine ingested %d turns in %.1f s (resident %s -> %s)",
                rows, time.monotonic() - started, _mb(before), _mb(after),
            )
        return rows

    def _compact(self) -> None:
        """Compress the engine's row groups. An in-memory table is stored raw until a
        checkpoint: on 711 k turns, 240 MiB before and 82 MiB after. Best effort,
        since a checkpoint is refused while another transaction is open."""
        try:
            with self.cursor() as cur:
                cur.execute(f"CHECKPOINT {ENGINE_CATALOG}")
        except duckdb.Error as exc:
            log.warning("usage engine could not compact its tables: %s", exc)

    def known_keys(self, candidates: Iterable[str]) -> set[str]:
        """The subset of *candidates* whose turn the engine already holds."""
        keys = list(set(candidates))
        if not keys:
            return set()
        with self.cursor() as cur:
            rows = cur.execute(
                f"SELECT t.dedup_key FROM {TURNS_TABLE} t "
                "JOIN (SELECT unnest(?) AS k) c ON t.dedup_key = c.k",
                [keys],
            ).fetchall()
        return {r[0] for r in rows}

    def _refresh_session_index(self) -> None:
        with self.cursor() as cur:
            rows = cur.execute(f"SELECT session_id, session_ix FROM {SESSIONS_TABLE}").fetchall()
        self._session_ix = dict(rows)

    # -- forget --------------------------------------------------------------

    def forget(
        self,
        *,
        session_ids: list[str] | None = None,
        cwd: str | None = None,
        older_than: datetime | None = None,
        all: bool = False,
    ) -> int:
        """Drop history from memory; return the number of turns dropped.

        Same keyword contract as ``HistoryStore.forget``: exactly one argument.
        ``cwd`` matches the sessions' stored ``cwd`` column exactly. With
        *older_than*, sessions left with no turns are dropped too.
        """
        given = sum(x for x in (session_ids is not None, cwd is not None, older_than is not None, bool(all)))
        if given != 1:
            raise ValueError("forget requires exactly one of: session_ids, cwd, older_than, all")
        with self._write_lock, self.cursor() as cur, self._transaction(cur):
            params: list = []
            session_clause = ""  # which session rows go (none for older_than: decided afterwards)
            if all:
                doomed = "TRUE"
                session_clause = "TRUE"
            elif session_ids is not None:
                ixs = [self._session_ix[s] for s in session_ids if s in self._session_ix]
                if not ixs:
                    return 0
                doomed = "session_ix IN (SELECT unnest(?))"
                session_clause = doomed
                params = [ixs]
            elif cwd is not None:
                doomed = f"session_ix IN (SELECT session_ix FROM {SESSIONS_TABLE} WHERE cwd = ?)"
                session_clause = "cwd = ?"
                params = [cwd]
            else:
                assert older_than is not None
                doomed = "ts < ?"
                params = [naive_utc(older_than)]
            affected: list[int] = []
            if older_than is not None:
                affected = [
                    r[0]
                    for r in cur.execute(
                        f"SELECT DISTINCT session_ix FROM {TURNS_TABLE} WHERE {doomed}", params
                    ).fetchall()
                ]
            doomed_ids = f"SELECT turn_id FROM {TURNS_TABLE} WHERE {doomed}"
            for table in FACT_TABLES_CHILD_FIRST[:-1]:
                cur.execute(f"DELETE FROM {table} WHERE turn_id IN ({doomed_ids})", params)
            dropped = cur.execute(f"DELETE FROM {TURNS_TABLE} WHERE {doomed}", params).fetchone()[0]
            if older_than is not None:
                if affected:
                    cur.execute(
                        f"""DELETE FROM {SESSIONS_TABLE} WHERE session_ix IN (SELECT unnest(?))
                        AND NOT EXISTS (SELECT 1 FROM {TURNS_TABLE} t
                            WHERE t.session_ix = {SESSIONS_TABLE}.session_ix)""",
                        [affected],
                    )
            else:
                cur.execute(f"DELETE FROM {SESSIONS_TABLE} WHERE {session_clause}", params)
        self._refresh_session_index()
        return dropped

    # -- one session ---------------------------------------------------------

    def session_stats(self, session_id: str) -> tuple[int, datetime | None]:
        """Turn count and last timestamp (UTC) of a session: the key for per-session caches."""
        ix = self._session_ix.get(session_id)
        if ix is None:
            return 0, None
        with self.cursor() as cur:
            count, last = cur.execute(
                f"SELECT count(*), max(ts) FROM {TURNS_TABLE} WHERE session_ix = ?", [ix]
            ).fetchone()
        return count, None if last is None else last.replace(tzinfo=timezone.utc)

    def session_turns(self, session_id: str) -> list[Turn]:
        """The session's turns in time order, priced, as the ``Turn``s the Python code consumes."""
        ix = self._session_ix.get(session_id)
        if ix is None:
            return []
        tool, skill = NameKind.TOOL.value, NameKind.SKILL.value
        stop = NameKind.STOP_REASON.value
        with self.cursor() as cur:
            rows = cur.execute(
                f"""SELECT t.turn_id, t.dedup_key, t.ts, m.model, t.input_tokens, t.output_tokens,
                    t.cache_read_tokens, t.cache_creation_tokens, t.cache_creation_1h_tokens,
                    t.is_sidechain, t.is_interrupted, sr.name, t.tool_use_count, t.tool_error_count,
                    t.unattributed_input_tokens, t.unattributed_output_tokens, sk.name, t.assumptions,
                    p.cost_usd, p.input_usd, p.output_usd, p.cache_read_usd, p.cache_creation_usd
                FROM {TURNS_TABLE} t
                JOIN models m ON m.model_id = t.model_id
                JOIN {PRICED_TURNS_VIEW} p ON p.turn_id = t.turn_id
                LEFT JOIN {NAMES_TABLE} sr ON sr.kind = '{stop}' AND sr.name_id = t.stop_reason_id
                LEFT JOIN {NAMES_TABLE} sk ON sk.kind = '{skill}' AND sk.name_id = t.attribution_skill_id
                WHERE t.session_ix = ?
                ORDER BY t.ts, t.turn_id""",
                [ix],
            ).fetchall()
            in_turns = f"SELECT turn_id FROM {TURNS_TABLE} WHERE session_ix = ?"
            tool_rows = cur.execute(
                f"""SELECT p.turn_id, n.name, p.input_tokens, p.output_tokens, p.cost_prorata,
                    p.input_share, p.output_share
                FROM {PRICED_TURN_TOOLS_VIEW} p
                JOIN {NAMES_TABLE} n ON n.kind = '{tool}' AND n.name_id = p.tool_id
                WHERE p.turn_id IN ({in_turns})""",
                [ix],
            ).fetchall()
            tool_counts = self._counts(cur, TOOL_CALLS_TABLE, "tool_id", tool, in_turns, ix)
            skill_counts = self._counts(cur, SKILL_CALLS_TABLE, "skill_id", skill, in_turns, ix)
        costs: dict[int, dict[str, ToolCost]] = {}
        shares: dict[int, list[float]] = {}  # turn_id -> [summed input share, summed output share]
        for turn_id, name, tin, tout, cost, in_share, out_share in tool_rows:
            costs.setdefault(turn_id, {})[name] = ToolCost(name, tin, tout, cost)
            total = shares.setdefault(turn_id, [0.0, 0.0])
            total[0] += in_share
            total[1] += out_share
        turns: list[Turn] = []
        for (
            turn_id, key, ts, model, inp, out, cr, cc, cc_1h, sidechain, interrupted, stop_reason,
            tool_uses, tool_errors, unattr_in, unattr_out, skill_name, bits,
            cost, input_usd, output_usd, cache_read_usd, cache_creation_usd,
        ) in rows:
            in_total, out_total = shares.get(turn_id, (0.0, 0.0))
            # The share of each side no tool accounts for, priced as the parser prices it.
            unattr_cost = output_usd * max(0.0, 1.0 - out_total) + (
                input_usd + cache_read_usd + cache_creation_usd
            ) * max(0.0, 1.0 - in_total)
            turns.append(
                Turn(
                    dedup_key=key,
                    timestamp=ts.replace(tzinfo=timezone.utc),
                    session_id=session_id,
                    model=model,
                    usage=Usage(inp, out, cr, cc, cc_1h),
                    is_sidechain=sidechain,
                    stop_reason=stop_reason,
                    assumptions=decode_assumptions(bits) or EMPTY_ASSUMPTIONS,
                    cost_usd=cost,
                    is_interrupted=interrupted,
                    tool_use_count=tool_uses,
                    tool_error_count=tool_errors,
                    tool_names=tool_counts.get(turn_id) or EMPTY_TOOL_NAMES,
                    tool_costs=costs.get(turn_id) or EMPTY_TOOL_COSTS,
                    unattributed_input_tokens=unattr_in,
                    unattributed_output_tokens=unattr_out,
                    unattributed_cost_usd=unattr_cost,
                    attribution_skill=skill_name,
                    skill_names=skill_counts.get(turn_id) or EMPTY_SKILL_NAMES,
                )
            )
        return turns

    @staticmethod
    def _counts(cur, table: str, column: str, kind: str, in_turns: str, ix: int) -> dict[int, Counter[str]]:
        counts: dict[int, Counter[str]] = {}
        rows = cur.execute(
            f"""SELECT c.turn_id, n.name, c.n FROM {table} c
            JOIN {NAMES_TABLE} n ON n.kind = '{kind}' AND n.name_id = c.{column}
            WHERE c.turn_id IN ({in_turns})""",
            [ix],
        ).fetchall()
        for turn_id, name, n in rows:
            counts.setdefault(turn_id, Counter())[name] = n
        return counts
