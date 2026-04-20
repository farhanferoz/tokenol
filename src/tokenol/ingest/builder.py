"""Build Turn objects from deduplicated RawEvents, applying cost computation."""

from __future__ import annotations

from pathlib import Path

from tokenol import assumptions as assumption_recorder
from tokenol.ingest.parser import dedup_key, iter_assistant_events
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import Turn, Usage


def build_turns(paths: list[Path]) -> list[Turn]:
    """Parse *paths*, deduplicate, compute cost, record assumptions."""
    turns: list[Turn] = []

    for ev, tags in iter_assistant_events(paths):
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
            )
        )

    return turns
