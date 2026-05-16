# tokenol Metrics Reference

All metric definitions, formulas, units, and known failure modes.

---

## Context Axis (`metrics/context.py`)

### `context_tokens(turn)`

**Definition:** Total tokens the model "sees" for a single turn.

**Formula:** `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`

**Units:** tokens

**Failure modes:** Does not include `output_tokens` (the model's generated response). This is intentional — context_tokens measures what is *sent in*, not what is generated.

---

### `max_turn_input(turns)`

**Definition:** The largest `context_tokens` value across all turns in a session.

**Formula:** `max(context_tokens(t) for t in turns)`

**Units:** tokens

**Failure modes:** Returns 0 for an empty turn list.

---

### `cache_reuse_ratio(turns)`

**Definition:** Fraction of cache-involved tokens that were reads (reuses) vs creations (writes).

**Formula:** `cache_read / (cache_read + cache_creation)`

**Units:** dimensionless ratio in [0, 1]. None if no cache activity.

**Failure modes:** Returns None when `cache_read + cache_creation == 0`. A ratio near 0 means cache is being written but not reused — inefficient. A ratio near 1 means nearly all cache activity is reads — very efficient.

---

### `non_cached_input_ratio(turns)`

**Definition:** Fraction of total context tokens that are plain (non-cached) input.

**Formula:** `sum(input_tokens) / sum(context_tokens)`

**Units:** dimensionless ratio in [0, 1]. None if total context is 0.

**Failure modes:** Returns None for empty turns. A high value (close to 1) means very little caching is occurring.

---

### `context_growth_rate(turns)`

**Definition:** Tokens added to context per additional turn. Computed as least-squares linear regression slope.

**Formula:** Sorts turns by timestamp, then:
```
mean_x = (n-1) / 2
mean_y = mean(context_tokens)
slope = sum((x - mean_x) * (y - mean_y)) / sum((x - mean_x)^2)
```

**Units:** tokens per turn

**Failure modes:** Returns 0.0 for fewer than 2 turns. Does not account for non-linear growth patterns (e.g. periodic context resets). A slope of 0 may mean the session was compacted mid-run.

---

## Window Axis (`metrics/windows.py`)

### 5-Hour Window

**Definition:** A rate-limit window starting at the first billable event and lasting 5 wall-clock hours. The next billable event after the window expires starts a new window.

**Algorithm:**
1. Sort turns by timestamp ascending.
2. For each billable turn (not interrupted): if no active window or `timestamp >= active_window.end`, start a new window.
3. Interrupted turns are attached to the window whose `[start, end)` contains their timestamp. Interrupted turns before the first window are dropped.

**Edge case:** A turn at exactly `window.end` starts a new window (half-open interval).

**Assumption tag:** `WINDOW_BOUNDARY_HEURISTIC` — the exact server-side rule is not published; this matches community reverse-engineering.

---

### `project_window(active, now, lookback)`

**Definition:** Extrapolates the active window's cost to its end using the recent burn rate.

**Formulas:**
- `elapsed = now - active.start`
- `remaining = max(active.end - now, 0)`
- `recent_turns = turns with timestamp >= now - lookback`
- `recent_cost = sum(cost_usd for recent_turns)`
- `burn_rate_usd_per_hour = recent_cost / lookback_hours`
- `projected_window_cost = active.cost_usd + burn_rate_usd_per_hour * remaining_hours`
- `over_reference = projected_window_cost > $50.00`

**Failure modes:** If lookback is very short and no turns fall in it, burn rate will be 0 and projection will equal current window cost. The `$50` reference threshold is hardcoded v0.1; configurable in a future release.

---

## Blow-Up Verdicts (`metrics/verdicts.py`)

Per-session verdict. First matching rule wins. Evaluated in this order:

| Verdict | Condition |
|---|---|
| `RUNAWAY_WINDOW` | `peak_window_cost > $50` |
| `CONTEXT_CREEP` | `max_turn_input > 500,000` AND `context_growth_rate > 2,000` tokens/turn |
| `TOOL_ERROR_STORM` | `tool_use_count >= 10` AND `tool_error_count / tool_use_count > 0.3` |
| `SIDECHAIN_HEAVY` | session is a sidechain AND `cost_usd > $5` |
| `OK` | none of the above |

**Thresholds are hardcoded in v0.1.** Configurable per-user in a future release.

**Failure modes:**
- `RUNAWAY_WINDOW` uses a strict `>` (not `>=`), so exactly $50 is OK.
- `TOOL_ERROR_STORM` uses strict `>0.3` (not `>=`), so exactly 30% error rate is OK.
- A session carries exactly one verdict; the first matching rule wins.

---

## Rollup Metrics (`metrics/rollups.py`)

### `SessionRollup`

Aggregated per-session metrics. Token sums are over billable (non-interrupted) turns only. Tool counts include all turns.

Key fields:
- `max_turn_input`: largest context window used in any single turn
- `cache_reuse_ratio`: session-level read/(read+write) ratio
- `context_growth_rate_val`: slope of context size per turn
- `peak_window_cost`: maximum cost within any single 5h window in the session
- `verdict`: blow-up verdict

### `ProjectRollup`

Aggregated by `cwd`. Sessions with no `cwd` are grouped under `(unknown)`.

### `ModelRollup`

Aggregated by `model` string. Turns with no model are grouped under `(unknown)`.

### `DailyToolCost` (0.6.0+)

Per-day `cost_usd` for a single tool over a rolling window. Built by `build_tool_cost_daily(turns, *, tool_name, days=30, today=None)`. Buckets are zero-filled so the chart x-axis always has `days` points. `today` defaults to UTC; local-TZ callers risk a day's drift around midnight UTC.

---

## Tool Cost Attribution (`ingest/parser.py`, `serve/state.py`)

The per-tool cost attribution model decomposes each billable turn's `cost_usd` across the tools it invoked. The split does **not** change the billable cost — it only assigns shares — and the per-tool / unattributed contributions sum to the turn total up to floating-point rounding.

### `Turn.tool_costs`

**Definition:** `dict[str, ToolCost]` mapping tool name → `ToolCost(input_tokens, output_tokens, cost_usd)`. One entry per tool that contributed to the turn under the default pro-rata model. Empty (`EMPTY_TOOL_COSTS` sentinel) for non-assistant turns and turns with no tool activity.

`ToolCost.input_tokens` and `ToolCost.output_tokens` are fractional (`float`) — the result of `token_pool * byte_share` and `output_tokens * byte_share` respectively. They are stored, not recomputed, so downstream attribution modes can recover the original share.

**Source:** Computed in `parser._attribute_cost` at parse time. Stored on `RawEvent.tool_costs` and propagated to `Turn.tool_costs` by `serve.state._build_turns_and_sessions`.

### Pro-rata attribution (default) — `parser._attribute_cost`

**Per-turn inputs:**

- `usage` — the assistant `usage` payload (input / output / cache_read / cache_creation tokens).
- `output_shares: dict[str, float]` — `tool_use` block bytes / total message bytes, computed once per turn from this turn's content.
- `input_shares: dict[str, float]` — `bytes_in_context_by_tool[name] / total_context_bytes`, where the per-session byte tallies accumulate over all prior turns since the last compaction reset. `total_context_bytes` includes a `non_tool_bytes_in_context` bucket covering text / thinking / sentinel-named blocks so non-tool content reduces per-tool shares rather than inflating them.

**Pre-computed cost / token pools:**

```
input_cost_pool   = input_usd + cache_read_usd + cache_creation_usd
input_token_pool  = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
```

**Per-tool result:**

```
ToolCost[name].input_tokens  = input_token_pool * input_shares.get(name, 0)
ToolCost[name].output_tokens = output_tokens    * output_shares.get(name, 0)
ToolCost[name].cost_usd      = output_usd       * output_shares.get(name, 0)
                             + input_cost_pool  * input_shares.get(name, 0)
```

**Unattributed leg** (stored on the same RawEvent / Turn as `unattributed_input_tokens`, `unattributed_output_tokens`, `unattributed_cost_usd`):

```
unattr_in_share  = max(0, 1 - sum(input_shares.values()))
unattr_out_share = max(0, 1 - sum(output_shares.values()))
unattributed_cost_usd = output_usd * unattr_out_share + input_cost_pool * unattr_in_share
```

The `max(0, ...)` guards protect the residual leg from going negative if a caller violates the precondition `sum(shares) <= 1.0`; they do **not** rescale per-tool amounts.

**Invariant:** `sum(tc.cost_usd for tc in tool_costs.values()) + unattributed_cost_usd == cost_for_turn(model, usage).total_usd`, up to floating-point rounding (typically < 1e-9 USD per turn).

### `excl_cache_read` attribution (Tool Mix panel, 0.6.1) — `serve.state._recompute_excl_cache_read`

**Difference:** `cache_read_usd` is dropped from the per-tool input cost pool entirely; what would have been distributed by `input_shares` flows into the non-tool residual.

**Formula** (per turn, applied at request time from the stored fractional tokens):

```
input_pool_excl = input_usd + cache_creation_usd                              # cache_read_usd omitted
in_share  = tc.input_tokens  / input_token_pool      # recover original share from stored fractional tokens
out_share = tc.output_tokens / output_token_count
new_cost[name] = in_share * input_pool_excl + out_share * output_usd
```

`input_token_pool` keeps all three input token components — only the **cost** pool changes, so share recovery is exact.

The aggregator (`state.build_breakdown_tools`) computes the residual as:

```
unattributed = cost_for_turn(model, usage).total_usd - sum(new_cost[name] for name in real_tool_names)
```

This is cheap — one cost-pricing call plus `len(tool_costs)` divisions per turn — and runs only when the Tool Mix panel toggle is set to `excl_cache_read`. All other surfaces (scorecards, project / model `by_tool`, tool detail page, daily charts) stay on pro-rata.

### Compaction reset (`parser.parse_file`)

**Condition:** `input_pool < COMPACTION_DROP_RATIO * peak_input_tokens` where `COMPACTION_DROP_RATIO = 0.2` and `peak_input_tokens` is the session's running maximum of `input + cache_read + cache_creation` tokens.

**Effect:** Clears `tool_use_id_to_name`, `bytes_in_context_by_tool`, and `non_tool_bytes_in_context`. Resets `peak_input_tokens = input_pool` so the running peak follows the post-compaction baseline (a session that genuinely stabilises below 20 % of its prior peak doesn't re-trigger on every turn).

**Consequence on attribution:** The turn where compaction is detected has `input_shares == {}` so its entire `input_cost_pool` attributes to `__unattributed__`. The output side still attributes via this turn's `output_shares` if `tool_use` blocks are present.

---

## Public API — Per-Tool Cost Fields (0.6.0+)

### `GET /api/breakdown/tools`

Query parameters:

- `range` — `7d | 30d | 90d | all` (existing). Bad values return 400.
- `mode` — `prorata` (default) or `excl_cache_read` (0.6.1). Unknown values fall back to `prorata` silently (forward-compatible: older servers degrade gracefully when clients persist a newer mode token).

Response envelope:

```json
{
  "range": "30d",
  "mode": "prorata",
  "tools": [
    {"name": "Read",   "cost_usd": 12.34, "count": 412, "last_active": "2026-05-16T13:00:00Z"},
    {"name": "Bash",   "cost_usd":  8.10, "count": 187, "last_active": "2026-05-16T12:55:00Z"},
    ...
    {"name": "other",  "cost_usd":  4.20, "count":  95, "tool_count": 14},
    {"name": "__unattributed__", "cost_usd": 5.67}
  ]
}
```

The `tools` list is top-10 tools ranked by `cost_usd`, followed by an `other` tail row that collapses lower-ranked tools (its `count` is the sum of those tools' invocations; `tool_count` is the number of tools collapsed), followed by an `__unattributed__` sentinel row (which carries only `name` and `cost_usd`). Each top-10 row carries `last_active` as an ISO-8601 string; the frontend's tokens-mode toggle uses `count` for bar values (no separate `tokens` field).

### `GET /api/tool/{name}`

Returns:

- `scorecards` — `{cost_usd, output_tokens, invocations, top_project: {cwd, label, cost_usd}}`. Values cover the requested range.
- `daily_cost` — list of 30 zero-filled daily points: `[{date: "YYYY-MM-DD", cost_usd: float}, ...]`.
- `by_project` — ranked-bar input: `[{cwd, project_label, cost_usd, count, last_active}, ...]`.
- `by_model` — ranked-bar input: `[{model, cost_usd, count, last_active}, ...]`.

Replaces the pre-0.6.0 `projects_using_tool` / `models_using_tool` arrays.

### `GET /api/project/{cwd_b64}`

Adds a `by_tool` block containing ranked per-tool cost for the project within the requested range.

### `GET /api/model/{name}`

Adds a `by_tool` block containing ranked per-tool cost for the model within the requested range.
