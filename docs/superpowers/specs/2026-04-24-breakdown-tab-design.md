# Breakdown tab — design

A new top-level page in the tokenol dashboard that answers the question *"where are my tokens going?"*, as a companion to the existing diagnostic Overview page. The design imports five chart ideas from `nateherkai/token-dashboard` — daily stacked bars, daily cache-reuse bars, tokens-by-project grouped bars, model-share donut, and top-tools horizontal bars — and re-homes them in tokenol's cream / Instrument-Serif / analytical aesthetic with tokenol-native additions that prevent the page from feeling like a direct port.

## Goals

1. Ship the five new chart concepts without crowding the existing Overview.
2. Keep each page coherent in purpose: Overview = diagnostic ("is my usage healthy?"); Breakdown = descriptive ("what did I do and where did the tokens go?").
3. Differentiate from the source inspiration through naming, metric selection, palette, and small tokenol-native touches.
4. Deliver in three independently-shippable PRs. The first two are pure UI on existing rollups; the third touches the ingest pipeline.

## Non-goals

- Redesigning the Overview page. It stays as-is beyond a small topbar tab addition.
- Migrating away from uPlot. uPlot stays for the Overview line charts.
- Cross-device preference persistence. Period state is per-tab, in-session only.
- Adding a "Tips" or rule-engine page like token-dashboard's.
- Touching drill-down pages (`/session/*`, `/project/*`, `/model/*`, `/day/*`).

## Page structure

A new route at `/breakdown` served from `breakdown.html`. The topbar gains a two-tab navigation element: `Overview | Breakdown`. The existing global period pills in the topbar stay wired to Overview; on the Breakdown page they are hidden (`body[data-page="breakdown"] .topbar-controls .pill-group { display: none }`), because Breakdown carries its own page-level period selector.

### Breakdown page layout (top to bottom)

```
┌─ Breakdown                                     [7D] 30D [90D] [All] ┐
│  tokens by day, project, model, and tool                            │
├────────────────────────────────────────────────────────────────────┤
│  ┌─ Activity ─┐ ┌─ Billable tokens ─┐ ┌─ Cache ─┐ ┌─ Est. Cost ───┐ │
│  │ 800        │ │ 24.7M             │ │ 2.2B    │ │ $5,938        │ │
│  │ sessions   │ │ 7.7M in · 17M out │ │ read    │ │ ↓ cache saved │ │
│  │ 45,294 t.  │ │                   │ │ 135M cr │ │   ≈ $18.7k    │ │
│  └────────────┘ └───────────────────┘ └─────────┘ └───────────────┘ │
├─ Time ─────────────────────────────────────────────────────────────┤
│  [Daily billable tokens    $5,938 · $198/d]  [Daily cache re-use]  │
├─ Breakdowns ───────────────────────────────────────────────────────┤
│  [Tokens by project (2×)         ]           [Model mix (donut)]   │
├─ Tools ────────────────────────────────────────────────────────────┤
│  [Tool mix — horizontal bars, full width]                          │
└────────────────────────────────────────────────────────────────────┘
```

- Page header row: left — page title ("Breakdown") in Instrument Serif plus one-line subtitle *"tokens by day, project, model, and tool"*; right — period pills.
- Period pills: `7D · 30D · 90D · All`. Default `30D`. Selected value persists in `sessionStorage` under key `tokenol.breakdown.period`.
- The equivalent `tokenol.overview.period` key is introduced for parity; each tab remembers its own period independently.
- Section group headings ("Time", "Breakdowns", "Tools") use tokenol's existing `.section-heading` pattern — Instrument Serif H2 on the left, right-aligned subtitle where useful.

## Scorecard row (4 cards, grouped)

Explicitly chosen over the 7-card token-dashboard opener to avoid visual rip-off and to give each card a mini-story: primary number plus 1–2 sub-numbers. All four cards respect the page's current period selection.

| Card | Primary | Sub-numbers |
|---|---|---|
| **Activity** | `{sessions}` sessions | `{turns}` turns |
| **Billable tokens** | `{input + output}` (e.g. 24.7M) | `{input}` in · `{output}` out |
| **Cache** | `{cache_read}` read (e.g. 2.2B) | `{cache_creation}` created |
| **Est. Cost** | `${cost_usd}` | cache saved ≈ `${cache_saved_usd}` (olive accent) |

### `cache_saved_usd` — new derived metric

Lives in `src/tokenol/metrics/cost.py`. Counterfactual: what cache reads would have cost if they had been billed at the full input rate for their model, minus what they actually cost.

```python
def cache_saved_usd(turns: Iterable[Turn]) -> float:
    """Per-turn: (cache_read_tokens × input_price_per_token) − actual cache_read cost.
    Summed across turns. Returns total USD saved by cache reuse over standard input billing."""
```

Surfaces in two places: under Est. Cost in the scorecard, and as the right-side subheading of the Daily cache re-use chart (`avg $X/d saved`).

## The five charts

