"""Every engine aggregation answers what the Python function it replaces answers.

One shared fixture (the round-trip fixture's hostile strings, plus two models, two
projects with a nested cwd, a session without a cwd, interrupted turns that still
carry tool and skill tags; the engine holds Claude turns only) is loaded into one engine and
into Python ``Turn`` lists. Each case runs one engine method and the existing
function it replaces over the same selection, and compares to relative 1e-9
(SQL sums in a different order than Python). A reference the serve layer no longer has
is a literal frozen from its last green run (the ``_*_WANT`` constants).
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
from tokenol.engine.queries import SkillDetail, TopTurn, UsageQueries
from tokenol.engine.sessions import grouped_project_cwds
from tokenol.enums import AttributionMode
from tokenol.ingest.parser import UNATTRIBUTED_TOOL, UNKNOWN_TOOL
from tokenol.metrics.cost import cache_saved_usd, cost_for_turn, rollup_by_date, rollup_by_hour
from tokenol.metrics.rollups import build_model_rollups
from tokenol.model.events import Session, Turn, Usage
from tokenol.serve.state import _short_model

REL = 1e-9
ABS = 1e-12  # sums that cancel to ~0 differ by rounding only
INCLUDE, EXCLUDE = InterruptedPolicy.INCLUDE, InterruptedPolicy.EXCLUDE
FIRST_DAY = BASE.date()
LAST_DAY = FIRST_DAY + timedelta(days=4)
SKILL = "brainstorm"

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


# Frozen from the Python functions the serve layer used before Task 6 converted them
# (`_build_series` in compare mode, `_bucket_turns`, `build_breakdown_tools`,
# `build_skill_breakdown`, `billable_token_totals`), run over this fixture. The engine side of
# each case is unchanged. Series rows are the non-empty buckets: (day or hour, turns, input,
# output, cache read, cache creation, cost[, interrupted turns]); the leading number is the
# length of the filled series, which proves the zero-fill without listing it.
_DAILY_BY_WANT = {'project': {'/p/one': (8,
                        [('2026-05-01', 6, 2850, 285, 70500, 2450, 0.025812499999999995, 1),
                         ('2026-05-02', 6, 10750, 1075, 80500, 2150, 0.0558375, 1),
                         ('2026-05-03', 6, 21900, 2190, 80000, 2700, 0.089775, 0),
                         ('2026-05-04', 6, 30900, 3090, 75000, 2700, 0.11227500000000001, 0),
                         ('2026-05-05', 6, 33350, 3335, 90500, 2450, 0.1105125, 1)]),
             '/p/two': (8,
                        [('2026-05-01', 3, 2400, 240, 50000, 2700, 0.036599999999999994, 0),
                         ('2026-05-02', 3, 6900, 690, 30000, 2700, 0.05085, 0),
                         ('2026-05-03', 3, 8150, 815, 20500, 1850, 0.0504375, 1),
                         ('2026-05-04', 3, 15900, 1590, 60000, 2700, 0.10035000000000002, 0),
                         ('2026-05-05', 3, 20400, 2040, 40000, 2700, 0.11460000000000001, 0)]),
             '/home/ünï/"quo,te"\nnext line': (8,
                                               [('2026-05-01', 3, 3000, 300, 45000, 0, 0.027000000000000003, 0),
                                                ('2026-05-02', 3, 7500, 750, 60000, 0, 0.051750000000000004, 0),
                                                ('2026-05-03', 3, 12000, 1200, 40000, 0, 0.066, 0),
                                                ('2026-05-04', 3, 11050, 1105, 25500, 50, 0.057562499999999996, 1),
                                                ('2026-05-05', 3, 21000, 2100, 35000, 0, 0.10500000000000001, 0)])},
 'model': {'claude-sonnet-4-6': (8,
                                 [('2026-05-01', 9, 6150, 615, 130500, 3350, 0.08028749999999998, 1),
                                  ('2026-05-02', 9, 20700, 2070, 125000, 3600, 0.1455, 0),
                                  ('2026-05-03', 9, 30950, 3095, 110500, 2750, 0.1840875, 1),
                                  ('2026-05-04', 9, 42250, 4225, 115500, 3650, 0.2398125, 1),
                                  ('2026-05-05', 9, 54650, 5465, 105500, 3350, 0.2914875, 1)]),
           'claude-haiku-4-5': (8,
                                [('2026-05-01', 6, 4800, 480, 65000, 5400, 0.0209, 0),
                                 ('2026-05-02', 6, 11650, 1165, 90500, 4850, 0.0329625, 1),
                                 ('2026-05-03', 6, 18450, 1845, 80500, 4250, 0.0413375, 1),
                                 ('2026-05-04', 6, 31800, 3180, 85000, 5400, 0.0634, 0),
                                 ('2026-05-05', 6, 40800, 4080, 115000, 5400, 0.0799, 0)])}}

_HOURLY_BY_WANT = {'project': {'/p/one': (24,
                        [('2026-05-03T00:00:00+00:00', 2, 6300, 630, 35000, 900, 0.027499999999999997),
                         ('2026-05-03T13:00:00+00:00', 2, 7300, 730, 15000, 900, 0.0262),
                         ('2026-05-03T23:00:00+00:00', 2, 8300, 830, 30000, 900, 0.036075)]),
             '/home/ünï/"quo,te"\nnext line': (24,
                                               [('2026-05-03T00:00:00+00:00', 1, 3500, 350, 0, 0, 0.01575),
                                                ('2026-05-03T13:00:00+00:00',
                                                 1,
                                                 4000,
                                                 400,
                                                 25000,
                                                 0,
                                                 0.025500000000000002),
                                                ('2026-05-03T23:00:00+00:00', 1, 4500, 450, 15000, 0, 0.02475)]),
             '/p/two': (24,
                        [('2026-05-03T00:00:00+00:00', 1, 50, 5, 500, 50, 0.0005625000000000001),
                         ('2026-05-03T13:00:00+00:00', 1, 3800, 380, 15000, 900, 0.025425000000000003),
                         ('2026-05-03T23:00:00+00:00', 1, 4300, 430, 5000, 900, 0.02445)])},
 'model': {'claude-sonnet-4-6': (24,
                                 [('2026-05-03T00:00:00+00:00', 3, 6650, 665, 15500, 350, 0.0361125),
                                  ('2026-05-03T13:00:00+00:00', 3, 11400, 1140, 45000, 1200, 0.06975),
                                  ('2026-05-03T23:00:00+00:00', 3, 12900, 1290, 50000, 1200, 0.078225)]),
           'claude-haiku-4-5': (24,
                                [('2026-05-03T00:00:00+00:00', 2, 6600, 660, 50000, 1800, 0.017375),
                                 ('2026-05-03T13:00:00+00:00', 2, 7600, 760, 30000, 1800, 0.016725),
                                 ('2026-05-03T23:00:00+00:00', 2, 4250, 425, 500, 650, 0.0072375)])}}

_BUCKET_WANT = {'project': {'/p/one': (99600,
                        9960,
                        395000,
                        12300,
                        0.1922000000000001,
                        0.09610000000000005,
                        0.07550000000000001,
                        0.029100000000000008,
                        0.3928999999999999),
             '/p/two': (53700, 5370, 200000, 12600, 0.1611, 0.08055, 0.06, 0.05062500000000001, 0.352275),
             '(unknown)': (54100,
                           5410,
                           220000,
                           16800,
                           0.05410000000000001,
                           0.027050000000000005,
                           0.022,
                           0.021974999999999998,
                           0.12512500000000001),
             '/home/ünï/"quo,te"\nnext line': (54500,
                                               5450,
                                               205000,
                                               0,
                                               0.1635,
                                               0.08175,
                                               0.0615,
                                               0.0,
                                               0.30674999999999997)},
 'model': {'claude-sonnet-4-6': (127700,
                                 12770,
                                 330000,
                                 9600,
                                 0.3831000000000001,
                                 0.19155000000000005,
                                 0.09900000000000003,
                                 0.04005,
                                 0.7137000000000001),
           'claude-haiku-4-5': (91000,
                                9100,
                                280000,
                                15000,
                                0.09100000000000001,
                                0.045500000000000006,
                                0.027999999999999997,
                                0.01995,
                                0.18445000000000003)}}

_TOOLS_WANT = {'prorata': {'Bash': (0.055645, 24, '2026-05-05T23:15:00+00:00'),
             'Edit': (0.037425, None, None),
             'Read': (0.021585, 11, '2026-05-05T23:12:00+00:00'),
             'mcp__sé,rver__"do\nit"': (0.0107925, None, None),
             '__unattributed__': (1.04081, None, None)},
 'excl-cache-read': {'Bash': (0.045720000000000004, 24, '2026-05-05T23:15:00+00:00'),
                     'Edit': (0.03285, None, None),
                     'Read': (0.017384999999999998, 11, '2026-05-05T23:12:00+00:00'),
                     'mcp__sé,rver__"do\nit"': (0.008692499999999999, None, None),
                     '__unattributed__': (1.06371, None, None)},
 'prorata-since': {'Bash': (0.039607500000000004, 14, '2026-05-05T23:15:00+00:00'),
                   'Edit': (0.0303225, None, None),
                   'Read': (0.014750000000000001, 6, '2026-05-05T23:12:00+00:00'),
                   'mcp__sé,rver__"do\nit"': (0.0073750000000000005, None, None),
                   '__unattributed__': (0.7987200000000001, None, None)}}

_SKILL_TOTALS_WANT = {'all': (1.1770500000000004, 288090.0, 59950.0), 'since': (0.89815, 240570.0, 48400.0)}

_BILLABLE_WANT = {'all': (256212.0, 288090.0), 'since': (215082.0, 240570.0)}

# Frozen from the Python functions the serve layer used before Task 7 converted them
# (`_accumulate_tool_costs`, `build_tool_detail`, `_accumulate_skill_costs`, `build_skill_detail`,
# `build_tool_cost_daily`, `build_skill_cost_daily`), run over this fixture. The engine side of each
# case is unchanged. Daily lists hold the non-zero days; the leading number is the 30-day length.
_TOOL_ACCUMULATE_WANT = ({'Bash': 0.055804375,
  'Edit': 0.037520625,
  'Read': 0.02164875,
  '__unattributed__': 0.0054121875,
  'mcp__sé,rver__"do\nit"': 0.010824375},
 {'Bash': 30, 'Grep': 12, 'Read': 11},
 {'Bash': '2026-05-05T23:15:00+00:00',
  'Edit': '2026-05-05T23:15:00+00:00',
  'Grep': '2026-05-05T23:15:00+00:00',
  'Read': '2026-05-05T23:12:00+00:00',
  'mcp__sé,rver__"do\nit"': '2026-05-05T23:12:00+00:00'})

_TOOL_DETAIL_WANT = {
    'Bash': {'by_model': {'claude-haiku-4-5': (0.013495, 10), 'claude-sonnet-4-6': (0.04215000000000001, 14)},
             'by_project': {'"quo,te"\nnext line': (0.017325, 6, '2026-05-05T23:15:00+00:00'),
                            '(unknown)': (0.006215, 6, '2026-05-05T13:09:00+00:00'),
                            'one': (0.017292500000000002, 8, '2026-05-05T23:12:00+00:00'),
                            'two': (0.0148125, 4, '2026-05-05T00:03:00+00:00')},
             'invocations': 24,
             'output_tokens': 2082.0,
             'total_cost': 0.055645},
    'Grep': {'by_model': {'claude-haiku-4-5': (0.0, 5), 'claude-sonnet-4-6': (0.0, 7)},
             'by_project': {'"quo,te"\nnext line': (0.0, 3, '2026-05-05T23:15:00+00:00'),
                            '(unknown)': (0.0, 3, '2026-05-05T13:09:00+00:00'),
                            'one': (0.0, 4, '2026-05-04T23:57:00+00:00'),
                            'two': (0.0, 2, '2026-05-05T00:03:00+00:00')},
             'invocations': 12,
             'output_tokens': 0.0,
             'total_cost': 0.0},
    'Edit': {'by_model': {'claude-haiku-4-5': (0.008355, 0), 'claude-sonnet-4-6': (0.02907, 0)},
             'by_project': {'"quo,te"\nnext line': (0.014175, 0, '2026-05-05T23:15:00+00:00'),
                            '(unknown)': (0.005084999999999999, 0, '2026-05-05T13:09:00+00:00'),
                            'one': (0.0105375, 0, '2026-05-04T23:57:00+00:00'),
                            'two': (0.007627499999999999, 0, '2026-05-05T00:03:00+00:00')},
             'invocations': 0,
             'output_tokens': 1422.0,
             'total_cost': 0.037425},
}

_SKILLS_WANT = {
    'all': ({'brainstorm': 0.14925000000000002, 'skill,"é"\nx': 0.1575},
            {'brainstorm': 11},
            {'brainstorm': '2026-05-05T23:15:00+00:00', 'skill,"é"\nx': '2026-05-05T13:10:00+00:00'}),
    'since': ({'brainstorm': 0.11400000000000002, 'skill,"é"\nx': 0.11399999999999999},
              {'brainstorm': 6},
              {'brainstorm': '2026-05-05T23:15:00+00:00', 'skill,"é"\nx': '2026-05-05T13:10:00+00:00'}),
}

_TOOL_DAILY_WANT = (30, [('2026-05-01', 0.00509875), ('2026-05-02', 0.01093875), ('2026-05-03', 0.008815),
                         ('2026-05-04', 0.021963749999999997), ('2026-05-05', 0.00882875)])
_SKILL_DAILY_WANT = (30, [('2026-05-01', 0.018), ('2026-05-02', 0.01725), ('2026-05-03', 0.0405),
                          ('2026-05-05', 0.07350000000000001)])

_SKILL_DETAIL_WANT = {
    SKILL: {'by_model': {'claude-sonnet-4-6': 0.14925000000000002},
            'by_project': {'"quo,te"\nnext line': (0.14925000000000002, '2026-05-05T23:15:00+00:00')},
            'invocations': 11,
            'output_tokens': 2650.0,
            'split': {'inline_usd': 0.14925000000000002, 'subagent_usd': 0.0},
            'total_cost': 0.14925000000000002},
    HOSTILE_SKILL: {'by_model': {'claude-sonnet-4-6': 0.1575},
                    'by_project': {'"quo,te"\nnext line': (0.1575, '2026-05-05T13:10:00+00:00')},
                    'invocations': 0,
                    'output_tokens': 2800.0,
                    'split': {'inline_usd': 0.081, 'subagent_usd': 0.0765},
                    'total_cost': 0.1575},
}


class Fixture:
    def __init__(self) -> None:
        self.turns = _build_turns()
        infos = [SessionInfo(sid, f"/tmp/{i}.jsonl", cwd, False) for i, (sid, cwd, _m) in enumerate(SESSIONS)]
        self.sessions = [
            Session(i.session_id, i.source_file, i.is_sidechain, i.cwd, [t for t in self.turns if t.session_id == i.session_id])
            for i in infos
        ]
        self.cwd_by_sid = grouped_project_cwds(self.sessions)
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


def _rows(rollups: list, label_attr: str) -> list[tuple]:
    """The non-empty buckets of a filled series, in the frozen literals' shape."""
    return [
        (getattr(r, label_attr).isoformat(), *dataclasses.astuple(r)[1:]) for r in rollups if r.turns
    ]


