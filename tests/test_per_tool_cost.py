"""Per-tool cost attribution: parser, rollups, and API."""

import json
from datetime import datetime, timezone
from pathlib import Path

from tokenol.ingest.parser import (
    _attribute_cost,
    _block_bytes,
    _output_byte_shares,
    parse_file,
)
from tokenol.model.events import RawEvent, ToolCost, Turn, Usage


def _sized(content):
    """Test helper: pre-size content blocks for the new _output_byte_shares
    signature. Mirrors the parser's per-line pre-pass."""
    return [(b, _block_bytes(b)) for b in content if isinstance(b, dict)]

FIXTURES = Path(__file__).parent / "fixtures"


def _write_jsonl(tmp_path, name, lines):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return p


def test_lingering_input_attribution_across_turns(tmp_path):
    """Read returns 50 KB on turn 1; turns 2-3 have no tool calls. Read's
    input attribution should grow on turns 2+3 as its result lingers in context."""
    big_result = "x" * 50_000
    lines = [
        {
            "type": "assistant", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                             "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "uuid": "u2", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big_result}
            ]},
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:02:00Z",
            "sessionId": "s1", "requestId": "r2", "uuid": "u3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m2", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 200, "output_tokens": 30,
                          "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Got it."}],
            },
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:03:00Z",
            "sessionId": "s1", "requestId": "r3", "uuid": "u4", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m3", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 30,
                          "cache_read_input_tokens": 50_500, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Anything else?"}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]
    assert len(assistants) == 3

    t1 = assistants[0]
    assert "Read" in t1.tool_costs
    assert t1.tool_costs["Read"].output_tokens > 0
    assert t1.tool_costs["Read"].input_tokens == 0

    t2 = assistants[1]
    assert "Read" in t2.tool_costs
    assert t2.tool_costs["Read"].input_tokens > 0
    assert t2.tool_costs["Read"].cost_usd > 0
    assert t2.unattributed_input_tokens < t2.tool_costs["Read"].input_tokens

    t3 = assistants[2]
    assert "Read" in t3.tool_costs
    assert t3.tool_costs["Read"].input_tokens > 0


def test_toolcost_dataclass_shape():
    tc = ToolCost(tool_name="Read", input_tokens=12.5, output_tokens=3.2, cost_usd=0.0042)
    assert tc.tool_name == "Read"
    assert tc.input_tokens == 12.5
    assert tc.output_tokens == 3.2
    assert tc.cost_usd == 0.0042


def test_rawevent_has_tool_costs_default_empty():
    ev = RawEvent(
        source_file="x.jsonl",
        line_number=1,
        event_type="assistant",
        session_id="s1",
        request_id="r1",
        message_id="m1",
        uuid="u1",
        timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
        usage=Usage(input_tokens=100, output_tokens=10),
        model="claude-opus-4-7",
        is_sidechain=False,
        stop_reason="end_turn",
    )
    assert ev.tool_costs == {}
    assert ev.unattributed_input_tokens == 0.0
    assert ev.unattributed_output_tokens == 0.0
    assert ev.unattributed_cost_usd == 0.0


def test_turn_has_tool_costs_default_empty():
    t = Turn(
        dedup_key="m1:r1",
        timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
        session_id="s1",
        model="claude-opus-4-7",
        usage=Usage(input_tokens=100, output_tokens=10),
        is_sidechain=False,
        stop_reason="end_turn",
    )
    assert t.tool_costs == {}
    assert t.unattributed_input_tokens == 0.0
    assert t.unattributed_output_tokens == 0.0
    assert t.unattributed_cost_usd == 0.0


def test_output_share_single_tool():
    content = [
        {"type": "text", "text": "I'll search for it."},
        {"type": "tool_use", "id": "a", "name": "Grep",
         "input": {"pattern": "foo"}},
    ]
    shares, unattributed = _output_byte_shares(_sized(content))
    assert set(shares.keys()) == {"Grep"}
    assert 0 < shares["Grep"] < 1
    assert 0 < unattributed < 1
    assert abs(shares["Grep"] + unattributed - 1.0) < 1e-9


