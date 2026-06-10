"""Skill cost dimension: parser, aggregation, and detail builders."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tokenol.ingest.builder import build_turns
from tokenol.model.events import EMPTY_SKILL_NAMES, RawEvent, Session, Turn, Usage
from tokenol.serve.state import build_breakdown_skills, build_skill_detail

FIXTURES = Path(__file__).parent / "fixtures"


def _turn(skill, cost, *, sidechain=False, model="claude-opus-4-8",
          out=0, ts=datetime(2026, 6, 10, 12, tzinfo=timezone.utc), skill_names=None):
    t = Turn(
        dedup_key=f"k{cost}-{skill}", timestamp=ts, session_id="s", model=model,
        usage=Usage(output_tokens=out), is_sidechain=sidechain, stop_reason=None,
    )
    t.cost_usd = cost
    t.attribution_skill = skill
    if skill_names:
        t.skill_names = Counter(skill_names)
    return t


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


def test_build_breakdown_skills_groups_cost_by_skill():
    turns = [
        _turn("tiered-review", 0.06, skill_names={"tiered-review": 1}),  # trigger-ish
        _turn("tiered-review", 4.00, sidechain=True),                    # fan-out
        _turn("simplify", 0.90),
        _turn(None, 5.00),  # un-skilled normal work: ignored
    ]
    rows = build_breakdown_skills(turns)
    by_name = {r["name"]: r for r in rows}
    assert by_name["tiered-review"]["cost_usd"] == 4.06
    assert by_name["simplify"]["cost_usd"] == 0.90
    assert "other" not in by_name  # only 2 skills, no tail
    # No "no-skill" residual row — un-skilled turns are simply excluded.
    assert set(by_name) == {"tiered-review", "simplify"}
    assert by_name["tiered-review"]["invocations"] == 1
    # Ranked by cost desc.
    assert [r["name"] for r in rows] == ["tiered-review", "simplify"]


def test_build_breakdown_skills_empty_returns_empty():
    assert build_breakdown_skills([]) == []
    assert build_breakdown_skills([_turn(None, 1.0)]) == []


def test_build_skill_detail_splits_inline_vs_subagent():
    turns = [
        _turn("tiered-review", 0.06, model="claude-opus-4-8",
              skill_names={"tiered-review": 1}),                 # inline trigger
        _turn("tiered-review", 4.00, sidechain=True,
              model="claude-opus-4-8", out=2000),                # sub-agent
        _turn("simplify", 0.90),                                 # other skill, ignored
    ]
    sessions = [Session(session_id="s", source_file="f.jsonl",
                        is_sidechain=False, cwd="/home/u/proj", turns=turns)]
    d = build_skill_detail("tiered-review", turns, sessions)
    assert d is not None
    assert d["name"] == "tiered-review"
    assert d["scorecards"]["cost_usd"] == 4.06
    assert d["scorecards"]["invocations"] == 1
    assert d["split"] == {"inline_usd": 0.06, "subagent_usd": 4.00}
    # by_model groups attributed turns' full cost by model.
    assert d["by_model"][0] == {"name": "claude-opus-4-8",
                                "cost_usd": 4.06, "invocations": 0}
    # by_project keyed on the session cwd.
    assert d["by_project"][0]["cost_usd"] == 4.06
    assert len(d["daily_cost"]) == 30


def test_build_skill_detail_unknown_returns_none():
    assert build_skill_detail("nope", [], []) is None
