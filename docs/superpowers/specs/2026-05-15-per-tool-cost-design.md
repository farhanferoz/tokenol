# Per-tool cost attribution — design

Show actual USD cost per tool across the dashboard. The 0.5.0 cost-visibility overhaul explicitly deferred this because per-tool cost requires a parser change to track byte attribution; this spec is that parser change plus the UI surfaces it unlocks.

The release is phased:

1. **Phase 1 — ranking (in 0.6.0).** Breakdown → Tool Mix gains a $ mode. Tool detail page (`/tool/{name}`) gains a daily-cost chart, a four-card scorecard band, and Cost-by-project + Cost-by-model bar charts.
2. **Phase 2a — mirror (in 0.6.0).** Project detail page and model detail page each gain a Cost-by-tool bar chart. Same component as Phase 1's bars.
3. **Phase 2b — heatmap on Breakdown (deferred).** A "Cost intersection" panel showing project × tool as a colored grid. Out of 0.6.0 — revisit once usage signals demand.

## Goals

1. Answer "which tools are costing me the most?" at a glance — Breakdown Tool Mix in $ mode.
2. Answer "where does this tool's cost concentrate?" — tool detail page Cost-by-project / Cost-by-model bars.
3. Answer "inside this project, which tools dominate?" — project detail page Cost-by-tool bar.
4. Reconcile attributed totals to overall spend — an explicit `unattributed` row on aggregate views.
5. Reuse one bar-row component across four UI surfaces (Breakdown, tool detail, project detail, model detail). No new chart types in Phase 1+2a (the heatmap in 2b is the only new component, deferred).
6. Reuse `chart.js` for the daily-cost line chart on the tool detail page. Visual consistency with Overview.
7. No DuckDB schema migration. Causal attribution is computed by the parser into the existing in-memory index at server startup.

## Non-goals

- Per-tool cost on Session detail (`/session/<id>`). Future work.
- Persisted per-tool cost in DuckDB. The `--persist` mode is segfaulting independently; the persistence story for tool costs comes once that's fixed.
- A toggle between "honest" and "proportional" attribution. Causal attribution is the only model we ship.
- Heatmap / Phase 2b in 0.6.0.

## Attribution model — causal

Per-turn cost (per `metrics/cost.py`) decomposes into four components: `input_usd`, `output_usd`, `cache_read_usd`, `cache_creation_usd`. All four must be attributed for the math to reconcile to total spend. The three input-side components share one byte-share split; the output-side component uses its own split.

### Output side (`output_usd`)

For an assistant message containing some mix of `text`, `thinking`, and `tool_use` content blocks:

- Compute byte sizes per content block (length of the JSON-serialized block, including any `input` parameter object on `tool_use` blocks).
- Total assistant-message bytes = sum of all block byte sizes.
- A tool's output share for that turn = `byte_size(its tool_use block) / total_assistant_message_bytes`. If the model emits multiple tool_use blocks for the same tool name in one turn, sum their bytes.
- Charge `output_usd × share` to the tool.
- `text` blocks (free-text reasoning) and `thinking` blocks (extended-thinking output) → `unattributed` output share.

### Input side — lingering context (`input_usd + cache_read_usd + cache_creation_usd`)

Both halves of a prior tool exchange linger in the conversation history and are replayed as input on every subsequent turn until compaction: the assistant's `tool_use` block AND the user's `tool_use_result` block. This is the dominant effect on long sessions and is the whole reason "naive output-only" attribution misleads.

A `tool_use_id → tool_name` map is built incrementally during parse: every assistant-side `tool_use` block records `id → name`. User-side `tool_use_result` blocks look up `tool_use_id` in that map. If the lookup misses (e.g., the matching `tool_use` was lost to compaction before parse caught up), attribute to a `__unknown__` bucket.

Two running tallies per session:

- `bytes_in_context_by_tool[tool_name]` — sum of bytes for `tool_use` blocks **and** matching `tool_use_result` blocks for that tool, still in context.
- `non_tool_bytes_in_context` — system prompt (when seen), tool definitions, user text, assistant `text`/`thinking` blocks.

For each assistant turn T:

