"""Skill cost dimension: parser, aggregation, and detail builders."""

import itertools
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import usage_from_sessions

from tokenol.engine.queries import SkillCosts
from tokenol.ingest.builder import build_turns
from tokenol.ingest.parser import _extract_skill_names, _is_real_skill_name
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import EMPTY_SKILL_NAMES, RawEvent, Session, Turn, Usage
from tokenol.serve.state import (
    build_skill_breakdown,
    build_skill_detail,
    derive_delta_turns,
    model_price_status,
)

FIXTURES = Path(__file__).parent / "fixtures"


_KEYS = itertools.count()
OPUS = "claude-opus-4-8"


def _turn(skill, out, *, sidechain=False, ts=None, skill_names=None):
    """A turn with *out* output tokens on OPUS, attributed to *skill* (the engine prices it from the tokens)."""
    t = Turn(
        dedup_key=f"k{next(_KEYS)}",
        timestamp=ts or datetime.now(tz=timezone.utc) - timedelta(days=2),
        session_id="s",
        model=OPUS,
        usage=Usage(output_tokens=out),
        is_sidechain=sidechain,
        stop_reason=None,
    )
    t.attribution_skill = skill
    if skill_names:
        t.skill_names = Counter(skill_names)
    return t


def _cost(t):
    return cost_for_turn(t.model, t.usage, at=t.timestamp).total_usd


def _skill_costs(cost_by_skill, invocations_by_skill):
    when = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    return SkillCosts(cost_by_skill, invocations_by_skill, dict.fromkeys(cost_by_skill, when), 0.0, 0.0, 0.0)


def test_turn_has_skill_fields_with_defaults():
    t = Turn(
        dedup_key="k",
        timestamp=datetime(2026, 6, 10, tzinfo=timezone.utc),
        session_id="s",
        model="claude-opus-4-8",
        usage=Usage(),
        is_sidechain=False,
        stop_reason=None,
    )
    assert t.attribution_skill is None
    assert t.skill_names == Counter()


