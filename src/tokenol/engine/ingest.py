"""Getting turns into the engine: from the history store in SQL, from Python through CSV.

Both paths end in the same ``TurnTableWriter``, which reads rows shaped like the
store's ``turns`` table (strings and JSON) and writes the engine's id-coded tables.
The store is that shape already; Python ``Turn``s are written to a temporary CSV
file with the standard ``csv`` module (its quoting handles commas, quotes and
newlines in cwd, tool and model strings) and bulk-loaded with ``COPY``, never
inserted row by row (``executemany`` measured 370x slower).

Searched before writing: ``persistence.store._turn_row`` already encodes a Turn
as one store row (JSON maps included), so the CSV path reuses it rather than
re-implementing that encoding; ``HistoryStore._TURN_COLUMNS`` gives the store's
column order and the default for a column an old file lacks.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tokenol.engine.codes import encode_assumptions
from tokenol.engine.pricing import (
    MODEL_RATES_TABLE,
    MODELS_TABLE,
    TURN_TOOLS_TABLE,
    TURNS_TABLE,
    rate_match_on,
)
from tokenol.engine.schema import (
    NAMES_TABLE,
    SESSIONS_TABLE,
    SKILL_CALLS_TABLE,
    TOOL_CALLS_TABLE,
    ModelCatalog,
    NameKind,
)
from tokenol.engine.sessions import SessionInfo
from tokenol.enums import AssumptionTag
from tokenol.model.events import Turn
from tokenol.model.registry import is_claude
from tokenol.persistence.store import HistoryStore, _turn_row

log = logging.getLogger(__name__)

# Written for a Python None; DuckDB reads it back as NULL. An empty field would
# make "" and NULL indistinguishable, and a cwd of "" is not a missing cwd.
CSV_NULL = "\\N"
INT32_MAX = 2**31 - 1
INT16_MAX = 2**15 - 1
# A write of more rows than this is "bulk": memory is returned to the operating
# system between its statements and after it. Measured on the real store, the
# three child inserts peaked at 2.7 GB without trimming between them.
BULK_INGEST_ROWS = 10_000
# The child tables are exploded from JSON this many turns at a time: parsing 700 k
# turns in one statement peaked near 700 MB above the resident size; 100 k at a
# time, with memory returned between chunks, keeps the transient near a third of that.
CHILD_CHUNK_TURNS = 100_000

STAGE_TURNS = "stage_turns"
STAGE_SESSIONS = "stage_sessions"
SOURCE_TURNS = "source_turns"
ASSUMPTION_BITS_TABLE = "assumption_bits"

# Staging column types, in the order of the store's ``turns`` columns. Integers
# are BIGINT here so a malformed log line cannot fail the COPY; the writer clamps.
_STAGE_COLUMN_TYPES: tuple[tuple[str, str], ...] = (
    ("dedup_key", "VARCHAR"), ("ts", "TIMESTAMP"), ("session_id", "VARCHAR"),
    ("model", "VARCHAR"), ("input_tokens", "BIGINT"), ("output_tokens", "BIGINT"),
    ("cache_read_tokens", "BIGINT"), ("cache_creation_tokens", "BIGINT"),
    ("cache_creation_1h_tokens", "BIGINT"), ("cost_usd", "DOUBLE"),
    ("is_sidechain", "BOOLEAN"), ("is_interrupted", "BOOLEAN"), ("stop_reason", "VARCHAR"),
    ("tool_use_count", "BIGINT"), ("tool_error_count", "BIGINT"), ("tool_names", "VARCHAR"),
    ("assumptions", "VARCHAR"), ("tool_costs", "VARCHAR"),
    ("unattributed_input_tokens", "DOUBLE"), ("unattributed_output_tokens", "DOUBLE"),
    ("unattributed_cost_usd", "DOUBLE"), ("attribution_skill", "VARCHAR"),
    ("skill_names", "VARCHAR"),
)
if [c for c, _ in _STAGE_COLUMN_TYPES] != [c for c, _ in HistoryStore._TURN_COLUMNS]:
    raise RuntimeError("staging columns must match the store's turns columns, in order")

_TS_COLUMN = 1  # position of ``ts`` in a store row


def naive_utc(moment: datetime) -> datetime:
    """Naive UTC, the engine's one timestamp form (a naive input is taken as UTC already)."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _cell(value: object) -> object:
    if value is None:
        return CSV_NULL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return value


