# Changelog

All notable changes to tokenol are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.6.1

### Added

- **Attribution mode toggle on the Tool Mix panel.** Two-position pill group in the panel header lets you switch between the existing pro-rata cost split and a new "exclude cache-read" lens. The second mode routes cache_read_usd 100% to the non-tool residual instead of distributing it pro-rata across visible tool bytes, answering "what do tools cost excluding the cost of keeping their output around for subsequent turns?" Selection persists in localStorage; hidden when the panel is displaying token counts (mode is a cost-only concept).
- `mode=` query parameter on `GET /api/breakdown/tools` — accepts `prorata` (default) or `excl_cache_read`. Unknown values fall back to `prorata` silently (forward-compatible: older servers degrade gracefully when clients persist a newer mode token). The response echoes the effective `mode` in a new top-level field.
- `state.build_breakdown_tools(turns, *, mode='prorata') -> list[dict]` — extracts the previously-inline aggregation loop from `api_breakdown_tools` into a unit-testable module-level function.
- `tokenol.enums.AttributionMode` — new `str` enum (`PRORATA` / `EXCL_CACHE_READ`) so consumers can pass typed values instead of string literals to `build_breakdown_tools` and the API endpoint.

### Changed

- **`_wireUnitPills` → `_wirePillGroup`** in `breakdown.js`. Renamed and the `dataAttr` parameter is now required (no default), so each of the five pill groups passes its attribute explicitly. Reads as "wire a pill group keyed on this data-attr" instead of pretending unit and mode pills are the same concept.
- **`build_breakdown_tools` unified branch shape.** Both pro-rata and excl_cache_read modes now build a per-tool cost dict + residual pair before a single shared accumulation loop. Removes a copy-pasted last-active update block and the duplicate `cost_for_turn(...)` call that excl-mode used to make per turn.
- Per-tool docs added to README, `docs/ASSUMPTIONS.md` (heuristics catalog), and `docs/METRICS.md` (formulas + API field reference).

### Fixed

