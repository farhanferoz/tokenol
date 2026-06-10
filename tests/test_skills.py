"""Skill cost dimension: parser, aggregation, and detail builders."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tokenol.ingest.builder import build_turns
from tokenol.model.events import EMPTY_SKILL_NAMES, RawEvent, Turn, Usage

FIXTURES = Path(__file__).parent / "fixtures"


def test_turn_has_skill_fields_with_defaults():
    t = Turn(
        dedup_key="k", timestamp=datetime(2026, 6, 10, tzinfo=timezone.utc),
        session_id="s", model="claude-opus-4-8", usage=Usage(),
        is_sidechain=False, stop_reason=None,
    )
    assert t.attribution_skill is None
    assert t.skill_names == Counter()


def test_rawevent_has_skill_fields_with_defaults():
    ev = RawEvent(
        source_file="f", line_number=1, event_type="assistant",
        session_id="s", request_id=None, message_id=None, uuid=None,
        timestamp=datetime(2026, 6, 10, tzinfo=timezone.utc),
        usage=Usage(), model="claude-opus-4-8",
        is_sidechain=False, stop_reason=None,
    )
    assert ev.attribution_skill is None
    assert ev.skill_names == Counter()


def test_empty_skill_names_sentinel_is_empty_counter():
    assert Counter() == EMPTY_SKILL_NAMES


def test_parser_reads_attribution_skill_and_invocations():
    turns = build_turns([FIXTURES / "skills.jsonl"])
    assert len(turns) == 3

    trigger = turns[0]
    assert trigger.skill_names == Counter({"tiered-review": 1})
    assert trigger.attribution_skill is None  # trigger turn itself isn't attributed

    subagent = turns[1]
    assert subagent.attribution_skill == "tiered-review"
    assert subagent.is_sidechain is True
    assert subagent.skill_names == Counter()

    inline = turns[2]
    assert inline.attribution_skill == "simplify"
    assert inline.is_sidechain is False
