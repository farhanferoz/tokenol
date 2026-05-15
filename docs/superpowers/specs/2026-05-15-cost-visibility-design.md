# Cost visibility overhaul — design

Make actual USD cost visible alongside tokens across the dashboard, and let Overview compare any two metrics on one chart. Two related but independent surfaces:

1. **Overview (`/`):** Hour-By-Hour and Daily History charts gain a dual-metric overlay so you can see, e.g., OUTPUT and COST on one chart with dual axes.
2. **Breakdown (`/breakdown`):** Daily Billable Tokens, Tokens by Project, and Model Mix gain a per-chart `TOKENS / $` toggle.

Both ship together in a single release. They are independent surfaces but conceptually one product change — "make cost visible everywhere you currently see tokens" — so a single spec keeps the rationale and trade-offs in one place.

## Goals

1. Glance-level answer to "did cost and output move together this week, or did one spike without the other?" without leaving Overview.
2. Glance-level answer to "which project / model is actually expensive?" without mentally pricing tokens.
3. Zero net new chrome on Overview (no new buttons or toggles beyond what's already there).
4. ≤30 px of new chrome per chart card on Breakdown (a single 2-state pill pair).
5. Persistence reuses the `localStorage` mechanism already used by LIN/LOG and the period pill — no new prefs.json key, no server-side preference. (Note: today the metric pill itself does **not** persist; this work adds `localStorage` for it on top of the existing pattern. A pre-existing dashboard user lands on `hit_pct` on first load post-upgrade, just as today.)
6. Defaults match today: Hour-By-Hour and Daily History open on `HIT%` single-series. Existing Breakdown charts open on tokens. Nothing changes for a user who never touches the new toggles.

## Non-goals

- A third overlaid series on Overview (max 2; three is where it gets ugly).
- Cost overlay on Daily Cache Re-use (cache-saved figure is already $-annotated in the title; the chart is dimensionless bytes).
- A `$` toggle on the Tools breakdown chart. Per-tool cost attribution would require a parser change to track per-tool token usage, the same limitation that caused the error-rate column to be omitted from the Tool drill-down (see RESUME). If/when that parser change happens, the toggle becomes trivial to add.
- A page-global "view everything as cost" switch on Breakdown. Per-chart toggles preserve mixed views (e.g., tokens-by-project + cost-by-model simultaneously).
- New cost calculations. Existing `metrics/cost.py` already prices every component per model; this work only surfaces those numbers in more places.
- New endpoints. All cost data needed already flows through `/api/snapshot` (Overview) and the existing `/api/breakdown/*` family (Breakdown). The breakdown endpoints get enriched payloads, not new routes.
- Cost overlay on Tool drill-down (`/tool/<name>`), Project drill-down, Model drill-down, or Session detail. Future work — not where the user flagged.

## Overview: dual-metric overlay

### Interaction model

Pills become a 2-of-N cyclic selector with order semantics. The existing pill set on both charts is `HIT% · $/KW · CTX · CACHE REUSE · OUTPUT · COST`.

State machine per pill, click by click:

| Current state of clicked pill | New state of clicked pill | Effect on other selected pill |
| --- | --- | --- |
| Unselected, none selected | Primary | — |
| Unselected, one already primary | Primary | Old primary → secondary |
| Unselected, primary + secondary already selected | Primary | Old primary → secondary; old secondary dropped |
| Primary | Secondary | Other becomes primary if it existed; else nothing else changes |
| Secondary | Unselected | Primary stays primary |

Max two metrics rendered at any time. Three series on one chart violates the "without making things ugly or complicated" constraint that started this work.

Pill style encodes state:

- **Primary:** filled (current look — same colour as the gold series).
- **Secondary:** outlined; outline colour = the secondary series colour.
- **Unselected:** ghost (current look).

Rationale for cyclic over shift-click or a separate "compare" button: zero new chrome, no hidden gesture, reuses the muscle memory of "click the metric I want." Discoverable on the second click of any metric.

### Applies to

Both Hour-By-Hour and Daily History on `/`. Identical behavior. State is independent per chart — Hour-By-Hour can have `$/kW` solo while Daily History has `OUTPUT + COST`.

### Axes

- **Left axis:** primary series, gold tick labels.
- **Right axis:** secondary series, in the secondary series colour. Same tick density (target 4–6 ticks). Tick label format reuses the existing per-metric formatters: `$`, `%`, `N:1`, raw tokens with `k`/`M` suffix.
- **Axis title:** the metric name and unit, in the matching series colour. This is the cue that "the gold axis goes with the gold line."

### LIN / LOG

LIN / LOG pill applies to the **primary** axis only. Secondary axis is always linear. Mixing log scales on a dual-axis chart is genuinely confusing — you can't visually compare slopes when one axis is log and the other isn't, and dual-log compounds the problem. Single-axis LIN/LOG keeps the existing semantics intact when only one metric is selected.

### Project / Model filters

The existing `PROJECT all ▼ MODEL all ▼` filters apply to both series. (A future enhancement could allow per-series filters — e.g., "OUTPUT for project X vs COST for all projects" — but that's a much harder UI and probably more confusing than useful. Not in scope.)

### Legend

When in overlay mode, render a small inline legend below the metric-pill row:

```
▬ output (left)   ▬ cost (right)
```

The square swatch matches each series colour. In single-series mode the legend is hidden — no chrome change vs today.

### Hover / tooltip

Tooltip already shows the active metric value at the hovered x. In overlay mode it stacks both values, each in its own series colour:

```
2026-05-12
output    2,045k
cost       $342
```

Same date/hour anchor; no per-series independent tooltips. The hover dot is drawn on both lines simultaneously at the same x.

### Visual restraint

- Primary line: 2 px, 100% opacity (current).
- Secondary line: 1.5 px, ~85% opacity.

This reinforces that the user picked their "main" metric and the second is contextual. Both rendered as step-lines (matches current Daily History and Hour-By-Hour treatment). No bars-and-lines mixing — bars are Breakdown's idiom, lines are Overview's.

### Colours

- **Primary:** existing gold (whatever CSS variable currently drives the line colour — to be confirmed in `styles.css` during implementation).
- **Secondary:** a cool-toned hue that does not clash with red (already meaningful as the alarm colour). Target slate-blue (`#5b7a8a`) or olive-green (`#6b7f55`). Final choice depends on what reads best against the cream background and which integrates cleanly with the existing palette. The implementation plan picks one and renders both in a quick local check before locking in.
- **New CSS variable:** `--series-secondary`. Defined once, referenced from every chart that supports overlay.

### Persistence

`localStorage`, following the same naming convention as the existing `_LS_H_SCALE`, `_LS_D_SCALE`, `_LS_PERIOD` constants in `app.js`. Two new keys:

- `tokenol.hMetric` — Hour-By-Hour
- `tokenol.dMetric` — Daily History

Note this is **new** persistence — `_hMetric` and `_dMetric` are currently re-initialized to `'hit_pct'` on every page load. The first time a user upgrades, they land on `HIT%` single-series as today; thereafter their last selection persists.

Stored value shape: a comma-separated pair `primary,secondary`, or a single token for single-series mode. The parser splits on comma; a value without a comma yields a one-element array and falls through the single-series code path. No migration needed.

The window URL stays clean (no `?metric=...` query string is added — that pattern is used today only for API requests via `URLSearchParams`, not for window state).

### Data shape

No payload changes. `/api/snapshot` already returns per-bucket fields for every metric. The frontend just consumes two fields instead of one.

## Breakdown: per-chart TOKENS / $ toggle

### Where it applies

Three charts on `/breakdown` get the toggle. They are listed below in the order they appear on the page.

1. **Daily Billable Tokens** (Time section). Stacked bars: input / output / cache_created per day. `$` mode swaps the y-axis to USD and bars to per-component cost. Existing legend (`total $8,087 · avg $269.58/d`) already shows the rollup in dollars — the toggle makes the per-bar numbers match.
2. **Tokens by Project** (Breakdowns section). Grouped or stacked bars: input + output per project. `$` mode shows input cost + output cost per project, stacked or grouped the same way as today. Cache-hit dots stay (they're a share metric — dollars wouldn't change their meaning). The "top 10 of 18 · 97% of billable" caption stays token-based (rephrasing it for $ mode is needless complexity).
3. **Model Mix** (Breakdowns section). Doughnut showing token share per model. `$` mode shows cost share per model — meaningful because opus tokens cost roughly 5× sonnet tokens, so a model that looks tiny in token share may dominate cost. The "4 models" caption stays. Slice tooltips already show the share percentage; in `$` mode they show cost share alongside dollar value.

### Charts that don't get the toggle, and why

- **Daily Cache Re-use** (Time section): the chart is bytes-saved over time, already $-annotated in the chart title (`total 11.7B · avg $1,668/d saved`). Adding a unit toggle would either duplicate the title annotation or remove the byte units that make "cache re-use" intelligible. Skip.
- **Tokens by Tool** (Tools section): per-tool cost attribution would require a parser change. Same limitation that omitted the error-rate column from Tool drill-down. Out of scope. The Tools chart keeps its current shape.

### Toggle UI

A pill pair on each chart card's top-right, next to the chart title and before the existing caption (e.g., "top 10 of 18 · 97% of billable"):

```
[ TOKENS ] [ $ ]
```

Two-state. Defaults to TOKENS (current behavior). Same visual treatment as the existing range-pill rows on those cards. State is per-chart, not page-global — selecting `$` on one chart does not affect the others.

### Data shape

`/api/breakdown/daily-tokens` already returns `cost_usd` per day. Frontend can plot it directly; no server change needed for chart #1.

`/api/breakdown/by-project` does **not** currently return cost. The current chart renders input and output as side-by-side bars per project (no stacking, no cache_created visible — verified in `breakdown.js`), so the cost payload mirrors that shape: `input_cost` and `output_cost` per project. No cache-cost fields needed on this endpoint.

`/api/breakdown/by-model` does **not** currently return cost. It needs `cost_usd` per model, plus the equivalent share (`cost_share = cost_usd / sum(cost_usd)`). The doughnut sizes by share, so it needs both the absolute and the share value.

Payload bloat is negligible — a handful of floats per row, gzipped over the wire. Single fetch, instant client-side toggle.

### Cost calculation

`metrics/cost.py` already prices every component (`input`, `output`, `cache_creation`, `cache_read`) per model per turn. The breakdown rollup builders in `metrics/rollups.py` and the per-project / per-model bucketers in `serve/app.py` (`_bucket_turns`) need to additionally aggregate cost-per-component into the existing structures. The arithmetic is `sum(turn.cost_for_component)` over the turns in each bucket, with the same model-specific pricing already used elsewhere — no new pricing logic.

### Persistence

`localStorage` keys, following Overview's convention:

- `tokenol.bd.timeUnit` — Daily Billable Tokens, value `tokens` or `cost`
- `tokenol.bd.projectUnit` — Tokens by Project
- `tokenol.bd.modelUnit` — Model Mix

Default value when the key is absent: `tokens`. Each chart's unit is independent.

## Architecture and shared pieces

### Chart layer

`chart.js` (~230 lines today) gains support for an optional secondary series in line-chart functions. The contract: callers pass `{ primary: {data, unit, formatter, scale}, secondary?: {data, unit, formatter} }`. If `secondary` is omitted, the chart renders as today — single axis, single series. If present, the chart adds a right axis and overlays the secondary series with the visual restraint described above (1.5 px, 85% opacity, secondary colour).

Breakdown bar charts do not get a "secondary series" feature — they get a unit toggle that swaps which field is plotted. Two distinct features in `chart.js`: dual-series for line charts, and a unit-switch parameter for bar charts.

### State layer (frontend)

`app.js` today holds `_hMetric` and `_dMetric` as single strings. They become pairs (or stay as strings parsed as comma-separated pairs). The wiring point — `_wireRange('hourly-metric-pills', m => ...)` — extends to handle the cyclic state machine. Single source of truth: one function that, given the previous state and the clicked pill, returns the next state.

`breakdown.js` gains three `_unit` state variables (one per toggle-eligible chart) and the corresponding pill-wire handlers. State independent across the three charts.

### Server layer

`/api/breakdown/by-project` and `/api/breakdown/by-model` get cost fields added to their response payloads. The bucketing functions in `serve/app.py` (`_bucket_turns`, the by-model code) accumulate cost alongside the existing token totals. No new endpoint, no new query parameter — the cost data ships in every response and the frontend chooses what to render.

Tests in `tests/serve/` that snapshot those endpoints' responses need their fixtures updated for the new fields.

## Error handling and edge cases

- **Zero-value buckets.** A day or project with zero output and zero cost renders a zero bar in both modes — no special case. The chart already handles zero values today.
- **Missing model in pricing table.** Already handled by `metrics/cost.py` — unknown models contribute zero cost. The aggregate cost may understate reality; surfacing that gap is out of scope for this work.
- **Single-metric value stored in `localStorage` from an older version.** Parser splits on comma; a value without a comma yields a one-element array, which falls through the single-series code path. No migration needed.
- **Pill click while data is loading.** Selection state updates immediately (UI is responsive); the next fetched payload is rendered with the new pill state. Same pattern as today.
- **Secondary axis with extreme scale mismatch** (e.g., output in millions vs cost in single dollars). Each axis auto-fits its own series — that's the entire point of dual-axis. The "Y AUTO-FITS DATA · HOVER FOR EXACT VALUES" footer already in place applies to both axes.

## Testing

- **Backend:** existing `/api/breakdown/*` response-shape tests get new cost-field assertions. Add one test per endpoint asserting cost equals the sum of `metrics/cost.py` per-turn cost across the relevant turns (uses an existing fixture; no new fixtures needed).
- **Frontend:** no automated frontend test suite exists today; visual verification only. Implementation includes a manual checklist of 4–5 representative overlay pairs to spot-check (e.g., `OUTPUT+COST`, `HIT%+$/KW`, `CACHE REUSE+CTX`, plus one where the units differ wildly to stress dual-axis scaling).
- **Persistence:** reload-and-check that the last selected metric pair and the last selected Breakdown unit pills are restored from `localStorage`. Spot-check, not automated.
- **Regression budget:** the only invasive change is `chart.js` — the line-chart function gains an optional `secondary` param. Test by loading every page that uses line charts (Overview, Day drill-down) with no overlay configured and confirming the rendering is byte-for-byte (or close enough) the same.

## Out of scope, explicitly

To prevent scope drift during implementation:

- Per-series project / model filters on Overview overlay (one filter applies to both series).
- $ toggle on Tools chart.
- $ toggle on Tool drill-down, Project drill-down, Model drill-down, Session detail.
- $ overlay on the Daily Cache Re-use chart.
- Adding cost columns to the Models table on Overview (it already has a COST column).
- Renaming any pills or changing the existing pill copy.
- Touching the cost calculation logic itself — only its consumption.

## Open question for implementation

One thing to confirm during implementation, not now:

- The exact secondary series colour. Slate-blue and olive-green are both candidates; pick whichever reads best against the cream background in a side-by-side local check before locking the CSS variable.

Everything else is settled.
