"""Session metadata held in Python beside the engine's tables."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What the engine knows about a session besides its turns."""

    session_id: str
    source_file: str
    cwd: str | None
    is_sidechain: bool
