"""The SQL price tables and views price exactly as the Python price functions do.

One property over random turns: every model in the price list (plus its
``-thinking`` and ``[1m]`` forms, an unknown Claude name, a non-Claude name and no
model), timestamps either side of every dated rate change, and token counts
either side of every long-prompt threshold, including a 1-hour cache write larger
than the whole creation total.
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tokenol.engine.codes import decode_assumptions
from tokenol.engine.pricing import (
    MODEL_RATES_DDL,
    MODEL_RATES_TABLE,
    MODELS_DDL,
    MODELS_TABLE,
    PRICED_TURN_TOOLS_VIEW,
    PRICED_TURNS_VIEW,
    TURN_TOOLS_TABLE,
    TURNS_TABLE,
    VIEWS_DDL,
    PriceTables,
)
from tokenol.enums import AssumptionTag
from tokenol.ingest.parser import _attribute_cost
from tokenol.metrics.cost import _rates, cache_saved_usd, cost_for_turn, resolve_pricing
from tokenol.model.events import Turn, Usage
from tokenol.model.pricing import CLAUDE_MODELS, rate_rows, select_rate
from tokenol.model.registry import _resolve_cached as resolve_cache
from tokenol.serve.state import _recompute_excl_cache_read

REL = 1e-12
# No real entry has both a dated change and a long-prompt band, so the order in
# which the two apply would go untested; this one has both.
BOTH_KEY = "claude-synthetic-both"
BOTH_ENTRY = {
    "family": "sonnet",
    "context": 200_000,
    "input": 3.0,
    "output": 15.0,
    "cache_write": 3.75,
    "cache_write_1h": 6.0,
    "cache_read": 0.3,
    "long_prompt": {
        "threshold": 150_000,
        "input": 6.0,
        "output": 22.5,
        "cache_write": 7.5,
        "cache_write_1h": 12.0,
        "cache_read": 0.6,
    },
    "earlier": {
        "until": datetime(2026, 3, 1, tzinfo=timezone.utc),
        "input": 2.0,
        "output": 10.0,
        "cache_write": 2.5,
        "cache_write_1h": 4.0,
        "cache_read": 0.2,
    },
}
ALL_ENTRIES = {**CLAUDE_MODELS, BOTH_KEY: BOTH_ENTRY}


@pytest.fixture(scope="module", autouse=True)
def _synthetic_entry():
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(CLAUDE_MODELS, BOTH_KEY, BOTH_ENTRY)
        resolve_cache.cache_clear()  # a string resolved before the patch would stay a fallback
        yield
    resolve_cache.cache_clear()


MODELS: list[str | None] = [
    *ALL_ENTRIES,
    *(f"{key}-thinking" for key in ALL_ENTRIES),
    *(f"{key}[1m]" for key in ALL_ENTRIES),
    "claude-not-a-real-model-99",
    "claude-opus-9-9",
    "gemini-2.5-pro",
    "(unknown)",
    "",
    None,
]
# Only a few models have a dated change or a long-prompt band, so draw them often.
BANDED_MODELS = [
    f"{key}{suffix}"
    for key, entry in ALL_ENTRIES.items()
    if "earlier" in entry or "long_prompt" in entry
    for suffix in ("", "-thinking", "[1m]")
]
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEAR = [
    timedelta(microseconds=-1),
    timedelta(0),
    timedelta(microseconds=1),
    timedelta(days=-30),
    timedelta(days=30),
]


def timestamps(changes: list[datetime]) -> st.SearchStrategy[datetime]:
    """Instants around the model's own price change (any, if it has none)."""
    anywhere = st.builds(lambda secs: EPOCH + timedelta(seconds=secs), st.integers(0, 365 * 86400))
    if not changes:
        return anywhere
    return st.one_of(
        st.builds(lambda c, d: c + d, st.sampled_from(changes), st.sampled_from(NEAR)), anywhere
    )


def pools(thresholds: list[int]) -> st.SearchStrategy[int]:
    """Prompt sizes either side of the model's own long-prompt threshold."""
    anywhere = st.one_of(st.just(0), st.integers(0, 2_000_000))
    if not thresholds:
        return anywhere
    near = st.builds(
        lambda t, d: max(0, t + d), st.sampled_from(thresholds), st.sampled_from([-1, 0, 1])
    )
    return st.one_of(near, near, anywhere)


