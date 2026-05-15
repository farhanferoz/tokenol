"""Parse JSONL files into RawEvent objects; deduplicate by message.id:requestId."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import RawEvent, ToolCost, Usage


def _parse_timestamp(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
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


def _extract_tool_blocks(content: list) -> tuple[Counter[str], int, int]:
    """Return (tool_names, tool_use_total, tool_error_count) from a content list.

    Named `tool_use` blocks are keyed by their `name` in the Counter. Unnamed
    or empty-name blocks are skipped from `tool_names` but still bump
    `tool_use_total` so the legacy `tool_use_count` field preserves its
    "count every tool_use block" semantics.
    """
    tool_names: Counter[str] = Counter()
    tool_use_total = 0
    tool_error = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            tool_use_total += 1
            name = block.get("name")
            if isinstance(name, str) and name:
                tool_names[name] += 1
        elif btype == "tool_result" and block.get("is_error") is True:
            tool_error += 1
    return tool_names, tool_use_total, tool_error


def _block_bytes(block: dict) -> int:
    """Byte-size of a content block when JSON-serialized with compact separators.

    Used as a proxy for token count; exact wire size is not the goal.
    """
    try:
        return len(json.dumps(block, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _output_byte_shares(content: list) -> tuple[dict[str, float], float]:
    """Split an assistant message's content into per-tool byte shares + unattributed.

    Returns (shares_by_tool_name, unattributed_share). Sum = 1.0.

    `tool_use` blocks attribute to their `name`. `text` and `thinking` blocks
    (and anything else) go to unattributed.
    """
    tool_bytes: dict[str, int] = {}
    unattributed_bytes = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        b = _block_bytes(block)
        if block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                tool_bytes[name] = tool_bytes.get(name, 0) + b
                continue
        unattributed_bytes += b
    total = sum(tool_bytes.values()) + unattributed_bytes
    if total <= 0:
        return {}, 1.0
    shares = {name: b / total for name, b in tool_bytes.items()}
    return shares, unattributed_bytes / total


def _attribute_cost(
    model: str | None,
    usage: Usage,
    output_shares: dict[str, float],
    input_shares: dict[str, float],
) -> tuple[dict[str, ToolCost], float, float, float]:
    """Split a turn's four cost components by the given byte shares.

    Returns (tool_costs, unattributed_input_tokens, unattributed_output_tokens, unattributed_cost_usd).
    """
    turn_cost = cost_for_turn(model, usage)

    input_token_pool = (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
    )
    input_cost_pool = (
        turn_cost.input_usd + turn_cost.cache_read_usd + turn_cost.cache_creation_usd
    )

    names = set(output_shares.keys()) | set(input_shares.keys())
    tool_costs: dict[str, ToolCost] = {}
    for name in names:
        out_share = output_shares.get(name, 0.0)
        in_share = input_shares.get(name, 0.0)
        tool_costs[name] = ToolCost(
            tool_name=name,
            input_tokens=input_token_pool * in_share,
            output_tokens=usage.output_tokens * out_share,
            cost_usd=turn_cost.output_usd * out_share + input_cost_pool * in_share,
        )

    out_attributed = sum(output_shares.values())
    in_attributed = sum(input_shares.values())
    unattr_out_share = max(0.0, 1.0 - out_attributed)
    unattr_in_share = max(0.0, 1.0 - in_attributed)

    return (
        tool_costs,
        input_token_pool * unattr_in_share,
        usage.output_tokens * unattr_out_share,
        turn_cost.output_usd * unattr_out_share + input_cost_pool * unattr_in_share,
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

            # Count tool blocks in message content
            content = msg.get("content") or []
            if not isinstance(content, list):
                content = []
            tool_names, tool_use_count, tool_error_count = _extract_tool_blocks(content)

            cwd: str | None = ev.get("cwd") or None
            if cwd and (
                (len(cwd) >= 2 and cwd[1] == ":" and cwd[0].isalpha())  # Windows drive letter
                or cwd.startswith("\\\\")  # UNC path
            ):
                # Normalize Windows-style separators so downstream path logic
                # (ancestor detection, basename extraction, URL encoding) can
                # treat every cwd as POSIX.
                cwd = cwd.replace("\\", "/")

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
                tool_use_count=tool_use_count,
                tool_error_count=tool_error_count,
                tool_names=tool_names,
                cwd=cwd,
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
            # Skip Claude Code's synthetic assistant markers (stop-sequence placeholders).
            # These aren't real API calls and carry zero-token usage.
            if ev.model == "<synthetic>":
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
