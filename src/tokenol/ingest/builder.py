"""Build Turn objects from deduplicated RawEvents, applying cost computation."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from tokenol import assumptions as assumption_recorder
from tokenol.ingest.parser import dedup_key, iter_assistant_events, parse_file
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import Session, Turn, Usage


def build_turns(paths: list[Path]) -> list[Turn]:
    """Parse *paths*, deduplicate, compute cost, record assumptions."""
    turns: list[Turn] = []

    for ev, tags in iter_assistant_events(paths):
        is_interrupted = ev.usage is None
        usage = ev.usage if ev.usage is not None else Usage()

        tc = cost_for_turn(ev.model, usage)
        tags.extend(t for t in tc.assumptions if t not in tags)
        assumption_recorder.record(tags)

        key = dedup_key(ev) or ev.uuid or str(id(ev))

        turns.append(
            Turn(
                dedup_key=key,
                timestamp=ev.timestamp,
                session_id=ev.session_id,
                model=ev.model,
                usage=usage,
                is_sidechain=ev.is_sidechain,
                stop_reason=ev.stop_reason,
                assumptions=tags,
                cost_usd=tc.total_usd,
                is_interrupted=is_interrupted,
                tool_use_count=ev.tool_use_count,
                tool_error_count=ev.tool_error_count,
            )
        )

    return turns


def build_sessions(turns: list[Turn], paths: list[Path] | None = None) -> list[Session]:
    """Group turns by session_id. One Session per JSONL file.

    If *paths* is provided, scans system events to extract cwd per session.
    """
    # Collect cwd per session_id from system events
    cwd_by_session: dict[str, str] = {}
    sidechain_by_session: dict[str, bool] = {}

    if paths:
        for path in paths:
            for raw_ev in parse_file(path):
                sid = raw_ev.session_id
                if raw_ev.cwd and sid not in cwd_by_session:
                    cwd_by_session[sid] = raw_ev.cwd
                if raw_ev.is_sidechain and sid not in sidechain_by_session:
                    sidechain_by_session[sid] = raw_ev.is_sidechain

    # Group turns by session_id; track source_file
    session_turns: dict[str, list[Turn]] = defaultdict(list)
    session_source: dict[str, str] = {}
    session_sidechain: dict[str, bool] = {}

    for turn in turns:
        sid = turn.session_id
        session_turns[sid].append(turn)
        if sid not in session_sidechain:
            session_sidechain[sid] = turn.is_sidechain

    # Build sessions — if we have paths, map session_id to source file
    if paths:
        for path in paths:
            # The JSONL stem is the session_id
            sid = path.stem
            session_source[sid] = str(path)
            # Also scan any turns whose session_id may differ from filename
        for turn in turns:
            sid = turn.session_id
            if sid not in session_source:
                session_source[sid] = ""
    else:
        for turn in turns:
            sid = turn.session_id
            if sid not in session_source:
                session_source[sid] = ""

    sessions: list[Session] = []
    for sid, t_list in session_turns.items():
        t_list.sort(key=lambda t: t.timestamp)
        sessions.append(
            Session(
                session_id=sid,
                source_file=session_source.get(sid, ""),
                is_sidechain=session_sidechain.get(sid, False),
                cwd=cwd_by_session.get(sid),
                turns=t_list,
            )
        )

    sessions.sort(key=lambda s: s.turns[0].timestamp if s.turns else s.session_id)
    return sessions