- **`_grouped_cwd_by_sid` memoization keyed on `id(sessions)` could return stale cwd remaps.** Python recycles `id()` for freed list objects, so a freshly-derived sessions list that happened to land at a recently-freed list's id would receive that prior list's cached remap — every session id missing from the old remap fell back to `"(unknown)"`. The cache key is now a content fingerprint (tuple of `(session_id, cwd)` pairs). Pre-existing latent bug surfaced by a declared-order test run during 0.6.1 release prep.
- **XSS hardening on project / session tables.** Eleven spots interpolated JSONL-derived strings (cwd, session_id) into HTML without escaping: `model.js`'s "projects using this model" cwd cell; `day.js`'s "top projects" title + cell and its "sessions" `data-id` + 8-char session-id cell; `app.js`'s "recent activity" cwd cell + the new "latest session" href; `components.js`'s shared `sessionRows` `<tr>` `data-id` + `title` + 8-char session-id cell; `project.js`'s "Top Turns" `data-sess` + 8-char session-id cell and its "Project Sessions" `data-id` + 8-char session-id cell. A pathological cwd basename or session_id like `"><img src=x onerror=…>` no longer executes. All eleven are pre-0.6.1 patterns surfaced by the release-gate security + adversarial review passes; included here to clear them before release.
- **Tool literally named `other` no longer collides with the synthetic collapse-tail row.** `_is_real_tool_name` (parser) now rejects `"other"` alongside `__unattributed__` / `__unknown__`. Without this filter, an attacker could craft a JSONL with a tool named `other` whose invocation count would be silently overwritten by the ranked-bar aggregator's tail-collapse logic.
- **`fmtUSD` rendered negative values as `$-0.50`** instead of `-$0.50`. Today no caller passes a negative — the bug was dormant — but `cost_per_kw`, `cost_usd`, and similar fields have no formal non-negativity guarantee, so a future regression would surface as garbled currency.
- **`$/kW` scorecard read `$0`** on the Overview page. `fmtUSD` (introduced in 0.6.0's "standardise dollar formatting on whole dollars" pass) was applying `Math.round` to every value, stamping out the entire sub-dollar signal — `$/kW` is inherently $0.01–$1 territory, so the scorecard, its `<$X GOOD · >$Y RED` threshold labels, and the `last hour: $…` sub-line all read `$0`. Now `fmtUSD` keeps two decimals for values in `[$0.01, $1)`, five decimals for `(0, $0.01)`, and whole dollars for `≥ $1` (the original rationale held for the $10+ table values). No call-site changes required.
- **Sub-dollar bypasses around the dashboard.** Sweep across every static JS file found four more places that bypassed the shared `fmtUSD` and would have shown `$0` for sub-dollar values: `session.js` had a local `fmtUSD = v => $${v.toFixed(2)}` (which would render a $0.003 turn cost as `$0.00`); `project.js` rendered `cost_per_kw` for both Top Turns and Project Sessions tables via inline `$${...toFixed(2)}`; `tool.js` daily-cost chart used `(v) => '$' + v.toFixed(2)` as its Y-axis tick callback; `chart.js` `Y_FMTRS.usd` (used by uPlot cost charts on Overview / Breakdown) was `$${v.toFixed(2)}`. All routed through the shared `fmtUSD` now.
- **`renderRankedBars` default formatter footgun.** The default `valueFormat` when omitted was `(n) => "$" + n.toFixed(2)`; now `fmtUSD`. No current caller relied on the default, but a future omission would have silently swallowed cents.
- **`↓0% vs 7d median` ghost arrow** on Overview tiles. When the rounded delta is exactly 0%, the tile would still render an arrow + "0%" (reading as a tiny decrease but actually flat). `_setTileDelta` now mirrors `deltaBadge`'s 0% suppression and falls through to the plain `vs <baseline> median` text.
- **Stale Tool Mix pill state on truthy-but-unknown localStorage values.** The `|| 'prorata'` fallback only triggered on falsy values, so a value left over from a prior build (or manually set) bypassed defaulting and left the pills with no selection highlighted. All five breakdown pill states now validate against a whitelist on load.

### Performance

- **`-215 MiB steady RSS` (-31 %)** for the no-persist server on a 92 K-turn / 375 K-event corpus (685 MiB → 470 MiB after-derive). Three stacked optimisations:
  1. **`slots=True` on every high-count dataclass.** `RawEvent`, `Turn`, `Usage`, `ToolCost`, `Session`, `Project` in the model layer (replicated 90 K-fold), plus the metrics-side rollup classes (`TurnCost`, `DailyRollup`, `HourlyRollup`, `SessionRollup`, `ProjectRollup`, `ModelRollup`, `DailyToolCost`), `PatternHit`, and `Window`. Saves 88 B/`Turn`, 88 B/`Usage`, ~60 B/`ToolCost` by dropping per-instance `__dict__`. **-82 MiB.**
  2. **Shared empty-container sentinels** (`EMPTY_TOOL_NAMES`, `EMPTY_TOOL_COSTS`, `EMPTY_ASSUMPTIONS` in `tokenol.model.events`). 275 K of 376 K events have no `tool_use` blocks; previously each carried a fresh empty `Counter` (80 B), empty `dict` (64 B), and on the `Turn` side every assumption-free turn carried a fresh empty `list` (56 B). All collapse to module-level singletons (audited: no in-place mutation anywhere in `src/tokenol/`). **-38 MiB.**
  3. **`sys.intern()` on low-cardinality string fields** at parse time: `source_file` (1929 unique paths × 376 K refs), `event_type` (~3 unique), `session_id` (447 unique × 92 K refs), `model` (~3 unique × 92 K refs), `stop_reason` (~5 unique), `cwd` (~50 unique). Strings from `json.loads` aren't interned by default; without sharing, the corpus carries 1.5 M near-duplicate `str` objects with ~50 B/string overhead. **-95 MiB.**

  No behavioural change — every transformation is internal to the parser + the dataclass layout. Restart the server (`uv run tokenol serve --port 8787`) to pick up the change.

### Notes

- No persistence changes — the mode toggle is purely a presentation-layer reinterpretation of already-stored per-turn `tool_costs` data.
- No changes to other panels — scorecards, daily charts, by-project, by-model, and the tool detail page (`/tool/{name}`) all stay on pro-rata regardless of the toggle.

## 0.6.0 — 2026-05-15

### Added

- **Per-tool cost attribution.** Causal model that splits a turn's four cost
  components (`input_usd + output_usd + cache_read_usd + cache_creation_usd`)
  across each tool by JSON byte share. Output side attributes by `tool_use`
  block bytes; input side attributes by lingering `tool_use` + `tool_use_result`
  bytes still in the conversation window. Compaction is detected heuristically
  (input pool drop below 20% of running peak) and resets the lingering tallies.
- **Breakdown → Tool Mix in `$` mode.** The TOKENS/$ toggle now extends to the
  Tool Mix panel; chart switches from Chart.js bars to a ranked-bar list. A
  dim italic `__unattributed__` row surfaces residual so totals reconcile to
  overall spend.
- **Tool detail page redesign** (`/tool/<name>`). 30-day daily-cost line chart,
  four scorecards (Est. Cost · Output tokens · Invocations · Top project), and
  cost-by-project + cost-by-model ranked bars replace the previous tables.
- **Project + model detail pages** gain a "Cost by tool" ranked-bar list.
- New API fields:
  - `/api/breakdown/tools` rows now include `cost_usd`, `count`, `last_active`,
    and a final `__unattributed__` sentinel row.
  - `/api/tool/{name}` adds `scorecards`, `daily_cost` (30 zero-filled points),
    `by_project`, `by_model`. Old `projects_using_tool` / `models_using_tool`
    keys removed.
  - `/api/project/{cwd_b64}` and `/api/model/{name}` add a `by_tool` block.

### Fixed (Tier 3 release-gate review)

Every finding surfaced by the Tier 3 review pipeline (6-specialist fan-out +
`fp-check` + `/second-opinion` + adversarial re-run) is fixed in this release.
No items deferred.

**Frontend correctness**

- **`breakdown.js` no longer crashes on load.** `const UNATTRIBUTED_TOOL =
  UNATTRIBUTED_TOOL;` was a temporal-dead-zone self-reference that threw
  `ReferenceError` and killed the entire Breakdown page. Replaced with the
  literal sentinel value.
- **Tool detail "30d total" subtitle now matches the chart.** `tool.js` was
  passing the all-time `scorecards.cost_usd` to the daily chart's "30d total"
  label; switched to summing the 30 daily points client-side.
- **`tool.js` model card cleanup.** Removed the `project_label` /
  `last_active` copy-paste branches that were only relevant to the
  by_project renderer.
- **XSS hardening in tool scorecards.** `tool.js` now passes interpolated
  values through `esc()` before rendering via `innerHTML`. A pathological cwd
  basename like `<img src=x onerror=…>` no longer executes (self-XSS only,
  but the fix is one import + four `esc(…)` calls).

**Aggregation correctness**

- **`by_tool` rollups now reconcile.** `_accumulate_tool_costs` and its
  callers (`build_project_detail`, `build_model_detail`) previously iterated
  only `tool_names`, dropping tools whose presence was purely linger-only
  cost. They now iterate `set(cost) | set(invs)`, so
  `sum(by_tool[].cost_usd)` matches the scorecard totals.
- **Tool detail page surfaces linger-only attribution.** `build_tool_detail`
  expanded its `tool_turns` filter to include turns where the tool appears
  in `tool_costs` even without a fresh invocation. Sentinel names
  (`__unattributed__` / `__unknown__`) explicitly return 404.
- **`__unknown__` no longer leaks as a clickable row.** `/api/breakdown/tools`
  folds `__unknown__` into the `__unattributed__` row; `_accumulate_tool_costs`
  does the same for project/model `by_tool` views.
- **`by_project` / `by_model` payloads capped at 50 entries** to bound API
  response size for users with hundreds of projects or model variations.
- **`other` row in Tool Mix now reports real call sums.** The "other" row's
  `count` (used as the bar value in tokens mode) is now the sum of tail tool
  invocations rather than the count of collapsed tools. The collapsed-tool
  count moved to a new `tool_count` field, displayed in the row label.
- **UTC-based date windows in new code paths.** `build_tool_detail` and
  `build_tool_cost_daily` now use `datetime.now(tz=timezone.utc).date()`
  instead of local `date.today()`, matching the UTC timestamps stored on
  every Turn.

**Parser correctness**

- **Compaction heuristic resets `peak_input_tokens`.** Previously a long
  session that stabilised below 20% of its historical peak kept re-triggering
  the reset on every turn, dumping all per-turn attribution into
  `__unattributed__`. Peak now resets to the new pool after a compaction
  event so the heuristic only fires on genuine context drops.
- **Sentinel tool-name collision rejected.** `_extract_tool_blocks` and
  `_output_byte_shares` now drop `tool_use` blocks whose name is
  `__unattributed__` or `__unknown__` so a hostile log can't hide cost under
  the cost-attribution sentinels.
- **Plain-string assistant content no longer dropped from byte tallies.**
  When `message.content` is a string (rare but spec-legal for short replies),
  the parser now wraps it as a single `text` block so its bytes feed the
  non-tool input pool on subsequent turns — preventing slight over-attribution
  to lingering tools.
- **`_block_bytes` catches `RecursionError`.** A deeply nested malformed
  content block would have crashed the entire `parse_file` (the previous
  `except (TypeError, ValueError)` missed `RecursionError`).
- **`_block_bytes` called once per content block.** `_output_byte_shares`
  now accepts pre-sized `(block, bytes)` pairs so each block is serialized
  exactly once per assistant turn rather than twice (output-share pass +
  context-accumulation pass).

**Performance**

- **`build_tool_cost_daily` now scoped to `tool_turns`** in
  `build_tool_detail` instead of walking the full corpus on every
  `/api/tool/{name}` request.
- **`_grouped_cwd_by_sid` memoized per snapshot.** O(C²) cwd ancestor scan
  no longer reruns on every API request; bounded LRU keyed on `id(sessions)`.

**Persistence**

- **`tool_costs` and `unattributed_*` round-trip through DuckDB.** Schema
  v2 adds `tool_costs JSON` plus three `unattributed_*` DOUBLE columns to
  the `turns` table; migration is idempotent via `ALTER … IF NOT EXISTS`.
  Existing v1 databases upgrade in place on open. Warm-tier breakdowns
  for `--persist` users on `range=all` now reconcile correctly. Older v1
  rows pre-dating this release hydrate with empty `tool_costs` until they
  age out of the window — re-ingest from the source JSONL files to backfill.

**API hardening**

- **`/api/tool/{name}` and `/api/model/{name}` accept path-segment names.**
  Switched to FastAPI's `{name:path}` converter so MCP tool names like
  `mcp__server/tool` resolve instead of 404ing; explicit validation rejects
  empty names, `..` path-traversal segments, and embedded NULs.

**Rollups**

- **`_rank_dict_with_others` is deterministic on ties.** Sort now uses
  `(-value, name)` so equal-cost entries don't shuffle "other" membership
  between runs.
- **Dead `build_tool_cost_rollups` / `ToolCostRollup` removed.**
  `state.py:_accumulate_tool_costs` was the only consumer of similar logic
  and lived in a different shape; the unused rollups version is gone.

### Notes

- DuckDB schema bumps to v2; migration is idempotent so opening an existing
  0.5.x history file upgrades in place. Per-tool token fields are floats
  (fractional after share split); aggregate reconciliation is exact to
  floating-point precision.

## 0.5.1 — 2026-05-15

### Fixes

- **`tokenol serve` now scans every `~/.claude*` directory by default.**
  Previously the dashboard honored `CLAUDE_CONFIG_DIR` and silently scoped
  itself to a single project when workspace isolation pointed the env var
  at one directory — which made Daily History look mysteriously empty for
  days when you were working in other projects. The dashboard is now
  always cross-project unless you explicitly pass `--scoped`. CLI commands
  (`daily`, `sessions`, `projects`, …) are unchanged — they still default
  to single-project with `--all-projects` / `-A` as the opt-in.
- The old `--all-projects` / `-A` flag on `serve` has been removed (the
  behavior it produced is now the default). Update any scripts that
  passed it; otherwise no action needed.

## 0.5.0 — 2026-05-15

### Features

- **Overview: dual-metric compare overlay on Hour-By-Hour and Daily History.**
  Each chart gains a small `compare` toggle pill. Toggle on → pick a second
  metric from the existing pill row and it renders on a right y-axis,
  overlaid on the primary series. The secondary line is slate-blue
  (`--series-secondary`) at 1.5 px for visual restraint; an inline legend
  below the pill row names both. LIN/LOG applies to the primary axis only;
  secondary axis is always linear. Toggle off drops the secondary and
  restores single-series view. Selection and compare state persist per chart
  in `localStorage`. First-time users land on `HIT%` single-series exactly
  as before.
- **Breakdown: per-chart `TOKENS / $` toggle on three cards.** Daily Billable
  Tokens, Tokens by Project, and Model Mix gain a small pill pair next to
  their titles. `$` mode shows actual cost stacked by component
  (input / output / cache created / cache read) so the bar height equals the
  per-bucket cost; Model Mix slice sizing switches to cost share, which
  surfaces how heavily Opus dominates cost despite being a small token
  share. Cache-hit dots, "top N of M" captions, and the existing
  $-annotated summary cards are unchanged. Each chart's mode is independent
  and persists in `localStorage`.
- **Backend payloads enriched with cost.** `/api/breakdown/by-project` returns
  per-component cost (`input_cost`, `output_cost`, `cache_creation_cost`,
  `cache_read_cost`); `/api/breakdown/by-model` returns `cost_usd` and
  `cost_share` per model; `/api/breakdown/daily-tokens` returns the same
  four per-component cost fields per day. No new endpoints, no schema
  changes. The Tools chart and the Daily Cache Re-use chart keep their
  current shapes — per-tool cost attribution would need a parser change,
  deliberately deferred (same constraint that omitted the error-rate column
  from Tool drill-down).

### Fixes

- **Chart y-axis lower bound is clamped to 0.** Non-negative tokenol metrics
  (Hit%, Output, Cost, …) no longer extend into a phantom negative-padding
  zone when the data is close to zero.
- **Right-axis labels no longer read as negatives.** Hidden the inward tick
  marks on the secondary axis — they were touching the dollar signs and
  visually reading as minus signs.
- **Secondary-metric switch rebuilds the chart instance.** uPlot fast-path
  now compares the secondary y-unit too, so switching the overlaid metric
  (e.g. Output → Cost) replaces the value formatter instead of leaving the
  old one in place.

### Internal

- `chart.js` `drawChart` (uPlot) accepts an optional `secondary` series with
  its own right y-axis (`y2`). Single-series callers pass the flat opts shape
  unchanged; dual-axis callers wrap it as `{ primary, secondary }`.
- `_bucket_turns` in `serve/app.py` accumulates per-component cost
  (`input_cost`, `output_cost`, `cache_read_cost`, `cache_creation_cost`,
  `total_cost`) alongside the existing token totals, so by-project and
  by-model endpoints reuse the same aggregation pass.
- CSS-var lookups in the breakdown palette and Overview legend are now
  memoized — design tokens are static for the page lifetime and were being
  hit several times per chart × every SSE tick.

## 0.4.1 — 2026-05-03

### Fixes
- **Daily History range pills now actually filter the chart.** `rollup_by_date`
  zero-filled the requested `[since, until]` window but never dropped turns
  dated before `since`, so 7D / 30D / 90D rendered the same full series as
  ALL. Turns outside the window are now skipped before bucketing.

## 0.4.0 — 2026-05-03

### Features
- **Persistent history that survives JSONL deletion** (opt-in via
  `--persist`). `tokenol serve --persist` backs the live in-memory
  dashboard with a single-file DuckDB store at `~/.tokenol/history.duckdb`
  (override via `TOKENOL_HISTORY_PATH`). On startup the store seeds the
  in-memory hot tier so cold start is bounded by `hot_window_days`
  (default 90), not by total history length. Each tick parses only JSONLs
  whose `mtime_ns` exceeds the per-session high-water mark — typically
  just today's active files — and appends derived turns to both memory and
  a background batch flush (every 30 s or 100 turns). Deleting a JSONL no
  longer drops its data from the dashboard; the affected sessions are
  marked `archived=True` and continue to render every quantitative panel.
  Only the per-turn modal's content snippets (user prompt, assistant
  preview, tool-call list) become unavailable for archived sessions, in
  line with the privacy intent of the deletion. Default off — `tokenol
  serve` without `--persist` matches the v0.3.2 resource profile
  byte-for-byte (no `import duckdb`, no `~/.tokenol/` directory, no extra
  steady RSS).