def _daily_by(fx: Fixture, key: GroupKey, fill_until: date):
    since = FIRST_DAY - timedelta(days=1)
    want = _DAILY_BY_WANT[key.value]
    names = fx.q.top_keys_by_cost(TurnFilter(), key, 8)
    assert names == list(want)  # the top-8 order compare mode ranks by
    got = fx.q.daily_by(TurnFilter(until=fill_until), key, since, fill_until)
    return {name: (len(got[name]), _rows(got[name], "date")) for name in names}, want


def _hourly_by(fx: Fixture, key: GroupKey, day: date):
    got = fx.q.hourly_by(TurnFilter(), key, day, True)
    want = _HOURLY_BY_WANT[key.value]
    return {name: (len(got[name]), _rows(got[name], "hour")) for name in want}, want


def _bucket_want(fx: Fixture, key: GroupKey, since: date | None):
    got = fx.q.bucket(TurnFilter(since=since, interrupted=EXCLUDE), key)
    fields = ("input", "output", "cache_read", "cache_creation", "input_cost", "output_cost",
              "cache_read_cost", "cache_creation_cost", "total_cost")
    return {k: tuple(getattr(b, n) for n in fields) for k, b in got.items()}, _BUCKET_WANT[key.value]


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


def _breakdown_tools(fx: Fixture, mode: AttributionMode, since: date | None, case: str):
    got = fx.q.tool_costs(TurnFilter(since=since, interrupted=EXCLUDE), mode)
    mine = {
        name: (cost, got.calls_by_tool.get(name), got.last_active[name].isoformat() if name in got.last_active else None)
        for name, cost in got.cost_by_tool.items()
    }
    mine[UNATTRIBUTED_TOOL] = (got.unattributed_usd, None, None)
    return mine, _TOOLS_WANT[case]


