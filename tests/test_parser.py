"""Parser unit tests."""

from pathlib import Path

from tokenol.enums import AssumptionTag
from tokenol.ingest.parser import iter_assistant_events, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_basic():
    events = list(parse_file(FIXTURES / "basic.jsonl"))
    assert len(events) == 2
    assert all(e.event_type == "assistant" for e in events)
    assert events[0].model == "claude-opus-4-7"
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 1000
    assert events[0].usage.output_tokens == 200


def test_parse_interrupted():
    events = list(parse_file(FIXTURES / "interrupted.jsonl"))
    assert len(events) == 1
    assert events[0].usage is None
    assert events[0].stop_reason is None


def test_parse_sidechain():
    events = list(parse_file(FIXTURES / "sidechain.jsonl"))
    assert len(events) == 1
    assert events[0].is_sidechain is True


def test_dedup_removes_duplicate():
    """Two events with same message.id + requestId should yield only one."""
    deduplicated = list(iter_assistant_events([FIXTURES / "dedup.jsonl"]))
    assert len(deduplicated) == 1


def test_dedup_passthrough_on_missing_ids():
    """Events missing message.id or requestId pass through without dedup."""
    results = list(iter_assistant_events([FIXTURES / "missing_ids.jsonl"]))
    assert len(results) == 1
    ev, tags = results[0]
    assert AssumptionTag.DEDUP_PASSTHROUGH in tags


def test_interrupted_turn_tag():
    """Interrupted turns get INTERRUPTED_TURN_SKIPPED tag."""
    results = list(iter_assistant_events([FIXTURES / "interrupted.jsonl"]))
    assert len(results) == 1
    ev, tags = results[0]
    assert AssumptionTag.INTERRUPTED_TURN_SKIPPED in tags