@st.composite
def usages(draw: st.DrawFn, thresholds: list[int]) -> Usage:
    """Split a pool near a boundary into its three parts; the 1 h share may exceed the total."""
    pool = draw(pools(thresholds))
    cut_a = draw(st.integers(0, pool))
    cut_b = draw(st.integers(cut_a, pool))
    creation = pool - cut_b
    return Usage(
        input_tokens=cut_a,
        cache_read_input_tokens=cut_b - cut_a,
        cache_creation_input_tokens=creation,
        output_tokens=draw(st.one_of(st.just(0), st.integers(0, 500_000))),
        cache_creation_1h_input_tokens=draw(st.integers(0, creation + 1_000)),
    )


@st.composite
def turns(draw: st.DrawFn) -> Turn:
    model = draw(st.one_of(st.sampled_from(BANDED_MODELS), st.sampled_from(MODELS)))
    entry, _tags = resolve_pricing(model)
    changes = [entry["earlier"]["until"]] if entry and "earlier" in entry else []
    thresholds = [entry["long_prompt"]["threshold"]] if entry and "long_prompt" in entry else []
    usage = draw(usages(thresholds))
    # Byte shares each side sum to at most 1, as the parser guarantees.
    names = draw(
        st.lists(st.sampled_from(["Bash", "Read", "Edit", "Grep"]), unique=True, max_size=4)
    )
    weights = [draw(st.floats(0.01, 1.0)) for _ in names]
    room = draw(st.floats(0.0, 1.0))
    out_w = [draw(st.floats(0.0, 1.0)) for _ in names]
    total_in = sum(weights) or 1.0
    total_out = sum(out_w) or 1.0
    input_shares = {n: room * w / total_in for n, w in zip(names, weights, strict=True)}
    out_scale = draw(st.sampled_from([1.0, 0.5]))
    output_shares = {n: w / total_out * out_scale for n, w in zip(names, out_w, strict=True)}
    ts = draw(timestamps(changes))
    return _turn(model, usage, ts, input_shares, output_shares)


def _turn(
    model: str | None,
    usage: Usage,
    ts: datetime,
    input_shares: dict[str, float],
    output_shares: dict[str, float],
) -> Turn:
    tool_costs, *_ = _attribute_cost(model, usage, output_shares, input_shares, at=ts)
    return Turn(
        dedup_key="k",
        timestamp=ts,
        session_id="s",
        model=model,
        usage=usage,
        is_sidechain=False,
        stop_reason=None,
        tool_costs=dict(tool_costs),
    )


def _boundary_batch() -> list[Turn]:
    """Every banded model at each edge of its price change and of its prompt-size threshold."""
    batch: list[Turn] = []
    for key, entry in ALL_ENTRIES.items():
        if "earlier" not in entry and "long_prompt" not in entry:
            continue
        until = entry["earlier"]["until"] if "earlier" in entry else EPOCH
        edge = entry["long_prompt"]["threshold"] if "long_prompt" in entry else 1_000
        for ts in (until - timedelta(microseconds=1), until, until + timedelta(microseconds=1)):
            for pool in (edge - 1, edge, edge + 1):
                creation = pool // 3
                usage = Usage(
                    input_tokens=pool - creation - 7,
                    cache_read_input_tokens=7,
                    cache_creation_input_tokens=creation,
                    output_tokens=1_000,
                    cache_creation_1h_input_tokens=creation + 5,
                )
                batch.append(_turn(key, usage, ts, {"Bash": 0.3, "Read": 0.2}, {"Bash": 0.6}))
    return batch


@functools.cache
def _db() -> tuple[duckdb.DuckDBPyConnection, PriceTables]:
    """One connection holding the price tables; each example refills turns / turn_tools."""
    tables = PriceTables(MODELS)
    con = duckdb.connect()
    con.execute(MODELS_DDL)
    con.execute(MODEL_RATES_DDL)
    con.executemany(
        f"INSERT INTO {MODELS_TABLE} VALUES (?, ?, ?, ?, ?, ?)", tables.models_dim_rows()
    )
    con.executemany(
        f"INSERT INTO {MODEL_RATES_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tables.rows_for_sql(),
    )
    con.execute(
        f"CREATE TABLE {TURNS_TABLE} (turn_id BIGINT, session_ix INTEGER, ts TIMESTAMP, model_id INTEGER, input_tokens BIGINT, "
        "output_tokens BIGINT, cache_read_tokens BIGINT, cache_creation_tokens BIGINT, cache_creation_1h_tokens BIGINT)"
    )
    con.execute(
        f"CREATE TABLE {TURN_TOOLS_TABLE} (turn_id BIGINT, tool_id INTEGER, input_tokens DOUBLE, output_tokens DOUBLE)"
    )
    for ddl in VIEWS_DDL:
        con.execute(ddl)
    return con, tables


