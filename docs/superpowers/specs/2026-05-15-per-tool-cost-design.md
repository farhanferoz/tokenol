# Per-tool cost attribution — design

Show actual USD cost per tool across the dashboard. The 0.5.0 cost-visibility overhaul explicitly deferred this because per-tool cost requires a parser change to track byte attribution; this spec is that parser change plus the UI surfaces it unlocks.

The release is phased:

1. **Phase 1 — ranking (in 0.6.0).** Breakdown → Tool Mix gains a $ mode. Tool detail page (`/tool?name=…`) gains a daily-cost chart, a four-card scorecard band, and Cost-by-project + Cost-by-model bar charts.
2. **Phase 2a — mirror (in 0.6.0).** Project detail page and model detail page each gain a Cost-by-tool bar chart. Same component as Phase 1's bars.
3. **Phase 2b — heatmap on Breakdown (deferred).** A "Cost intersection" panel showing project × tool as a colored grid. Out of 0.6.0 — revisit once usage signals demand.

## Goals

1. Answer "which tools are costing me the most?" at a glance — Breakdown Tool Mix in $ mode.
2. Answer "where does this tool's cost concentrate?" — tool detail page Cost-by-project / Cost-by-model bars.
3. Answer "inside this project, which tools dominate?" — project detail page Cost-by-tool bar.
4. Reconcile attributed totals to overall spend — an explicit `unattributed` row on aggregate views.
5. Reuse one bar-row component across all five surfaces. No new chart types in Phase 1+2a (the heatmap in 2b is the only new component, deferred).
6. Reuse `chart.js` for the daily-cost line chart on the tool detail page. Visual consistency with Overview.
7. No DuckDB schema migration. Causal attribution is computed by the parser into the existing in-memory index at server startup.

## Non-goals

- Per-tool cost on Session detail (`/session/<id>`). Future work.
- Persisted per-tool cost in DuckDB. The `--persist` mode is segfaulting independently; the persistence story for tool costs comes once that's fixed.
- A toggle between "honest" and "proportional" attribution. Causal attribution is the only model we ship.
- Heatmap / Phase 2b in 0.6.0.

## Attribution model — causal

Cost = `input_tokens × in_rate + output_tokens × out_rate`. Per-tool $ is computed by distributing both sides of every turn's token bill among tools that *caused* the cost.

### Output side

For an assistant message that contains one or more `tool_use` blocks plus free text:

