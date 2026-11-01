"""Price tables and priced views for the usage engine's SQL.

The rates come from ``model.pricing.rate_rows`` (the one precedence definition),
so SQL and ``metrics.cost.cost_for_turn`` cannot disagree on which rate applies.
The views repeat only ``cost_for_turn``'s arithmetic: tokens x rate / 1e6, with
the 1 hour cache-write share clamped to the creation total.

Tables the views read (the engine schema creates them; ``PriceTables`` fills the
first two):

* ``models(model_id, model, entry_key, price_status, context_window, assumptions)``
* ``model_rates(model_id, valid_from, valid_until, pool_above, pool_at_most,
  input_rate, output_rate, cache_write_rate, cache_write_1h_rate, cache_read_rate)``
* ``turns(turn_id, ts, model_id, input_tokens, output_tokens, cache_read_tokens,
  cache_creation_tokens, cache_creation_1h_tokens)``; ``ts`` is naive UTC
* ``turn_tools(turn_id, tool_id, input_tokens, output_tokens)``; the stored,
  fractional token shares of each tool
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from tokenol.engine.codes import encode_assumptions
from tokenol.metrics.cost import TOKENS_PER_MILLION, price_status, resolve_pricing
from tokenol.model.pricing import context_window, entry_key, rate_rows

MODELS_TABLE = "models"
MODEL_RATES_TABLE = "model_rates"
TURNS_TABLE = "turns"
TURN_TOOLS_TABLE = "turn_tools"
PRICED_TURNS_VIEW = "priced_turns"
PRICED_TURN_TOOLS_VIEW = "priced_turn_tools"

MODELS_DDL = f"""CREATE TABLE {MODELS_TABLE} (
    model_id INTEGER PRIMARY KEY, model VARCHAR, entry_key VARCHAR, price_status VARCHAR,
    context_window INTEGER, assumptions INTEGER)"""
MODEL_RATES_DDL = f"""CREATE TABLE {MODEL_RATES_TABLE} (
    model_id INTEGER, valid_from TIMESTAMP, valid_until TIMESTAMP,
    pool_above BIGINT, pool_at_most BIGINT, input_rate DOUBLE, output_rate DOUBLE,
    cache_write_rate DOUBLE, cache_write_1h_rate DOUBLE, cache_read_rate DOUBLE)"""

_M = float(TOKENS_PER_MILLION)

# A model with no rate row (unknown or non-Claude) matches nothing in the left
# join, so its rates are NULL and coalesce to 0, as cost_for_turn prices it.
PRICED_TURNS_DDL = f"""CREATE OR REPLACE VIEW {PRICED_TURNS_VIEW} AS
SELECT turn_id, ts, model_id, pool,
    input_usd, output_usd, cache_read_usd, cache_creation_usd,
    input_usd + output_usd + cache_read_usd + cache_creation_usd AS cost_usd,
    cache_read_tokens * input_rate / {_M} - cache_read_tokens * cache_read_rate / {_M}
        AS cache_saved_usd
