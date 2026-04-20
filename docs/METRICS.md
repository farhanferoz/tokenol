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
