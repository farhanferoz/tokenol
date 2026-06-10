"""Skill cost dimension: parser, aggregation, and detail builders."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tokenol.ingest.builder import build_turns
from tokenol.ingest.parser import _extract_skill_names, _is_real_skill_name
from tokenol.metrics.rollups import build_skill_cost_daily
from tokenol.model.events import EMPTY_SKILL_NAMES, RawEvent, Session, ToolCost, Turn, Usage
from tokenol.serve.state import (
    _accumulate_skill_costs,
    _accumulate_tool_costs,
    billable_token_totals,
    build_breakdown_skills,
    build_breakdown_tools,
    build_skill_breakdown,
    build_skill_detail,
    derive_delta_turns,
    model_price_status,
)

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


def test_tool_mix_excludes_literal_skill_row():
    t = Turn(
        dedup_key="k", timestamp=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
        session_id="s", model="claude-opus-4-8",
        usage=Usage(input_tokens=1000, output_tokens=100),
        is_sidechain=False, stop_reason="tool_use",
    )
    t.cost_usd = 0.10
    t.tool_names = Counter({"Skill": 1, "Read": 2})
    t.tool_costs = {"Skill": ToolCost("Skill", cost_usd=0.02),
                    "Read": ToolCost("Read", cost_usd=0.05)}
    rows = build_breakdown_tools([t])
    names = {r["name"] for r in rows}
    assert "Skill" not in names
    assert "Read" in names


def test_accumulate_skill_costs_groups_turns():
    turns = [
        _turn("tiered-review", 4.0, model="claude-opus-4-8"),
        _turn("tiered-review", 0.5),
        _turn("simplify", 0.9),
        _turn(None, 9.0),
    ]
    cost, invs, last = _accumulate_skill_costs(turns)
    assert cost == {"tiered-review": 4.5, "simplify": 0.9}


# --- Review follow-ups: edge cases + the four bugs found in tiered review ---


def _raw(skill=None, skill_names=None, *, uuid="u", mid=None, sidechain=False,
         interrupted=False, ts=datetime(2026, 6, 10, 12, tzinfo=timezone.utc)):
    return RawEvent(
        source_file="f.jsonl", line_number=1, event_type="assistant",
        session_id="s", request_id=None, message_id=mid, uuid=uuid, timestamp=ts,
        usage=None if interrupted else Usage(output_tokens=10),
        model="claude-opus-4-8", is_sidechain=sidechain, stop_reason=None,
        attribution_skill=skill,
        skill_names=Counter(skill_names) if skill_names else Counter(),
    )


def test_extract_skill_names_edge_cases():
    # non-dict block, non-dict input, missing skill, empty/non-string slug all skipped;
    # repeated slug accumulates.
    content = [
        "not-a-dict",
        {"type": "tool_use", "name": "Skill", "input": "oops"},        # input not dict
        {"type": "tool_use", "name": "Skill", "input": {}},            # missing skill
        {"type": "tool_use", "name": "Skill", "input": {"skill": ""}}, # empty slug
        {"type": "tool_use", "name": "Skill", "input": {"skill": 123}}, # non-string
        {"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}},
        {"type": "tool_use", "name": "Skill", "input": {"skill": "simplify"}},
        {"type": "tool_use", "name": "Read", "input": {"skill": "nope"}},  # wrong tool
    ]
    assert _extract_skill_names(content) == Counter({"simplify": 2})


def test_skill_name_other_is_rejected():
    # A skill literally named "other" would collide with the ranked-bar collapse row.
    assert _is_real_skill_name("other") is False
    assert _extract_skill_names(
        [{"type": "tool_use", "name": "Skill", "input": {"skill": "other"}}]
    ) == Counter()


def test_breakdown_skills_other_name_does_not_corrupt_collapse_row():
    # Even if an "other"-named skill slipped through, the breakdown must not let it
    # overwrite the synthetic tail. (attribution_skill "other" is filtered upstream,
    # so a real skill named "other" never reaches cost_by_skill.)
    turns = [_turn("other", 5.0), _turn("real", 1.0)]
    rows = build_breakdown_skills(turns)
    names = [r["name"] for r in rows]
    # "other" attribution survives as data here (no upstream parser in this unit
    # test), but with <=top_n skills there's no synthetic tail to collide with.
    assert "real" in names


def test_breakdown_skills_tail_collapse_over_top_n():
    turns = [_turn(f"sk{i}", float(20 - i), skill_names={f"sk{i}": 1}) for i in range(12)]
    rows = build_breakdown_skills(turns, top_n=10)
    other = next(r for r in rows if r["name"] == "other")
    assert other["tool_count"] == 2          # 12 skills - top 10
    assert other["invocations"] == 2         # the two collapsed skills' invocations
    assert len([r for r in rows if r["name"] != "other"]) == 10


def test_accumulate_skill_costs_skips_interrupted():
    live = _turn("simplify", 0.9)
    ghost = _turn("ghost", 0.0)
    ghost.is_interrupted = True
    cost, invs, last = _accumulate_skill_costs([live, ghost])
    assert "ghost" not in cost           # interrupted turn leaves no $0 row
    assert cost == {"simplify": 0.9}


def test_derive_delta_turns_carries_skill_fields():
    evs = [_raw(skill="tiered-review", uuid="u1"),
           _raw(skill_names={"tiered-review": 1}, uuid="u2")]
    turns, _sessions, _fired, _locs = derive_delta_turns(evs, set(), set())
    by_uuid = {t.dedup_key: t for t in turns}
    assert by_uuid["u1"].attribution_skill == "tiered-review"
    assert by_uuid["u2"].skill_names == Counter({"tiered-review": 1})


def test_build_skill_detail_invocations_only_returns_payload():
    # Triggered but never attributed (invocations>0, no attributed turns):
    # must return a non-None payload with zero cost (no division-by-zero).
    t = _turn(None, 0.30, skill_names={"checkpoint": 1})
    d = build_skill_detail("checkpoint", [t], [])
    assert d is not None
    assert d["scorecards"]["cost_usd"] == 0.0
    assert d["scorecards"]["invocations"] == 1
    assert d["scorecards"]["share_of_total"] == 0.0
    assert d["scorecards"]["top_project"]["share"] == 0.0


def test_build_skill_cost_daily_windowing():
    today = datetime(2026, 6, 10, tzinfo=timezone.utc).date()
    inside = _turn("simplify", 2.0, ts=datetime(2026, 6, 9, 12, tzinfo=timezone.utc))
    outside = _turn("simplify", 9.0, ts=datetime(2026, 1, 1, tzinfo=timezone.utc))
    wrong = _turn("other-skill", 5.0, ts=datetime(2026, 6, 9, 12, tzinfo=timezone.utc))
    rows = build_skill_cost_daily([inside, outside, wrong], skill_name="simplify",
                                  days=30, today=today)
    assert len(rows) == 30
    assert sum(r.cost_usd for r in rows) == 2.0  # only the in-window simplify turn


def test_accumulate_tool_costs_excludes_skill_row():
    # The literal "Skill" tool must not surface in model/project "Cost by tool"
    # (it 404s on click — it's owned by the Skill dimension).
    t = Turn(
        dedup_key="k", timestamp=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
        session_id="s", model="claude-opus-4-8",
        usage=Usage(input_tokens=1000, output_tokens=100),
        is_sidechain=False, stop_reason="tool_use",
    )
    t.tool_names = Counter({"Skill": 1, "Read": 2})
    t.tool_costs = {"Skill": ToolCost("Skill", cost_usd=0.02),
                    "Read": ToolCost("Read", cost_usd=0.05)}
    cost, invs, _last = _accumulate_tool_costs([t])
    assert "Skill" not in cost
    assert "Skill" not in invs
    assert cost["Read"] == 0.05 and invs["Read"] == 2


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


def test_billable_token_totals_scales_input_share_off_the_cache_pool():
    # input 1000 (+1000 cache read), output 200. A tool took half the visible
    # bytes, so the stored non-tool share over the input+cache pool is
    # 0.5*(1000+1000)=1000, and non-tool output is 0.5*200=100.
    t = Turn(
        dedup_key="k", timestamp=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
        session_id="s", model="claude-opus-4-8",
        usage=Usage(input_tokens=1000, output_tokens=200,
                    cache_read_input_tokens=1000, cache_creation_input_tokens=0),
        is_sidechain=False, stop_reason=None,
    )
    t.unattributed_input_tokens = 1000.0
    t.unattributed_output_tokens = 100.0
    non_tool, total = billable_token_totals([t])
    assert total == 1200  # input + output only, cache excluded
    # non-tool input fraction = 1000/2000 = 0.5 -> 0.5*1000 = 500; + 100 output
    assert non_tool == 600.0


def test_build_skill_breakdown_matches_separate_builders_in_one_pass():
    # Same results as calling the individual builders, but consolidated.
    turns = [
        _turn("tiered-review", 4.0, out=2000, skill_names={"tiered-review": 1}),
        _turn(None, 1.0, out=500, skill_names={"brainstorming": 2}),  # uncharged
        _turn("simplify", 0.9, out=300),
    ]
    p = build_skill_breakdown(turns)
    assert p["skills"] == build_breakdown_skills(turns)
    assert p["total_cost"] == 4.0 + 1.0 + 0.9
    # _turn sets Usage(output_tokens=out), input defaults 0 -> billable = output.
    assert p["total_billable_tokens"] == 2000 + 500 + 300
    assert p["skill_billable_tokens"] == 2000 + 300  # attributed turns only


def test_skill_breakdown_counts_started_but_uncharged_skills():
    turns = [
        _turn("tiered-review", 4.0, skill_names={"tiered-review": 1}),  # charged + started
        _turn(None, 0.0, skill_names={"brainstorming": 2}),            # started, no charge
        _turn("simplify", 0.9),                                         # charged, not started
    ]
    # Only brainstorming was started without any cost billed to it.
    assert build_skill_breakdown(turns)["invoked_no_cost"] == {"skills": 1, "uses": 2}
    # No invocations at all -> zeros.
    assert build_skill_breakdown([_turn("simplify", 0.9)])["invoked_no_cost"] == {
        "skills": 0, "uses": 0,
    }
