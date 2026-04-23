"""Build the session drill-down payload for GET /api/session/<id>."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from tokenol.metrics.cost import cost_for_turn
from tokenol.metrics.patterns import detect_patterns
from tokenol.metrics.rollups import build_session_rollup
from tokenol.metrics.verdicts import compute_verdict
from tokenol.model.events import Session, Turn

_SNIPPET_LEN = 500


def _extract_text(content: str | list | None) -> str:
    """Pull plain text from a message content field (str or content-block list)."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(parts)


def _snip(text: str) -> str:
    if len(text) <= _SNIPPET_LEN:
        return text
    return text[:_SNIPPET_LEN] + "…"


def _parse_turn_snippets(
    source_file: str,
    session_id: str,
    turn: Turn,
) -> tuple[str, str, list[dict], int | None]:
    """Return (user_prompt, assistant_preview, tool_calls, source_line)."""
    try:
        path = Path(source_file)
        if not path.exists():
            return "", "", [], None
    except Exception:
        return "", "", [], None

    # dedup_key = "message_id:request_id" or passthrough
    # We match on message_id (first component) + session_id + timestamp as fallback
    target_msg_id: str | None = None
    if ":" in turn.dedup_key:
        target_msg_id = turn.dedup_key.split(":", 1)[0]

    target_ts = turn.timestamp.isoformat()

    events: list[tuple[int, dict]] = []  # (1-indexed line, parsed event)
    try:
        with path.open() as fh:
            for lineno, raw in enumerate(fh, 1):
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("sessionId") == session_id:
                    events.append((lineno, ev))
    except Exception:
        return "", "", [], None

    # Find the assistant event for this turn
    asst_lineno: int | None = None
    asst_ev: dict | None = None
    for lineno, ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        if target_msg_id and msg.get("id") == target_msg_id:
            asst_lineno = lineno
            asst_ev = ev
            break
        # Timestamp fallback
        if ev.get("timestamp", "").replace("Z", "+00:00") == target_ts:
            asst_lineno = lineno
            asst_ev = ev
            break

    if asst_ev is None:
        return "", "", [], None

    # Find the user event immediately preceding the assistant event
    user_text = ""
    for _lineno, ev in reversed([(ln, e) for ln, e in events if ln < (asst_lineno or 0)]):
        if ev.get("type") == "user":
            user_text = _snip(_extract_text(ev.get("message", {}).get("content")))
            break

    # Extract assistant preview (text blocks only)
    asst_text = _snip(_extract_text(asst_ev.get("message", {}).get("content")))

    # Extract tool calls from assistant content blocks
    tool_calls: list[dict] = []
    content = asst_ev.get("message", {}).get("content")
    if isinstance(content, list):
        tool_ids = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append({"name": block.get("name", ""), "ok": True, "id": block.get("id")})
                tool_ids.append(block.get("id"))
        # Check subsequent user events for tool errors
        if tool_ids:
            for _, ev in events:
                if ev.get("type") != "user" or not isinstance(ev.get("message", {}).get("content"), list):
                    continue
                for block in ev["message"]["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        for tc in tool_calls:
                            if tc.get("id") == block.get("tool_use_id"):
                                tc["ok"] = False

    # Strip internal "id" field before returning
    for tc in tool_calls:
        tc.pop("id", None)

    return user_text, asst_text, tool_calls, asst_lineno


def build_turn_detail(session: Session, turn_idx: int) -> dict | None:
    """Return single-turn detail payload including text snippets."""
    if turn_idx < 0 or turn_idx >= len(session.turns):
        return None

    t = session.turns[turn_idx]
    tc = cost_for_turn(t.model, t.usage)
    user_prompt, asst_preview, tool_calls, source_line = _parse_turn_snippets(
        session.source_file, session.session_id, t
    )

    return {
        "session_id": session.session_id,
        "turn_idx": turn_idx,
        "ts": t.timestamp.isoformat(),
        "model": t.model,
        "stop_reason": t.stop_reason,
        "is_sidechain": t.is_sidechain,
        "cost_components": {
            "input":          tc.input_usd,
            "output":         tc.output_usd,
            "cache_read":     tc.cache_read_usd,
            "cache_creation": tc.cache_creation_usd,
        },
        "token_counts": {
            "input":         t.usage.input_tokens,
            "output":        t.usage.output_tokens,
            "cache_read":    t.usage.cache_read_input_tokens,
            "cache_creation": t.usage.cache_creation_input_tokens,
            "total_visible": (
                t.usage.input_tokens
                + t.usage.cache_read_input_tokens
                + t.usage.cache_creation_input_tokens
            ),
        },
        "tool_calls":        tool_calls,
        "user_prompt":       user_prompt,
        "assistant_preview": asst_preview,
        "source_file":       session.source_file,
        "source_line":       source_line,
    }


def build_session_detail(session: Session) -> dict:
    """Build the full session detail payload from a Session object."""
    turns = session.turns

    sr = build_session_rollup(session)
    verdict = compute_verdict(sr)

    first_ts = turns[0].timestamp.isoformat() if turns else None
    last_ts = turns[-1].timestamp.isoformat() if turns else None

    turn_rows = []
    for t in turns:
        tc = cost_for_turn(t.model, t.usage)
        turn_rows.append({
            "ts": t.timestamp.isoformat(),
            "model": t.model,
            "input_tokens": t.usage.input_tokens,
            "output_tokens": t.usage.output_tokens,
            "cache_read_tokens": t.usage.cache_read_input_tokens,
            "cache_creation_tokens": t.usage.cache_creation_input_tokens,
            "cost_usd": t.cost_usd,
            "is_sidechain": t.is_sidechain,
            "tool_use_count": t.tool_use_count,
            "tool_error_count": t.tool_error_count,
            "stop_reason": t.stop_reason,
            "cost_components": {
                "input":          tc.input_usd,
                "output":         tc.output_usd,
                "cache_read":     tc.cache_read_usd,
                "cache_creation": tc.cache_creation_usd,
            },
        })

    patterns = [asdict(h) for h in detect_patterns(turns)]

    return {
        "session_id": session.session_id,
        "source_file": session.source_file,
        "model": sr.model,
        "cwd": session.cwd or "",
        "verdict": verdict.value,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "totals": {
            "cost_usd": sr.cost_usd,
            "turns": len(turns),
            "tool_uses": sr.tool_use_count,
            "tool_errors": sr.tool_error_count,
        },
        "turns": turn_rows,
        "patterns": patterns,
    }