def _tool_accumulate(fx: Fixture):
    got = fx.q.tool_costs(TurnFilter(interrupted=INCLUDE), AttributionMode.PRORATA)
    mine_cost = dict(got.cost_by_tool)
    if got.unknown_usd:
        mine_cost[UNATTRIBUTED_TOOL] = got.unknown_usd
    mine_last = {name: moment.isoformat() for name, moment in got.last_seen.items()}
    return (mine_cost, got.calls_by_tool, mine_last), _TOOL_ACCUMULATE_WANT


def _tool_detail(fx: Fixture, tool: str):
    cutoff = BASE + timedelta(days=3)
    rows = fx.q.tool_detail_rows(TurnFilter(interrupted=EXCLUDE), tool, recent_since=cutoff)
    assert rows
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
    }
    want = dict(_TOOL_DETAIL_WANT[tool])
    # The recent-call window moves with the clock in the old payload, so it is checked live.
    mine["recent"] = sum(r.recent_invocations for r in rows)
    want["recent"] = sum(t.tool_names.get(tool, 0) for t in fx.pick(policy=EXCLUDE) if t.timestamp >= cutoff)
    return mine, want


def _skills(fx: Fixture, since: date | None, case: str):
    got = fx.q.skill_costs(TurnFilter(since=since, interrupted=EXCLUDE))
    mine = (got.cost_by_skill, got.invocations_by_skill, {k: v.isoformat() for k, v in got.last_active.items()},
            got.total_cost, got.total_billable_tokens, got.skill_billable_tokens)
    cost, invs, last = _SKILLS_WANT[case]
    return mine, (cost, invs, last, *_SKILL_TOTALS_WANT[case])