- `Preferences.hot_window_days` (default `90`, accepted range `1..3650`),
  exposed via the existing `/api/prefs` endpoint. Takes effect on next
  startup.
- `Session.archived: bool` field surfaced through `/api/session/{id}` and
  `/api/session/{id}/turn/{idx}`; the session-detail page renders an
  amber "Archived — text snippets unavailable" badge and hides the
  per-turn snippet block when the flag is set.
- `tokenol.persistence.forget_handoff` — a pidfile + atomic request-file
  handshake so a future `tokenol forget` CLI (PR 2) can apply deletions
  to a live serve within one tick, without requiring a restart.

### Changes
- `duckdb` moved from a core dependency to the new `[persist]` optional
  extras group. Default `pip install tokenol` no longer pulls the DuckDB
  binary wheel (~30 MB saved). Users who pass `--persist` install with
  `pip install 'tokenol[persist]'`.
- `build_snapshot_full` now accepts optional `history_store` and
  `flush_queue` arguments. When neither is supplied the legacy whole-corpus
  derivation path is used unchanged, so CLI report commands and any
  existing test that constructs a bare `ParseCache` keep working.
- Default mode prints a yellow `WARNING` at startup if it finds an existing
  `~/.tokenol/history.duckdb` (or `TOKENOL_HISTORY_PATH`), prompting the
  user to pass `--persist` if they want to use it (rather than silently
  ignoring the file).
