"""The four engine objects a server or command works with, opened together.

Searched before writing: ``grep -rn "UsageEngine(" src`` and ``grep -rn "SessionIndex(" src`` found
only the engine's own tests building these by hand, so the wiring (engine, then index seeded from
it, then queries and the rollup cache over both) had no home before this.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tokenol.engine.engine import UsageEngine
from tokenol.engine.queries import UsageQueries
from tokenol.engine.sessions import SessionIndex, SessionRollupCache


@dataclass(frozen=True, slots=True)
class UsageContext:
    """One engine with the session index, queries and rollup cache that read it."""

    engine: UsageEngine
    index: SessionIndex
    queries: UsageQueries
    rollups: SessionRollupCache

    @classmethod
    def open(cls, store_path: Path | None) -> UsageContext:
        """Open the engine on *store_path* (``None`` or a missing file: empty) and seed the index from it.

        Raises ``StoreLockedError`` exactly as ``UsageEngine.open`` does.
        """
        engine = UsageEngine.open(store_path)
        index = SessionIndex(engine)
        index.add(engine.session_infos())
        return cls(engine, index, UsageQueries(engine), SessionRollupCache(engine, index))

    def close(self) -> None:
        self.engine.close()
