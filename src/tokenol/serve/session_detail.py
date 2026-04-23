"""Build the session drill-down payload for GET /api/session/<id>."""

from __future__ import annotations

from tokenol.metrics.cost import cost_for_turn
from tokenol.metrics.rollups import build_session_rollup
from tokenol.metrics.verdicts import compute_verdict
from tokenol.model.events import Session


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
    }
