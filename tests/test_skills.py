"""Skill cost dimension: parser, aggregation, and detail builders."""

from collections import Counter
from datetime import datetime, timezone

from tokenol.model.events import EMPTY_SKILL_NAMES, RawEvent, Turn, Usage


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
    assert EMPTY_SKILL_NAMES == Counter()
