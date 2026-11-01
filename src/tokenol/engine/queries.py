"""Every aggregation the dashboard endpoints need, answered in SQL from the usage engine.

Each method takes a ``TurnFilter`` and returns the dataclasses the Python code already
consumes (``DailyRollup``, ``HourlyRollup``, ``ModelRollup``, ``DailyToolCost``) or one of
the small frozen shapes defined here. SQL filters, groups and sums only (DE10): metric
formulas (hit %, $/kW, ratios) stay in ``metrics.context`` and are applied to the sums.
Prices come only from the ``priced_turns`` / ``priced_turn_tools`` views.

The interrupted-turn policy is the caller's, set on the filter (INV 6b item 7):

* INCLUDE: daily / hourly rollups, model rollups, session rollups, recent activity, and
  the tool costs behind the model and project detail pages.
* EXCLUDE: the tools breakdown, skill costs, tool detail, ``billable_totals``, ``bucket``.

Searched before writing: ``grep -rn "GROUP BY" src`` found only the history store's own
reads (strings and JSON, a different schema), and the Python references are the
``metrics`` / ``serve`` functions named on each method, so nothing was reusable here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from tokenol.engine.engine import UsageEngine
from tokenol.engine.filters import (
    MODELS_ALIAS,
    SESSIONS_ALIAS,
    TURNS_ALIAS,
    UNKNOWN_LABEL,
    GroupKey,
    SearchField,
    TurnFilter,
)
from tokenol.engine.ingest import sql_literal
from tokenol.engine.pricing import (
    PRICED_TURN_TOOLS_VIEW,
    PRICED_TURNS_VIEW,
    TURNS_TABLE,
)
from tokenol.engine.schema import (
    NAMES_TABLE,
    SESSIONS_TABLE,
    SKILL_CALLS_TABLE,
    TOOL_CALLS_TABLE,
    NameKind,
)
from tokenol.enums import AttributionMode
from tokenol.ingest.parser import SKILL_TOOL, UNATTRIBUTED_TOOL, UNKNOWN_TOOL
from tokenol.metrics.context import cache_reuse_n_to_1, cost_per_kw, ctx_ratio_n_to_1
from tokenol.metrics.cost import DailyRollup, HourlyRollup
from tokenol.metrics.rollups import DailyToolCost, ModelRollup

_T, _S, _M = TURNS_ALIAS, SESSIONS_ALIAS, MODELS_ALIAS
_P = "p"  # priced_turns

_TOOL_KIND = sql_literal(NameKind.TOOL.value)
_SKILL_KIND = sql_literal(NameKind.SKILL.value)

# A label expression per grouping; empty and null both mean "unknown", as in the Python code.
_LABEL_SQL: dict[GroupKey, str] = {
    GroupKey.PROJECT: f"coalesce(nullif({_S}.project_cwd, ''), {sql_literal(UNKNOWN_LABEL)})",
    GroupKey.MODEL: f"coalesce(nullif({_M}.model, ''), {sql_literal(UNKNOWN_LABEL)})",
}

# One window of days the tool and skill detail pages chart.
DETAIL_DAYS = 30

_POOL_SQL = f"({_T}.input_tokens + {_T}.cache_read_tokens + {_T}.cache_creation_tokens)"


@dataclass(frozen=True, slots=True)
class BucketTotals:
    """Tokens and the four cost components of one group (the shape of ``_bucket_turns``)."""

    input: int
    output: int
    cache_read: int
    cache_creation: int
    input_cost: float
    output_cost: float
    cache_read_cost: float
    cache_creation_cost: float
    total_cost: float
    turns: int
    last_ts: datetime | None  # newest turn of the group (UTC)


@dataclass(frozen=True, slots=True)
class Totals:
    turns: int
    sessions: int  # sessions with at least one turn in the filter
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    cache_saved_usd: float


@dataclass(frozen=True, slots=True)
class ComponentCosts:
    input: float
    output: float
    cache_creation: float
    cache_read: float


@dataclass(frozen=True, slots=True)
class ToolCosts:
    """Per-tool sums for the tools breakdown and the model / project detail pages.

    ``Skill`` is left out everywhere (the Skill dimension owns it) and ``__unknown__``
    is left out of ``cost_by_tool``: its prorata cost is ``unknown_usd``.
    ``unattributed_usd`` is the residual no tool accounts for, in the requested mode.
    ``last_active`` is the newest turn that both has a cost entry and a call of the tool
    (the breakdown's rule); ``last_seen`` is the newest turn with either (the detail pages').
    """

    cost_by_tool: dict[str, float] = field(default_factory=dict)
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    last_active: dict[str, datetime] = field(default_factory=dict)
    last_seen: dict[str, datetime] = field(default_factory=dict)
    unknown_usd: float = 0.0
    unattributed_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolDetailRow:
    """One tool's cost and calls within one (project, model) pair."""

    project: str
    model: str
    cost_usd: float
    output_tokens: float
    invocations: int
    recent_invocations: int  # calls on or after ``recent_since``
    last_active: datetime