def _skill_detail(fx: Fixture, skill: str):
    cutoff = BASE + timedelta(days=3)
    detail = fx.q.skill_detail(TurnFilter(interrupted=EXCLUDE), skill, recent_since=cutoff)
    assert detail.rows
    by_project: dict[str, tuple[float, str]] = {}
    by_model: dict[str, float] = {}
    for r in detail.rows:
        cost, last = by_project.get(r.project, (0.0, ""))
        by_project[r.project] = (cost + r.cost_usd, max(last, r.last_active.isoformat()))
        by_model[r.model] = by_model.get(r.model, 0.0) + r.cost_usd
    mine = {
        "total_cost": sum(r.cost_usd for r in detail.rows),
        "output_tokens": sum(r.output_tokens for r in detail.rows),
        "invocations": detail.invocations,
        "split": {
            "inline_usd": sum(r.inline_usd for r in detail.rows),
            "subagent_usd": sum(r.subagent_usd for r in detail.rows),
        },
        "by_project": {p.rsplit("/", 1)[-1] if p != "(unknown)" else p: v for p, v in by_project.items()},
        "by_model": by_model,
    }
    # The recent-call window moves with the clock in the old payload, so it is checked live.
    mine["recent"] = detail.recent_invocations
    want = dict(_SKILL_DETAIL_WANT[skill])
    want["recent"] = sum(t.skill_names.get(skill, 0) for t in fx.pick(policy=EXCLUDE) if t.timestamp >= cutoff)
    return mine, want