- `select_edge_paths` now tracks per-file `mtime_ns` instead of comparing
  filesystem mtime to turn timestamps — fixes a freshness bug in the
  store-backed snapshot path where backdated turn timestamps could silently
  exclude files from re-parse.

### Notes
- See `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md` for
  the gating-and-extras design.
- See `docs/superpowers/specs/2026-05-02-persistent-history-design.md` for
  the underlying store design.

## [0.3.2] — 2026-04-28

### Fixed
- **Dashboard fallback endpoints no longer freeze on stale turn counts.**
  The 0.3.1 `/api/snapshot` fast-path stopped refreshing
  `app.state.snapshot_result`, so every endpoint that fell back to it
  (`/api/hourly`, `/api/daily`, `/api/models`, `/api/recent`, `/api/session/*`,
  `/api/project/*`, `/api/breakdown/*`, `/api/search`, `/api/model/*`,
  `/api/tool/*`) silently froze at first-page-load turn counts. Symptom: the
  HIT% panel showed live turns but switching to $/KW, CTX, Cache Reuse, Output,
  or Cost rendered stale numbers. Fix: `SnapshotBroadcaster` now exposes its
  freshest `SnapshotResult`, and fallback endpoints prefer it over the
  app-level cache.

## [0.3.1] — 2026-04-27