All section headings follow tokenol's existing `.section-heading` pattern. The two daily charts carry a right-aligned subheading with a per-period total and average.

### 1. Daily billable tokens (Time section)

- **Type:** stacked bar, one bar per day.
- **Stacks:** input (`--amber`), output (`--alarm`), cache_creation (`--green`). Palette variables already defined in `styles.css:2–23`.
- **Data:** `DailyRollup` fields `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cost_usd`.
- **Subheading:** `total ${sum_cost} · avg ${cost / days}/d`.
- **Library:** Chart.js `type: 'bar'`, `stacked: true`.

### 2. Daily cache re-use (Time section)

- **Type:** single-series bar, one bar per day.
- **Color:** `--green` (shares stack color with cache_creation on chart 1 to tie them together visually).
- **Data:** `DailyRollup.cache_read_tokens`.
- **Subheading:** `total {sum_cache_read} · avg $X/d saved` (uses `cache_saved_usd` divided by days).
- **Library:** Chart.js `type: 'bar'`.

### 3. Tokens by project (Breakdowns section, 2× wide)

- **Type:** grouped bar, one project per x-axis tick.
- **Groups:** input (`--amber`) vs output (`--alarm`).
- **Data:** `ProjectRollup` — already in the snapshot.
- **Distinctive: cache-health dot** — a small 8-px colored dot prefixes each project tick label, injected via Chart.js `ticks.callback`. Dot color is driven by the project's `cache_hit_rate` using the thresholds already defined for Overview's Hit% tile:
  - green (≥ `HIT_PCT_GREEN`, default 95%)
  - amber (between red and green)
  - red (< `HIT_PCT_RED`, default 85%)
- **Click behavior:** clicking a project group drills to `/project/{cwd_b64}` (existing route).
- **Library:** Chart.js grouped `type: 'bar'`.

### 4. Model mix (Breakdowns section, 1× wide)

