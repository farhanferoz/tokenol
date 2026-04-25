# Changelog

All notable changes to tokenol are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
