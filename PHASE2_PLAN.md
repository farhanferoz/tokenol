# tokenol — Phase 2 Implementation Plan

*Handoff for the agent implementing Phase 2 of `REPO_PLAN.md`.*

**Rules:**
1. Do **only** what's listed. Do not expand scope.
2. Every numeric threshold, formula, and CLI surface is spelled out below
   — do not reinterpret. If you think a spec is wrong, stop and report,
   do not silently "improve."
3. Follow existing Phase 1 patterns: `Metric` protocols in `metrics/`,
   `AssumptionTag` recording, Rich tables in `report/text.py`, Typer
   commands in `cli.py`.
4. Keep tests green at every step. New features require new tests.
5. Model: Sonnet 4.6 at medium effort.
6. Budget: ~4 focused hours. Stop and report if any task exceeds 60 min
   without a working test.

---

## 0. Preconditions (do first)

Verify before touching anything:
```bash
python -m pytest tests/ -q   # expect 17 passed
ruff check src tests         # expect clean
tokenol daily --since 3d     # expect ccusage parity on reference dates
```
If any fails, stop. Do not proceed.

---

## 1. Extend the data model

### 1.1 Add `is_interrupted: bool` to `Turn`

**File:** `src/tokenol/model/events.py`

Derived field. Simplifies membership tests in hot loops (currently
`AssumptionTag.INTERRUPTED_TURN_SKIPPED in turn.assumptions` runs per
turn in every rollup).

```python
@dataclass
class Turn:
    ...                       # existing fields unchanged
    is_interrupted: bool = False
```

Set it in `build_turns()` when `ev.usage is None`. Then update
`rollup_by_date` to use `turn.is_interrupted` instead of the list
membership check.

### 1.2 Add a `tool_uses` / `tool_errors` counter to `RawEvent`

**File:** `src/tokenol/ingest/parser.py`

Extend `RawEvent` with two new fields:
```python
tool_use_count: int = 0       # number of tool_use blocks in message.content
tool_error_count: int = 0     # number of tool_result blocks with is_error=true
```

These are computed from `message.content` during parsing. Iterate the
content list (if present); count blocks where:
- `type == "tool_use"` → `tool_use_count`
- `type == "tool_result"` AND `is_error == true` → `tool_error_count`

Note: `tool_result` typically appears in `user` events (the follow-up
after a tool_use), not `assistant` events. Count them from whichever
event type they live in. Both user and assistant events need parsing,
but only assistant events get deduplicated.

### 1.3 Populate the `Session` dataclass

**File:** `src/tokenol/ingest/builder.py`

Add a second builder function:
```python
def build_sessions(turns: list[Turn]) -> list[Session]:
    """Group turns by session_id. One Session per JSONL file."""
```
Sort turns within a session by timestamp. Populate `is_sidechain` from
any turn's flag (all turns in one file share it).

---

## 2. Context metrics (`metrics/context.py`)

**New file.** Implement these as pure functions, one per metric. All
take `list[Turn]` and return a scalar or a list.

### 2.1 Total context per turn

```python
def context_tokens(turn: Turn) -> int:
    """input + cache_read + cache_creation (what the model 'sees')."""
    u = turn.usage
    return u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
```

### 2.2 Max turn input (per session)

```python
def max_turn_input(turns: list[Turn]) -> int:
    return max((context_tokens(t) for t in turns), default=0)
```

### 2.3 Cache reuse ratio

```python
def cache_reuse_ratio(turns: list[Turn]) -> float | None:
    reads = sum(t.usage.cache_read_input_tokens for t in turns)
    creates = sum(t.usage.cache_creation_input_tokens for t in turns)
    denom = reads + creates
    return reads / denom if denom > 0 else None
```

### 2.4 Non-cached input ratio

```python
def non_cached_input_ratio(turns: list[Turn]) -> float | None:
    raw = sum(t.usage.input_tokens for t in turns)
    total = sum(context_tokens(t) for t in turns)
    return raw / total if total > 0 else None
```

### 2.5 Context growth rate

Simple linear regression slope over (turn_index, context_tokens).
Use `statistics.linear_regression` if available (Python 3.10+), else
compute manually:

```python
def context_growth_rate(turns: list[Turn]) -> float:
    """Tokens added to context per turn. Uses least-squares slope."""
    n = len(turns)
    if n < 2: return 0.0
    xs = list(range(n))
    ys = [context_tokens(t) for t in turns]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den > 0 else 0.0
```

Sort turns by timestamp before calling. Units: tokens per turn.

### 2.6 Tests

Create `tests/test_context_metrics.py` with hand-computed expected
values. Use `basic.jsonl` (2 turns: ctx 1600 and 3000 → growth = 1400
tokens/turn, max = 3000).

---

## 3. 5-hour window alignment (`metrics/windows.py`)