def test_output_share_multiple_tools_same_name_sum():
    content = [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
        {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/y"}},
        {"type": "tool_use", "id": "c", "name": "Grep", "input": {"pattern": "z"}},
    ]
    shares, unattributed = _output_byte_shares(_sized(content))
    assert set(shares.keys()) == {"Read", "Grep"}
    assert shares["Read"] > shares["Grep"]
    assert abs(unattributed) < 1e-9


def test_output_share_thinking_block_unattributed():
    content = [
        {"type": "thinking", "thinking": "x" * 500},
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
    ]
    shares, unattributed = _output_byte_shares(_sized(content))
    assert "Read" in shares
    assert unattributed > shares["Read"]


def test_output_share_empty_content():
    shares, unattributed = _output_byte_shares([])
    assert shares == {}
    assert unattributed == 1.0


def test_attribute_cost_uses_all_four_components():
    """Cache-read and cache-creation must also be distributed by the input share,
    not lumped into 'unattributed'. On Opus a cache_read at $0.50/M is 10× cheaper
    than fresh input at $5/M — but it's still real cost the user wants attributed."""
    usage = Usage(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=2_000,
    )
    output_shares = {"Read": 0.6}
    input_shares = {"Read": 0.4}
    tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        "claude-opus-4-7", usage, output_shares, input_shares
    )

    assert "Read" in tool_costs
    tc = tool_costs["Read"]
    assert tc.output_tokens == 200 * 0.6
    assert tc.input_tokens == 1000 * 0.4 + 10_000 * 0.4 + 2_000 * 0.4
    # Opus rates: input 5, output 25, cache_read 0.5, cache_write 6.25 per 1M
    expected_cost = (
        200 * 25 / 1_000_000 * 0.6
        + 1000 * 5 / 1_000_000 * 0.4
        + 10_000 * 0.5 / 1_000_000 * 0.4
        + 2_000 * 6.25 / 1_000_000 * 0.4
    )
    assert abs(tc.cost_usd - expected_cost) < 1e-9
    assert abs(unattr_out - 200 * 0.4) < 1e-9
    assert abs(unattr_in - (1000 + 10_000 + 2_000) * 0.6) < 1e-9


def test_attribute_cost_unknown_model_zero():
    usage = Usage(input_tokens=1000, output_tokens=200)
    tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        None, usage, {"Read": 1.0}, {"Read": 1.0}
    )
    assert tool_costs["Read"].cost_usd == 0.0
    assert tool_costs["Read"].input_tokens == 1000.0
    assert tool_costs["Read"].output_tokens == 200.0
    assert unattr_cost == 0.0


def test_compaction_resets_tallies(tmp_path):
    """Turn 1 calls Read with a 50 KB result. Turn 2 has input_tokens that drop
    sharply (compaction). Turn 2 should not attribute to Read because the
    context was reset by the compaction heuristic."""
    big_result = "x" * 50_000
    lines = [
        {
            "type": "assistant", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 10_000, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                             "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "uuid": "u2", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big_result}
            ]},
        },
        # Compaction: input_tokens drops from peak (60_000) to 500 (< 20%).
        {
            "type": "assistant", "timestamp": "2026-05-15T10:02:00Z",
            "sessionId": "s1", "requestId": "r2", "uuid": "u3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m2", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 500, "output_tokens": 30,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Compacted then asked again."}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]

    assert "Read" in assistants[0].tool_costs

    post = assistants[1]
    assert "Read" not in post.tool_costs
    assert post.unattributed_input_tokens >= 0


def test_unknown_tool_use_id_goes_to_unknown_bucket(tmp_path):
    """A tool_result whose tool_use_id has no matching prior tool_use block
    (e.g. compaction lost the call) lands in __unknown__ — never crashes."""
    lines = [
        {
            "type": "user", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "uuid": "u1", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "leftover"}
            ]},
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u2", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 10,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]
    t1 = assistants[0]
    assert "__unknown__" in t1.tool_costs
    assert t1.tool_costs["__unknown__"].input_tokens > 0


def test_golden_fixture_reconciliation():
    """Three-turn fixture with Read + Bash. Per-tool cost + unattributed = total cost
    within 5% reconciliation tolerance."""
    from tokenol.metrics.cost import cost_for_turn
    events = list(parse_file(FIXTURES / "per_tool_basic.jsonl"))
    assistants = [e for e in events if e.event_type == "assistant"]
    assert len(assistants) == 3

    total_attributed_cost = 0.0
    total_unattr_cost = 0.0
    total_turn_cost = 0.0
    seen_tools: set[str] = set()

    for ev in assistants:
        for tc in ev.tool_costs.values():
            total_attributed_cost += tc.cost_usd
            seen_tools.add(tc.tool_name)
        total_unattr_cost += ev.unattributed_cost_usd
        total_turn_cost += cost_for_turn(ev.model, ev.usage).total_usd

    assert seen_tools == {"Read", "Bash"}

    reconciled = total_attributed_cost + total_unattr_cost
    assert abs(reconciled - total_turn_cost) / max(total_turn_cost, 1e-9) < 0.05


def test_builder_propagates_tool_costs():
    from tokenol.ingest.builder import build_sessions, build_turns
    fixture = FIXTURES / "per_tool_basic.jsonl"
    turns = build_turns([fixture])
    sessions = build_sessions(turns, [fixture])
    assert len(sessions) == 1
    session = sessions[0]
    assert len(session.turns) == 3

    t1 = session.turns[0]
    assert "Read" in t1.tool_costs
    assert t1.tool_costs["Read"].output_tokens > 0
    assert t1.unattributed_cost_usd >= 0


def test_build_tool_cost_daily_zero_fills():
    from datetime import date

    from tokenol.ingest.builder import build_sessions, build_turns
    from tokenol.metrics.rollups import build_tool_cost_daily

    fixture = FIXTURES / "per_tool_basic.jsonl"
    turns = build_turns([fixture])
    sessions = build_sessions(turns, [fixture])
    all_turns = [t for s in sessions for t in s.turns]
    today = date(2026, 5, 15)
    series = build_tool_cost_daily(all_turns, tool_name="Read", days=30, today=today)
    assert len(series) == 30
    nonzero = [p for p in series if p.cost_usd > 0]
    assert len(nonzero) == 1
    assert nonzero[0].date == today


