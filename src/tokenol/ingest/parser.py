"""Parse JSONL files into RawEvent objects; deduplicate by message.id:requestId."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.model.events import RawEvent, Usage


def _parse_timestamp(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _parse_usage(msg: dict) -> Usage | None:
    u = msg.get("usage")
    if not u or "input_tokens" not in u:
        return None
    return Usage(
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
    )


def parse_file(path: Path) -> Iterator[RawEvent]:
    """Yield one RawEvent per non-blank, parseable line of *path*."""
    session_id = path.stem  # filename without .jsonl == sessionId

    # Sidechain detection: lives under a subagents/ subdir anywhere in the path
    is_sidechain = "subagents" in path.parts

    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if not isinstance(ev, dict):
                continue

            event_type = ev.get("type", "")
            if not event_type:
                continue

            msg = ev.get("message") or {}

            yield RawEvent(
                source_file=str(path),
                line_number=lineno,
                event_type=event_type,
                session_id=ev.get("sessionId", session_id),
                request_id=ev.get("requestId"),
                message_id=msg.get("id"),
                uuid=ev.get("uuid"),
                timestamp=_parse_timestamp(ev.get("timestamp", "")),
                usage=_parse_usage(msg),
                model=ev.get("model") or msg.get("model"),
                is_sidechain=ev.get("isSidechain", is_sidechain),
                stop_reason=msg.get("stop_reason"),
                raw=ev,
            )


def dedup_key(ev: RawEvent) -> str | None:
    """Compound dedup key matching ccusage: `message.id:requestId`.

    Returns None if either component is missing — those events pass through.
    """
    if ev.message_id is None or ev.request_id is None:
        return None
    return f"{ev.message_id}:{ev.request_id}"


def iter_assistant_events(
    paths: list[Path],
) -> Iterator[tuple[RawEvent, list[AssumptionTag]]]:
    """Yield (event, tags) for deduplicated assistant events across *paths*.

    Dedup rule: keep the last occurrence per (message.id, requestId).
    Events with a missing component pass through (tag: DEDUP_PASSTHROUGH).
    Interrupted turns (no usage data) are yielded with tag INTERRUPTED_TURN_SKIPPED
    so callers can count them but exclude from cost.
    """
    # Two-pass: collect all events keyed by dedup key, preserve last-wins.
    # Memory-efficient enough for typical log sizes.
    seen: dict[str, RawEvent] = {}
    passthroughs: list[RawEvent] = []

    for path in paths:
        for ev in parse_file(path):
            if ev.event_type != "assistant":
                continue
            key = dedup_key(ev)
            if key is None:
                passthroughs.append(ev)
            else:
                seen[key] = ev

    for ev in passthroughs:
        tags: list[AssumptionTag] = [AssumptionTag.DEDUP_PASSTHROUGH]
        if ev.usage is None:
            tags.append(AssumptionTag.INTERRUPTED_TURN_SKIPPED)
        yield ev, tags

    for ev in seen.values():
        tags = []
        if ev.usage is None:
            tags.append(AssumptionTag.INTERRUPTED_TURN_SKIPPED)
        yield ev, tags