@dataclass(frozen=True, slots=True)
class SkillCosts:
    """Per-skill sums (the shape ``_accumulate_skill_costs`` returns) plus the spend totals."""

    cost_by_skill: dict[str, float]
    invocations_by_skill: dict[str, int]
    last_active: dict[str, datetime]
    total_cost: float
    total_billable_tokens: float
    skill_billable_tokens: float


@dataclass(frozen=True, slots=True)
class LatestTurn:
    """A project's newest turn in the filter, plus what recent activity reads beside it."""

    project: str
    session_id: str
    timestamp: datetime
    model: str | None
    pool: int  # input + cache read + cache creation tokens of that turn
    turns: int  # turns of the project in the filter
    primary_model: str | None  # most used model; ties go to the one seen first


def _utc(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.replace(tzinfo=timezone.utc)


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _empty_daily(day: date) -> DailyRollup:
    return DailyRollup(day, 0, 0, 0, 0, 0, 0.0, 0)


def _empty_hourly(hour: datetime) -> HourlyRollup:
    return HourlyRollup(hour, 0, 0, 0, 0, 0, 0.0)


class UsageQueries:
    """Aggregations over one ``UsageEngine``; every read goes through ``engine.cursor()``."""

    def __init__(self, engine: UsageEngine) -> None:
        self._engine = engine

    # -- plumbing ------------------------------------------------------------

    def _from(self, priced: bool) -> str:
        sql = (
            f"FROM {TURNS_TABLE} {_T} "
            f"JOIN {SESSIONS_TABLE} {_S} ON {_S}.session_ix = {_T}.session_ix "
            f"JOIN models {_M} ON {_M}.model_id = {_T}.model_id"
        )
        if priced:
            # session_ix in the join lets a one-session filter reach the view before its rates join.
            sql += (
                f" JOIN {PRICED_TURNS_VIEW} {_P} ON {_P}.turn_id = {_T}.turn_id "
                f"AND {_P}.session_ix = {_T}.session_ix"
            )
        return sql

    def _query(
        self, select: str, f: TurnFilter, tail: str = "", *, priced: bool = True, joins: str = ""
    ) -> list[tuple]:
        """``SELECT select FROM <turns joins> <joins> WHERE <filter> <tail>``; user values are parameters."""
        where, params = f.where()
        sql = f"SELECT {select} {self._from(priced)} {joins} WHERE {where} {tail}"
        with self._engine.cursor() as cur:
            return cur.execute(sql, params).fetchall()

    def _filtered_turns_cte(self, f: TurnFilter, columns: str) -> tuple[str, list]:
        where, params = f.where()
        return f"ft AS MATERIALIZED (SELECT {columns} {self._from(True)} WHERE {where})", params

    # -- rollups -------------------------------------------------------------

    def _series(
        self, f: TurnFilter, key: GroupKey | None, period_sql: str
    ) -> dict[str, dict[object, tuple]]:
        """``{label: {period: (turns, in, out, cache_read, cache_creation, cost, interrupted)}}``."""
        label = "'all'" if key is None else _LABEL_SQL[key]
        rows = self._query(
            f"{label}, {period_sql}, count(*), sum({_T}.input_tokens), sum({_T}.output_tokens), "
            f"sum({_T}.cache_read_tokens), sum({_T}.cache_creation_tokens), sum({_P}.cost_usd), "
            f"count(*) FILTER (WHERE {_T}.is_interrupted)",
            f,
            "GROUP BY 1, 2",
        )
        out: dict[str, dict[object, tuple]] = defaultdict(dict)
        for name, period, *values in rows:
            out[name][period] = tuple(values)
        return out

    @staticmethod
    def _daily_rollups(
        buckets: dict[object, tuple], fill_since: date | None, fill_until: date | None
    ) -> list[DailyRollup]:
        by_day: dict[date, DailyRollup] = {
            day: DailyRollup(day, *values) for day, values in buckets.items()
        }
        if fill_since is not None:
            for day in _days(fill_since, fill_until or _today()):
                by_day.setdefault(day, _empty_daily(day))
        return [by_day[day] for day in sorted(by_day)]

    def daily(
        self, f: TurnFilter, fill_since: date | None = None, fill_until: date | None = None
    ) -> list[DailyRollup]:
        """Per UTC day, ascending; zero-filled from *fill_since* to *fill_until* (default today)."""
        series = self._series(f, None, f"CAST({_T}.ts AS DATE)")
        return self._daily_rollups(series.get("all", {}), fill_since, fill_until)

    def daily_by(
        self,
        f: TurnFilter,
        key: GroupKey,
        fill_since: date | None = None,
        fill_until: date | None = None,
    ) -> dict[str, list[DailyRollup]]:
        """``daily`` per project or model. The unknown group is dropped, as compare mode does."""
        series = self._series(f, key, f"CAST({_T}.ts AS DATE)")
        return {
            name: self._daily_rollups(buckets, fill_since, fill_until)
            for name, buckets in sorted(series.items())
            if name != UNKNOWN_LABEL
        }

    @staticmethod
    def _hourly_rollups(
        buckets: dict[object, tuple], target_date: date | None, fill_day: bool
    ) -> list[HourlyRollup]:
        by_hour: dict[datetime, HourlyRollup] = {}
        for hour, values in buckets.items():
            aware = _utc(hour)
            by_hour[aware] = HourlyRollup(aware, *values[:-1])  # no interrupted column
        if fill_day and target_date:
            start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
            for h in range(24):
                hour = start + timedelta(hours=h)
                by_hour.setdefault(hour, _empty_hourly(hour))
        return [by_hour[hour] for hour in sorted(by_hour)]

    @staticmethod
    def _one_day(f: TurnFilter, target_date: date | None) -> TurnFilter:
        return f if target_date is None else replace(f, since=target_date, until=target_date)

    def hourly(self, f: TurnFilter, target_date: date | None = None, fill_day: bool = False) -> list[HourlyRollup]:
        """Per UTC hour, ascending; *target_date* narrows to that UTC day, *fill_day* adds 0-23."""
        series = self._series(self._one_day(f, target_date), None, f"date_trunc('hour', {_T}.ts)")
        return self._hourly_rollups(series.get("all", {}), target_date, fill_day)

    def hourly_by(
        self, f: TurnFilter, key: GroupKey, target_date: date | None = None, fill_day: bool = False
    ) -> dict[str, list[HourlyRollup]]:
        series = self._series(self._one_day(f, target_date), key, f"date_trunc('hour', {_T}.ts)")
        return {
            name: self._hourly_rollups(buckets, target_date, fill_day)
            for name, buckets in sorted(series.items())
            if name != UNKNOWN_LABEL
        }

    def model_rollups(self, f: TurnFilter) -> list[ModelRollup]:
        """Per model string, cost-descending. Interrupted turns count as turns, not as tokens or cost."""
        billed = f"NOT {_T}.is_interrupted"
        rows = self._query(
            f"{_LABEL_SQL[GroupKey.MODEL]}, count(*), "
            f"coalesce(sum({_T}.input_tokens) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_T}.output_tokens) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_T}.cache_read_tokens) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_T}.cache_creation_tokens) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_P}.cost_usd) FILTER (WHERE {billed}), 0), "
            f"sum({_T}.tool_use_count), sum({_T}.tool_error_count), "
            f"coalesce(sum({_P}.input_usd) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_P}.output_usd) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_P}.cache_read_usd) FILTER (WHERE {billed}), 0), "
            f"coalesce(sum({_P}.cache_creation_usd) FILTER (WHERE {billed}), 0), "
            f"count(*) FILTER (WHERE {_T}.is_sidechain), count(*) FILTER (WHERE {_T}.is_interrupted)",
            f,
            "GROUP BY 1",
        )
        total_cost = sum(row[6] for row in rows) or 1.0
        result = [
            ModelRollup(
                model=model,
                turns=turns,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=reads,
                cache_creation_tokens=creates,
                cost_usd=cost,
                tool_use_count=tool_uses,
                tool_error_count=tool_errors,
                input_usd=input_usd,
                output_usd=output_usd,
                cache_read_usd=cache_read_usd,
                cache_creation_usd=cache_creation_usd,
                sidechain_turns=sidechain,
                interrupted_turns=interrupted,
                ctx_ratio_n_to_1=ctx_ratio_n_to_1(reads, out),
                cache_reuse_n_to_1=cache_reuse_n_to_1(reads, creates),
                cost_per_kw_val=cost_per_kw(cost, out),
                cost_share=cost / total_cost,
            )
            for (
                model, turns, inp, out, reads, creates, cost, tool_uses, tool_errors, input_usd,
                output_usd, cache_read_usd, cache_creation_usd, sidechain, interrupted,
            ) in rows
        ]
        result.sort(key=lambda r: (-r.cost_usd, r.model))
        return result

    # -- grouping and totals -------------------------------------------------

    def top_keys_by_cost(self, f: TurnFilter, key: GroupKey, n: int) -> list[str]:
        """The *n* costliest groups, cost-descending (ties by label); the unknown group is skipped."""
        rows = self._query(
            f"{_LABEL_SQL[key]} AS label, sum({_P}.cost_usd) AS cost",
            f,
            f"GROUP BY 1 HAVING label <> {sql_literal(UNKNOWN_LABEL)} ORDER BY cost DESC, label LIMIT {int(n)}",
        )
        return [label for label, _cost in rows]

    def bucket(self, f: TurnFilter, key: GroupKey) -> dict[str, BucketTotals]:
        rows = self._query(
            f"{_LABEL_SQL[key]}, sum({_T}.input_tokens), sum({_T}.output_tokens), "
            f"sum({_T}.cache_read_tokens), sum({_T}.cache_creation_tokens), sum({_P}.input_usd), "
            f"sum({_P}.output_usd), sum({_P}.cache_read_usd), sum({_P}.cache_creation_usd), "
            f"sum({_P}.cost_usd), count(*), max({_T}.ts)",
            f,
            "GROUP BY 1",
        )
        return {label: BucketTotals(*values[:-1], _utc(values[-1])) for label, *values in rows}

    def totals(self, f: TurnFilter) -> Totals:
        (row,) = self._query(
            f"count(*), count(DISTINCT {_T}.session_ix), coalesce(sum({_T}.input_tokens), 0), "
            f"coalesce(sum({_T}.output_tokens), 0), coalesce(sum({_T}.cache_read_tokens), 0), "
            f"coalesce(sum({_T}.cache_creation_tokens), 0), coalesce(sum({_P}.cost_usd), 0), "
            f"coalesce(sum({_P}.cache_saved_usd), 0)",
            f,
        )
        return Totals(*row)

    def cost_components_by_date(self, f: TurnFilter) -> dict[date, ComponentCosts]:
        rows = self._query(
            f"CAST({_T}.ts AS DATE), sum({_P}.input_usd), sum({_P}.output_usd), "
            f"sum({_P}.cache_creation_usd), sum({_P}.cache_read_usd)",
            f,
            "GROUP BY 1",
        )
        return {day: ComponentCosts(*values) for day, *values in rows}

    def billable_totals(self, f: TurnFilter) -> tuple[float, float]:
        """``(non_tool, total)`` input+output tokens, as ``billable_token_totals`` defines them."""
        pool = _POOL_SQL
        ((nontool, total),) = self._query(
            f"coalesce(sum(CASE WHEN {pool} > 0 THEN {_T}.unattributed_input_tokens / {pool} "
            f"* {_T}.input_tokens ELSE 0 END + {_T}.unattributed_output_tokens), 0), "
            f"coalesce(sum({_T}.input_tokens + {_T}.output_tokens), 0)",
            f,
            priced=False,
        )
        return float(nontool), float(total)

    def earliest_date(self, f: TurnFilter) -> date | None:
        ((first,),) = self._query(f"min({_T}.ts)", f, priced=False)
        return None if first is None else first.date()

    def distinct_values(self, column: SearchField, f: TurnFilter) -> list[str]:
        """Distinct models (never null or empty) or ISO dates present in the filter, sorted."""
        if column is SearchField.MODEL:
            rows = self._query(f"DISTINCT {_M}.model", f, "ORDER BY 1", priced=False)
            return [model for (model,) in rows if model]
        rows = self._query(f"DISTINCT CAST({_T}.ts AS DATE)", f, "ORDER BY 1", priced=False)
        return [day.isoformat() for (day,) in rows]

    def latest_turn_per_project(self, f: TurnFilter) -> list[LatestTurn]:
        """Each project's newest turn (ties: lowest turn id) with its turn count and primary model."""
        label = _LABEL_SQL[GroupKey.PROJECT]
        latest = self._query(
            f"{label} AS project, {_S}.session_id, {_T}.ts, {_M}.model, {_POOL_SQL}, "
            f"count(*) OVER (PARTITION BY {label})",
            f,
            f"QUALIFY row_number() OVER (PARTITION BY {label} ORDER BY {_T}.ts DESC, {_T}.turn_id) = 1",
            priced=False,
        )
        # The model counter only counts named models; the first-seen model wins a tie.
        first_seen: dict[str, dict[str, tuple[int, datetime]]] = defaultdict(dict)
        for project, model, count, first in self._query(
            f"{label}, {_M}.model, count(*), min({_T}.ts)",
            f,
            "GROUP BY 1, 2",
            priced=False,
        ):
            if model:
                first_seen[project][model] = (count, first)
        out = []
        for project, session_id, ts, model, pool, turns in latest:
            counts = first_seen.get(project, {})
            primary = min(counts, key=lambda m: (-counts[m][0], counts[m][1])) if counts else None
            out.append(LatestTurn(project, session_id, _utc(ts), model, pool, turns, primary))
        out.sort(key=lambda r: r.project)
        return out

    # -- tools ---------------------------------------------------------------

    def _tool_rows(self, f: TurnFilter, mode: AttributionMode):
        """Per-tool aggregates and the per-turn residual sums for ``tool_costs``."""
        cte, params = self._filtered_turns_cte(
            f,
            f"{_T}.turn_id, {_T}.ts, {_P}.cost_usd, {_P}.input_usd, {_P}.output_usd, "
            f"{_P}.cache_read_usd, {_P}.cache_creation_usd",
        )
        cost_col = "cost_prorata" if mode is AttributionMode.PRORATA else "cost_excl_cache_read"
        per_pair = f"""
            ptt AS MATERIALIZED (
                SELECT ft.turn_id, ft.ts, pt.tool_id, n.name, pt.{cost_col} AS cost,
                    pt.cost_prorata, pt.input_share, pt.output_share
                FROM ft JOIN {PRICED_TURN_TOOLS_VIEW} pt ON pt.turn_id = ft.turn_id
                JOIN {NAMES_TABLE} n ON n.kind = {_TOOL_KIND} AND n.name_id = pt.tool_id),
            calls AS (
                SELECT ft.turn_id, ft.ts, c.tool_id, n.name, c.n
                FROM ft JOIN {TOOL_CALLS_TABLE} c ON c.turn_id = ft.turn_id
                JOIN {NAMES_TABLE} n ON n.kind = {_TOOL_KIND} AND n.name_id = c.tool_id)"""
        unknown = sql_literal(UNKNOWN_TOOL)
        with self._engine.cursor() as cur:
            per_tool = cur.execute(
                f"""WITH {cte}, {per_pair}
                SELECT coalesce(a.name, b.name), coalesce(sum(a.cost), 0), count(a.turn_id),
                    coalesce(sum(b.n), 0),
                    max(coalesce(a.ts, b.ts)) FILTER (WHERE a.turn_id IS NOT NULL AND b.turn_id IS NOT NULL),
                    max(coalesce(a.ts, b.ts)), coalesce(sum(a.cost_prorata), 0)
                FROM ptt a FULL OUTER JOIN calls b ON a.turn_id = b.turn_id AND a.tool_id = b.tool_id
                GROUP BY 1""",
                params,
            ).fetchall()
            # Per turn: what the tools (all but __unknown__) cost, and what share of each side they hold.
            residual_expr = (
                "ft.output_usd * greatest(0, 1 - coalesce(s.o, 0)) "
                "+ (ft.input_usd + ft.cache_read_usd + ft.cache_creation_usd) * greatest(0, 1 - coalesce(s.i, 0))"
                if mode is AttributionMode.PRORATA
                else "greatest(0, ft.cost_usd - coalesce(s.tools, 0))"
            )
            ((residual,),) = cur.execute(
                f"""WITH {cte}, {per_pair},
                sums AS (
                    SELECT turn_id, sum(input_share) AS i, sum(output_share) AS o,
                        sum(cost) FILTER (WHERE name <> {unknown}) AS tools
                    FROM ptt GROUP BY turn_id)
                SELECT coalesce(sum({residual_expr}), 0) FROM ft LEFT JOIN sums s ON s.turn_id = ft.turn_id""",
                params,
            ).fetchall()
        return per_tool, residual

    def tool_costs(self, f: TurnFilter, mode: AttributionMode) -> ToolCosts:
        """Per-tool cost (in *mode*), calls and last activity, and the residual no tool accounts for."""
        per_tool, residual = self._tool_rows(f, mode)
        cost_by_tool: dict[str, float] = {}
        calls_by_tool: dict[str, int] = {}
        last_active: dict[str, datetime] = {}
        last_seen: dict[str, datetime] = {}
        unknown_usd = 0.0
        for name, cost, costed_turns, calls, both_ts, any_ts, prorata in per_tool:
            if name == SKILL_TOOL:
                continue
            if name == UNKNOWN_TOOL:
                unknown_usd = prorata
                continue
            if costed_turns:
                cost_by_tool[name] = cost
            if calls:
                calls_by_tool[name] = calls
            if both_ts is not None:
                last_active[name] = _utc(both_ts)
            last_seen[name] = _utc(any_ts)
        # Prorata: the residual is the unattributed share plus the unknown slice. Exclude-cache-read
        # already holds it (turn cost minus every tool but unknown), clamped per turn.
        unattributed = residual + (unknown_usd if mode is AttributionMode.PRORATA else 0.0)
        return ToolCosts(cost_by_tool, calls_by_tool, last_active, last_seen, unknown_usd, unattributed)

    def _named_id(self, kind: NameKind, name: str) -> int | None:
        with self._engine.cursor() as cur:
            row = cur.execute(
                f"SELECT name_id FROM {NAMES_TABLE} WHERE kind = ? AND name = ?", [kind.value, name]
            ).fetchone()
        return None if row is None else row[0]

    def tool_detail_rows(
        self, f: TurnFilter, tool: str, recent_since: datetime | None = None
    ) -> list[ToolDetailRow]:
        """Per (project, model) cost and calls of *tool* over turns that cost it or called it.

        *recent_since* (UTC) counts the calls made on or after it. Special names have no detail page.
        """
        if tool in (UNATTRIBUTED_TOOL, UNKNOWN_TOOL, SKILL_TOOL):
            return []
        tool_id = self._named_id(NameKind.TOOL, tool)
        if tool_id is None:
            return []
        where, params = f.where()
        recent = datetime(1, 1, 1) if recent_since is None else recent_since.replace(tzinfo=None)
        with self._engine.cursor() as cur:
            rows = cur.execute(
                f"""SELECT {_LABEL_SQL[GroupKey.PROJECT]}, {_LABEL_SQL[GroupKey.MODEL]},
                    coalesce(sum(pt.cost_prorata), 0), coalesce(sum(pt.output_tokens), 0),
                    coalesce(sum(c.n), 0), coalesce(sum(c.n) FILTER (WHERE {_T}.ts >= ?), 0), max({_T}.ts)
                {self._from(False)}
                LEFT JOIN {PRICED_TURN_TOOLS_VIEW} pt ON pt.turn_id = {_T}.turn_id
                    AND pt.session_ix = {_T}.session_ix AND pt.tool_id = {tool_id}
                LEFT JOIN {TOOL_CALLS_TABLE} c ON c.turn_id = {_T}.turn_id AND c.tool_id = {tool_id}
                WHERE {where} AND (pt.turn_id IS NOT NULL OR c.n > 0)
                GROUP BY 1, 2""",
                [recent, *params],
            ).fetchall()
        return [
            ToolDetailRow(project, model, cost, out, calls, recent_calls, _utc(last))
            for project, model, cost, out, calls, recent_calls, last in rows
        ]

    @staticmethod
    def _daily_window(f: TurnFilter, days: int, today: date | None) -> tuple[TurnFilter, list[date]]:
        end = today or _today()
        start = end - timedelta(days=days - 1)
        lo = start if f.since is None else max(f.since, start)
        hi = end if f.until is None else min(f.until, end)
        return replace(f, since=lo, until=hi), _days(start, end)

    def tool_cost_daily(
        self, f: TurnFilter, tool: str, days: int = DETAIL_DAYS, today: date | None = None
    ) -> list[DailyToolCost]:
        """Per-day prorata cost of *tool* over the last *days* days, zero-filled."""
        window, calendar = self._daily_window(f, days, today)
        tool_id = self._named_id(NameKind.TOOL, tool)
        sums: dict[date, float] = {}
        if tool_id is not None:
            sums = dict(
                self._query(
                    f"CAST({_T}.ts AS DATE), sum(pt.cost_prorata)",
                    window,
                    "GROUP BY 1",
                    priced=False,
                    joins=f"JOIN {PRICED_TURN_TOOLS_VIEW} pt ON pt.turn_id = {_T}.turn_id "
                    f"AND pt.session_ix = {_T}.session_ix AND pt.tool_id = {tool_id}",
                )
            )
        return [DailyToolCost(day, sums.get(day, 0.0)) for day in calendar]

    # -- skills --------------------------------------------------------------

    def skill_costs(self, f: TurnFilter) -> SkillCosts:
        """Cost, calls and last activity per skill; a turn's full cost goes to its attributed skill."""
        where, params = f.where()
        sk = f"sk.kind = {_SKILL_KIND} AND sk.name_id"
        with self._engine.cursor() as cur:
            attributed = cur.execute(
                f"""SELECT sk.name, sum({_P}.cost_usd), max({_T}.ts), sum({_T}.input_tokens + {_T}.output_tokens)
                {self._from(True)} JOIN {NAMES_TABLE} sk ON {sk} = {_T}.attribution_skill_id
                WHERE {where} GROUP BY 1""",
                params,
            ).fetchall()
            invocations = cur.execute(
                f"""SELECT sk.name, sum(c.n) {self._from(False)}
                JOIN {SKILL_CALLS_TABLE} c ON c.turn_id = {_T}.turn_id
                JOIN {NAMES_TABLE} sk ON {sk} = c.skill_id
                WHERE {where} GROUP BY 1""",
                params,
            ).fetchall()
            ((total_cost, total_billable),) = cur.execute(
                f"""SELECT coalesce(sum({_P}.cost_usd), 0), coalesce(sum({_T}.input_tokens + {_T}.output_tokens), 0)
                {self._from(True)} WHERE {where}""",
                params,
            ).fetchall()
        return SkillCosts(
            cost_by_skill={name: cost for name, cost, _ts, _tokens in attributed},
            invocations_by_skill={name: int(n) for name, n in invocations},
            last_active={name: _utc(ts) for name, _cost, ts, _tokens in attributed},
            total_cost=total_cost,
            total_billable_tokens=float(total_billable),
            skill_billable_tokens=float(sum(tokens for *_rest, tokens in attributed)),
        )

    def skill_cost_daily(
        self, f: TurnFilter, skill: str, days: int = DETAIL_DAYS, today: date | None = None
    ) -> list[DailyToolCost]:
        """Per-day full cost of turns attributed to *skill* over the last *days* days, zero-filled."""
        window, calendar = self._daily_window(f, days, today)
        skill_id = self._named_id(NameKind.SKILL, skill)
        sums: dict[date, float] = {}
        if skill_id is not None:
            sums = dict(
                self._query(
                    f"CAST({_T}.ts AS DATE), sum({_P}.cost_usd)",
                    window,
                    f"AND {_T}.attribution_skill_id = {skill_id} GROUP BY 1",
                )
            )
        return [DailyToolCost(day, sums.get(day, 0.0)) for day in calendar]