- Compute byte sizes per `tool_use` block (JSON-serialized form of the block including its `input` parameter object) and total assistant-message byte size including free text + tool_use blocks.
- A tool's output share for that turn = `byte_size(its tool_use block) / total_assistant_message_bytes`.
- Charge `output_tokens × share × out_rate` to the tool.
- Free-text bytes (the model's reasoning) → `unattributed` output share.

### Input side — lingering context

A `tool_use_result` from a prior turn lives in the conversation history and is replayed as input on every subsequent turn until compaction. This is the dominant effect on long sessions and is the whole reason "naive output-only" attribution misleads.

For each turn T:

- Maintain a running tally `bytes_in_context_by_tool[tool_name]`, updated each turn as we encounter `tool_use_result` blocks. The `tool_use_id` on each result is looked up in a `tool_use_id → tool_name` map built from earlier assistant turns in the same session.
- Maintain a running tally `non_tool_bytes_in_context` for prompt, system, tool definitions, user messages, free-text assistant reasoning.
- A tool's input share for turn T = `bytes_in_context_by_tool[tool] / total_bytes_in_context`.
- Charge `input_tokens × share × in_rate` to the tool. Non-tool share → `unattributed`.

If the session is compacted (we detect this from the existing compaction marker), the running tallies reset for the new context window.

### Reconciliation

For any aggregate (day, project, model, all-time): `sum(per_tool_cost) + unattributed_cost ≈ total_cost`. The approximation is from byte-to-token ratio variance across content types; we accept ≤5% drift and surface the unattributed bar explicitly so the user can see what's not attributed.

## Parser architecture (Option 3 — in-memory at startup)

Source of truth is JSONL. The 0.5.1 cross-project flow already walks all `~/.claude*` JSONLs at server startup and builds an in-memory event/turn index. This spec extends that pass.

### New parser output

In `src/tokenol/ingest/parser.py`, the per-session walk gains a second responsibility: build per-turn `tool_costs`. Concretely, each Turn event grows a new field:

```python
@dataclass
class ToolCost:
    tool_name: str
    input_tokens: float       # may be fractional after share math
    output_tokens: float
    est_cost: float           # priced via existing model/pricing.py

# On Turn (model/events.py):
tool_costs: dict[str, ToolCost]   # keyed by tool_name
unattributed_input_tokens: float
unattributed_output_tokens: float
unattributed_est_cost: float
```

The parser is the only place that owns the `tool_use_id → tool_name` map and the running byte tallies. The map and tallies are session-scoped (reset between sessions, and reset on compaction within a session).

### Aggregation layer

`src/tokenol/metrics/rollups.py` already owns `build_project_rollups`, `build_model_rollups`, and `build_tool_mix`. It gains:

- `build_tool_cost_rollups(turns) -> list[ToolCostRollup]` — per-tool aggregate across the given turn slice (total est_cost, input/output tokens, invocations, last_active, plus `by_project` and `by_model` sub-rollups).
- `build_tool_cost_daily(turns, *, tool_name, days=30) -> list[DailyCost]` — per-day cost series for one tool, for the daily-cost chart.
- Existing `ProjectRollup` and `ModelRollup` dataclasses grow a `tool_costs: list[ToolCostEntry]` field populated alongside the existing per-component data.

### Persistence

None. The in-memory index is rebuilt at startup like every other rollup today. DuckDB schema unchanged.

## UI surfaces

### Surface 1 — Breakdown → Tool Mix in $ mode

Existing panel `bp-tools-title` on `/breakdown`. Already a horizontal bar chart of tools by tokens.

- The page-level TOKENS/$ toggle (already wired for other panels via `data-bdunit="cost"`) extends to this panel.
- In $ mode: bars are sized and labelled by `est_cost`, ranked descending. The "other (N)" rollup at the bottom keeps the long tail compact (already the existing pattern).
- An explicit `unattributed` row appears below "other" with the dim style, value = `sum(turn.unattributed_est_cost)` for the period. A small "about cost attribution →" link points to a help anchor explaining the model.

### Surface 2 — Tool detail page (`/tool?name=…`)

Currently shows only the name, summary, projects table, models table. After:

- **Daily cost chart** (full-width banner at the top of the page). Line+area in the existing tokenol orange, period defaults to 30 days but honors the page-level period selector if/when one is added (consistent with how Overview's daily chart already works). Y axis = $, X axis = date. Rendered by the existing `chart.js` (which already renders the Overview daily history). Header line: `Daily cost · last 30d   total $X.XX` on the left, `peak <date> · $X.XX` on the right.
- **Scorecard band** of four cards: Est. Cost (with % of total spend sub), Output tokens (with avg/call sub), Invocations (with 7-day sub), Top project (with that project's $ + % of this tool's spend).
- **Cost-by-project bar chart** — horizontal bars, ranked. Each row: project name + (invocations · last-active) subtitle | bar | $ value. Click row → project detail page.
- **Cost-by-model bar chart** — same shape, rows are models.

The existing two tables are replaced by these bar charts. Invocation count and last-active are preserved as dim subtitles in the bar row, so no data is lost.

### Surface 3 — Project detail page (Phase 2a)

Project detail page (`/project?name=…`) gains:

- **Cost-by-tool bar chart** — mirror of Surface 2's Cost-by-project, but with tools as rows.
- **Cost-by-model bar chart** — mirror, with models as rows. (Symmetry across all three drill-down pages.)

Placement: below whatever the page shows today, above any existing tables.

### Surface 4 — Model detail page (Phase 2a)

Model detail page (`/model?name=…`) gains:

- **Cost-by-tool bar chart** — same component, tools as rows.
- (Cost-by-project mirror already exists conceptually; if missing, add for full symmetry.)

### Shared bar-row component

A reusable component renders one ranked horizontal bar list with the shape: `[label + subtitle | bar | $ value]`. Rendered in vanilla JS+CSS to match the existing tokenol style (no chart library for the bars — they're just CSS div rectangles inside a grid). Used by Surfaces 1, 2 (×2), 3 (×2), 4 (×1 or ×2). Click row → drill-in URL based on row type.

Selecting between the row types is handled by the calling code, not the component; the component just gets `{ label, sublabel, value, max, hrefOnClick }` rows and renders.

## API surface

### New endpoint

- `GET /api/breakdown/by-tool` — list of `{tool_name, input_tokens, output_tokens, est_cost, invocations, last_active}` for the requested period, sorted by `est_cost desc`. Filterable by `project`, `model`, `start`, `end`. Includes a sentinel `tool_name = "__unattributed__"` row for reconciliation.

### Extended endpoints

- `GET /api/tool/{name}` — currently returns name + project/model tables. Adds: `scorecards { est_cost, output_tokens, invocations, top_project }`, `daily_cost: [{date, est_cost}]` (30 days), `by_project: [{project, est_cost, invocations, last_active}]`, `by_model: [{model, est_cost, invocations}]`.
- `GET /api/project/{name}` — adds `by_tool: [{tool_name, est_cost, invocations, last_active}]`.
- `GET /api/model/{name}` — adds `by_tool: [{tool_name, est_cost, invocations}]`.

Cross-project mode (default in 0.5.1) honors the existing `--scoped` flag. Endpoint payloads are unchanged by scope — only the in-memory dataset differs.

## Testing

### Parser unit tests

- **Single tool, single result.** One assistant turn calls Read, one user turn returns 10 KB. Verify Read gets the output share for its `tool_use` block bytes and a small input share for the next turn (its result is in context).
- **Multiple tools, one assistant message.** Assistant turn contains both `Grep` and `Read` `tool_use` blocks. Verify byte-share split is correct on the output side.
- **Lingering input.** Three turns: Read returns 50 KB on turn 1, no tools on turns 2–3. Verify Read's input attribution grows on turns 2 and 3 proportionally to the 50 KB share of total context.
- **Compaction reset.** Inject the existing compaction marker; verify running tallies reset and post-compaction turns don't attribute to pre-compaction tools.

### Golden-file test

A fixed JSONL fixture (3–5 turns, 2–3 tools) → expected `{tool: ToolCost}` dict. Stored under `tests/fixtures/` next to the existing parser fixtures.

### Aggregation test

For a multi-session in-memory index, hit each `rollup_tool_cost(group_by=…)` variant and verify the sum reconciles to the unattributed-inclusive total within ≤5%.

### End-to-end smoke

`tokenol serve` against a fixed JSONL fixture; assert `/api/breakdown/by-tool` shape and that `sum(rows.est_cost) ≈ snapshot.total_est_cost` (within reconciliation tolerance).

## Migration & rollout

- No data migration. Restart `tokenol serve` → parser rebuilds the in-memory index with the new fields.
- The 0.5.0 spec's noted limitation ("Per-tool cost attribution would require a parser change... If/when that parser change happens, the toggle becomes trivial to add") is satisfied by this work. The `$` toggle on Tool Mix in 0.5.0 was deliberately omitted; this spec adds it.
- `--persist` mode remains independently broken; this work does not regress or fix it. When `--persist` is unblocked, a follow-up can persist `turn_tool_cost` rows to skip the parser pass on startup.