FROM (
    SELECT t.turn_id, t.ts, t.model_id, t.pool, t.cache_read_tokens,
        coalesce(r.input_rate, 0) AS input_rate,
        coalesce(r.cache_read_rate, 0) AS cache_read_rate,
        t.input_tokens * coalesce(r.input_rate, 0) / {_M} AS input_usd,
        t.output_tokens * coalesce(r.output_rate, 0) / {_M} AS output_usd,
        t.cache_read_tokens * coalesce(r.cache_read_rate, 0) / {_M} AS cache_read_usd,
        (t.cache_creation_tokens - t.cache_1h_tokens) * coalesce(r.cache_write_rate, 0) / {_M}
            + t.cache_1h_tokens * coalesce(r.cache_write_1h_rate, 0) / {_M} AS cache_creation_usd
    FROM (
        SELECT *, input_tokens + cache_read_tokens + cache_creation_tokens AS pool,
            least(cache_creation_1h_tokens, cache_creation_tokens) AS cache_1h_tokens
        FROM {TURNS_TABLE}
    ) AS t
    LEFT JOIN {MODEL_RATES_TABLE} AS r
        ON r.model_id = t.model_id
        AND (r.valid_from IS NULL OR t.ts >= r.valid_from)
        AND (r.valid_until IS NULL OR t.ts < r.valid_until)
        AND (r.pool_above IS NULL OR t.pool > r.pool_above)
        AND (r.pool_at_most IS NULL OR t.pool <= r.pool_at_most)
)"""

# Per-tool dollars from token shares (never stored dollars). A zero denominator
# contributes 0, as ingest.parser._attribute_cost and
# serve.state._recompute_excl_cache_read do.
PRICED_TURN_TOOLS_DDL = f"""CREATE OR REPLACE VIEW {PRICED_TURN_TOOLS_VIEW} AS
SELECT turn_id, tool_id, input_tokens, output_tokens, input_share, output_share,
    output_share * output_usd + input_share * (input_usd + cache_read_usd + cache_creation_usd)
        AS cost_prorata,
    output_share * output_usd + input_share * (input_usd + cache_creation_usd)
        AS cost_excl_cache_read
FROM (
    SELECT tt.turn_id, tt.tool_id, tt.input_tokens, tt.output_tokens,
        CASE WHEN p.pool > 0 THEN tt.input_tokens / p.pool ELSE 0 END AS input_share,
        CASE WHEN tu.output_tokens > 0 THEN tt.output_tokens / tu.output_tokens ELSE 0 END
            AS output_share,
        p.input_usd, p.output_usd, p.cache_read_usd, p.cache_creation_usd
    FROM {TURN_TOOLS_TABLE} AS tt
    JOIN {PRICED_TURNS_VIEW} AS p ON p.turn_id = tt.turn_id
    JOIN {TURNS_TABLE} AS tu ON tu.turn_id = tt.turn_id
)"""

VIEWS_DDL = (PRICED_TURNS_DDL, PRICED_TURN_TOOLS_DDL)


def _naive_utc(moment: datetime | None) -> datetime | None:
    """The engine stores timestamps as naive UTC."""
    return None if moment is None else moment.astimezone(timezone.utc).replace(tzinfo=None)


class PriceTables:
    """The ``models`` and ``model_rates`` rows for the distinct model strings in the engine.

    A model id is the model's position in sorted order (None first), so the ids
    are stable for a given set of strings.
    """

    def __init__(self, models: Iterable[str | None]) -> None:
        self._models: tuple[str | None, ...] = tuple(
            sorted(set(models), key=lambda m: (m is not None, m or ""))
        )

    def model_id(self, model: str | None) -> int:
        return self._models.index(model)

    def rows_for_sql(self) -> list[tuple]:
        """``model_rates`` rows: one per rate row of every priced model."""
        rows: list[tuple] = []
        for model_id, model in enumerate(self._models):
            entry, _tags = resolve_pricing(model)
            if entry is None:
                continue
            for row in rate_rows(entry):
                rates = row.rates
                rows.append(
                    (
                        model_id,
                        _naive_utc(row.valid_from),
                        _naive_utc(row.valid_until),
                        row.pool_above,
                        row.pool_at_most,
                        rates["input"],
                        rates["output"],
                        rates["cache_write"],
                        rates["cache_write_1h"],
                        rates["cache_read"],
                    )
                )
        return rows

    def models_dim_rows(self) -> list[tuple]:
        """``models`` rows: id, string, entry key (NULL if unpriced), status, context, tag bits."""
        rows: list[tuple] = []
        for model_id, model in enumerate(self._models):
            entry, tags = resolve_pricing(model)
            key = None if entry is None else entry_key(entry)
            # Exact price-list keys only, as the context ratio has always looked it up.
            window = None if model is None else context_window(model)
            rows.append(
                (model_id, model, key, price_status(model).value, window, encode_assumptions(tags))
            )
        return rows