**New file.** Rule from `REPO_PLAN.md §3`: "Boundary = first billable
event starts a window; window runs 5h wall-clock; next event after
expiry starts a new window."

### 3.1 Algorithm

```python
from datetime import timedelta

WINDOW_DURATION = timedelta(hours=5)

@dataclass
class Window:
    start: datetime             # first event timestamp
    end: datetime               # start + 5h (closed below, open above)
    turns: list[Turn]

    @property
    def cost_usd(self) -> float: return sum(t.cost_usd for t in self.turns)
    @property
    def total_tokens(self) -> int: return sum(context_tokens(t) for t in self.turns)
```

### 3.2 Window builder

```python
def align_windows(turns: list[Turn]) -> list[Window]:
    """Partition turns into 5-hour wall-clock windows.

    Only billable turns (not interrupted) open windows; interrupted
    turns are attached to whichever window contains their timestamp,
    or dropped if before the first window.
    """
```

Steps:
1. Sort turns by timestamp ascending.
2. Iterate; for each billable turn, if no active window OR timestamp >=
   active_window.end, start a new window at that timestamp.
3. Attach each turn (billable or not) to the most recent window whose
   [start, end) contains its timestamp. Drop turns before the first
   window.

Edge case: when `start + 5h` equals next turn's timestamp exactly →
that turn starts a new window (half-open interval).