@contextmanager
def staged_csv(rows: Iterable[Sequence[object]]) -> Iterator[Path]:
    """Write *rows* to a temporary CSV file, deleted when the block exits."""
    fd, name = tempfile.mkstemp(prefix="tokenol-stage-", suffix=".csv", dir=tempfile.gettempdir())
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")  # QUOTE_MINIMAL
            for row in rows:
                writer.writerow([_cell(v) for v in row])
        yield path
    finally:
        path.unlink(missing_ok=True)


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _copy_csv(cur, table: str, path: Path) -> None:
    cur.execute(
        f"COPY {table} FROM {sql_literal(str(path))} "
        # The dialect is stated, not sniffed: csv.writer's QUOTE_MINIMAL with a
        # doubled-quote escape, which is also what a sniffer would have to guess.
        f"(FORMAT csv, HEADER false, AUTO_DETECT false, DELIMITER ',', QUOTE '\"', "
        f"ESCAPE '\"', NULL {sql_literal(CSV_NULL)})"
    )


def _clamped(column: str) -> str:
    return f"least(s.{column}, {INT32_MAX})::INTEGER"


class TurnTableWriter:
    """The one set of SQL that fills the engine's fact tables from store-shaped rows."""

    def __init__(self, release_memory: Callable[[], object]) -> None:
        self._release_memory = release_memory

    def write(self, cur, relation: str) -> int:
        """Insert every row of *relation* (models and sessions already in place); return the count."""
        self._add_names(cur, relation)
        bits = self._assumption_bits(cur, relation)
        base = cur.execute(f"SELECT coalesce(max(turn_id) + 1, 0) FROM {TURNS_TABLE}").fetchone()[0]
        rows = self._insert_turns(cur, relation, base)
        self._insert_children(cur, relation, base, rows, trim=rows > BULK_INGEST_ROWS)
        cur.execute(f"DROP TABLE {bits}")
        return rows

    def _add_names(self, cur, relation: str) -> None:
        tool, skill, stop = NameKind.TOOL.value, NameKind.SKILL.value, NameKind.STOP_REASON.value
        cur.execute(
            f"""INSERT INTO {NAMES_TABLE}
            SELECT (SELECT coalesce(max(name_id) + 1, 0) FROM {NAMES_TABLE})
                    + row_number() OVER (ORDER BY kind, name) - 1, name, kind
            FROM (
                SELECT DISTINCT name, kind FROM (
                    SELECT je.key AS name, '{tool}' AS kind
                        FROM {relation} s CROSS JOIN json_each(s.tool_names) AS je
                    UNION ALL SELECT je.key, '{tool}'
                        FROM {relation} s CROSS JOIN json_each(s.tool_costs) AS je
                    UNION ALL SELECT je.key, '{skill}'
                        FROM {relation} s CROSS JOIN json_each(s.skill_names) AS je
                    UNION ALL SELECT s.attribution_skill, '{skill}'
                        FROM {relation} s WHERE s.attribution_skill IS NOT NULL
                    UNION ALL SELECT s.stop_reason, '{stop}'
                        FROM {relation} s WHERE s.stop_reason IS NOT NULL
                ) AS found
            ) AS d
            WHERE NOT EXISTS (
                SELECT 1 FROM {NAMES_TABLE} n WHERE n.name = d.name AND n.kind = d.kind)"""
        )

    def _assumption_bits(self, cur, relation: str) -> str:
        """Temp table mapping each distinct stored tag list to its bit set (a handful of rows)."""
        cur.execute(
            f"SELECT DISTINCT coalesce(s.assumptions::VARCHAR, '[]') FROM {relation} s"
        )
        mapping = [
            (text, encode_assumptions(AssumptionTag(v) for v in json.loads(text)))
            for (text,) in cur.fetchall()
        ]
        cur.execute(f"CREATE TEMP TABLE {ASSUMPTION_BITS_TABLE} (src VARCHAR, bits INTEGER)")
        cur.executemany(f"INSERT INTO {ASSUMPTION_BITS_TABLE} VALUES (?, ?)", mapping)
        return ASSUMPTION_BITS_TABLE

    def _insert_turns(self, cur, relation: str, base: int) -> int:
        stop, skill = NameKind.STOP_REASON.value, NameKind.SKILL.value
        # The context pool of the clamped, stored token counts, as the priced view sees it.
        pool = " + ".join(
            f"{_clamped(c)}::BIGINT" for c in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
        )
        rate_on = rate_match_on("rt", "m.model_id", "s.ts", f"({pool})")
        sql = f"""INSERT INTO {TURNS_TABLE}
            SELECT * FROM (
                SELECT {base} + row_number() OVER (ORDER BY s.ts, s.dedup_key) - 1 AS turn_id,
                    s.dedup_key, s.ts, ss.session_ix, m.model_id,
                    {_clamped("input_tokens")}, {_clamped("output_tokens")},
                    {_clamped("cache_read_tokens")}, {_clamped("cache_creation_tokens")},
                    {_clamped("cache_creation_1h_tokens")},
                    s.is_sidechain, s.is_interrupted, sr.name_id,
                    s.tool_use_count::SMALLINT, s.tool_error_count::SMALLINT,
                    coalesce(s.unattributed_input_tokens, 0), coalesce(s.unattributed_output_tokens, 0),
                    sk.name_id, ab.bits, rt.rate_ix
                FROM {relation} s
                JOIN {MODELS_TABLE} m ON m.model = s.model
                JOIN {SESSIONS_TABLE} ss ON ss.session_id = s.session_id
                LEFT JOIN {NAMES_TABLE} sr ON sr.kind = '{stop}' AND sr.name = s.stop_reason
                LEFT JOIN {NAMES_TABLE} sk ON sk.kind = '{skill}' AND sk.name = s.attribution_skill
                JOIN {ASSUMPTION_BITS_TABLE} ab ON ab.src = coalesce(s.assumptions::VARCHAR, '[]')
                LEFT JOIN {MODEL_RATES_TABLE} rt ON {rate_on}
            ) ORDER BY turn_id"""
        return cur.execute(sql).fetchone()[0]

    def _insert_children(self, cur, relation: str, base: int, rows: int, *, trim: bool) -> None:
        for low in range(base, base + rows, CHILD_CHUNK_TURNS):
            self._insert_children_chunk(cur, relation, low, low + CHILD_CHUNK_TURNS)
            if trim:
                self._release_memory()

    def _insert_children_chunk(self, cur, relation: str, low: int, high: int) -> None:
        tool, skill = NameKind.TOOL.value, NameKind.SKILL.value
        # The turn_id window lets a small batch skip every older row group and
        # bounds how many JSON documents one statement parses.
        new_turn = (
            f"JOIN {TURNS_TABLE} t ON t.dedup_key = s.dedup_key "
            f"AND t.turn_id >= {low} AND t.turn_id < {high}"
        )
        cur.execute(
            f"""INSERT INTO {TURN_TOOLS_TABLE}
            SELECT t.turn_id, n.name_id,
                coalesce(json_extract(je.value, '$.i')::DOUBLE, 0),
                coalesce(json_extract(je.value, '$.o')::DOUBLE, 0)
            FROM {relation} s {new_turn}
            CROSS JOIN json_each(s.tool_costs) AS je
            JOIN {NAMES_TABLE} n ON n.kind = '{tool}' AND n.name = je.key"""
        )
        for table, kind, column in (
            (TOOL_CALLS_TABLE, tool, "tool_names"),
            (SKILL_CALLS_TABLE, skill, "skill_names"),
        ):
            cur.execute(
                f"""INSERT INTO {table}
                SELECT t.turn_id, n.name_id, least(je.value::INTEGER, {INT16_MAX})::SMALLINT
                FROM {relation} s {new_turn}
                CROSS JOIN json_each(s.{column}) AS je
                JOIN {NAMES_TABLE} n ON n.kind = '{kind}' AND n.name = je.key"""
            )