- **Type:** doughnut chart.
- **Data:** `ModelRollup` — share of billable tokens (`input + output`, cache excluded to match Billable tokens scorecard math) per model.
- **Palette:** cycles through `--amber`, `--alarm`, `--green`, `--cool`, `--mute`, `--amber-dim` in order. No vivid neon colors. Legend paginates if > 6 models (tokenol's low-usage tail can be long, as seen in `nateherkai/token-dashboard` example data).
- **Click behavior:** clicking a slice drills to `/model/{name}` (existing route).
- **Library:** Chart.js `type: 'doughnut'`.

### 5. Tool mix (Tools section, full width)

- **Type:** horizontal bar, ranked by call count. Top 10 plus "others" row.
- **Data:** new — requires parser change (see PR3 section below).
- **Library:** Chart.js `type: 'bar'` with `indexAxis: 'y'`.

## Backend changes

### API endpoints (all new, all read-only)

All piggyback on the in-memory `snapshot_result` cache (`app.py:53,69`), applying range filtering in memory. SSE tick behavior is unchanged; the client re-fetches breakdown endpoints on tick if the Breakdown tab is active.

| Endpoint | Source rollup | Response shape |
|---|---|---|
| `GET /api/breakdown/summary?range=30d` | `DailyRollup` + session list | `{sessions, turns, input, output, cache_read, cache_create, cost_usd, cache_saved_usd}` |
| `GET /api/breakdown/daily-tokens?range=30d` | `DailyRollup` | `[{date, input, output, cache_creation, cache_read, cost}]` |
| `GET /api/breakdown/by-project?range=30d` | `ProjectRollup` | `[{project, cwd_b64, input, output, cache_hit_rate}]` |
| `GET /api/breakdown/by-model?range=30d` | `ModelRollup` | `[{model, input, output, share}]` |
| `GET /api/breakdown/tools?range=30d` | new `tool_mix` aggregate (PR3) | `[{tool, count}]` |

All endpoints validate `range ∈ {7d, 30d, 90d, all}`, returning 400 on an unknown value. `insufficient_history` is returned using the same convention `/api/daily` already uses (`app.py:230–234`).

### PR3: tool-name ingest

The only invasive change in the project.

1. **`src/tokenol/ingest/parser.py:38–45`** — replace scalar `tool_use = 0` with `tool_uses: Counter[str]`. Capture `block["name"]` when `btype == "tool_use"`. Keep the existing turn-level aggregate count for backwards compat as `sum(tool_uses.values())`.
2. **`src/tokenol/model/events.py`** — add `tool_names: Counter[str]` field to Turn (default empty Counter). Keep existing `tool_use_count: int` untouched, populated from `sum(tool_names.values())`.
3. **`src/tokenol/metrics/rollups.py`** — add `tool_mix: Counter[str]` to `SessionRollup` aggregated across turns. Add a top-level `build_tool_mix(sessions: Iterable[SessionRollup], top_n: int = 10) -> list[dict]` that returns `[{tool, count}]` ranked descending, optionally collapsing the tail into `{tool: "others", count: sum_of_tail}`.
No cache-invalidation work is required. `ParseCache` (`src/tokenol/serve/state.py:49–60`) is in-memory only and re-parses any file whose `(size, mtime_ns)` changed. No disk cache exists.

Backwards compat: every existing `tool_use_count` consumer keeps its value; nothing downstream breaks.

## Frontend architecture

New or changed files:

- `src/tokenol/serve/static/breakdown.html` — page shell: topbar (with new tabs), page heading, scorecard grid, Time / Breakdowns / Tools section shells.
- `src/tokenol/serve/static/breakdown.js` — scorecard render, Chart.js loader, five chart renderers, period pill state (sessionStorage `tokenol.breakdown.period`).
- `src/tokenol/serve/static/chart.js` — extended with Chart.js global defaults tuned to tokenol's palette (font: Instrument Serif for titles / JetBrains Mono for tick labels; colors from existing CSS tokens).
- `src/tokenol/serve/static/styles.css` — new classes: `.nav-tabs`, `.scorecard`, `.scorecard-card`, `.breakdown-section`, `.chart-subheading`. Reuses existing cream/serif tokens; no framework swap.
- `src/tokenol/serve/static/index.html` — topbar gets the new tab markup. Page-level marker attribute (`data-page="overview"` / `data-page="breakdown"`) on `<body>` lets CSS hide the global period pills on the Breakdown page.
- `src/tokenol/serve/app.py` — `@app.get("/breakdown")` returning `breakdown.html`; the five `/api/breakdown/*` routes.
- `src/tokenol/metrics/cost.py` — `cache_saved_usd()`.

Chart.js is loaded from CDN via a `<script defer>` tag in `breakdown.html` only — not on Overview. Version pinned (`chart.js@4.4`), ~60 KB gzipped.

## Testing

Follows the existing project convention (pytest, fixture-driven, `tests/test_diagnostics.py` is the closest structural analog).

### PR1 tests
- `tests/test_breakdown_api.py` — `summary` and `daily-tokens` endpoints, range filtering, insufficient-history response, empty-snapshot response.
- `tests/test_cost.py` — `cache_saved_usd()` zero reads, typical reads, cross-model pricing, zero-cost-edge case.
- `tests/test_app_routes.py` — `/breakdown` returns 200 and HTML.

### PR2 tests
- `tests/test_breakdown_api.py` — add `by-project` and `by-model` endpoint tests; confirm `cache_hit_rate` pass-through and Billable tokens share math (cache-excluded).

### PR3 tests
- `tests/test_parser.py` — tool-name extraction from canonical fixtures: single tool, multiple tools per turn, mixed text + tool blocks, zero tools.
- `tests/test_rollups.py` — `tool_mix` aggregation up through Session level; `build_tool_mix` ranking and top-N + "others" rollup.
- `tests/test_breakdown_api.py` — `tools` endpoint ranking and top-N cutoff.

No integration tests against live Claude Code logs — fixtures only, per repo convention.

## Delivery

Three independently-shippable PRs, in order:

### PR 1 — Breakdown scaffolding + Time section
- Adds the new route, the tab nav, scorecard row, period pills, and the two daily charts.
- Zero ingest changes.

### PR 2 — Breakdowns section
- Adds `by-project` and `by-model` endpoints plus the project grouped-bars (with cache-health dots) and the model-mix donut.
- Still zero ingest changes.

### PR 3 — Tool mix + tool-name ingest
- Parser extension, schema field, rollup aggregation, API endpoint, Tool mix horizontal bar panel.

## Differentiation summary

Deliberate choices to prevent the page from feeling like a port of `nateherkai/token-dashboard`:

- **Tab name:** "Breakdown", not "Overview/Usage/Activity".
- **Subtitle:** *"tokens by day, project, model, and tool"* — self-describing, editorial voice.
- **Scorecard:** 4 grouped cards instead of 7 flat ones. Drops standalone Input and Cache-creation (noisy without their partners). Cost card carries a tokenol-native **cache saved** counterfactual badge.
- **Chart titles:** "Daily billable tokens" / "Daily cache re-use" / "Model mix" / "Tool mix". Terse, analytical, matches tokenol's glossary vocabulary.
- **No chatty captions:** tokenol uses its existing glossary modal (`?` key) rather than an inline educational paragraph under each chart.
- **Palette:** cream background, `--amber` / `--alarm` / `--green` / `--cool` accents (existing tokenol tokens). No neon violet / teal / pink.
- **Typography:** Instrument Serif titles, JetBrains Mono tick labels.
- **Cache-health dots** on project bars tie descriptive ⇆ diagnostic, an interaction `token-dashboard` doesn't make.
- **Per-tab period memory** — each tab remembers its own period, rather than a single shared global.

## Explicit out-of-scope

- uPlot → Chart.js migration. Keep both.
- Persisting period across server restarts.
- "Tips" / rule engine page.
- Drill-down page changes.
- Workspace-isolation UX changes (`CLAUDE_CONFIG_DIR` / `--all-projects`) — the Breakdown page respects whatever set of projects the snapshot already contains.
