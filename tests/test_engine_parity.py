"""Every engine aggregation answers what the Python function it replaces answers.

One shared fixture (the round-trip fixture's hostile strings, plus two models, two
projects with a nested cwd, a session without a cwd, interrupted turns that still
carry tool and skill tags; the engine holds Claude turns only) is loaded into one engine and
into Python ``Turn`` lists. Each case runs one engine method and the existing
function it replaces over the same selection, and compares to relative 1e-9
(SQL sums in a different order than Python).
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from test_engine_ingest import (
    BASE,
    HAIKU,
    HOSTILE_CWD,
    HOSTILE_SKILL,
    HOSTILE_TOOL,
    SONNET,
    _priced_turn,
)

from tokenol.engine import SessionInfo, UsageEngine
from tokenol.engine.filters import GroupKey, InterruptedPolicy, SearchField, TurnFilter
from tokenol.engine.queries import UsageQueries
from tokenol.enums import AttributionMode
from tokenol.ingest.parser import UNATTRIBUTED_TOOL, UNKNOWN_TOOL
from tokenol.metrics.cost import cache_saved_usd, cost_for_turn, rollup_by_date, rollup_by_hour
from tokenol.metrics.rollups import (
    build_model_rollups,
    build_skill_cost_daily,
    build_tool_cost_daily,
)
from tokenol.model.events import Session, Turn, Usage
from tokenol.serve.app import _bucket_turns
from tokenol.serve.state import (
    _accumulate_skill_costs,
    _accumulate_tool_costs,
    _build_series,
    _grouped_cwd_by_sid,
    _short_model,
    billable_token_totals,
    build_breakdown_tools,
    build_skill_breakdown,
    build_tool_detail,
)

REL = 1e-9
ABS = 1e-12  # sums that cancel to ~0 differ by rounding only
INCLUDE, EXCLUDE = InterruptedPolicy.INCLUDE, InterruptedPolicy.EXCLUDE
FIRST_DAY = BASE.date()
LAST_DAY = FIRST_DAY + timedelta(days=4)
SKILL = "brainstorm"
TOP_TOOLS = 10  # build_breakdown_tools collapses the tail past this many tools

SESSIONS = [
    ("sA", "/p/one", SONNET),
    ("sB", "/p/one/backend", HAIKU),  # rolls up into /p/one
    ("sC", "/p/two", SONNET),
    ("sD", None, HAIKU),
    ("s,1", HOSTILE_CWD, SONNET),
]
TOOL_SHARES = (
    {"Bash": 0.4, "Read": 0.2, "Skill": 0.1, UNKNOWN_TOOL: 0.05, HOSTILE_TOOL: 0.1},
    {"Bash": 0.1, "Edit": 0.3},  # Edit costs without a recorded call
)


def _build_turns() -> list[Turn]:
    turns: list[Turn] = []
    n = 0
    for day in range(5):
        for hour in (0, 13, 23):
            for sid, _cwd, model in SESSIONS:
                n += 1
                at = BASE.replace(hour=hour, minute=n % 60) + timedelta(days=day)
                fields: dict = {"is_sidechain": n % 4 == 0, "stop_reason": "end_turn", "model": model}
                usage = Usage(100 * n, 10 * n, 5_000 * (n % 7), 300 * (n % 5), 100 * (n % 3))
                if n % 3 == 0:
                    shares = TOOL_SHARES[n % 2]
                    fields.update(
                        out_shares=shares,
                        in_shares={k: v / 2 for k, v in shares.items()},
                        tool_use_count=3,
                        tool_error_count=n % 2,
                        tool_names=Counter({"Bash": 2, "Skill": 1, "Grep": 1} if n % 2 else {"Read": 1}),
                    )
                if n % 5 == 0:
                    fields.update(attribution_skill=SKILL if n % 2 else HOSTILE_SKILL)
                if n % 6 == 0:
                    fields.update(skill_names=Counter({SKILL: 1}))
                if n % 11 == 0:
                    # Interrupted. Real ones carry no tokens; these do, so a rollup that
                    # skips them and one that counts them give different answers.
                    usage = Usage(50, 5, 500, 50, 0)
                    fields.update(
                        is_interrupted=True,
                        tool_names=Counter({"Bash": 1}),
                        attribution_skill=SKILL,
                        skill_names=Counter({SKILL: 2}),
                    )
                turns.append(_priced_turn(f"k{n}", sid, at, usage=usage, **fields))
    return turns


class Fixture:
    def __init__(self) -> None:
        self.turns = _build_turns()
        infos = [SessionInfo(sid, f"/tmp/{i}.jsonl", cwd, False) for i, (sid, cwd, _m) in enumerate(SESSIONS)]
        self.sessions = [
            Session(i.session_id, i.source_file, i.is_sidechain, i.cwd, [t for t in self.turns if t.session_id == i.session_id])
            for i in infos
        ]
        self.cwd_by_sid = _grouped_cwd_by_sid(self.sessions)
        self.engine = UsageEngine.open(None)
        self.engine.add_turns(self.turns, infos)
        with self.engine.cursor() as cur:
            for sid, project in self.cwd_by_sid.items():
                cur.execute("UPDATE sessions SET project_cwd = ? WHERE session_id = ?", [project, sid])
        self.q = UsageQueries(self.engine)

    def pick(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        projects: tuple[str, ...] = (),
        models: tuple[str, ...] = (),
        session_id: str | None = None,
        policy: InterruptedPolicy = INCLUDE,
    ) -> list[Turn]:
        """The Python-side selection a ``TurnFilter`` with these arguments means."""
        return [
            t
            for t in self.turns
            if (since is None or t.timestamp.date() >= since)
            and (until is None or t.timestamp.date() <= until)
            and (not projects or self.cwd_by_sid[t.session_id] in projects)
            and (not models or t.model in models)
            and (session_id is None or t.session_id == session_id)
            and (policy is INCLUDE or not t.is_interrupted)
        ]


@pytest.fixture(scope="module")
def fx() -> Fixture:
    return Fixture()


def _close(got, want, path: str = "") -> None:
    """Equal, with floats compared at relative 1e-9."""
    if dataclasses.is_dataclass(want):
        assert type(got) is type(want), path
        for field in dataclasses.fields(want):
            _close(getattr(got, field.name), getattr(want, field.name), f"{path}.{field.name}")
    elif isinstance(want, dict):
        assert got.keys() == want.keys(), path
        for key in want:
            _close(got[key], want[key], f"{path}[{key!r}]")
    elif isinstance(want, (list, tuple)):
        assert len(got) == len(want), path
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _close(g, w, f"{path}[{i}]")
    elif isinstance(want, float) or isinstance(got, float):
        assert got == pytest.approx(want, rel=REL, abs=ABS), path
    else:
        assert got == want, path


def _series(fx: Fixture, project: str, model: str, turns: list[Turn], points_fn) -> dict[str, list]:
    return {s["label"]: s["points"] for s in _build_series(turns, fx.cwd_by_sid, project, model, points_fn)}


def _daily_by(fx: Fixture, key: GroupKey, fill_until: date):
    f = TurnFilter(until=fill_until)
    since = FIRST_DAY - timedelta(days=1)
    names = fx.q.top_keys_by_cost(TurnFilter(), key, 8)
    got = fx.q.daily_by(f, key, since, fill_until)
    project, model = ("compare", "all") if key is GroupKey.PROJECT else ("all", "compare")
    want = _series(fx, project, model, fx.turns, lambda sub: rollup_by_date(sub, since, fill_until))
    assert names == list(want)  # same top-8 order as the compare mode ranks
    return {name: got[name] for name in names}, want


def _hourly_by(fx: Fixture, key: GroupKey, day: date):
    got = fx.q.hourly_by(TurnFilter(), key, day, True)
    day_turns = fx.pick(since=day, until=day)
    project, model = ("compare", "all") if key is GroupKey.PROJECT else ("all", "compare")
    want = _series(fx, project, model, day_turns, lambda sub: rollup_by_hour(sub, day, True))
    return {name: got[name] for name in want}, want


def _bucket_want(fx: Fixture, key: GroupKey, since: date | None):
    key_fn = (
        (lambda s, _t: fx.cwd_by_sid.get(s.session_id, "(unknown)"))
        if key is GroupKey.PROJECT
        else (lambda _s, t: t.model or "(unknown)")
    )
    names = ("input", "output", "cache_read", "cache_creation", "input_cost", "output_cost",
             "cache_read_cost", "cache_creation_cost", "total_cost")
    want = {k: {n: v[n] for n in names} for k, v in _bucket_turns(fx.sessions, since, key_fn).items()}
    got = fx.q.bucket(TurnFilter(since=since, interrupted=EXCLUDE), key)
    return {k: {n: getattr(b, n) for n in names} for k, b in got.items()}, want


def _totals(fx: Fixture, since: date | None):
    turns = fx.pick(since=since)
    want = (
        len(turns),
        len({t.session_id for t in turns}),
        sum(t.usage.input_tokens for t in turns),
        sum(t.usage.output_tokens for t in turns),
        sum(t.usage.cache_read_input_tokens for t in turns),
        sum(t.usage.cache_creation_input_tokens for t in turns),
        sum(t.cost_usd for t in turns),
        cache_saved_usd(turns),
    )
    return dataclasses.astuple(fx.q.totals(TurnFilter(since=since))), want


def _components(fx: Fixture):
    want: dict[date, tuple[float, float, float, float]] = {}
    for t in fx.pick(policy=EXCLUDE):
        tc = cost_for_turn(t.model, t.usage, at=t.timestamp)
        old = want.get(t.timestamp.date(), (0.0, 0.0, 0.0, 0.0))
        want[t.timestamp.date()] = (
            old[0] + tc.input_usd, old[1] + tc.output_usd, old[2] + tc.cache_creation_usd, old[3] + tc.cache_read_usd,
        )
    got = fx.q.cost_components_by_date(TurnFilter(interrupted=EXCLUDE))
    return {d: dataclasses.astuple(c) for d, c in got.items()}, want


def _breakdown_tools(fx: Fixture, mode: AttributionMode, since: date | None):
    f = TurnFilter(since=since, interrupted=EXCLUDE)
    rows = build_breakdown_tools(fx.pick(since=since, policy=EXCLUDE), mode=mode.value)
    assert len(rows) <= TOP_TOOLS, "fixture must stay below the top-10 cut"
    got = fx.q.tool_costs(f, mode)
    want = {r["name"]: (r["cost_usd"], r.get("count"), r.get("last_active")) for r in rows[:-1]}
    mine = {
        name: (cost, got.calls_by_tool.get(name), got.last_active[name].isoformat() if name in got.last_active else None)
        for name, cost in got.cost_by_tool.items()
    }
    mine[UNATTRIBUTED_TOOL] = (got.unattributed_usd, None, None)
    want[UNATTRIBUTED_TOOL] = (rows[-1]["cost_usd"], None, None)
    return mine, want


def _tool_accumulate(fx: Fixture):
    cost, invs, last = _accumulate_tool_costs(fx.turns)
    got = fx.q.tool_costs(TurnFilter(interrupted=INCLUDE), AttributionMode.PRORATA)
    mine_cost = dict(got.cost_by_tool)
    if got.unknown_usd:
        mine_cost[UNATTRIBUTED_TOOL] = got.unknown_usd
    return (mine_cost, got.calls_by_tool, got.last_seen), (dict(cost), dict(invs), last)


def _tool_detail(fx: Fixture, tool: str):
    cutoff = BASE + timedelta(days=3)
    f = TurnFilter(interrupted=EXCLUDE)
    payload = build_tool_detail(tool, fx.pick(), fx.sessions)
    rows = fx.q.tool_detail_rows(f, tool, recent_since=cutoff)
    assert payload is not None and rows
    by_project: dict[str, tuple[float, int, str]] = {}
    by_model: dict[str, tuple[float, int]] = {}
    for r in rows:
        cost, invs, last = by_project.get(r.project, (0.0, 0, ""))
        by_project[r.project] = (cost + r.cost_usd, invs + r.invocations, max(last, r.last_active.isoformat()))
        cost, invs = by_model.get(r.model, (0.0, 0))
        by_model[r.model] = (cost + r.cost_usd, invs + r.invocations)
    mine = {
        "total_cost": sum(r.cost_usd for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "invocations": sum(r.invocations for r in rows),
        "by_project": {p.rsplit("/", 1)[-1] if p != "(unknown)" else p: v for p, v in by_project.items()},
        "by_model": by_model,
        "recent": sum(r.recent_invocations for r in rows),
    }
    sc = payload["scorecards"]
    want = {
        "total_cost": sc["cost_usd"],
        "output_tokens": sc["output_tokens"],
        "invocations": sc["invocations"],
        "by_project": {r["project_label"]: (r["cost_usd"], r["invocations"], r["last_active"]) for r in payload["by_project"]},
        "by_model": {r["name"]: (r["cost_usd"], r["invocations"]) for r in payload["by_model"]},
        "recent": sum(t.tool_names.get(tool, 0) for t in fx.pick(policy=EXCLUDE) if t.timestamp >= cutoff),
    }
    return mine, want


def _skills(fx: Fixture, since: date | None):
    f = TurnFilter(since=since, interrupted=EXCLUDE)
    turns = fx.pick(since=since, policy=EXCLUDE)
    cost, invs, last = _accumulate_skill_costs(turns)
    breakdown = build_skill_breakdown(turns)
    got = fx.q.skill_costs(f)
    mine = (got.cost_by_skill, got.invocations_by_skill, got.last_active,
            got.total_cost, got.total_billable_tokens, got.skill_billable_tokens)
    want = (dict(cost), dict(invs), last, breakdown["total_cost"],
            breakdown["total_billable_tokens"], breakdown["skill_billable_tokens"])
    return mine, want


# Frozen from the Python `_build_recent_activity` before it was converted to read the engine
# (now = day 2 at 13:30 UTC, window 60 min; the window has no upper bound, so it reaches days 3-4):
# project -> (latest session, last turn at, turns, primary model, visible tokens).
_RECENT_WANT = {
    "(unknown)": ("sD", "2026-05-05T23:14:00+00:00", 8, "haiku-4-5", 28600),
    HOSTILE_CWD: ("s,1", "2026-05-05T23:15:00+00:00", 8, "sonnet-4-6", 32500),
    "/p/one": ("sB", "2026-05-05T23:12:00+00:00", 16, "sonnet-4-6", 17800),
    "/p/two": ("sC", "2026-05-05T23:13:00+00:00", 8, "sonnet-4-6", 23200),
}


def _recent(fx: Fixture):
    # Off midnight: a bound truncated to its date would keep day 2's earlier hours and change the answer.
    now = datetime.combine(FIRST_DAY + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=13, minutes=30
    )
    got = {r.project: r for r in fx.q.latest_turn_per_project(TurnFilter(since_ts=now - timedelta(minutes=60)))}
    mine = {
        p: (r.session_id, r.timestamp.isoformat(), r.turns, _short_model(r.primary_model), r.pool)
        for p, r in got.items()
    }
    return mine, _RECENT_WANT


def _cases(fx: Fixture) -> dict[str, Callable[[], tuple]]:
    """Each case builds (engine answer, Python answer) when called, so a failure names the case."""
    mid = FIRST_DAY + timedelta(days=2)
    fill_until = LAST_DAY + timedelta(days=2)
    all_f = TurnFilter()
    return {
        "daily-filled": lambda: (
            fx.q.daily(all_f, FIRST_DAY - timedelta(days=2), fill_until),
            rollup_by_date(fx.turns, FIRST_DAY - timedelta(days=2), fill_until),
        ),
        "daily-unfilled": lambda: (fx.q.daily(all_f), rollup_by_date(fx.turns)),
        "daily-since-until": lambda: (
            fx.q.daily(TurnFilter(since=mid, until=LAST_DAY), mid, LAST_DAY),
            rollup_by_date(fx.turns, mid, LAST_DAY),
        ),
        "daily-project-model-session": lambda: (
            fx.q.daily(TurnFilter(projects=("/p/one",), models=(HAIKU,))),
            rollup_by_date(fx.pick(projects=("/p/one",), models=(HAIKU,))),
        ),
        "daily-one-session": lambda: (
            fx.q.daily(TurnFilter(session_id="s,1")),
            rollup_by_date(fx.pick(session_id="s,1")),
        ),
        "daily-by-project": lambda: _daily_by(fx, GroupKey.PROJECT, fill_until),
        "daily-by-model": lambda: _daily_by(fx, GroupKey.MODEL, fill_until),
        "hourly-filled": lambda: (
            fx.q.hourly(all_f, mid, True),
            rollup_by_hour(fx.pick(since=mid, until=mid), mid, True),
        ),
        "hourly-unfilled-all-days": lambda: (fx.q.hourly(all_f), rollup_by_hour(fx.turns)),
        "hourly-by-project": lambda: _hourly_by(fx, GroupKey.PROJECT, mid),
        "hourly-by-model": lambda: _hourly_by(fx, GroupKey.MODEL, mid),
        "model-rollups": lambda: (fx.q.model_rollups(all_f), build_model_rollups(fx.turns)),
        "model-rollups-since": lambda: (
            fx.q.model_rollups(TurnFilter(since=mid)),
            build_model_rollups(fx.pick(since=mid)),
        ),
        "bucket-project": lambda: _bucket_want(fx, GroupKey.PROJECT, None),
        "bucket-model-since": lambda: _bucket_want(fx, GroupKey.MODEL, mid),
        "totals": lambda: _totals(fx, None),
        "totals-since": lambda: _totals(fx, mid),
        "cost-components-by-date": lambda: _components(fx),
        "tools-prorata": lambda: _breakdown_tools(fx, AttributionMode.PRORATA, None),
        "tools-excl-cache-read": lambda: _breakdown_tools(fx, AttributionMode.EXCL_CACHE_READ, None),
        "tools-prorata-since": lambda: _breakdown_tools(fx, AttributionMode.PRORATA, mid),
        "tools-accumulate-includes-interrupted": lambda: _tool_accumulate(fx),
        "tool-detail-bash": lambda: _tool_detail(fx, "Bash"),
        "tool-detail-no-cost-entry": lambda: _tool_detail(fx, "Grep"),
        "tool-detail-no-call": lambda: _tool_detail(fx, "Edit"),
        "tool-cost-daily": lambda: (
            fx.q.tool_cost_daily(TurnFilter(interrupted=EXCLUDE), "Bash", today=LAST_DAY),
            build_tool_cost_daily(fx.pick(), tool_name="Bash", today=LAST_DAY),
        ),
        "skills": lambda: _skills(fx, None),
        "skills-since": lambda: _skills(fx, mid),
        "skill-cost-daily": lambda: (
            fx.q.skill_cost_daily(TurnFilter(interrupted=EXCLUDE), SKILL, today=LAST_DAY),
            build_skill_cost_daily(fx.pick(), skill_name=SKILL, today=LAST_DAY),
        ),
        "billable-totals": lambda: (
            fx.q.billable_totals(TurnFilter(interrupted=EXCLUDE)),
            billable_token_totals(fx.turns),
        ),
        "billable-totals-since": lambda: (
            fx.q.billable_totals(TurnFilter(since=mid, interrupted=EXCLUDE)),
            billable_token_totals(fx.pick(since=mid)),
        ),
        "latest-turn-per-project": lambda: _recent(fx),
        "distinct-models": lambda: (
            fx.q.distinct_values(SearchField.MODEL, all_f),
            sorted({t.model for t in fx.turns if t.model}),
        ),
        "distinct-dates": lambda: (
            fx.q.distinct_values(SearchField.DATE, all_f),
            sorted({str(t.timestamp.date()) for t in fx.turns}),
        ),
        "earliest-date": lambda: (fx.q.earliest_date(all_f), min(t.timestamp.date() for t in fx.turns)),
        "earliest-date-empty": lambda: (fx.q.earliest_date(TurnFilter(since=date(2030, 1, 1))), None),
    }


@pytest.mark.parametrize("name", list(_cases(None)))
def test_engine_aggregation_equals_its_python_reference(fx: Fixture, name: str) -> None:
    got, want = _cases(fx)[name]()
    _close(got, want, name)
