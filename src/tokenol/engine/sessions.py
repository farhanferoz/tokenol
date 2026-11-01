"""Session metadata held in Python beside the engine's tables, and the per-session rollup cache."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

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


# Root-level directories whose immediate children are user/mount homes, not
# projects: "/home/ff235" and "/mnt/scratch" group projects, they are not one.
_HOME_PARENTS: frozenset[str] = frozenset({"home", "Users", "mnt", "media", "srv"})

# Memoize grouped_project_cwds on a content fingerprint of the sessions list.
# Why content, not `id(sessions)`: Python recycles ids for freed objects, so
# successive derives (or successive test runs) can collide on `id()` and return
# stale results. The fingerprint is cheap to compute (one tuple build) and
# changes whenever a session is added, removed, or has its cwd rewritten.
_GROUPED_CWD_CACHE: dict[tuple, dict[str, str]] = {}
_GROUPED_CWD_CACHE_MAX = 8


def is_container_cwd(cwd: str) -> bool:
    """Return True if *cwd* is a directory that holds projects rather than being one.

    A home directory, a filesystem root, or anything above them is a container:
    a session started there (``cd ~ && claude``) is its own project, but it must
    never become the roll-up target for every project underneath it.

    Matched by shape, not against this machine's ``$HOME``, because cwds are
    ingested from other machines too (the DGX Spark sync mirrors its cages in
    here, and it has its own home path). "Home-shaped" means exactly one
    component below a root-level container: ``/home/x``, ``/Users/x``,
    ``/root``, ``/mnt/x``.
    """
    parts = PurePosixPath(cwd).parts
    if len(parts) <= 2:  # "/", "/home", "/Users", "/mnt", "/root"
        return True
    return len(parts) == 3 and parts[1] in _HOME_PARENTS


class _HasCwd(Protocol):
    """A session or a session's metadata: all the grouping needs."""

    @property
    def session_id(self) -> str: ...

    @property
    def cwd(self) -> str | None: ...


def grouped_project_cwds(sessions: Sequence[_HasCwd]) -> dict[str, str]:
    """Return {session_id: canonical_cwd}, where nested cwds roll up to their
    shortest active ancestor.

    Example: if both "/dev/proj" and "/dev/proj/backend" appear as cwds, every
    session in "/dev/proj/backend" is remapped to "/dev/proj". Sibling or
    unrelated cwds stay separate. The "(unknown)" sentinel is preserved as-is.

    Container directories (see :func:`is_container_cwd`) are never roll-up
    targets. Without that guard a single ``cd ~ && claude`` session collapses
    every project on the machine into one "ff235" bucket, because the home
    directory is a proper ancestor of all of them.

    O(C²) in the number of distinct cwds; memoized on a content fingerprint so
    a server with many projects doesn't re-do the work on every API request.
    """
    key = tuple((s.session_id, s.cwd or "(unknown)") for s in sessions)
    cached = _GROUPED_CWD_CACHE.get(key)
    if cached is not None:
        return cached

    raw = dict(key)
    cwds = {cwd for cwd in raw.values() if cwd != "(unknown)"}
    groupable = {cwd for cwd in cwds if not is_container_cwd(cwd)}
    remap: dict[str, str] = {}
    for cwd in cwds:
        ancestors = [o for o in groupable if len(o) < len(cwd) and cwd.startswith(o + "/")]
        remap[cwd] = min(ancestors, key=len) if ancestors else cwd
    result = {sid: remap.get(cwd, cwd) for sid, cwd in raw.items()}

    if len(_GROUPED_CWD_CACHE) >= _GROUPED_CWD_CACHE_MAX:
        # Drop oldest entry (insertion order = oldest-first in CPython 3.7+).
        _GROUPED_CWD_CACHE.pop(next(iter(_GROUPED_CWD_CACHE)))
    _GROUPED_CWD_CACHE[key] = result
    return result


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
            self._project_map = grouped_project_cwds(list(self._infos.values()))
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
