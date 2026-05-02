"""DuckDB-backed durable store for tokenol's derived analytics.

The store is single-process, single-writer. The broadcaster owns the write
connection; FastAPI handlers that need warm-tier reads acquire short-lived
read connections via :func:`read_connection`.

Schema is versioned via ``meta.schema_version``. ``HistoryStore.__init__``
applies any missing migrations idempotently, so opening an existing file
either upgrades or no-ops.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