def test_rank_dict_with_others_top_n_plus_other():
    from tokenol.metrics.rollups import _rank_dict_with_others
    d = {"Read": 10.0, "Bash": 7.0, "Grep": 5.0, "Edit": 3.0, "Glob": 1.5}
    out = _rank_dict_with_others(d, top_n=3)
    names = [r["name"] for r in out]
    assert names == ["Read", "Bash", "Grep", "other"]
    assert out[-1]["name"] == "other"
    assert abs(out[-1]["value"] - 4.5) < 1e-9
    # "other" row's tool_count is the number of collapsed tools — used by the
    # frontend label "(N tools)". The bar's call-count for tokens mode is
    # set separately by the breakdown endpoint.
    assert out[-1].get("tool_count") == 2


def test_rank_dict_with_others_deterministic_tie_break():
    """Equal values must sort by name ascending so the 'other' membership and
    head order are reproducible across runs."""
    from tokenol.metrics.rollups import _rank_dict_with_others
    d = {"zebra": 5.0, "apple": 5.0, "mango": 5.0}
    out1 = _rank_dict_with_others(d, top_n=2)
    out2 = _rank_dict_with_others({k: v for k, v in reversed(list(d.items()))}, top_n=2)
    assert [r["name"] for r in out1] == [r["name"] for r in out2]
    assert [r["name"] for r in out1][:2] == ["apple", "mango"]


def test_rank_dict_with_others_skips_other_when_short():
    from tokenol.metrics.rollups import _rank_dict_with_others
    d = {"Read": 10.0, "Bash": 7.0}
    out = _rank_dict_with_others(d, top_n=5)
    names = [r["name"] for r in out]
    assert names == ["Read", "Bash"]


def test_sentinel_tool_name_rejected_by_extract_tool_blocks():
    """A hostile log emitting a tool_use with name `__unattributed__` or
    `__unknown__` must not land in tool_names — otherwise the attacker can
    hide cost under the cost-attribution sentinels."""
    from tokenol.ingest.parser import _extract_tool_blocks
    content = [
        {"type": "tool_use", "id": "a", "name": "__unattributed__"},
        {"type": "tool_use", "id": "b", "name": "__unknown__"},
        {"type": "tool_use", "id": "c", "name": "Read"},
    ]
    tool_names, tool_use_total, _ = _extract_tool_blocks(content)
    assert dict(tool_names) == {"Read": 1}
    # tool_use_total still counts every block — preserves legacy semantics.
    assert tool_use_total == 3


def test_plain_string_content_is_wrapped(tmp_path):
    """Anthropic API sometimes emits content as a plain string (short replies).
    Those bytes must still feed the non-tool input pool on subsequent turns,
    not be silently dropped."""
    lines = [
        {
            "type": "assistant", "timestamp": "2026-05-16T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                # Plain string content — historically zeroed by the parser.
                "content": "Quick reply with no tool calls.",
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assert len(events) == 1
    ev = events[0]
    # The plain string gets wrapped as a text block, so it is unattributed
    # output share = 1.0 (no tool_use blocks at all), not a crash.
    assert ev.unattributed_output_tokens > 0


def test_compaction_resets_peak_so_steady_state_doesnt_re_fire(tmp_path):
    """After a compaction event, a long run of small-input turns must not
    keep re-triggering the reset on every turn (which would dump every turn's
    input cost into unattributed)."""
    big_result = "x" * 50_000
    lines = [
        # Turn 1: peak input 60_000 (10_000 fresh + 50_000 cache_read).
        {
            "type": "assistant", "timestamp": "2026-05-16T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 10_000, "output_tokens": 20,
                          "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                             "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-16T10:01:00Z",
            "sessionId": "s1", "uuid": "u2", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big_result}
            ]},
        },
        # Turn 2: input drops to 500 (compaction). Peak should reset here.
        {
            "type": "assistant", "timestamp": "2026-05-16T10:02:00Z",
            "sessionId": "s1", "requestId": "r2", "uuid": "u3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m2", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 500, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu2", "name": "Grep",
                             "input": {"pattern": "foo"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-16T10:03:00Z",
            "sessionId": "s1", "uuid": "u4", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": "matches"}
            ]},
        },
        # Turn 3: same scale as turn 2. Pre-fix this would have re-fired the
        # reset (still <20% of original 60_000 peak) and dumped Grep input
        # share into unattributed. Post-fix peak is 500 from turn 2, so this
        # turn is *not* a compaction event and Grep gets attributed.
        {
            "type": "assistant", "timestamp": "2026-05-16T10:04:00Z",
            "sessionId": "s1", "requestId": "r3", "uuid": "u5", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m3", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 400, "output_tokens": 10,
                          "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]
    assert len(assistants) == 3
    # Turn 3 should attribute non-trivial input share to Grep (its result is
    # in context); without the peak reset, all of it would land unattributed.
    t3 = assistants[2]
    assert "Grep" in t3.tool_costs
    assert t3.tool_costs["Grep"].input_tokens > 0