def _top_turns(fx: Fixture, projects: tuple[str, ...], n: int):
    got = fx.q.top_turns_by_cost(TurnFilter(projects=projects, interrupted=EXCLUDE), n)
    # Cost-descending; the stable sort keeps arrival order, which is the engine's turn-id order.
    ranked = sorted(fx.pick(projects=projects, policy=EXCLUDE), key=lambda t: t.cost_usd, reverse=True)[:n]
    want = [
        TopTurn(
            t.timestamp, t.session_id, t.model, t.cost_usd, t.usage.input_tokens, t.usage.output_tokens,
            t.usage.cache_read_input_tokens, t.usage.cache_creation_input_tokens,
        )
        for t in ranked
    ]
    return got, want


def _daily_cost_rows(rows) -> tuple[int, list[tuple[str, float]]]:
    return len(rows), [(r.date.isoformat(), r.cost_usd) for r in rows if r.cost_usd]


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
        "tools-prorata": lambda: _breakdown_tools(fx, AttributionMode.PRORATA, None, "prorata"),
        "tools-excl-cache-read": lambda: _breakdown_tools(fx, AttributionMode.EXCL_CACHE_READ, None, "excl-cache-read"),
        "tools-prorata-since": lambda: _breakdown_tools(fx, AttributionMode.PRORATA, mid, "prorata-since"),
        "tools-accumulate-includes-interrupted": lambda: _tool_accumulate(fx),
        "tool-detail-bash": lambda: _tool_detail(fx, "Bash"),
        "tool-detail-no-cost-entry": lambda: _tool_detail(fx, "Grep"),
        "tool-detail-no-call": lambda: _tool_detail(fx, "Edit"),
        "tool-cost-daily": lambda: (
            _daily_cost_rows(fx.q.tool_cost_daily(TurnFilter(interrupted=EXCLUDE), "Bash", today=LAST_DAY)),
            _TOOL_DAILY_WANT,
        ),
        "skills": lambda: _skills(fx, None, "all"),
        "skills-since": lambda: _skills(fx, mid, "since"),
        "skill-cost-daily": lambda: (
            _daily_cost_rows(fx.q.skill_cost_daily(TurnFilter(interrupted=EXCLUDE), SKILL, today=LAST_DAY)),
            _SKILL_DAILY_WANT,
        ),
        "skill-detail": lambda: _skill_detail(fx, SKILL),
        "skill-detail-sidechain-split": lambda: _skill_detail(fx, HOSTILE_SKILL),
        "skill-detail-unknown": lambda: (
            fx.q.skill_detail(TurnFilter(interrupted=EXCLUDE), "no-such-skill"),
            SkillDetail(0, 0, []),
        ),
        "top-turns-by-cost": lambda: _top_turns(fx, ("/p/one",), 1000),
        "top-turns-by-cost-limited": lambda: _top_turns(fx, (), 3),
        "session-ids": lambda: (
            fx.q.session_ids(TurnFilter(since=mid, until=mid)),
            sorted({t.session_id for t in fx.pick(since=mid, until=mid)}),
        ),
        "session-ids-project": lambda: (
            fx.q.session_ids(TurnFilter(projects=("/p/one",))),
            sorted({t.session_id for t in fx.pick(projects=("/p/one",))}),
        ),
        "billable-totals": lambda: (
            fx.q.billable_totals(TurnFilter(interrupted=EXCLUDE)),
            _BILLABLE_WANT["all"],
        ),
        "billable-totals-since": lambda: (
            fx.q.billable_totals(TurnFilter(since=mid, interrupted=EXCLUDE)),
            _BILLABLE_WANT["since"],
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