class StoreLoader:
    """Fills an empty engine from a history store's catalog, entirely in SQL."""

    def __init__(self, writer: TurnTableWriter, models: ModelCatalog) -> None:
        self._writer = writer
        self._models = models

    def load(self, cur, source_catalog: str) -> int:
        """Load every Claude turn of ``<source_catalog>.main.turns``; return the row count.

        Non-Claude rows are left out, by the Claude-only rule the store reads always applied.
        """
        src = f'"{source_catalog}".main'
        present = {
            r[0] for r in cur.execute(f"SELECT column_name FROM (DESCRIBE {src}.turns)").fetchall()
        }
        columns = ", ".join(
            name if name in present else f"{default} AS {name}"
            for name, default in HistoryStore._TURN_COLUMNS
        )
        stored_models = [
            m for (m,) in cur.execute(f"SELECT DISTINCT model FROM {src}.turns").fetchall()
        ]
        self._models.add(sorted(m for m in stored_models if is_claude(m)))
        self._models.write_new(cur)
        cur.execute(
            f"""CREATE TEMP VIEW {SOURCE_TURNS} AS
            SELECT {columns} FROM {src}.turns WHERE model IN (SELECT model FROM {MODELS_TABLE})"""
        )
        cur.execute(
            f"""INSERT INTO {SESSIONS_TABLE}
            SELECT row_number() OVER (ORDER BY d.session_id) - 1, d.session_id,
                coalesce(se.source_file, ''), se.cwd, se.cwd, coalesce(se.is_sidechain, false)
            FROM (SELECT DISTINCT session_id FROM {SOURCE_TURNS}) AS d
            LEFT JOIN {src}.sessions se ON se.session_id = d.session_id"""
        )
        rows = self._writer.write(cur, SOURCE_TURNS)
        cur.execute(f"DROP VIEW {SOURCE_TURNS}")
        return rows


