# Per-tool attribution mode toggle (Tool Mix panel)

**Status:** approved
**Owner:** Farhan
**Target version:** 0.6.1
**Spec date:** 2026-05-16

## Summary

Add a two-position mode toggle to the Tool Mix panel on `/breakdown`. The toggle lets the user re-cast the per-tool cost figures under one alternative mechanically-defined formula. Default is the current pro-rata math; the second mode credits `cache_read` cost entirely to the non-tool residual instead of distributing it across tools.

Scope is intentionally narrow: Tool Mix bars and subtitle only. No other panel changes, no persistence changes, no parser changes.

## Motivation

On a 7-day window of heavy Claude Code usage the Tool Mix subtitle currently reports `$1,041 tool cost · $828 non-tool`. The split surprises the user — intuition says non-tool (system prompts, thinking, plain text) should dominate. Investigation shows the math is correct: `cache_read` is ~65% of total billing (e.g. $1,225 of $1,874 over the window) and the byte-share pro-rata redistributes that cache_read pool over visible content. Tool I/O (`Read` returns, `Bash` stdout, `Edit` tool_result payloads) is the largest visible-byte source, so it absorbs the largest slice of cache_read cost — including the slice that conceptually represents conversation overhead.

The number isn't wrong, but it isn't the only defensible attribution. A second mode that excludes `cache_read` from the input pool answers a different, equally legitimate question: "what does using these tools cost me, excluding the cost of keeping their output around in subsequent turns?" The user wants to see both lenses without committing to one.

## Goals

- Expose the current pro-rata attribution alongside one alternative, switchable from the Tool Mix panel header.
- Keep the math fully mechanical — no heuristics, no estimates, no fudge factors. Every dollar in the alternative mode must be derivable from API billing components and stored per-turn data.
- Keep total cost invariant across modes (only the tool-vs-non-tool split changes).
- Zero impact on persisted data, parser code, ingest path, or any other panel.
- Default behaviour schema-compatible with today's `/api/breakdown/tools` response: existing clients keep working. The response gains a new informational `mode` field (always echoed, defaulting to `prorata`); the `tools` array's per-element shape is unchanged.

## Non-goals

- No change to scorecards, daily charts, by-project breakdown, by-model breakdown, the tool detail page (`/tool/{name}`), or the model detail page (`/model/{name}`). Every other surface stays on pro-rata, always.
- No estimated quantities — explicitly **not** shipping the previously-considered "credit system prompt to non-tool" mode, which would require guessing system-prompt token size.
- No `tokens` unit changes. Token counts already aren't redistributed by mode; the toggle is hidden when the panel is displaying token counts.
- No new persistence column. Existing DuckDB schema v2 stays as-is.
- No URL state. Mode preference is local-only, like the existing per-panel period and unit toggles.
- No `prefs.json` surface. No server-side default override.
- Not back-fillable across modes for arbitrary historical windows in any way that differs from today's behaviour: both modes work on whatever turns are already in the window.

## Modes

| Mode key | Pill label | Formula (per turn, per tool) |
|---|---|---|
| `prorata` (default) | `PRO-RATA` | `in_share * (input_usd + cache_read_usd + cache_creation_usd) + out_share * output_usd` |
| `excl_cache_read` | `EXCL CACHE-READ` | `in_share * (input_usd + cache_creation_usd) + out_share * output_usd`; `cache_read_usd` flows 100% to the non-tool residual |

Where `in_share` and `out_share` are the per-tool byte shares computed at parse time and reconstructable from stored per-tool `input_tokens` / `output_tokens` divided by the turn's raw `input_token_pool` / `output_tokens`. `input_usd`, `cache_read_usd`, `cache_creation_usd`, `output_usd` come from `cost_for_turn(turn.model, turn.usage)`.

### Worked example

7-day window, observed totals from the running instance:

- `input_tokens`: 81,353
- `output_tokens`: 12,546,155
- `cache_read_tokens`: 2,451,414,539
- `cache_creation_tokens`: 59,047,492
- Total cost: $1,874.17 (≈ Opus 4.7 rates)

Decomposition:
- `cache_read_usd` ≈ $1,225 (65% of total)
- `cache_creation_usd` ≈ $369 (20%)
- `output_usd` ≈ $313 (17%)
- `input_usd` ≈ $0.4 (<1%)