Record `AssumptionTag.WINDOW_BOUNDARY_HEURISTIC` once per `align_windows`
call (don't spam per turn).

### 3.3 Burn-rate projection

```python
def project_window(active: Window, now: datetime, lookback: timedelta) -> dict:
    """Given the active window and current time, extrapolate usage to
    window.end using the last *lookback*-minutes burn rate.

    Returns: {
        'elapsed_in_window': timedelta,
        'remaining_in_window': timedelta,
        'recent_cost': float,     # cost in last lookback minutes
        'burn_rate_usd_per_hour': float,
        'projected_window_cost': float,
        'over_reference': bool,   # True if projected > $50 (flag, not cap)
    }
    ```

Formula:
- `elapsed = now - active.start`
- `remaining = max(active.end - now, 0)`
- `recent_turns = [t for t in active.turns if t.timestamp >= now - lookback]`
- `recent_cost = sum(cost_usd)` for those
- `burn_rate_usd_per_hour = recent_cost / lookback.total_seconds() * 3600`
- `projected_window_cost = active.cost_usd + burn_rate_usd_per_hour * remaining.total_seconds()/3600`
- `over_reference = projected_window_cost > 50.0` (hardcoded reference
  for v0.1; configurable later)

### 3.4 Tests

`tests/test_windows.py` with a synthetic fixture containing 3 turns at
`T`, `T+2h`, `T+6h` → expect 2 windows.

---

## 4. Per-session / per-project / per-model rollups

### 4.1 `metrics/rollups.py` (new file)

Three classes sharing a base. Implement as dataclasses with an
`accumulate(turn)` method so the `_accumulate_turn` helper in `cost.py`
can be generalized.

Actually: **do not** generalize `_accumulate_turn`. Copy the additive
fields (turns, tokens, cost) but keep each rollup dataclass specific to
its grouping key. Premature abstraction hurt us before.

```python
@dataclass
class SessionRollup:
    session_id: str
    source_file: str
    is_sidechain: bool
    first_ts: datetime
    last_ts: datetime
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    max_turn_input: int
    cache_reuse_ratio: float | None
    tool_use_count: int
    tool_error_count: int
    verdict: BlowUpVerdict       # computed post-rollup; see §5
```

Similar shapes for `ProjectRollup` (key = cwd) and `ModelRollup`
(key = `model` string).

### 4.2 CWD extraction for project grouping

`cwd` lives in `system` events inside the JSONL (schema has
`cwd` field on some system events). Scan once per file, take the first
non-null `cwd` seen. If none, fall back to config dir path. Store on
`Session`.

Add to `parse_file`: yield system events too (we already yield all
types — builder can filter). Add `cwd: str | None` to `Session` and
populate in `build_sessions`.

---

## 5. Blow-up verdicts (`metrics/verdicts.py`)

**New file.** Pure function over a `SessionRollup`, returns a
`BlowUpVerdict`.

### 5.1 Thresholds (v0.1 — hardcoded, documented in `docs/METRICS.md`)

| Verdict | Condition |
|---|---|
| `RUNAWAY_WINDOW` | session's peak 5h-window cost > **$50** |
| `CONTEXT_CREEP` | `max_turn_input` > **500_000** AND `context_growth_rate` > **2000** tokens/turn |
| `SIDECHAIN_HEAVY` | session is itself sidechain AND cost > **$5** |
| `TOOL_ERROR_STORM` | `tool_use_count` >= **10** AND `tool_error_count / tool_use_count` > **0.3** |
| `OK` | none of the above |

### 5.2 Evaluation order

First matching rule wins, in this order:
`RUNAWAY_WINDOW` → `CONTEXT_CREEP` → `TOOL_ERROR_STORM` → `SIDECHAIN_HEAVY` → `OK`

A session can only carry one verdict label. (Multi-label is a Phase 3
concern.)

### 5.3 Tests

`tests/test_verdicts.py` with a handcrafted `SessionRollup` per verdict
plus one `OK` case.

---

## 6. CLI commands

### 6.1 `tokenol live --last <duration>`

```bash
tokenol live --last 5m
tokenol live --last 45m
tokenol live --last 2h
```

**Required** `--last`. Accepts `Nm`, `Nh`, `Ns` (minutes, hours,
seconds). Reject bare numbers.

**Output** (single table, no footer):
```
Active 5h window: started 2026-04-20 14:05 UTC, 2h47m elapsed, 2h13m remaining
Last 20m:   18 turns,  $3.82 spent,  burn rate $11.46/hr
Window:     $7.24 spent,  projected $32.67 at end of window
```

One-shot only. No `--refresh` in Phase 2 (dashboard covers that).

Exit code 1 if `over_reference` is True (lets users chain with `&&`).

### 6.2 `tokenol sessions --top N --sort KEY`

Default: `--top 10 --sort cost`.
Sort keys (enum-backed, enforce via Typer enum):
```
cost | input | output | cache_read | turns | max_input | duration
```

Output columns:
```
Session | Model | Start | Turns | Max input | Cost | Verdict
```

Truncate `session_id` to first 8 chars. Show `Verdict` colored (green
OK, red CONTEXT_CREEP, etc.).

### 6.3 `tokenol projects [--since 14d]`

Output columns:
```
Project (cwd) | Sessions | Turns | Tokens | Cost | Cache reuse %
```

Sort by cost desc. Show share-of-total in a trailing row.

### 6.4 `tokenol models [--since 14d]`

Output columns:
```
Model | Turns | Input | Output | Cache read | Cost | Tool-error %
```

Sort by cost desc.

---

## 7. Wire up dormant flags from Phase 1

- `--strict`: if set, raise `typer.BadParameter` when any assumption
  fired during the run. Check via `assumption_recorder.fired()` after
  `_load_turns`, before rendering.
- `--show-assumptions`: force the footer to print even if empty (useful
  for CI output). Current behavior prints only when non-empty.
- `--log-level`: set root `logging` level at CLI entry point. Use
  Python's `logging` module — don't invent a new one.

Add a `tests/test_cli.py` with Typer's `CliRunner` covering:
- `--strict` exits non-zero when assumptions fire
- `--show-assumptions` prints footer when no assumptions
- Each new command returns 0 on the fixtures dir

---

## 8. Documentation

### 8.1 `docs/METRICS.md`

Every metric from §2, §3, §5 with:
- Definition
- Formula
- Units
- Known failure modes / edge cases

### 8.2 `docs/ASSUMPTIONS.md`

Reproduce §14 from `REPO_PLAN.md` as a standalone reference, adding any
new tags introduced in Phase 2.

### 8.3 Update `REPO_PLAN.md`

Strike through completed Phase 2 bullets in §6. Move Phase 2 notes
discovered during implementation into the relevant doc files.

---

## 9. Out of scope for Phase 2

Do **not**:
- Implement the dashboard (`tokenol serve`) — Phase 4
- Implement `tokenol pivot`, `tokenol watch`, or HTML reports — post-v1
- Tune verdict thresholds per-user or via config file — post-v1
- Link sidechain sessions to parent sessions across files — v0.2 (needs
  probe work; for Phase 2, sidechains aggregate as their own sessions
  with `is_sidechain=True`)
- Compute historical percentiles for `RUNAWAY_WINDOW` — v0.2

Add anything that feels worth doing but isn't listed here to a new
`FUTURE_WORK.md` file under a dated section.

---

## 10. Exit criteria (matches `REPO_PLAN.md §6`)

All must pass:
1. `tokenol live --last 20m` answers "am I burning too fast right now?"
2. `tokenol sessions --top 5 --sort cost` answers "which sessions caused
   my expensive day?"
3. `tokenol projects --since 14d` and `tokenol models --since 14d`
   answer "what dominated my last two weeks?"
4. `tokenol daily` still matches ccusage within 2% (Phase 1 guarantee
   must not regress).
5. All new metrics have unit tests with hand-computed expected values.
6. `ruff check` clean, all tests pass.
7. Verdict thresholds from §5.1 have a test per branch.

---

## 11. Success handoff

When done, post a summary to the user with:
- New files added (list)
- Test count before → after (should be 17 → ~35)
- One example of each new CLI command's output (pasted verbatim)
- Any decisions made where the spec was ambiguous (should be zero if
  this plan is sufficient — but if you make any, flag them)

If you hit a blocker that isn't resolved by this document, stop and ask.
Do not guess at thresholds, formulas, or CLI shapes.
