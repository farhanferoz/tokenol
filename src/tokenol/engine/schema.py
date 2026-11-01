"""Tables of the usage engine and the dimension-table writers.

Every string that repeats (session, model, tool, skill, stop reason) is held once
in a small dimension table and referenced by a dense integer id, which is what
keeps 700 k turns near 120 MB. Table and column names the priced views read are
fixed in ``engine.pricing``; the DDL for those four tables comes from there.

Searched before writing: ``grep -rn "CREATE TABLE" src`` finds only the history
store's own schema (a different shape: strings and JSON, not ids), and
``engine/pricing.py`` for the four shared tables, so nothing was reusable here.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from tokenol.engine.pricing import (
    MODEL_RATES_DDL,
    MODEL_RATES_TABLE,
    MODELS_DDL,
    MODELS_TABLE,
    TURN_TOOLS_TABLE,
    TURNS_TABLE,
    VIEWS_DDL,
    PriceTables,
)

# The engine's tables live in this in-memory catalog in every connection mode.
ENGINE_CATALOG = "usage_engine"

SESSIONS_TABLE = "sessions"
NAMES_TABLE = "names"
TOOL_CALLS_TABLE = "tool_calls"
SKILL_CALLS_TABLE = "skill_calls"


class NameKind(str, Enum):
    """Which vocabulary a row of the ``names`` table belongs to."""

    TOOL = "TOOL"
    SKILL = "SKILL"
    STOP_REASON = "STOP_REASON"


SESSIONS_DDL = f"""CREATE TABLE {SESSIONS_TABLE} (
    session_ix INTEGER, session_id VARCHAR, source_file VARCHAR, cwd VARCHAR,
    project_cwd VARCHAR, is_sidechain BOOLEAN)"""
NAMES_DDL = f"""CREATE TABLE {NAMES_TABLE} (
    name_id SMALLINT, name VARCHAR, kind VARCHAR)"""
# Token columns are INTEGER: the largest stored value on the real store (711 k
# turns) is 966,643, far below 2^31 - 1. The writers clamp rather than fail, so a
# malformed log line cannot stop the server.
TURNS_DDL = f"""CREATE TABLE {TURNS_TABLE} (
    turn_id INTEGER, dedup_key VARCHAR, ts TIMESTAMP, session_ix INTEGER, model_id INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER, cache_creation_1h_tokens INTEGER,
    is_sidechain BOOLEAN, is_interrupted BOOLEAN, stop_reason_id SMALLINT,
    tool_use_count SMALLINT, tool_error_count SMALLINT,
    unattributed_input_tokens DOUBLE, unattributed_output_tokens DOUBLE,
    attribution_skill_id SMALLINT, assumptions INTEGER)"""
TURN_TOOLS_DDL = f"""CREATE TABLE {TURN_TOOLS_TABLE} (
    turn_id INTEGER, tool_id SMALLINT, input_tokens DOUBLE, output_tokens DOUBLE)"""
TOOL_CALLS_DDL = f"""CREATE TABLE {TOOL_CALLS_TABLE} (turn_id INTEGER, tool_id SMALLINT, n SMALLINT)"""
SKILL_CALLS_DDL = f"""CREATE TABLE {SKILL_CALLS_TABLE} (turn_id INTEGER, skill_id SMALLINT, n SMALLINT)"""

# Order matters only in that the views come after the tables they read.
SCHEMA_DDL: tuple[str, ...] = (
    SESSIONS_DDL,
    MODELS_DDL,
    MODEL_RATES_DDL,
    NAMES_DDL,
    TURNS_DDL,
    TURN_TOOLS_DDL,
    TOOL_CALLS_DDL,
    SKILL_CALLS_DDL,
    *VIEWS_DDL,
)

# Child rows are deleted before their turns; sessions last.
FACT_TABLES_CHILD_FIRST: tuple[str, ...] = (
    TURN_TOOLS_TABLE,
    TOOL_CALLS_TABLE,
    SKILL_CALLS_TABLE,
    TURNS_TABLE,
)


def create_schema(cur) -> None:
    """Create every table and view on *cur*, whose default catalog is the engine's."""
    for ddl in SCHEMA_DDL:
        cur.execute(ddl)


class ModelCatalog:
    """Stable integer ids for model strings, and the rows that price them.

    ``PriceTables`` numbers models by sorted position, so adding a string would
    shift existing ids and every ``turns.model_id`` with them. The catalog keeps
    its own id per string instead and asks ``PriceTables`` for one model at a
    time (position 0), then re-labels the rows with the stable id. Rows are only
    ever added, never rewritten.
    """

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._unwritten: list[str] = []

    def add(self, models: Iterable[str]) -> None:
        """Remember model strings not seen before; ``write_new`` persists them."""
        for model in models:
            if model not in self._ids:
                self._ids[model] = len(self._ids)
                self._unwritten.append(model)

    def write_new(self, cur) -> int:
        """Insert the ``models`` and ``model_rates`` rows of strings not yet persisted.

        Call ``mark_written`` once the surrounding transaction has committed; until
        then a rollback leaves the strings pending, so the next call inserts them.
        """
        pending = list(self._unwritten)
        model_rows: list[tuple] = []
        rate_rows: list[tuple] = []
        for model in pending:
            model_id = self._ids[model]
            prices = PriceTables([model])
            model_rows.extend((model_id, *row[1:]) for row in prices.models_dim_rows())
            rate_rows.extend((model_id, *row[1:]) for row in prices.rows_for_sql())
        if model_rows:
            cur.executemany(f"INSERT INTO {MODELS_TABLE} VALUES (?, ?, ?, ?, ?, ?)", model_rows)
        if rate_rows:
            cur.executemany(
                f"INSERT INTO {MODEL_RATES_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rate_rows
            )
        return len(pending)

    def mark_written(self) -> None:
        self._unwritten.clear()
