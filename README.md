# tokenol

Audit [Claude Code](https://claude.com/claude-code) JSONL session logs for cost, cache health, context blow-ups, and 5-hour rate-limit pressure.

`tokenol` parses the session transcripts that Claude Code writes to `~/.claude*/projects/**/*.jsonl` and produces per-day, per-session, per-project, and per-model rollups — plus a live burn-rate view for the active 5-hour window.

## Install

```bash
pipx install tokenol
```

Requires Python 3.10+.

## Quick start

```bash
# Daily token / cost aggregates over the last 14 days
tokenol daily

# Hourly breakdown for today
tokenol hourly

# Top 10 most expensive sessions in the last 30 days
tokenol sessions --since 30d --top 10 --sort cost

# Per-project rollup
tokenol projects

# Live view: burn rate + projected end-of-window cost
tokenol live --last 20m
```

All commands scan every JSONL file under `$CLAUDE_CONFIG_DIR` (falling back to the standard `~/.claude*` locations) and deduplicate turns using the same `message.id:requestId` compound key that [ccusage](https://github.com/ryoppippi/ccusage) uses.

### Scanning multiple projects

If you use workspace isolation (one `~/.claude-<project>` directory per repo, pointed at via `CLAUDE_CONFIG_DIR`), `tokenol` by default only sees the currently-active project. Pass **`--all-projects`** (or `-A`) to any command to scan every `~/.claude*` directory and get a cross-project view:

```bash
# Total spend across every project in the last 14 days
tokenol daily --since 14d --all-projects

# Which sessions cost the most, globally
tokenol sessions --since 30d --top 10 -A
```

You can also set `CLAUDE_CONFIG_DIR` to a colon- or comma-separated list of paths to scan a specific subset.

## Commands

| Command    | What it shows                                                               |
| ---------- | --------------------------------------------------------------------------- |
| `daily`    | Per-day tokens (input, output, cache read/creation), cost, turn count       |
| `hourly`   | Per-hour breakdown for a single day (defaults to today)                     |
| `live`     | Active 5-hour window burn rate, recent-activity rate, projected final cost  |
| `sessions` | Per-session detail table with blow-up verdict (RUNAWAY, CONTEXT_CREEP, …)  |
| `projects` | Per-project rollup grouped by `cwd`                                         |
| `models`   | Per-model rollup with tool-use counts and error rates                       |
| `verify`   | Cross-check tokenol totals against `ccusage --json` (if installed)          |
| `serve`    | Launch a local browser dashboard with live burn-rate gauge and all panels   |

Every command accepts:

- `--since 14d` — lookback window (e.g. `7d`, `30d`, or an ISO date)
- `--all-projects` / `-A` — scan every `~/.claude*` directory (ignores `CLAUDE_CONFIG_DIR`)
- `--strict` — exit non-zero if any cost-computation assumption fired
- `--show-assumptions` — always print the assumption footer
- `--log-level debug|info|warning`

`tokenol sessions` additionally takes `--sort` (`cost`, `input`, `output`, `cache_read`, `turns`, `max_input`, `duration`) and `--top`.

`tokenol live` takes `--last 20m|2h|30s` and exits non-zero if the projected window cost exceeds the configured reference.

## Live dashboard

```bash
# Install with dashboard dependencies
pipx install 'tokenol[serve]'

# Start the dashboard (binds to http://127.0.0.1:8787)
tokenol serve

# Cross-project view, faster tick, custom reference threshold
tokenol serve --all-projects --tick 2s --reference 25

# Open browser automatically
tokenol serve --open
```

The dashboard runs entirely in your browser and updates every 5 seconds as Claude Code writes new events to disk. It shows burn rate, projected window cost, today's spend, daily history, per-session and per-project breakdowns, a 14-day cost heatmap, and a live feed of the most recent turns. Click any session row to open a drill-down page with a per-turn chart, tool-use timeline, and sortable turn table.

Settings (tick interval, reference threshold, range toggles) are persisted in `localStorage` and survive page reloads. Changes to tick interval and reference threshold are applied to the running server immediately without a page reload.

See [`docs/DASHBOARD.md`](docs/DASHBOARD.md) for a full panel reference and keyboard shortcuts.

## What it detects

For every session, `tokenol` computes a blow-up verdict against spec-defined thresholds:

| Verdict (table label)         | Trigger                                                |
| ----------------------------- | ------------------------------------------------------ |
| `RUNAWAY_WINDOW` (`runaway`)  | Any 5-hour window costs ≥ \$50                         |
| `CONTEXT_CREEP` (`ctx-creep`) | Max single-turn input ≥ 500k **and** growth ≥ 2k/turn  |
| `TOOL_ERROR_STORM` (`tool-errs`) | ≥ 10 tool uses with > 30% error rate                |
| `SIDECHAIN_HEAVY` (`sidechain`) | Sidechain session costing > \$5                      |
| `OK` (`ok`)                   | Everything else                                        |

### Daily efficiency columns

The `tokenol daily` report shows these cost/cache efficiency ratios:

| Column   | Meaning                                                | Target     |
| -------- | ------------------------------------------------------ | ---------- |
| `$/kW`   | USD per 1,000 output tokens (cost per unit of "work")  | `< $0.20`  |
| `Ctx`    | Context tokens read per output token                   | lower is better |
| `CacheE` | Cache reads per cache-creation token (reuse ratio)     | `> 50:1`   |
| `Hit%`   | % of context served from cache (vs. paid input/create) | `> 98%`    |

Thresholds live in `src/tokenol/metrics/verdicts.py` and can be tuned per-project.

## Pricing

Flat per-model rates (no 1M-token tier surcharge — matches ccusage's default behaviour). The current registry lives in `src/tokenol/metrics/cost.py`. When a turn's model isn't in the registry, `tokenol` records an `UNKNOWN_MODEL_FALLBACK` assumption tag and uses a conservative default; run with `--show-assumptions` or `--strict` to surface these.

See [`docs/METRICS.md`](docs/METRICS.md) for metric definitions and [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) for the full list of assumption tags.

## Development

```bash
git clone https://github.com/farhanferoz/tokenol
cd tokenol
uv sync --extra dev
uv run pytest
uv run ruff check
```

## Licence

MIT