- `total_bytes_in_context = sum(bytes_in_context_by_tool.values()) + non_tool_bytes_in_context`.
- Per-tool input share = `bytes_in_context_by_tool[tool] / total_bytes_in_context`.
- Charge `(input_usd + cache_read_usd + cache_creation_usd) × share` to the tool. Non-tool share → `unattributed`.

After attributing turn T's cost, *then* fold T's own content blocks into the running tallies (so T's tool_use blocks linger for turn T+1 onward — they don't self-attribute on the turn they're emitted on, since that's already covered by output-side attribution).

### Compaction reset

There is no compaction marker in the JSONL. `metrics/patterns.py` already infers compaction from input-token drops (`compaction_drop_ratio = 0.8`). The parser reuses the same heuristic: if `input_tokens` on turn T+1 is < 20% of the running peak input on the session, reset both running tallies. Note this is a coarse proxy — a known limitation, surfaced in the unattributed bar.

### Reconciliation

For any aggregate (day, project, model, all-time): `sum(per_tool_cost_usd) + unattributed_cost_usd ≈ total_cost_usd`. The approximation comes from (a) byte-to-token ratio variance across content types, (b) compaction misdetection. Target tolerance: ≤5% drift for typical sessions, ≤10% for sessions with heavy compaction. The unattributed bar surfaces the residual so the user can see what's not attributed.

## Parser architecture (Option 3 — in-memory at startup)

Source of truth is JSONL. The 0.5.1 cross-project flow already walks all `~/.claude*` JSONLs at server startup and builds an in-memory event/turn index. This spec extends that pass.

### New parser output

`RawEvent` today only retains *derivatives* of message content (`tool_use_count`, `tool_names`, `tool_error_count`) — the raw block bytes are discarded after parse. Byte-share math therefore happens **during** parse (the only point we see `message.content`), with the per-tool attribution attached to the resulting Turn.

In `src/tokenol/ingest/parser.py`, the per-session walk gains a stateful per-session attribution pass that:
- Iterates events in JSONL order (user events too, not just assistant — today user events are parsed but discarded; under this spec their content is processed for byte-share, then still discarded post-attribution).
- Maintains the `tool_use_id → tool_name` map and the two running byte tallies described above.
- For each assistant event, computes the per-tool share splits and emits the result on the corresponding Turn.

Each Turn event grows new fields:

```python
@dataclass
class ToolCost:
    tool_name: str
    input_tokens: float       # may be fractional after share math
    output_tokens: float
    cost_usd: float           # matches existing Turn.cost_usd field name

# On Turn (model/events.py):
tool_costs: dict[str, ToolCost]   # keyed by tool_name
unattributed_input_tokens: float
unattributed_output_tokens: float
unattributed_cost_usd: float
```

