"""Per-tool cost attribution: parser, rollups, and API."""

from datetime import datetime, timezone

from tokenol.model.events import RawEvent, ToolCost, Turn, Usage


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
    assert t.unattributed_cost_usd == 0.0