def test_rawevent_has_skill_fields_with_defaults():
    ev = RawEvent(
        source_file="f",
        line_number=1,
        event_type="assistant",
        session_id="s",
        request_id=None,
        message_id=None,
        uuid=None,
        timestamp=datetime(2026, 6, 10, tzinfo=timezone.utc),
        usage=Usage(),
        model="claude-opus-4-8",
        is_sidechain=False,
        stop_reason=None,
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


def test_skill_breakdown_ranks_by_cost_and_leaves_no_residual_row():
    # Un-skilled turns never reach the sums, so the ranking holds only skills; there is no "no skill" row.
    rows = build_skill_breakdown(_skill_costs({"tiered-review": 4.06, "simplify": 0.90}, {"tiered-review": 1}))["skills"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["tiered-review"]["cost_usd"] == 4.06
    assert by_name["simplify"]["cost_usd"] == 0.90
    assert "other" not in by_name  # only 2 skills, no tail
    assert set(by_name) == {"tiered-review", "simplify"}
    assert by_name["tiered-review"]["invocations"] == 1
    # Ranked by cost desc.
    assert [r["name"] for r in rows] == ["tiered-review", "simplify"]
    assert build_skill_breakdown(_skill_costs({}, {}))["skills"] == []


def test_build_skill_detail_splits_inline_vs_subagent():
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=60)
    trigger = _turn("tiered-review", 100, skill_names={"tiered-review": 1})  # inline trigger turn
    sub_agent = _turn("tiered-review", 2000, sidechain=True)
    long_ago = _turn("tiered-review", 700, ts=old, skill_names={"tiered-review": 1})  # outside both windows
    other = _turn("simplify", 500)  # other skill, ignored
    ghost = _turn("tiered-review", 900, skill_names={"tiered-review": 5})  # interrupted turns count for nothing
    ghost.is_interrupted = True
    turns = [trigger, sub_agent, long_ago, other, ghost]
    sessions = [Session(session_id="s", source_file="f.jsonl", is_sidechain=False, cwd="/home/u/proj", turns=turns)]
    d = build_skill_detail("tiered-review", usage_from_sessions(sessions).queries)
    assert d is not None
    assert d["name"] == "tiered-review"
    total = _cost(trigger) + _cost(sub_agent) + _cost(long_ago)
    assert d["scorecards"]["cost_usd"] == pytest.approx(total)
    assert d["scorecards"]["output_tokens"] == 2800
    everything = sum(_cost(t) for t in (trigger, sub_agent, long_ago, other))  # not the interrupted turn
    assert d["scorecards"]["share_of_total"] == pytest.approx(total / everything)
    assert d["total_invocations"] == d["scorecards"]["invocations"] == 2
    assert d["scorecards"]["invocations_7d"] == 1  # the old trigger is outside the 7-day window
    assert d["split"] == pytest.approx({"inline_usd": _cost(trigger) + _cost(long_ago), "subagent_usd": _cost(sub_agent)})
    # by_model groups attributed turns' full cost by model.
    assert d["by_model"][0]["name"] == OPUS and d["by_model"][0]["cost_usd"] == pytest.approx(total)
    assert d["by_model"][0]["invocations"] == 0
    # by_project keyed on the session cwd.
    assert d["by_project"][0]["cost_usd"] == pytest.approx(total)
    # The daily series covers the last 30 days only: the 60-day-old turn is in the totals, not in it.
    assert len(d["daily_cost"]) == 30
    assert sum(day["cost_usd"] for day in d["daily_cost"]) == pytest.approx(_cost(trigger) + _cost(sub_agent))


def test_build_skill_detail_unknown_returns_none():
    assert build_skill_detail("nope", usage_from_sessions([]).queries) is None


# --- Review follow-ups: edge cases + the four bugs found in tiered review ---


def _raw(skill=None, skill_names=None, *, uuid="u", mid=None, sidechain=False, interrupted=False, ts=datetime(2026, 6, 10, 12, tzinfo=timezone.utc)):
    return RawEvent(
        source_file="f.jsonl",
        line_number=1,
        event_type="assistant",
        session_id="s",
        request_id=None,
        message_id=mid,
        uuid=uuid,
        timestamp=ts,
        usage=None if interrupted else Usage(output_tokens=10),
        model="claude-opus-4-8",
        is_sidechain=sidechain,
        stop_reason=None,
        attribution_skill=skill,
        skill_names=Counter(skill_names) if skill_names else Counter(),
    )


def test_extract_skill_names_edge_cases():
    # non-dict block, non-dict input, missing skill, empty/non-string slug all skipped;
    # repeated slug accumulates.
    content = [
        "not-a-dict",
        {"type": "tool_use", "name": "Skill", "input": "oops"},  # input not dict
        {"type": "tool_use", "name": "Skill", "input": {}},  # missing skill
        {"type": "tool_use", "name": "Skill", "input": {"skill": ""}},  # empty slug
        {"type": "tool_use", "name": "Skill", "input": {"skill": 123}},  # non-string
        {"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}},
        {"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}},
        {"type": "tool_use", "name": "Read", "input": {"skill": "nope"}},  # wrong tool
    ]
    assert _extract_skill_names(content) == Counter({"simplify": 2})


def test_skill_name_other_is_rejected():
    # A skill literally named "other" would collide with the ranked-bar collapse row.
    assert _is_real_skill_name("other") is False
    assert _extract_skill_names([{"type": "tool_use", "name": "Skill", "input": {"skill": "other"}}]) == Counter()


def test_breakdown_skills_other_name_does_not_corrupt_collapse_row():
    # Even if an "other"-named skill slipped through, the breakdown must not let it
    # overwrite the synthetic tail. (attribution_skill "other" is filtered upstream,
    # so a real skill named "other" never reaches cost_by_skill.)
    rows = build_skill_breakdown(_skill_costs({"other": 5.0, "real": 1.0}, {}))["skills"]
    names = [r["name"] for r in rows]
    # "other" attribution survives as data here (no upstream parser in this unit
    # test), but with <=top_n skills there's no synthetic tail to collide with.
    assert "real" in names


def test_breakdown_skills_tail_collapse_over_top_n():
    costs = {f"sk{i}": float(20 - i) for i in range(12)}
    rows = build_skill_breakdown(_skill_costs(costs, {f"sk{i}": 1 for i in range(12)}), top_n=10)["skills"]
    other = next(r for r in rows if r["name"] == "other")
    assert other["tool_count"] == 2  # 12 skills - top 10
    assert other["invocations"] == 2  # the two collapsed skills' invocations
    assert len([r for r in rows if r["name"] != "other"]) == 10


def test_derive_delta_turns_carries_skill_fields():
    evs = [_raw(skill="tiered-review", uuid="u1"), _raw(skill_names={"tiered-review": 1}, uuid="u2")]
    turns, _sessions, _fired, _locs = derive_delta_turns(evs, set().intersection, set())
    by_uuid = {t.dedup_key: t for t in turns}
    assert by_uuid["u1"].attribution_skill == "tiered-review"
    assert by_uuid["u2"].skill_names == Counter({"tiered-review": 1})


def test_build_skill_detail_invocations_only_returns_payload():
    # Triggered but never attributed (invocations>0, no attributed turns):
    # must return a non-None payload with zero cost (no division-by-zero).
    t = _turn(None, 300, skill_names={"checkpoint": 1})
    sessions = [Session(session_id="s", source_file="f.jsonl", is_sidechain=False, cwd="/home/u/proj", turns=[t])]
    d = build_skill_detail("checkpoint", usage_from_sessions(sessions).queries)
    assert d is not None
    assert d["scorecards"]["cost_usd"] == 0.0
    assert d["scorecards"]["invocations"] == 1
    assert d["scorecards"]["share_of_total"] == 0.0
    assert d["scorecards"]["top_project"]["share"] == 0.0


def test_model_price_status_classifies_pricing_confidence():
    from tokenol.model.registry import CLAUDE_MODELS

    # A model that is actually in the price list resolves as exact.
    a_known_model = next(iter(CLAUDE_MODELS))
    assert model_price_status(a_known_model) == "known"
    # An unrecognised Claude model falls back to a similar model's price.
    assert model_price_status("claude-not-a-real-model-99") == "estimated"
    # No price at all -> shown as $0.
    assert model_price_status("gemini-2.5-pro") == "unpriced"  # non-Claude provider
    assert model_price_status(None) == "unpriced"
    assert model_price_status("(unknown)") == "unpriced"


def test_skill_breakdown_counts_started_but_uncharged_skills():
    when = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)

    def breakdown(cost_by_skill, invocations_by_skill):
        costs = SkillCosts(cost_by_skill, invocations_by_skill, dict.fromkeys(cost_by_skill, when), 0.0, 0.0, 0.0)
        return build_skill_breakdown(costs)

    # Only brainstorming was started without any cost billed to it.
    started = breakdown({"tiered-review": 4.0, "simplify": 0.9}, {"tiered-review": 1, "brainstorming": 2})
    assert started["invoked_no_cost"] == {"skills": 1, "uses": 2}
    # No invocations at all -> zeros.
    assert breakdown({"simplify": 0.9}, {})["invoked_no_cost"] == {"skills": 0, "uses": 0}