Pro-rata mode (today): tool $1,041 / non-tool $830.
Excl-cache-read mode: cache_read_usd ($1,225) leaves the pro-rata pool entirely, so the tool share drops by roughly `tool_in_share × $1,225 ≈ 0.57 × $1,225 ≈ $700`. Approximate result: tool $343 / non-tool $1,531.

Total cost remains $1,874 in both modes — only the split changes.

## Architecture

```
GET /api/breakdown/tools?range=7d&mode=excl_cache_read
        |
        v
serve/app.py:api_breakdown_tools(request, range="30d", mode="prorata")
        |  validate mode -> 'prorata' fallback for any unknown value
        |  window-filter turns by `range` (unchanged from today)
        v
serve/state.py:build_tool_detail(turns, *, mode="prorata")
        |
        |  for each turn t:
        |     per_tool_cost: dict[str, float] = _tool_cost_for_mode(t, mode)
        |        if mode == 'prorata':
        |           per_tool_cost = {n: tc.cost_usd for n, tc in t.tool_costs.items()}
        |        else:  # excl_cache_read
        |           per_tool_cost = _recompute_excl_cache_read(t)
        |  sum per_tool_cost across turns into accumulator
        |  __unattributed__ row = window_total_cost - sum(real-tool per_tool_cost)
        v
JSON: { range, mode, tools: [...] }
```

`_recompute_excl_cache_read(turn)` is a new module-private helper in `serve/state.py`:

```python
def _recompute_excl_cache_read(turn: Turn) -> dict[str, float]:
    usage = turn.usage
    input_token_pool = usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
    output_token_count = usage.output_tokens
    turn_cost = cost_for_turn(turn.model, usage)
    input_pool_excl = turn_cost.input_usd + turn_cost.cache_creation_usd  # drop cache_read
    out = {}
    for name, tc in turn.tool_costs.items():
        in_share = (tc.input_tokens / input_token_pool) if input_token_pool else 0.0
        out_share = (tc.output_tokens / output_token_count) if output_token_count else 0.0
        out[name] = in_share * input_pool_excl + out_share * turn_cost.output_usd
    return out
```

### Why no re-parsing is needed

`tc.input_tokens` was stored at parse time as `input_token_pool × in_share`; dividing it back by the raw token pool recovers `in_share` exactly. Same for `out_share` via `tc.output_tokens / usage.output_tokens`. `cost_for_turn()` is a pure function of `(model, usage)` used elsewhere already — no new dependencies, no new I/O.

### Edge cases (inherited from existing pro-rata code)

- Sentinel-name collision (`__unattributed__`, `__unknown__`) — already rejected by `_is_real_tool_name` at parse time, so they never appear as keys in `turn.tool_costs`.
- Linger-only tools (tool used in past turn, no fresh `tool_use` this turn) — already surfaced via the `set(cost) | set(invs)` union in `_accumulate_tool_costs`. The recomputation runs over `turn.tool_costs`, which already contains the linger entry.
- Compaction reset — doesn't matter at this layer. We work with already-attributed shares.
- Interrupted turns — already filtered (no `usage`).
- Unknown model — `cost_for_turn` already tags `UNKNOWN_MODEL_FALLBACK` and returns zero costs; both modes yield zero for that turn.

## API surface

### External

**Endpoint:** `GET /api/breakdown/tools`

**New query parameter:** `mode`
- Type: string
- Values: `prorata` (default) or `excl_cache_read`
- Unknown values fall back silently to `prorata` (matches existing range-fallback pattern).
- Absent parameter = `prorata`. The response always includes a top-level `mode` field echoing the effective mode (new field, additive — existing clients that ignore unknown keys are unaffected).

**Response shape:** unchanged except for an added `mode` echo.

```json
{
  "range": "7d",
  "mode": "excl_cache_read",
  "tools": [
    {"name": "Read", "count": 3017, "cost_usd": 150.41, "last_active": "2026-05-16T10:22:20.607000+00:00"},
    {"name": "Bash", "count": 8573, "cost_usd": 142.10, "last_active": "..."},
    ...,
    {"name": "other", "tool_count": 7, "count": 182, "cost_usd": 2.10},
    {"name": "__unattributed__", "cost_usd": 1432.7}
  ]
}
```

