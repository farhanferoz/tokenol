# Changelog

All notable changes to tokenol are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
