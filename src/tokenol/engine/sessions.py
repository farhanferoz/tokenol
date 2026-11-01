"""Session metadata held in Python beside the engine's tables, and the per-session rollup cache."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tokenol.metrics.rollups import SessionRollup, build_session_rollup
from tokenol.metrics.verdicts import compute_verdict
from tokenol.model.events import Session

if TYPE_CHECKING:
    from tokenol.engine.engine import UsageEngine

# Sessions the engine has no turns for sort after every session that has some.
_NO_TURNS = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What the engine knows about a session besides its turns."""

    session_id: str
    source_file: str
    cwd: str | None
    is_sidechain: bool


class SessionIndex:
    """Every session's metadata, its project grouping, and whether its file is gone.

    The grouping is memoised and recomputed only when the session set changes; each
    recomputation is mirrored into the engine's ``sessions.project_cwd``.
    """

    def __init__(self, engine: UsageEngine) -> None:
        self._engine = engine
        self._infos: dict[str, SessionInfo] = {}
        self._first_ts: dict[str, datetime] = {}
        self._ordered: list[SessionInfo] | None = None
        self._project_map: dict[str, str] | None = None
        self._live_ids: frozenset[str] | None = None

    def add(self, infos: Iterable[SessionInfo]) -> None:
        """Register sessions, replacing any with the same id. Resets order and grouping."""
        for info in infos:
            self._infos[info.session_id] = info
        self._ordered = None
        self._project_map = None

    def get(self, session_id: str) -> SessionInfo | None:
        return self._infos.get(session_id)

    def all(self) -> list[SessionInfo]:
        """Sessions ordered by first turn time (the engine's ``min(ts)``), as the dashboard lists them."""
        if self._ordered is None:
            self._first_ts = self._engine.session_first_times()
            self._ordered = sorted(
                self._infos.values(),
                key=lambda s: (self._first_ts.get(s.session_id, _NO_TURNS), s.session_id),
            )
        return list(self._ordered)

    def set_live_paths(self, paths: Iterable[Path | str]) -> None:
        """The JSONL files present now; a session whose file is not among them is archived."""
        self._live_ids = frozenset(Path(p).stem for p in paths)

    def archived(self, session_id: str) -> bool:
        return self._live_ids is not None and session_id not in self._live_ids

    def project_map(self) -> dict[str, str]:
        """{session_id: grouped project cwd}."""
        if self._project_map is None:
            # Imported here: serve.state will import the engine package, so a module-level
            # import would be circular.
            from tokenol.serve.state import _grouped_cwd_by_sid

            self._project_map = _grouped_cwd_by_sid(list(self._infos.values()))
            self._engine.set_project_cwds(self._project_map)
        return self._project_map

    def project_cwd(self, session_id: str) -> str:
        return self.project_map().get(session_id, "(unknown)")


class SessionRollupCache:
    """One ``SessionRollup`` (verdict set) per session, kept until explicitly invalidated.

    The server invalidates the sessions each tick's new turns touched, and any it forgets.
    Polling the engine for changes instead costs ~1 ms per session, 2-3 s a tick over 2.4k.
    """

    def __init__(self, engine: UsageEngine, index: SessionIndex) -> None:
        self._engine = engine
        self._index = index
        self._cache: dict[str, SessionRollup] = {}

    def rollup(self, session_id: str) -> SessionRollup:
        return self.rollups([session_id])[0]

    def rollups(self, session_ids: Iterable[str]) -> list[SessionRollup]:
        """Rollups in the order asked; misses are computed in one batched engine read."""
        ids = list(session_ids)
        missing = [sid for sid in dict.fromkeys(ids) if sid not in self._cache]
        if missing:
            turns_by_sid = self._engine.session_turns_many(missing)
            for sid in missing:
                info = self._index.get(sid)
                session = Session(
                    session_id=sid,
                    source_file=info.source_file if info else "",
                    is_sidechain=info.is_sidechain if info else False,
                    cwd=info.cwd if info else None,
                    turns=turns_by_sid.get(sid, []),
                    archived=self._index.archived(sid),
                )
                rollup = build_session_rollup(session)
                rollup.verdict = compute_verdict(rollup)
                self._cache[sid] = rollup
        return [self._cache[sid] for sid in ids]

    def invalidate(self, session_ids: Iterable[str]) -> None:
        for sid in session_ids:
            self._cache.pop(sid, None)

    def clear(self) -> None:
        self._cache.clear()
