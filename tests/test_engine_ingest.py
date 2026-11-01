"""Turns come back from the usage engine as they went in, whichever way they got there.

One fixture of turns with hostile strings (comma, double quote, newline,
non-ASCII in cwd, model-adjacent names, tool and skill names, dedup keys),
interrupted turns, empty and non-empty tool and skill maps, and passthrough keys,
loaded two ways: Python turns through CSV staging, and a store file attached
read-only (after which a writer can open that file in the same process). Each must return field-for-field equal,
time-ordered turns; ``known_keys`` must answer membership; ``forget`` must remove
exactly the right rows for each of its four arguments.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from tokenol.engine import SessionInfo, UsageEngine
from tokenol.engine.sessions import SessionIndex, SessionRollupCache
from tokenol.enums import AssumptionTag
from tokenol.ingest.parser import _attribute_cost
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import (
    EMPTY_ASSUMPTIONS,
    EMPTY_SKILL_NAMES,
    EMPTY_TOOL_COSTS,
    EMPTY_TOOL_NAMES,
    Session,
    Turn,
    Usage,
)
from tokenol.persistence.store import HistoryStore

REL = 1e-12
BASE = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
HOSTILE_CWD = '/home/ünï/"quo,te"\nnext line'
HOSTILE_TOOL = 'mcp__sé,rver__"do\nit"'
HOSTILE_SKILL = 'skill,"é"\nx'
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"  # sorts before SONNET: a position-based model id would shift


def _priced_turn(key: str, sid: str, at: datetime, **fields) -> Turn:
    """A turn priced and attributed exactly as the parser would."""
    usage = fields.pop("usage")
    model = fields.pop("model", SONNET)
    out_shares = fields.pop("out_shares", {})
    in_shares = fields.pop("in_shares", {})
    cost = cost_for_turn(model, usage, at=at)
    tools, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        model, usage, out_shares, in_shares, cost, at
    )
    return Turn(
        dedup_key=key,
        timestamp=at,
        session_id=sid,
        model=model,
        usage=usage,
        cost_usd=cost.total_usd,
        tool_costs=dict(tools) if tools else EMPTY_TOOL_COSTS,
        unattributed_input_tokens=unattr_in,
        unattributed_output_tokens=unattr_out,
        unattributed_cost_usd=unattr_cost,
        **fields,
    )


def _fixture() -> tuple[list[Turn], list[SessionInfo]]:
    plain = Usage(1200, 300, 40_000, 5_000, 2_000)
    worked = _priced_turn(
        'msg,"1"\nx:req_é', "s,1", BASE + timedelta(minutes=5, microseconds=123456),
        usage=plain,
        is_sidechain=False,
        stop_reason="tool_use",
        assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
        tool_use_count=3,
        tool_error_count=1,
        tool_names=Counter({"Bash": 2, HOSTILE_TOOL: 1}),
        out_shares={"Bash": 0.5, HOSTILE_TOOL: 0.3},
        in_shares={"Bash": 0.2, HOSTILE_TOOL: 0.1},
        attribution_skill=HOSTILE_SKILL,
        skill_names=Counter({HOSTILE_SKILL: 1}),
    )
    interrupted = _priced_turn(
        "msg_2:req_2", "s,1", BASE + timedelta(minutes=9),
        usage=Usage(),
        is_sidechain=False,
        stop_reason=None,
        is_interrupted=True,
        assumptions=[AssumptionTag.INTERRUPTED_TURN_SKIPPED],
    )
    first = _priced_turn(
        "msg_0:req_0", "s,1", BASE,
        usage=Usage(10, 20, 0, 0, 0),
        is_sidechain=False,
        stop_reason="end_turn",
        assumptions=EMPTY_ASSUMPTIONS,
        tool_names=EMPTY_TOOL_NAMES,
        skill_names=EMPTY_SKILL_NAMES,
    )
    passthrough = _priced_turn(
        "passthrough:/tmp/x.jsonl:12", "s2", BASE + timedelta(hours=1),
        usage=Usage(500, 100, 0, 0, 0),
        is_sidechain=True,
        stop_reason="end_turn",
        assumptions=[AssumptionTag.DEDUP_PASSTHROUGH],
    )
    # Deliberately not in time order: the engine, not the caller, orders a session.
    turns = [worked, interrupted, first, passthrough]
    sessions = [
        SessionInfo("s,1", "/tmp/a,b.jsonl", HOSTILE_CWD, False),
        SessionInfo("s2", "/tmp/c.jsonl", "/p/two", True),
    ]
    return turns, sessions


def _assert_same(expected: Turn, actual: Turn) -> None:
    for field in dataclasses.fields(Turn):
        want, got = getattr(expected, field.name), getattr(actual, field.name)
        if field.name == "tool_costs":
            assert got.keys() == want.keys()
            for name in want:
                assert got[name].tool_name == name
                for part in ("input_tokens", "output_tokens", "cost_usd"):
                    assert getattr(got[name], part) == pytest.approx(getattr(want[name], part), rel=REL)
        elif isinstance(want, float):
            assert got == pytest.approx(want, rel=REL, abs=1e-15), field.name
        else:
            assert got == want, field.name
    # Empty maps are the shared sentinels, as store hydration returns them.
    for name, sentinel in (
        ("tool_names", EMPTY_TOOL_NAMES),
        ("skill_names", EMPTY_SKILL_NAMES),
        ("tool_costs", EMPTY_TOOL_COSTS),
        ("assumptions", EMPTY_ASSUMPTIONS),
    ):
        if not getattr(expected, name):
            assert getattr(actual, name) is sentinel, name


def _engine(mode: str, tmp_path: Path, turns: list[Turn], sessions: list[SessionInfo]) -> UsageEngine:
    if mode == "csv":
        engine = UsageEngine.open(None)
        assert engine.add_turns(turns, sessions) == len(turns)
        return engine
    path = tmp_path / "h.duckdb"
    store = HistoryStore(path)
    store.flush(turns, [Session(s.session_id, s.source_file, s.is_sidechain, s.cwd) for s in sessions])
    store.close()
    engine = UsageEngine.open(path)
    # The engine detached the file: a writer opens it read-write in this process and can flush.
    writer = HistoryStore(path)
    writer.flush([_priced_turn("late:1", "s2", BASE + timedelta(days=9), usage=Usage(1, 1, 0, 0, 0),
                               is_sidechain=True, stop_reason=None)], [])
    assert not writer.is_empty()
    writer.close()
    return engine


def _turn_count(engine: UsageEngine) -> int:
    with engine.cursor() as cur:
        return cur.execute("SELECT count(*) FROM turns").fetchone()[0]


def _orphans(engine: UsageEngine) -> int:
    with engine.cursor() as cur:
        return sum(
            cur.execute(
                f"SELECT count(*) FROM {t} WHERE turn_id NOT IN (SELECT turn_id FROM turns)"
            ).fetchone()[0]
            for t in ("turn_tools", "tool_calls", "skill_calls")
        )


@pytest.mark.parametrize("mode", ["csv", "store"])
def test_engine_round_trip_known_keys_and_forget(mode: str, tmp_path: Path) -> None:
    turns, sessions = _fixture()
    engine = _engine(mode, tmp_path, turns, sessions)

    # Round trip: field-for-field equal and time-ordered.
    for sid in ("s,1", "s2"):
        want = sorted((t for t in turns if t.session_id == sid), key=lambda t: t.timestamp)
        got = engine.session_turns(sid)
        assert len(got) == len(want)
        for expected, actual in zip(want, got, strict=True):
            _assert_same(expected, actual)
        assert engine.session_stats(sid) == (len(want), want[-1].timestamp)
    assert engine.session_turns("unknown") == []
    assert engine.session_stats("unknown") == (0, None)

    # Session rows survive hostile strings; project_cwd starts equal to cwd.
    with engine.cursor() as cur:
        rows = cur.execute(
            "SELECT session_id, source_file, cwd, project_cwd, is_sidechain FROM sessions "
            "ORDER BY session_id"
        ).fetchall()
    assert rows == [
        (s.session_id, s.source_file, s.cwd, s.cwd, s.is_sidechain)
        for s in sorted(sessions, key=lambda s: s.session_id)
    ]

    # Membership.
    keys = {t.dedup_key for t in turns}
    assert engine.known_keys([*keys, "absent", ""]) == keys
    assert engine.known_keys([]) == set()

    # A second batch: held keys are skipped, and a model that sorts before every
    # known one must not disturb the ids (and so the prices) of the turns already in.
    newer = _priced_turn(
        "msg_h:req_h", "s3", BASE + timedelta(days=1),
        usage=Usage(70, 30, 10, 0, 0), model=HAIKU, is_sidechain=False, stop_reason="end_turn",
    )
    # An empty cwd is not a missing one; both must survive the CSV file.
    batch_sessions = [SessionInfo("s3", "/tmp/s3.jsonl", "", False), SessionInfo("s4", "", None, False)]
    assert engine.add_turns([*turns, newer, newer], batch_sessions) == 1
    with engine.cursor() as cur:
        assert cur.execute(
            "SELECT session_id, source_file, cwd FROM sessions WHERE session_id IN ('s3','s4') ORDER BY 1"
        ).fetchall() == [("s3", "/tmp/s3.jsonl", ""), ("s4", "", None)]
    _assert_same(newer, engine.session_turns("s3")[0])
    for sid in ("s,1", "s2"):
        want = sorted((t for t in turns if t.session_id == sid), key=lambda t: t.timestamp)
        for expected, actual in zip(want, engine.session_turns(sid), strict=True):
            _assert_same(expected, actual)
    assert engine.known_keys(["msg_h:req_h"]) == {"msg_h:req_h"}

    # forget: exactly one argument, and each one removes exactly its rows.
    with pytest.raises(ValueError):
        engine.forget()
    with pytest.raises(ValueError):
        engine.forget(all=True, cwd="/p/two")
    first_cut = BASE + timedelta(minutes=7)  # drops two of s,1's three turns

    def fresh():
        e = UsageEngine.open(None)
        e.add_turns([*turns, newer], [*sessions, SessionInfo("s3", "/tmp/s3.jsonl", None, False)])
        with e.cursor() as cur:  # a grouped project value that differs from the stored cwd
            cur.execute("UPDATE sessions SET project_cwd = '/p/two' WHERE session_id = 's,1'")
        return e

    survivors = {
        "session_ids": (dict(session_ids=["s2", "nope"]), 1, {"s,1": 3, "s2": 0, "s3": 1}),
        # cwd is the stored column: s,1's grouped project_cwd is also /p/two and must not match
        "cwd": (dict(cwd="/p/two"), 1, {"s,1": 3, "s2": 0, "s3": 1}),
        "older_than": (dict(older_than=first_cut), 2, {"s,1": 1, "s2": 1, "s3": 1}),
        "all": (dict(all=True), 5, {"s,1": 0, "s2": 0, "s3": 0}),
    }
    for name, (kwargs, dropped, left) in survivors.items():
        e = fresh()
        assert e.forget(**kwargs) == dropped, name
        for sid, count in left.items():
            assert e.session_stats(sid)[0] == count, (name, sid)
        assert _orphans(e) == 0, name
        e.close()
    # older_than drops a session once none of its turns remain.
    e = fresh()
    e.forget(older_than=BASE + timedelta(hours=2))
    assert e.session_turns("s2") == [] and _turn_count(e) == 1
    with e.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM sessions WHERE session_id IN ('s,1','s2')").fetchone()[0] == 0
    e.close()

    engine.close()

    _check_project_cwd_and_rollup_cache(turns, newer)


def _check_project_cwd_and_rollup_cache(turns: list[Turn], newer: Turn) -> None:
    """Project grouping is mirrored into the engine; a cached rollup lives until invalidated."""
    home = "/home/ff235"
    sessions = [
        SessionInfo("s,1", "/tmp/a.jsonl", "/p/two/deep", False),  # nested: groups under /p/two
        SessionInfo("s2", "/tmp/c.jsonl", "/p/two", True),
        SessionInfo("s3", "/tmp/s3.jsonl", home, False),  # a container is its own project ...
        SessionInfo("s5", "/tmp/s5.jsonl", f"{home}/proj", False),  # ... and nobody's ancestor
    ]
    engine = UsageEngine.open(None)
    engine.add_turns([*turns, newer], sessions)
    index = SessionIndex(engine)
    index.add(sessions)
    expected = {"s,1": "/p/two", "s2": "/p/two", "s3": home, "s5": f"{home}/proj"}
    assert index.project_map() == expected
    with engine.cursor() as cur:
        stored = dict(cur.execute("SELECT session_id, project_cwd FROM sessions").fetchall())
    assert stored == expected
    assert [s.session_id for s in index.all()[:3]] == ["s,1", "s2", "s3"]  # first-turn order

    cache = SessionRollupCache(engine, index)
    first = cache.rollup("s2")
    assert first.turns == 1
    late = _priced_turn("late:2", "s2", BASE + timedelta(days=2), usage=Usage(5, 5, 0, 0, 0),
                        is_sidechain=True, stop_reason=None)
    engine.add_turns([late], [])
    assert cache.rollup("s2") is first  # reused until a tick says otherwise
    cache.invalidate(["s2"])
    assert cache.rollup("s2").turns == 2
    assert [r.session_id for r in cache.rollups(["s3", "s2"])] == ["s3", "s2"]
    engine.close()
