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
                tool_names=ev.tool_names,
                tool_costs=ev.tool_costs,
                unattributed_input_tokens=ev.unattributed_input_tokens,
                unattributed_output_tokens=ev.unattributed_output_tokens,
                unattributed_cost_usd=ev.unattributed_cost_usd,
            )
        )

    return turns


def build_sessions(turns: list[Turn], paths: list[Path] | None = None) -> list[Session]:
    """Group turns by session_id. Multiple sessions can exist in one JSONL file.

    If *paths* is provided, scans each file to extract cwd and source path for each session.
    """
    cwd_by_session: dict[str, str] = {}
    session_source: dict[str, str] = {}

    if paths:
        for path in paths:
            for raw_ev in parse_file(path):
                sid = raw_ev.session_id
                # Map each session to its source file (first encounter wins).
                if sid not in session_source:
                    session_source[sid] = str(path)
                # Extract first cwd for each session.
                if raw_ev.cwd and sid not in cwd_by_session:
                    cwd_by_session[sid] = raw_ev.cwd

    session_turns: dict[str, list[Turn]] = defaultdict(list)
    session_sidechain: dict[str, bool] = {}

    for turn in turns:
        sid = turn.session_id
        session_turns[sid].append(turn)
        if sid not in session_sidechain:
            session_sidechain[sid] = turn.is_sidechain

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
