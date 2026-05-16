"""Parse JSONL files into RawEvent objects; deduplicate by message.id:requestId."""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import (
    EMPTY_TOOL_COSTS,
    EMPTY_TOOL_NAMES,
    RawEvent,
    ToolCost,
    Usage,
)

UNATTRIBUTED_TOOL = "__unattributed__"
UNKNOWN_TOOL = "__unknown__"
COMPACTION_DROP_RATIO = 0.2


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


def _is_real_tool_name(name: object) -> bool:
    """A tool_use block's name is real only if it's a non-empty string that
    doesn't collide with the cost-attribution sentinels or the synthetic
    "other" row used by ranked-bar collapsing — a hostile log could otherwise
    hide its share of cost under `__unattributed__` / `__unknown__` or
    masquerade as the collapsed tail.
    """
    return (
        isinstance(name, str)
        and bool(name)
        and name not in (UNATTRIBUTED_TOOL, UNKNOWN_TOOL, "other")
    )


def _extract_tool_blocks(content: list) -> tuple[Counter[str], int, int]:
    """Return (tool_names, tool_use_total, tool_error_count) from a content list.

    Named `tool_use` blocks are keyed by their `name` in the Counter. Unnamed,
    empty-name, or sentinel-collision (``__unattributed__`` / ``__unknown__``)
    blocks are skipped from `tool_names` but still bump `tool_use_total` so the
    legacy `tool_use_count` field preserves its "count every tool_use block"
    semantics.
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
            if _is_real_tool_name(name):
                tool_names[name] += 1
        elif btype == "tool_result" and block.get("is_error") is True:
            tool_error += 1
    if not tool_names:
        return EMPTY_TOOL_NAMES, tool_use_total, tool_error
    return tool_names, tool_use_total, tool_error


def _block_bytes(block: dict) -> int:
    """Byte-size of a content block when JSON-serialized with compact separators.

    Used as a proxy for token count; exact wire size is not the goal.
    Returns 0 for content that fails to serialize (non-JSON types, deeply
    nested structures hitting Python's recursion limit, or circular refs).
    """
    try:
        return len(json.dumps(block, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return 0


def _output_byte_shares(block_sizes: list[tuple[dict, int]]) -> tuple[dict[str, float], float]:
    """Split an assistant message's pre-sized content blocks into per-tool byte
    shares + unattributed. Returns (shares_by_tool_name, unattributed_share);
    sum = 1.0.

    Accepts pre-computed ``(block, byte_size)`` pairs so callers can compute
    block sizes once per turn and share them between this function and the
    context-accumulation tally.

    `tool_use` blocks (with a real, non-sentinel name) attribute to that name.
    `text`, `thinking`, sentinel-named blocks, and anything else go to
    unattributed.
    """
    tool_bytes: dict[str, int] = {}
    unattributed_bytes = 0
    for block, b in block_sizes:
        if block.get("type") == "tool_use":
            name = block.get("name")
            if _is_real_tool_name(name):
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

    `output_usd` is distributed by `output_shares`.
    `input_usd + cache_read_usd + cache_creation_usd` are combined into a single
    input-side pool and distributed by `input_shares`.

    Precondition: `sum(output_shares.values()) <= 1.0` and
    `sum(input_shares.values()) <= 1.0`. Callers satisfy this by construction
    (shares are computed from `bytes_per_tool / total_bytes`).
    The `max(0.0, ...)` guards below only protect the *unattributed* leg from
    negative values; they do NOT rescale per-tool amounts if a caller violates
    the precondition.

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
        tool_costs if tool_costs else EMPTY_TOOL_COSTS,
        input_token_pool * unattr_in_share,
        usage.output_tokens * unattr_out_share,
        turn_cost.output_usd * unattr_out_share + input_cost_pool * unattr_in_share,
    )


def parse_file(path: Path) -> Iterator[RawEvent]:
    """Yield one RawEvent per non-blank, parseable line of *path*.

    Per-session state is maintained for per-tool cost attribution:
    - `tool_use_id_to_name` maps assistant-side tool_use IDs to tool names.
    - `bytes_in_context_by_tool` / `non_tool_bytes_in_context` are running byte tallies
      of content still in the conversation window.
    - Compaction is detected heuristically (input drop ≥80% from running peak) and
      resets both tallies.
    """
    # Intern fields with low cardinality (1 unique per file or a handful across
    # the corpus). Each parsed event would otherwise hold a fresh Python str for
    # source_file / session_id / event_type / model / stop_reason / cwd; sharing
    # them via the intern table saves ~30-50 MiB across the 376 K event corpus.
    source_file_str = sys.intern(str(path))
    session_id_default = sys.intern(path.stem)

    # Sidechain detection: lives under a subagents/ subdir anywhere in the path
    is_sidechain = "subagents" in path.parts

    tool_use_id_to_name: dict[str, str] = {}
    bytes_in_context_by_tool: dict[str, int] = {}
    non_tool_bytes_in_context = 0
    peak_input_tokens = 0

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
            event_type = sys.intern(event_type)

            msg = ev.get("message") or {}

            # Count tool blocks in message content. Plain-string content (rare
            # but spec-legal for short assistant replies) is wrapped as a single
            # ``text`` block so its bytes still contribute to non-tool input
            # share on subsequent turns; otherwise lingering tool bytes would
            # absorb a disproportionate slice of the input pool.
            content = msg.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            elif not isinstance(content, list):
                content = []
            tool_names, tool_use_count, tool_error_count = _extract_tool_blocks(content)

            usage = _parse_usage(msg)
            model = ev.get("model") or msg.get("model")
            if model:
                model = sys.intern(model)

            cwd: str | None = ev.get("cwd") or None
            if cwd and (
                (len(cwd) >= 2 and cwd[1] == ":" and cwd[0].isalpha())  # Windows drive letter
                or cwd.startswith("\\\\")  # UNC path
            ):
                # Normalize Windows-style separators so downstream path logic
                # (ancestor detection, basename extraction, URL encoding) can
                # treat every cwd as POSIX.
                cwd = cwd.replace("\\", "/")
            if cwd:
                cwd = sys.intern(cwd)

            tool_costs: dict[str, ToolCost] = EMPTY_TOOL_COSTS
            unattr_in = unattr_out = unattr_cost = 0.0

            # Block sizes are computed once per line and reused by both the
            # output-share computation and the context-accumulation tally below.
            block_sizes = [(b, _block_bytes(b)) for b in content if isinstance(b, dict)]

            if event_type == "assistant" and usage is not None:
                input_pool = (
                    usage.input_tokens
                    + usage.cache_read_input_tokens
                    + usage.cache_creation_input_tokens
                )
                if peak_input_tokens > 0 and input_pool < COMPACTION_DROP_RATIO * peak_input_tokens:
                    tool_use_id_to_name.clear()
                    bytes_in_context_by_tool.clear()
                    non_tool_bytes_in_context = 0
                    # Reset peak to the post-compaction input pool so a session
                    # that stabilises below 20% of its historical peak doesn't
                    # keep re-firing the compaction branch on every turn (which
                    # would otherwise dump all attribution into 'unattributed').
                    peak_input_tokens = input_pool
                else:
                    peak_input_tokens = max(peak_input_tokens, input_pool)

                output_shares, _ = _output_byte_shares(block_sizes)
                total_ctx_bytes = sum(bytes_in_context_by_tool.values()) + non_tool_bytes_in_context
                if total_ctx_bytes > 0:
                    input_shares = {
                        name: b / total_ctx_bytes
                        for name, b in bytes_in_context_by_tool.items()
                    }
                else:
                    input_shares = {}
                tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
                    model, usage, output_shares, input_shares
                )

            # Fold this line's content into the running tallies so the next
            # assistant turn can attribute its input side against them.
            for block, b in block_sizes:
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name")
                    bid = block.get("id")
                    if _is_real_tool_name(name) and isinstance(bid, str) and bid:
                        tool_use_id_to_name[bid] = name
                        bytes_in_context_by_tool[name] = (
                            bytes_in_context_by_tool.get(name, 0) + b
                        )
                    else:
                        non_tool_bytes_in_context += b
                elif btype == "tool_result":
                    bid = block.get("tool_use_id")
                    name = tool_use_id_to_name.pop(bid, UNKNOWN_TOOL) if bid else UNKNOWN_TOOL
                    bytes_in_context_by_tool[name] = (
                        bytes_in_context_by_tool.get(name, 0) + b
                    )
                else:
                    non_tool_bytes_in_context += b

            sid = ev.get("sessionId")
            sid = sys.intern(sid) if sid else session_id_default
            stop_reason = msg.get("stop_reason")
            if stop_reason:
                stop_reason = sys.intern(stop_reason)
            yield RawEvent(
                source_file=source_file_str,
                line_number=lineno,
                event_type=event_type,
                session_id=sid,
                request_id=ev.get("requestId"),
                message_id=msg.get("id"),
                uuid=ev.get("uuid"),
                timestamp=_parse_timestamp(ev.get("timestamp", "")),
                usage=usage,
                model=model,
                is_sidechain=ev.get("isSidechain", is_sidechain),
                stop_reason=stop_reason,
                tool_use_count=tool_use_count,
                tool_error_count=tool_error_count,
                tool_names=tool_names,
                cwd=cwd,
                tool_costs=tool_costs,
                unattributed_input_tokens=unattr_in,
                unattributed_output_tokens=unattr_out,
                unattributed_cost_usd=unattr_cost,
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