`count`, `last_active`, and the `other`/collapsed-tools fields are mode-invariant — only `cost_usd` is recomputed.

### Internal

- `state.build_tool_detail(turns, *, mode: str = "prorata")` — gains keyword-only `mode` parameter; default unchanged.
- `state._recompute_excl_cache_read(turn) -> dict[str, float]` — new module-private helper, tested in isolation.
- `state._accumulate_tool_costs` does **not** gain a `mode` parameter — `build_tool_detail` is the only caller of the new recomputation. Other callers of `_accumulate_tool_costs` (project detail page, model detail page) stay on pro-rata, matching the non-goals.

## UI behaviour

### Header layout

```
Tool Mix    [7D 30D 90D ALL]   [PRO-RATA EXCL CACHE-READ]   [TOKENS $]    11 tools · $X tool cost · $Y non-tool
```

The new pill group sits between the period pills and the value-unit toggle. Uses the same `.pill-button` styling as the existing pill groups for visual consistency.

### State management

- `localStorage` key: `tokenol.breakdown.toolMode`, default `'prorata'`.
- Joins the existing per-panel state pattern (`tokenol.breakdown.toolPeriod`, `tokenol.breakdown.toolUnit`).
- Module-level `let _bdToolMode = localStorage.getItem('tokenol.breakdown.toolMode') || 'prorata';`
- A `wirePillGroup(...)` call near the existing period/unit wiring, mirroring their structure.

### Interaction

- Clicking a mode pill: updates state, persists to `localStorage`, refetches `/api/breakdown/tools?range=<_bdToolPeriod>&mode=<_bdToolMode>`, re-renders Tool Mix only.
- Mode affects: the Tool Mix bar values (`cost_usd`) and the subtitle's two cost figures (`$X tool cost · $Y non-tool`).
- Mode does **not** affect: the `N tools` count, the per-tool `M calls` count, or anything outside the Tool Mix panel.

### Hidden in TOKENS mode

When `_bdToolUnit === 'tokens'`, the attribution-mode pill group is hidden (`display: none`). Reasoning: attribution mode is a cost-only concept; token counts aren't redistributed by mode. Showing a disabled control would invite confusion.

When the user switches back to `$`, the pill group reappears with the persisted mode selection.

### Tooltips

Each pill carries a `title` attribute with a one-sentence formula description:
- `PRO-RATA`: "Pro-rata: every dollar of input + cache + output is split by visible-byte share across tools."
- `EXCL CACHE-READ`: "Cache-read excluded: cost of carrying tool output across turns goes to non-tool, not the tool."

Native browser tooltips — no popover component, no new dependencies.

### Subtitle wording

Stays as `<N tools> · <$X tool cost> · <$Y non-tool>` in both modes. The active pill is the implicit explanation; the tooltip is the explicit one. No mode-dependent wording changes.

## Testing strategy

### Unit tests

In `tests/test_per_tool_cost.py` and `tests/test_serve_state.py`:

1. **Formula correctness, single turn** — known `(model, usage, tool_costs)`; assert per-tool new cost matches `in_share × (input_usd + cache_creation_usd) + out_share × output_usd` to within float epsilon; assert sum of new tool costs + new unattributed = turn total cost.
2. **Zero-token edge cases** — `input_token_pool == 0` and `output_tokens == 0`; no `ZeroDivisionError`; affected share is 0.0.
3. **Empty `tool_costs`** — both modes yield `{}` for tools; full turn_cost in unattributed.
4. **Defensive sentinel rejection** — synthetic `__unattributed__` key in `tool_costs` (shouldn't occur in practice but a defensive check): never surfaces as a real tool; folds into unattributed under both modes.
5. **Linger-only tool** — `input_tokens > 0, output_tokens = 0`: produces a non-zero cost in `excl_cache_read` mode from the input-side share alone.
6. **Multi-turn aggregation** — 5 synthetic turns spanning two days and two models; call `build_tool_detail(turns, mode='excl_cache_read')`; assert sum of `tool_cost + unattributed_cost` equals sum of `cost_for_turn(...).total_usd` across the turns (mode invariance of total).
7. **Non-cost field invariance** — `count`, `last_active`, `tool_count` identical between `mode='prorata'` and `mode='excl_cache_read'`.

### Endpoint tests

In `tests/test_serve_app.py`:

8. **`GET /api/breakdown/tools?mode=excl_cache_read`** — returns 200; `mode` echoed back; sum of `cost_usd` across tools + unattributed matches the window total from `/api/breakdown/summary` to within float epsilon.
9. **Default mode** — `GET` without `mode` returns the same per-tool `cost_usd` values as `GET` with `mode=prorata`, and echoes `"mode": "prorata"` in the response envelope.
10. **Invalid mode value** — `GET ...?mode=bogus` falls back to `prorata` silently (no 4xx); `mode` echoed back as `prorata`.
11. **Empty window** — range covering zero turns: both modes return `tools=[]` (or just `__unattributed__` with 0.0); no division errors.

### Frontend (manual smoke)

No automated frontend test infrastructure exists in this repo. Smoke loop via running `tokenol serve`:

12. Pill renders in the header; defaults to `PRO-RATA`; switches state on click; persists across page reload.
13. Clicking a mode pill triggers exactly one `/api/breakdown/tools` fetch and re-renders bars + subtitle.
14. In `TOKENS` unit mode, the attribution pill group is hidden; switching back to `$` restores it with the persisted selection.
15. The mode pill is the only thing on the page that changes when toggled (scope-containment sanity check — scorecards, daily charts, by-project, by-model, tool detail page all show identical numbers regardless of mode).
16. Tooltip on each pill renders the expected explanation.

### Optional property test

Generate 20 random valid turns via Hypothesis; assert `total_cost_under_mode == window_total_cost` for both modes (generative form of test 6).

## Risks and mitigations

- **Risk:** users compare a tool's `cost_usd` in Tool Mix against the same tool's `cost_usd` on the tool detail page and see different numbers when in `excl_cache_read` mode.
  **Mitigation:** the per-pill `title` tooltip names the formula; the tool detail page is explicitly out of scope (it stays on pro-rata always). A future iteration could add the toggle to that page; today, the mismatch is acceptable because the panel header makes the active mode visible.

- **Risk:** floating-point drift — recomputed `cost_usd` doesn't sum to exactly the same window total as the original pro-rata mode.
  **Mitigation:** float64 precision is comfortably sufficient for million-dollar windows with cent-level outputs (the invariant test uses `pytest.approx(..., abs=1e-6)` as a guard rather than exact equality).

- **Risk:** unknown `mode` values causing 4xx silently break a typoed query.
  **Mitigation:** matches the existing `range` fallback behaviour, which has been the codebase's convention since 0.5.x. Documented in the spec.

- **Risk:** scope creep — once shipped, "why isn't the tool detail page mode-aware?" becomes a recurring question.
  **Mitigation:** captured as future work below. Not blocking 0.6.1.

## Out of scope / future work

- Adding the mode toggle to the tool detail page (`/tool/{name}`) and model detail page (`/model/{name}`).
- Adding a third mode (e.g. output-only) — judged a curiosity rather than analysis-grade.
- Sub-categorizing the `__unattributed__` bucket into text / thinking / system-prompt-overhead. Would require parser changes and possibly heuristics; deferred.
- Persisting mode preference server-side in `prefs.json`.
- Exposing mode in the URL for shareable links.

## Acceptance criteria

- `GET /api/breakdown/tools` without `mode` and with `mode=prorata` produce identical per-tool `cost_usd` values; both responses echo `"mode": "prorata"`.
- `GET /api/breakdown/tools?mode=excl_cache_read` shifts `cache_read_usd` from tool bucket to `__unattributed__` such that the per-window total is invariant.
- The Tool Mix panel header on `/breakdown` renders a `PRO-RATA / EXCL CACHE-READ` pill group, defaulting to `PRO-RATA`, persisted in `localStorage`, hidden when the panel is in `TOKENS` unit mode.
- Switching the mode pill re-fetches and re-renders Tool Mix only; no other panel reacts.
- All new unit tests and endpoint tests pass; existing 311-test suite stays green; `ruff check` clean.