class TurnStager:
    """Bulk-loads Python Turns and session rows through a temporary CSV file."""

    def __init__(self, writer: TurnTableWriter, models: ModelCatalog) -> None:
        self._writer = writer
        self._models = models

    def upsert_sessions(self, cur, indexed: Sequence[tuple[int, SessionInfo]]) -> None:
        """Insert unseen sessions and update the ones whose details changed."""
        if not indexed:
            return
        cur.execute(
            f"""CREATE TEMP TABLE {STAGE_SESSIONS} (session_ix INTEGER, session_id VARCHAR,
                source_file VARCHAR, cwd VARCHAR, is_sidechain BOOLEAN)"""
        )
        rows = ((ix, i.session_id, i.source_file, i.cwd, i.is_sidechain) for ix, i in indexed)
        with staged_csv(rows) as path:
            _copy_csv(cur, STAGE_SESSIONS, path)
        # project_cwd starts equal to cwd; the project grouping rewrites it later.
        cur.execute(
            f"""INSERT INTO {SESSIONS_TABLE}
            SELECT st.session_ix, st.session_id, st.source_file, st.cwd, st.cwd, st.is_sidechain
            FROM {STAGE_SESSIONS} st
            WHERE NOT EXISTS (SELECT 1 FROM {SESSIONS_TABLE} x WHERE x.session_ix = st.session_ix)"""
        )
        cur.execute(
            f"""UPDATE {SESSIONS_TABLE} SET source_file = st.source_file, cwd = st.cwd,
                project_cwd = st.cwd, is_sidechain = st.is_sidechain
            FROM {STAGE_SESSIONS} st
            WHERE {SESSIONS_TABLE}.session_ix = st.session_ix
            AND ({SESSIONS_TABLE}.source_file IS DISTINCT FROM st.source_file
                OR {SESSIONS_TABLE}.cwd IS DISTINCT FROM st.cwd
                OR {SESSIONS_TABLE}.is_sidechain IS DISTINCT FROM st.is_sidechain)"""
        )
        cur.execute(f"DROP TABLE {STAGE_SESSIONS}")

    def add_turns(self, cur, turns: Sequence[Turn]) -> int:
        """Stage *turns* and write them; their sessions must already be upserted."""
        if not turns:
            return 0
        self._models.add(sorted({t.model for t in turns if t.model}))
        self._models.write_new(cur)
        columns = ", ".join(f"{name} {kind}" for name, kind in _STAGE_COLUMN_TYPES)
        cur.execute(f"CREATE TEMP TABLE {STAGE_TURNS} ({columns})")
        with staged_csv(self._store_rows(turns)) as path:
            _copy_csv(cur, STAGE_TURNS, path)
        rows = self._writer.write(cur, STAGE_TURNS)
        cur.execute(f"DROP TABLE {STAGE_TURNS}")
        return rows

    @staticmethod
    def _store_rows(turns: Iterable[Turn]) -> Iterator[list[object]]:
        for turn in turns:
            row = list(_turn_row(turn))
            row[_TS_COLUMN] = naive_utc(turn.timestamp)
            yield row