def _close(got: float | None, want: float) -> bool:
    return got is not None and got == pytest.approx(want, rel=REL, abs=1e-15)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@example(batch=None)  # None stands for the boundary batch, built once the synthetic entry exists
@given(batch=st.lists(turns(), min_size=1, max_size=6))
def test_sql_prices_match_python(batch: list[Turn]) -> None:
    if batch is None:
        batch = _boundary_batch()
    con, tables = _db()
    con.execute(f"DELETE FROM {TURNS_TABLE}")
    con.execute(f"DELETE FROM {TURN_TOOLS_TABLE}")
    tool_ids = {"Bash": 0, "Read": 1, "Edit": 2, "Grep": 3}
    for i, t in enumerate(batch):
        u = t.usage
        con.execute(
            f"INSERT INTO {TURNS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                i,
                0,
                t.timestamp.replace(tzinfo=None),
                tables.model_id(t.model),
                u.input_tokens,
                u.output_tokens,
                u.cache_read_input_tokens,
                u.cache_creation_input_tokens,
                u.cache_creation_1h_input_tokens,
            ],
        )
        for name, tc in t.tool_costs.items():
            con.execute(
                f"INSERT INTO {TURN_TOOLS_TABLE} VALUES (?, ?, ?, ?)",
                [i, tool_ids[name], tc.input_tokens, tc.output_tokens],
            )

    rows = con.execute(
        f"SELECT turn_id, input_usd, output_usd, cache_read_usd, cache_creation_usd, cost_usd, cache_saved_usd, pool FROM {PRICED_TURNS_VIEW}"
    ).fetchall()
    assert len(rows) == len(batch)  # a rate join that fans out or drops a turn shows here
    priced = {r[0]: r for r in rows}
    for i, t in enumerate(batch):
        want = cost_for_turn(t.model, t.usage, at=t.timestamp)
        entry, tags = resolve_pricing(t.model)
        if entry is not None:
            # Precedence is one definition: exactly one row covers the (time, pool), so row order cannot matter.
            assert (
                sum(r.applies(t.timestamp, t.usage.input_token_pool) for r in rate_rows(entry)) == 1
            )
            assert _rates(entry, t.usage, t.timestamp) == select_rate(
                rate_rows(entry), t.timestamp, t.usage.input_token_pool
            )
        (bits,) = con.execute(
            f"SELECT assumptions FROM {MODELS_TABLE} WHERE model_id = ?", [tables.model_id(t.model)]
        ).fetchone()
        assert decode_assumptions(bits) == [tag for tag in AssumptionTag if tag in want.assumptions]
        _, got_in, got_out, got_cr, got_cc, got_total, got_saved, got_pool = priced[i]
        assert got_pool == t.usage.input_token_pool
        for got, exp in (
            (got_in, want.input_usd),
            (got_out, want.output_usd),
            (got_cr, want.cache_read_usd),
            (got_cc, want.cache_creation_usd),
            (got_total, want.total_usd),
            (got_saved, cache_saved_usd([t])),
        ):
            assert _close(got, exp), (t.model, t.timestamp, t.usage)

    tool_rows = con.execute(
        f"SELECT turn_id, tool_id, cost_prorata, cost_excl_cache_read FROM {PRICED_TURN_TOOLS_VIEW}"
    ).fetchall()
    assert len(tool_rows) == sum(len(t.tool_costs) for t in batch)
    per_tool = {(r[0], r[1]): r for r in tool_rows}
    for i, t in enumerate(batch):
        excl = _recompute_excl_cache_read(t)
        for name, tc in t.tool_costs.items():
            _, _, prorata, excl_cache_read = per_tool[(i, tool_ids[name])]
            assert _close(prorata, tc.cost_usd), (t.model, name)
            assert _close(excl_cache_read, excl[name]), (t.model, name)