Fractional `input_tokens` / `output_tokens` are an arithmetic consequence of the share split. They aggregate cleanly across turns. UI displays round to integers when shown alongside non-attributed token figures (e.g., the scorecard's "Output tokens" reads `412k`, not `412,041.7`).

The parser is the only place that owns the `tool_use_id → tool_name` map and the running byte tallies. The map and tallies are session-scoped (reset between sessions, and reset on compaction within a session per the heuristic above).

MCP tools and subagents are first-class tool names — no namespace rollup. `mcp__claude_ai_Gmail__search_threads` and `mcp__claude_ai_Google_Calendar__create_event` are separate tool rows. `Agent` (subagent invocations) is one tool row regardless of `subagent_type`; the subagent's internal tool calls are not visible at the parent session level and are not attributed back to the calling tool.

### Aggregation layer

`src/tokenol/metrics/rollups.py` already owns `build_project_rollups`, `build_model_rollups`, and `build_tool_mix`, plus the `_rank_counter_with_others(counter, top_n)` helper used today by `/api/breakdown/tools` for the top-N + "other" rollup. The same helper is reused for the new $-ranked surfaces; only the input changes from a `Counter[str]` of invocations to a `dict[str, float]` of costs (with a thin adapter or a small `_rank_dict_with_others` sibling — implementer's choice).

The module gains:

- `build_tool_cost_rollups(turns) -> list[ToolCostRollup]` — per-tool aggregate across the given turn slice (total cost_usd, input/output tokens, invocations, last_active, plus `by_project` and `by_model` sub-rollups).
- `build_tool_cost_daily(turns, *, tool_name, days=30) -> list[DailyCost]` — per-day cost series for one tool, for the daily-cost chart.
- Existing `ProjectRollup` and `ModelRollup` dataclasses grow a `tool_costs: list[ToolCostEntry]` field populated alongside the existing per-component data.

### Persistence

None. The in-memory index is rebuilt at startup like every other rollup today. DuckDB schema unchanged.

## UI surfaces

### Surface 1 — Breakdown → Tool Mix in $ mode

Existing panel `bp-tools-title` on `/breakdown`. Already a horizontal bar chart of tools by tokens.

- The page-level TOKENS/$ toggle (already wired for other panels via `data-bdunit="cost"`) extends to this panel.
- In $ mode: bars are sized and labelled by `cost_usd`, ranked descending. The "other (N)" rollup at the bottom keeps the long tail compact (reusing `_rank_counter_with_others` from `metrics/rollups.py`, or its dict-of-cost sibling).
- An explicit `unattributed` row appears below "other" with the dim style, value = `sum(turn.unattributed_cost_usd)` for the period. A small "about cost attribution →" link points to a help anchor explaining the model.

### Surface 2 — Tool detail page (`/tool/{name}`)

Currently shows only the name, summary, projects table, models table. After:

- **Daily cost chart** (full-width banner at the top of the page). Line+area in the existing tokenol orange, hardcoded 30-day window for v1 (no period selector — adding one is future work). Y axis = $, X axis = date. Rendered by the existing `chart.js` (which already renders the Overview daily history). Header line: `Daily cost · last 30d   total $X.XX` on the left, `peak <date> · $X.XX` on the right.
- **Scorecard band** of four cards: Est. Cost (with % of total spend sub), Output tokens (with avg/call sub), Invocations (with 7-day sub), Top project ranked by `cost_usd` desc (with that project's $ + % of this tool's spend).
- **Cost-by-project bar chart** — horizontal bars, ranked by `cost_usd` desc. Each row: project name + (invocations · last-active) subtitle | bar | $ value. Click row → `/project/{cwd_b64}` (base64-encode the project's cwd, same as the existing Overview project links).
- **Cost-by-model bar chart** — same shape, rows are models, click row → `/model/{name}`.

The existing two tables are replaced by these bar charts. Invocation count and last-active are preserved as dim subtitles in the bar row, so no data is lost.

### Surface 3 — Project detail page (Phase 2a)

Project detail page (`/project/{cwd_b64}`) gains:

- **Cost-by-tool bar chart** — mirror of Surface 2's Cost-by-project, but with tools as rows.
- **Cost-by-model bar chart** — mirror, with models as rows. (Symmetry across all three drill-down pages.)

Placement: below whatever the page shows today, above any existing tables.

### Surface 4 — Model detail page (Phase 2a)

Model detail page (`/model/{name}`) gains:

- **Cost-by-tool bar chart** — same component, tools as rows.
- (Cost-by-project mirror already exists conceptually; if missing, add for full symmetry.)

### Shared bar-row component

A reusable component renders one ranked horizontal bar list with the shape: `[label + subtitle | bar | $ value]`. Rendered in vanilla JS+CSS to match the existing tokenol style (no chart library for the bars — they're just CSS div rectangles inside a grid). Used by Surfaces 1, 2 (×2), 3 (×2), 4 (×1 or ×2). Click row → drill-in URL based on row type.

Lives in `src/tokenol/serve/static/components.js` (alongside the existing shared bits). Exports a single `renderRankedBars(container, rows, opts)` function — caller supplies `rows = [{label, sublabel, value, href}]`, the component computes the max for bar widths and handles the "unattributed" styled-dim row when `row.kind === "unattributed"`. The "other (N)" rollup row is constructed server-side via `_rank_counter_with_others`-style helper — the component just renders whatever rows it's given.

## API surface

### Extended endpoints (no new routes)

- `GET /api/breakdown/tools` (existing) — currently returns `{range, tools: [{name, count, ...}]}` with `_rank_counter_with_others`-shaped rows. Payload extends to include `cost_usd`, `input_tokens`, `output_tokens`, `last_active` per row, plus an appended sentinel `{name: "__unattributed__", cost_usd, ...}` row when the page-level TOKENS/$ toggle is on `$`. The frontend renders the sentinel with the dim "unattributed" style. The `range` query parameter (`30d`, `7d`, `all`, etc.) is unchanged.
- `GET /api/tool/{name}` — currently returns name + project/model tables. Adds: `scorecards { cost_usd, output_tokens, invocations, top_project: {name, cost_usd, share} }`, `daily_cost: [{date, cost_usd}]` (30 days, zero-filled), `by_project: [{cwd_b64, project_label, cost_usd, invocations, last_active}]`, `by_model: [{name, cost_usd, invocations}]`. `cwd_b64` is included so the frontend can link directly to `/project/{cwd_b64}` without a second lookup.
- `GET /api/project/{cwd_b64}` — adds `by_tool: [{name, cost_usd, invocations, last_active}]`.
- `GET /api/model/{name}` — adds `by_tool: [{name, cost_usd, invocations}]`.

Cross-project mode (the default in 0.5.1; `--scoped` restricts to the current project) does not change endpoint shapes — only the underlying in-memory dataset differs. The `is_interrupted` and `is_sidechain` filters applied today by the Breakdown endpoints are reused.

## Testing

### Parser unit tests

- **Single tool, single result.** One assistant turn calls Read, one user turn returns 10 KB. Verify Read gets the output share for its `tool_use` block bytes and a small input share for the next turn (its result is in context).
- **Multiple tools, one assistant message.** Assistant turn contains both `Grep` and `Read` `tool_use` blocks. Verify byte-share split is correct on the output side.
- **Lingering input.** Three turns: Read returns 50 KB on turn 1, no tools on turns 2–3. Verify Read's input attribution grows on turns 2 and 3 proportionally to the 50 KB share of total context.
- **Cache-tier attribution.** Construct a turn with non-zero `cache_read_input_tokens` and `cache_creation_input_tokens`. Verify all three input-side components (input_usd + cache_read_usd + cache_creation_usd) are distributed by the same byte share, not just `input_usd`.
- **Compaction reset (heuristic).** Synthesize a session where `input_tokens` drops to <20% of running peak. Verify the parser resets running tallies and post-drop turns don't attribute to pre-drop tools.
- **Thinking block exclusion.** Assistant turn with a `thinking` block + a `tool_use` block. Verify thinking bytes land in unattributed output, not in the tool's share.
- **Unknown tool_use_id.** Construct a tool_use_result with a tool_use_id that has no matching prior `tool_use` block. Verify it lands in the `__unknown__` bucket and doesn't crash.

### Golden-file test

A fixed JSONL fixture (3–5 turns, 2–3 tools) → expected `{tool: ToolCost}` dict. Stored under `tests/fixtures/` next to the existing parser fixtures.

### Aggregation test

For a multi-session in-memory index, exercise each `build_tool_cost_rollups` / `build_tool_cost_daily` path and verify `sum(per_tool_cost_usd) + unattributed_cost_usd ≈ sum(turn.cost_usd)` within ≤5% (≤10% for fixtures with simulated compaction).

### End-to-end smoke

`tokenol serve` against a fixed JSONL fixture; assert the extended `/api/breakdown/tools` shape and that `sum(rows.cost_usd) ≈ snapshot.total_cost_usd` (within reconciliation tolerance). Also hit `/api/tool/{name}` and assert scorecards + daily_cost + by_project + by_model shape.

## Migration & rollout

- No data migration. Restart `tokenol serve` → parser rebuilds the in-memory index with the new fields.
- The 0.5.0 spec's noted limitation ("Per-tool cost attribution would require a parser change... If/when that parser change happens, the toggle becomes trivial to add") is satisfied by this work. The `$` toggle on Tool Mix in 0.5.0 was deliberately omitted; this spec adds it.
- `--persist` mode remains independently broken; this work does not regress or fix it. When `--persist` is unblocked, a follow-up can persist `turn_tool_cost` rows to skip the parser pass on startup.