### Fixed
- **Dashboard auto-update self-heals from SSE drift.** The live dashboard
  could intermittently freeze on stale tile/chart values while the SSE
  connection appeared healthy (dot green, messages arriving), requiring a
  hard reload. Reproduced reliably in long-lived browser tabs; root cause
  is browser-environmental (extension hooks, accumulated tab state, or
  long-lived `EventSource` quirks). Fix is a layered, root-cause-agnostic
  resilience set in the static client:
  - `/api/snapshot` polling backstop every 30 s while the tab is visible —
    state self-heals within 30 s even if SSE delivery silently breaks.
  - Force-reconnect on `visibilitychange → visible` when the last message
    is older than 15 s (browsers throttle background-tab timers/SSE).
  - 90 s SSE staleness watchdog (silent stalls don't always fire `onerror`).
  - Live "last update Ns ago" tooltip on the SSE dot for at-a-glance
    freshness.

### Changed
- `/api/snapshot` reuses `SnapshotBroadcaster.cached_payload(period)` when
  an SSE group is live for the requested period, avoiding a redundant full
  rebuild. Cuts the snapshot fetch from ~175 ms to ~100 ms (mostly JSON
  serialization), making the new poll backstop essentially free.

## [0.3.0] — 2026-04-27

### Changed
- **`tokenol serve` resource use slashed.** On a real session-history workload,
  steady-state RSS drops ~8× (from ~4 GiB to ~500 MiB) and idle CPU falls to
  near zero between heartbeats. Multi-tab dashboards now share a single
  background producer, so adding tabs does not multiply server CPU.
- The SSE stream (`/api/stream`) is driven by `SnapshotBroadcaster`: one task
  per `period` fans payloads out to N subscribers, each maintaining its own
  shallow-diff state. The wire format is unchanged.
- The producer now gates rebuilds on JSONL file `(path, size, mtime_ns)`
  changes, with a configurable heartbeat (default 60 s) so time-windowed
  panels (`recent_activity`, day boundaries) stay reasonably fresh. Trade-off:
  panels may lag wall-clock by up to the heartbeat between file writes.
- `ParseCache` now memoizes derived `(turns, sessions, fired)` keyed on the
  active file-key set; idle ticks skip the per-tick `_build_turns_and_sessions`
  rebuild entirely.
- `_build_turns_and_sessions` now returns the per-build assumption-fired
  `Counter` instead of mutating the global `assumption_recorder`.
- `create_app` migrated from the deprecated `@app.on_event("shutdown")` to a
  lifespan context manager.

### Removed
- **`RawEvent.raw` field.** Was populated by the parser with the full JSON
  dict (message bodies, tool I/O) for "extensibility" but read by no
  downstream code. Removing it is the dominant memory win. Code that wants
  raw JSON should re-read from disk (as `serve/session_detail.py` already
  does).

## [0.2.0] — 2026-04-25

### Added
- **Breakdowns tab** (`/breakdown`): new top-level page with three sections —
  Time (daily billable-tokens stacked bars + daily cache-reuse bars),
  Breakdowns (tokens-by-project grouped bars with cache-health dots,
  model-mix doughnut), and Tools (tool-mix horizontal bars). SSE-driven,
  in-place refresh, period-pill state persisted in sessionStorage.
- **Tool drill-down** (`/tool/<name>`): per-tool usage and error stats with
  click-through from the Tool Mix chart on the Breakdowns tab.
- **Breakdown API**: `/api/breakdown/summary`, `/api/breakdown/daily-tokens`,
  `/api/breakdown/by-project`, `/api/breakdown/by-model`,
  `/api/breakdown/tools`, `/api/tool/<name>`.
- **Cache-health thresholds** are now plumbed through `/api/prefs` and
  configurable from the Settings modal.
- **Per-turn tool names** captured in the parser and surfaced through
  `SessionRollup.tool_mix` (top-N aggregator with "others" bucket).
- **Top-nav tabs** (Overview / Breakdowns) on the dashboard topbar.

### Changed
- Chart.js defaults now derive from tokenol CSS design tokens for visual
  consistency across charts.
- `/api/breakdown/by-project` consolidates nested cwds using the same
  shortest-proper-ancestor rule as the projects rollup.

### Fixed
- Sessions are now keyed by JSONL `session_id` rather than file stem, so
  multi-session files (and renamed files) attribute turns correctly.

## [0.1.1] — 2026-04-23

### Fixed
- README screenshots now render on PyPI: switched from relative paths to
  absolute `raw.githubusercontent.com` URLs.

## [0.1.0] — 2026-04-23

Initial public release.

### Added
- **CLI** (`tokenol`): `daily`, `hourly`, `live`, `sessions`, `projects`,
  `models`, `verify`, `serve`.
- **Ingestion**: discovery across `~/.claude*` dirs (honours `CLAUDE_CONFIG_DIR`),
  JSONL parsing, compound-key deduplication (`message.id:requestId`),
  Windows cwd normalization.
- **Metrics**: cost rollups with full 4-component billing (input, output,
  cache_read, cache_creation); 5-hour rolling-window cost; context growth,
  cache hit rate, cache reuse, cost-per-kW output; session verdicts
  (`OK`, `CONTEXT_CREEP`, `RUNAWAY_WINDOW`, `TOOL_ERROR_STORM`,
  `SIDECHAIN_HEAVY`).
- **Pattern detection** on session drill-down: `idle_expiry`,
  `compaction_reinflation`, `context_ceiling_plateau`, `sidechain_explosion`,
  `tool_error_storm` with severity escalation.
- **Live dashboard** (`tokenol serve`): SSE-streamed main view with headline
  tiles (+ last-hour trajectory), hourly / daily charts (linear ↔ log
  toggle), model and project rollups, recent-activity table.
- **Drill-down pages**: `/session/<id>` (patterns, cost-per-turn small
  multiples, per-turn modal), `/project/<cwd>` (cache trend with auto
  hourly/daily bucketing, verdict distribution with tooltips),
  `/day/<date>`, `/model/<name>`.
- **Preferences**: user-editable thresholds and ranges persisted via
  `XDG_CONFIG_HOME`.
- **Project grouping**: shortest-proper-ancestor cwd rule generalizes across
  nested repos without per-user configuration.

### Tested
- 184 unit + integration tests on Python 3.10 / 3.11 / 3.12.

[0.2.0]: https://github.com/farhanferoz/tokenol/releases/tag/v0.2.0
[0.1.1]: https://github.com/farhanferoz/tokenol/releases/tag/v0.1.1
[0.1.0]: https://github.com/farhanferoz/tokenol/releases/tag/v0.1.0
