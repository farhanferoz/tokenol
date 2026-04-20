# Phase 3 — Live Streaming Frontend

## Goal

A local, always-on browser dashboard that shows the same data as `tokenol live` / `tokenol daily`, updating in real time as Claude Code writes new events to disk. Glanceable on a second monitor while the user works. Kills the 5h-window-runaway risk by making burn rate visually impossible to ignore.

## Non-goals

- **Authentication / multi-user / remote deployment.** Localhost-only.
- **Mobile / responsive.** Target ≥ 1280 px desktop. One breakpoint, one layout.
- **Database.** Recompute from raw JSONL on every tick. Cache by `(path, mtime, size)`.
- **Write operations.** Dashboard is read-only.
- **Sound / desktop notifications on alarm.** Deferred to Phase 4.
- **Light theme.** Dark only — a light palette would dilute the mission-control voice.
- **Retention > 90 days in UI.** Anything beyond 90 d stays in the CLI.

## Architecture

```
┌──────────────────────────────────────────────┐
│  Browser (single page, vanilla JS)           │
│   ├─ EventSource('/api/stream')              │
│   ├─ uPlot sparklines + custom burn gauge    │
│   └─ Solari-board digit roll on updates      │
└──────────────────────────────────────────────┘
                     ▲
                     │ HTTP + SSE
                     │
┌──────────────────────────────────────────────┐
│  FastAPI + uvicorn (tokenol serve)           │
│   ├─ GET  /                 → dashboard shell│
│   ├─ GET  /session/<id>     → drill-down page│
│   ├─ GET  /api/snapshot     → full initial   │
│   ├─ GET  /api/session/<id> → turn timeline  │
│   ├─ GET  /api/stream       → SSE tick       │
│   └─ GET  /assets/*         → static CSS / JS│
└──────────────────────────────────────────────┘
                     ▲
                     │ reuses src/tokenol/ingest + metrics
                     │
            ┌────────┴────────┐
            │  ~/.claude*/**.jsonl  │
            └─────────────────┘
```

### Update cadence

- **Default SSE tick:** 5 s. With the mtime cache, each tick costs ~50–200 ms during active use (one active file parsed) and ~5 ms idle (just stat calls). Safe even with 5000+ JSONL files.
- **User-configurable** from a settings menu: `2s / 5s / 10s / 30s`.
- **Idle back-off (on by default):** if nothing has changed for 30 s, tick interval drops to `max(tick × 3, 15 s)`. Resumes at configured tick on the next change.
- **What gets re-read:** files whose `mtime_ns` or `size` changed since the last tick. Unchanged files use cached `RawEvent` lists. New files are picked up automatically.
- **SSE payload:** full snapshot on first connect; subsequent messages carry only the top-level keys whose values changed (shallow diff). Client deep-merges into local state.

## Backend

### New module: `src/tokenol/serve/`

```
serve/
├── __init__.py
├── app.py           # FastAPI app factory
├── streaming.py     # SSE generator + tick loop with idle back-off
├── state.py         # cached snapshot builder, mtime-keyed parse cache
├── session_detail.py# drill-down payload builder
├── static/          # index.html, session.html, styles.css, app.js, gauge.js, solari.js
└── templates/       # only if we need server-side rendering (unlikely)
```

### Snapshot payload shape

```jsonc
{
  "generated_at": "2026-04-20T12:55:32Z",
  "config": {
    "reference_usd": 50.0,
    "all_projects": true,
    "tick_seconds": 5
  },
  "active_window": {
    "start": "2026-04-20T12:00:00Z",
    "end":   "2026-04-20T17:00:00Z",
    "elapsed_seconds": 3332,
    "remaining_seconds": 14668,
    "cost_usd": 8.08,
    "projected_window_cost": 62.70,
    "over_reference": true,
    "burn_rate_usd_per_hour_1m": 14.20,
    "burn_rate_usd_per_hour_5m": 12.50,
    "burn_rate_usd_per_hour_15m": 9.80,
    "burn_rate_usd_per_hour_60m": 7.10,
    "burn_rate_series": [           // last 60 min @ 1-min buckets
      {"t": "2026-04-20T11:55:00Z", "usd_per_hour": 9.2},
      // ...
    ]
  },
  "today": {
    "date": "2026-04-20",
    "cost_usd": 30.08,
    "output_tokens": 281600,
    "cache_read_tokens": 38050000,
    "hit_rate": 0.95,
    "cost_per_kw": 0.107,
    "turns": 443,
    "hourly": [                     // one entry per UTC hour of today
      {"hour": "2026-04-20T07:00:00Z", "cost_usd": 0.70, "turns": 4},
      // ...
    ]
  },
  "daily_90d": [                    // one entry per day, last 90 days, zero-filled
    {"date": "2026-04-06", "cost_usd": 119.73, "output_tokens": 682700, "cost_per_kw": 0.175, "hit_rate": 0.979},
    // ...
  ],
  "sessions_14d": [                 // trimmed to 50 highest-cost for payload size
    {"id": "41adebc8", "model": "sonnet-4.6", "first_ts": "...", "last_ts": "...",
     "cost_usd": 28.49, "turns": 441, "max_input": 230300, "verdict": "sidechain",
     "cwd": "/home/ff235/dev/claude_rate_limit"},
    // ...
  ],
  "projects_14d": [
    {"cwd": "/home/ff235/dev/claude_rate_limit", "cost_usd": 38.26, "sessions": 3,
     "turns": 647, "cache_reuse_ratio": 0.953},
    // ...
  ],
  "models_14d": [
    {"model": "opus-4.7", "cost_usd": 19.44, "turns": 164, "input_tokens": 830,
     "output_tokens": 164000, "cache_read_tokens": 15040000, "tool_error_rate": 0.0},
    // ...
  ],
  "heatmap_14d": {                  // UTC hour × date, last 14 days
    "dates": ["2026-04-07", ..., "2026-04-20"],
    "hours": [0, 1, ..., 23],
    "cells": [[0.0, 0.0, ..., 12.3], ...]   // 14 rows × 24 cols, USD
  },
  "recent_turns": [                 // last 20 assistant turns across all sessions
    {
      "ts": "2026-04-20T12:54:11Z",
      "session_id": "41adebc8",
      "model": "sonnet-4.6",
      "cost_usd": 0.0234,
      "input_tokens": 25,
      "output_tokens": 412,
      "cache_read_tokens": 124000,
      "is_sidechain": true,
      "tool_use_count": 1,
      "tool_error_count": 0
    },
    // ...
  ],
  "assumptions_fired": {
    "DEDUP_PASSTHROUGH": 1179,
    // ...
  }
}
```

Client-visible ranges are filtered from these arrays — the server always ships the widest range (14 d for sessions/projects/models, 90 d for daily, 14 d × 24 h for heatmap, 60 m for burn series), the client trims based on the user's range selector without a round-trip.

### Session-detail payload (`/api/session/<id>`)

```jsonc
{
  "session_id": "41adebc8",
  "source_file": "/home/ff235/.claude-.../41adebc8.jsonl",
  "model": "sonnet-4.6",
  "cwd": "/home/ff235/dev/claude_rate_limit",
  "verdict": "sidechain",
  "first_ts": "2026-04-20T08:12:03Z",
  "last_ts": "2026-04-20T12:54:11Z",
  "totals": { "cost_usd": 28.49, "turns": 441, "tool_uses": 384, "tool_errors": 2 },
  "turns": [
    {
      "ts": "2026-04-20T08:12:03Z",
      "model": "sonnet-4.6",
      "input_tokens": 25, "output_tokens": 412,
      "cache_read_tokens": 124000, "cache_creation_tokens": 18000,
      "cost_usd": 0.0234, "is_sidechain": false,
      "tool_use_count": 1, "tool_error_count": 0,
      "stop_reason": "end_turn"
    },
    // ... every billable turn in order ...
  ]
}
```

### Parse cache

Keyed by `(path, size, mtime_ns)`. Value is the parsed `RawEvent` list for that file. On each tick:

1. Glob all JSONL paths (respects `--all-projects`).
2. For each path, check the cache; reparse if `(size, mtime_ns)` differs.
3. Rebuild turns/sessions/rollups from cached events.

Only the 1–2 files actively being written hit the parser per tick.

### CLI surface

```bash
tokenol serve                        # bind 127.0.0.1:8787, 5s tick, current workspace
tokenol serve --port 9000            # different port
tokenol serve --tick 2s              # faster updates
tokenol serve --all-projects         # scan every ~/.claude* dir
tokenol serve --reference 25         # override $50 reference
tokenol serve --open                 # open the dashboard in the default browser
```

`--tick` and `--reference` only set the **defaults**; users can change both at runtime via the settings menu, and those choices persist in `localStorage`.

Server exits on SIGINT / SIGTERM and prints the bind URL once at startup.

## Frontend

### Zero-build philosophy

- Single `index.html` + `session.html`, each with a `<link>` to Google Fonts (Instrument Serif + JetBrains Mono), a single stylesheet, and `<script type="module">`.
- No bundler, no framework, no TypeScript step. Plain ES2022 modules.
- Third-party via CDN: **uPlot** (~40 KB min) for line charts and sparklines. **Motion One** (~13 KB) only if CSS + `requestAnimationFrame` isn't enough. The burn-rate gauge and solari-board roll are hand-written SVG + ES modules.

### Header

```
┌──────────────────────────────────────────────────────────────────────────┐
│  tokenol ― live    [burn 1m 5m 15m 60m]    2026-04-20 13:35 BST  ●  ≡   │
└──────────────────────────────────────────────────────────────────────────┘
```

- Title on the left (Instrument Serif italic).
- Burn-lookback radio toggle (only lights one at a time — 5 m by default).
- Wall clock (updates every second, local timezone).
- Connection dot: pulse when SSE is connected, solid alarm-red on disconnect.
- `≡` button opens the settings drawer (right-side sliding panel).

### Main layout — asymmetric 12-column grid (1280 px baseline)

```
┌─────────────────────────────────────────────────────────────┐
│  HEADER (see above)                                         │
├─────────────────────────────┬───────────────────────────────┤
│                             │  TODAY                        │
│   BURN GAUGE                │  $30.08     443 turns         │
│   $12.50 / hr (last 5m)     │  $/kW $0.107   Hit% 95.0%     │
│   needle + projection arc   │                               │
│                             │  ▌▌▌▌▌█▄▃ Today by hour       │
│   PROJECTED $62.70          │                               │
│   over $50 reference  ⚠     ├───────────────────────────────┤
│                             │  LAST 14 DAYS        [7 14 30 │
│   WINDOW 12:00–17:00        │  $3,580 total         90]     │
│   37 m in / 4 h 23 m left   │  ▁▂▃▄▅▆▇█▇▆▅▄  (uPlot spark) │
│                             │  $/kW drift:  ─╱╲─╱▔─         │
│   BURN HISTORY 60m          │  best 04-08 $61 · worst 04-16 │
│   ────────╱▔╲──────         │                        $786   │
├─────────────────────────────┼───────────────────────────────┤
│  TOP SESSIONS     [24h 7d   │  MODELS           [24h 7d 14d │
│                    14d]     │                    all]       │
│  sonnet-4.6 41adebc8 $28.49 │  opus-4.7    ████████  54%    │
│  opus-4.6   dfd49f2a $7.94  │  sonnet-4.6  ████      28%    │
│  opus-4.7   5b158033 $3.25  │  opus-4.6    ██        12%    │
│  …                          │  haiku-4.5   ▏         2%     │
├─────────────────────────────┼───────────────────────────────┤
│  TOP PROJECTS     [24h 7d   │  COST HEATMAP (hour × day)    │
│                    14d]     │  last 14d × 24h grid, amber   │
│  claude_rate_limit  $38.26  │  intensity = $ per hour       │
│  StratSense         $12.40  │                               │
│  …                          │                               │
├─────────────────────────────┴───────────────────────────────┤
│  LIVE FEED — last 20 turns (newest first)                   │
│  12:54:11  sonnet-4.6  41adebc8  +$0.0234   (sidechain)     │
│  12:54:09  sonnet-4.6  41adebc8  +$0.0198                   │
│  …                                                          │
└─────────────────────────────────────────────────────────────┘
```

Each panel with a range toggle remembers its last choice in `localStorage`. Session rows are clickable → `/session/<id>` drill-down.

### Drill-down page (`/session/<id>`)

One page, three sections:

1. **Header strip**: session id, cwd, model, verdict pill, totals ($28.49 · 441 turns · 2 tool errors).
2. **Turn-by-turn line chart** (uPlot, ~400 px tall): three stacked series — input+cache_creation (amber), cache_read (amber-dim), output (cool-blue). X axis is time. Clicking a point highlights the corresponding row below.
3. **Tool-use timeline**: compact swim-lane showing tool calls over time, with red ticks where `is_error` is true. Hovering a tick shows the tool name + which turn it belongs to.
4. **Turn table**: paged (100 per page), sortable by ts / cost / max_input / tool_errors.

Uses `GET /api/session/<id>`. Not live — a single fetch on load + a manual refresh button. Going live on the drill-down would cost too much bandwidth on active sessions for marginal benefit.

### Metrics per panel

| Panel | Metrics shown |
|---|---|
| **Burn gauge (hero)** | Burn rate $/hr at selected lookback, projected window cost, elapsed/remaining in window, over-reference flag |
| **Burn history 60 m** | $/hr line for the last 60 min |
| **Today** | Total cost, turns, output tokens, cache hit %, $/kW, hourly bars (today) |
| **Last N days** | Daily cost sparkline, total, best & worst day, $/kW drift line, range selector (7/14/30/90 d) |
| **Top sessions** | id, model, cost, turns, max-input, verdict, cwd (tooltip), range selector (24 h / 7 d / 14 d), click → drill-down |
| **Top projects** | cwd, cost, sessions, cache reuse %, range selector (24 h / 7 d / 14 d) |
| **Models** | model, cost share %, tokens, tool-error rate (tooltip), range selector (24 h / 7 d / 14 d / all) |
| **Cost heatmap** | 14 × 24 grid, amber intensity = hourly spend — reveals "always blow up at 3 am" patterns |
| **Live feed** | ts, session short-id, model, Δcost, tokens, sidechain pill, tool-use chip |

### Settings drawer

Right-side slide-over, opened by the `≡` button. All settings persisted in `localStorage` under the key `tokenol:prefs:v1`. A "Reset to defaults" button at the bottom.

| Setting | Default | Options |
|---|---|---|
| Reference threshold | `$50` | `$10 / $25 / $50 / $100 / custom` |
| Burn lookback | `5 m` | `1 m / 5 m / 15 m / 60 m` |
| Daily range | `14 d` | `7 d / 14 d / 30 d / 90 d` |
| Sessions / projects / models range | `24 h` | `24 h / 7 d / 14 d` (models also `all`) |
| Tick interval | `5 s` | `2 s / 5 s / 10 s / 30 s` |
| Idle back-off | `on` | `on / off` |
| Hide sidechain in feed | `off` | `on / off` (excludes subagent turns from live feed only) |
| Reduce motion | follows OS | `auto / on / off` |

Alarm sound / desktop notifications deferred to Phase 4.

### Visual direction

**Tone:** refined mission-control / editorial terminal. Dense, purposeful, unfussy. Every pixel has a job. No rounded cards. No drop shadows. Sharp 1 px dividers and generous negative space.

**Colours** (CSS variables):

- `--bg: #0e0c0a`          warm near-black
- `--fg: #f5f0e6`          warm off-white (body text)
- `--mute: #7a756b`        warm gray (labels, metadata)
- `--rule: #2a261f`        dividers
- `--amber: #ffb647`       active data, live numbers
- `--amber-dim: #8a6730`   past / inactive data
- `--alarm: #f04438`       over-reference, runaway
- `--cool: #6faed8`        projected / future values

No gradients. No pure white. No purple.

**Typography**:

- Display / section headings: **Instrument Serif**, italic, 24–40 px. Used only on section titles and the window-time range — gives the dashboard its editorial voice.
- Data / numbers: **JetBrains Mono**, weights 300 and 600. Tabular numerics (`font-variant-numeric: tabular-nums`) so solari rolls don't jitter.
- Labels (UPPERCASE, tracked 0.08 em): JetBrains Mono 300. Uppercase via CSS.

**Motion**:

- **Solari-board digit roll** when any live number changes: 400 ms, ~5 glyph "spins" per digit before landing. Custom `<solari-number>` element. Disabled under `prefers-reduced-motion: reduce`.
- Burn gauge: needle eases 300 ms ease-out to the new value; projection arc animates in lockstep.
- New turn in live feed: fade-in + 4 px downward slide + brief amber underline flash, 600 ms.
- Connection dot: 1.5 s pulse when connected; solid alarm on disconnect.
- Heatmap cells: cross-fade on value change, 250 ms.

**Background / atmosphere**:

- Subtle SVG `feTurbulence` noise at 3 % opacity over the page.
- No vignettes, no decorative patterns.

**Hero gauge details**:

- Semicircular SVG arc, 180°, with tick marks every 10° (`$10/hr`, `$20/hr`, …, `$100/hr`).
- Amber fill sweeps left → right as rate climbs; turns alarm-red once past the over-reference zone.
- Background arc in `--rule`.
- Needle: 2 px amber line with a 6 px filled circle at the tip.
- A cool-blue dashed arc marks the projection at window end.

### Browser compatibility

Chrome / Firefox / Safari current + 1 prior major. No IE, no polyfills.

## Task breakdown for Sonnet

Each step is independently testable. Sonnet should run tests + a manual `tokenol serve` check after each.

### Task 1 — Backend scaffolding (`serve/app.py`, `serve/state.py`)

- Add `fastapi`, `uvicorn[standard]`, `watchfiles` to `[project.optional-dependencies.serve]`.
- `serve/app.py` — FastAPI app factory + the five routes (`/`, `/session/<id>`, `/api/snapshot`, `/api/session/<id>`, `/api/stream`).
- `serve/state.py` — `(path, size, mtime_ns)`-keyed parse cache; `build_snapshot(all_projects, reference_usd) → dict` that reuses `ingest/` + `metrics/`.
- Pre-compute the `heatmap_14d` grid (14 × 24 USD cells) and the per-hour today series once per tick.
- Unit tests: cache hit/miss, snapshot shape stability vs a fixture, empty-state snapshot, heatmap cell totals match `rollup_by_date`.

### Task 2 — SSE streaming (`serve/streaming.py`)

- Async generator used by `/api/stream`.
- Each tick: rebuild snapshot, shallow-diff against the previous emitted payload, send only changed top-level keys as one SSE `data:` line.
- Idle back-off: after 30 s with no changes, multiply the tick by 3 (capped at 15 s). Reset on the next change.
- Tick is taken from a server-level mutable config mutated by a `POST /api/prefs` endpoint (Task 3) so settings changes in the UI take effect without a reconnect.
- Integration test with `httpx.AsyncClient` covering: first connect (full snapshot), subsequent connects (diff only), idle back-off, reconnect on disconnect.

### Task 3 — CLI subcommand `tokenol serve`

- New `@app.command()` in `cli.py`: wires up `--port`, `--tick`, `--reference`, `--open`, `--all-projects`.
- `uvicorn.run(..., reload=False)`. Log bind URL to stderr.
- `POST /api/prefs` (JSON body: `{tick_seconds?, reference_usd?}`) for runtime overrides from the UI.
- Integration test: start the server in a thread, hit `/api/snapshot`, assert 200 + JSON shape.

### Task 4 — Static shell (`serve/static/index.html`, `styles.css`)

- HTML skeleton matching the layout sketch, Google Fonts, CSS variables.
- Zero JS behaviour yet — one `fetch('/api/snapshot')` on load renders static values.
- Visual pass against the sketch. Screenshot review before proceeding.

### Task 5 — Live updates (`serve/static/app.js`)

- Open `EventSource('/api/stream')`.
- Deep-merge incoming diffs into local state; re-render only changed DOM nodes.
- Connection dot: `.pulse` when connected, `.alarm` on error.
- Range selectors (per panel) slice the already-received server arrays client-side.

### Task 6 — `<solari-number>` component (`serve/static/solari.js`)

- Custom element: watches `value` attribute, animates each digit through ~5 intermediates before landing.
- Accepts `format` attr (e.g. `$%.2f`, `%d`, `%.1f%%`).
- Tabular-numerics so layout doesn't jitter.
- Honours `prefers-reduced-motion`.

### Task 7 — Burn-rate gauge (`serve/static/gauge.js`)

- `<burn-gauge>` custom element. 180° SVG arc with ticks, needle, projection arc, amber→alarm zone.
- Attrs: `rate`, `projected`, `reference`, `max-rate`.
- Colours from CSS variables (inheritable). No hex in JS.

### Task 8 — Charts and small components

- **Daily sparkline** (uPlot, ~15 lines): last N days cost.
- **$/kW drift line** (uPlot): same X range as daily.
- **Today hourly bars**: plain CSS flex widths, amber intensity per hour.
- **Models share bar**: plain CSS flex, hue per model via stable hash → HSL.
- **Cost heatmap**: 14 × 24 CSS grid, background-color from amber-to-dim interpolation. Hover = tooltip with exact cost.
- **Burn history 60m line** (uPlot): cost/hr over the last 60 min, with a dashed reference line at the current reference threshold.
- **`<live-feed>`** component: last-20 list with the fade-in-from-bottom animation on new rows; a "hide sidechain" class toggled from settings.

### Task 9 — Session drill-down page (`serve/static/session.html`, `session.js`)

- Route `GET /session/<id>` renders `session.html`.
- On load, fetch `/api/session/<id>`.
- **Turn chart**: uPlot with three series (input+creation / cache_read / output) vs time.
- **Tool-use timeline**: simple SVG swim-lane; hover tooltip.
- **Turn table**: paged (100/page), sortable by ts / cost / max_input / tool_errors. 400 turns × 8 cols is the stress-test target.
- Back link to the main dashboard in the header.

### Task 10 — Settings drawer (`serve/static/settings.js`)

- Right-side slide-over opened by `≡`. All settings from the table above, persisted to `localStorage:tokenol:prefs:v1`.
- Changing "Tick interval" or "Reference" issues a `POST /api/prefs`; other settings are client-only.
- "Reset to defaults" button. Visible keyboard shortcut hint: `?` opens the drawer.

### Task 11 — Polish pass

- Reduced-motion support throughout.
- Dead-feed detection: if no updates for > 30 s, fade the live-feed section and show "IDLE — no activity".
- 5-minute-stale detection on Today's numbers (soft-fade, still correct).
- Title bar text reflects state: `tokenol — $12.50/hr`, `tokenol — ⚠ over $50`, `tokenol — IDLE`.
- One-colour favicon: the amber arc of the gauge.
- Keyboard shortcuts: `g` jump to gauge, `l` focus live feed, `s` open settings, `/` focus sessions list.

### Task 12 — Docs

- README: add a "Live dashboard" section with a screenshot and `pipx install 'tokenol[serve]'`.
- `docs/DASHBOARD.md`: what each panel means, settings explained, links back to `docs/METRICS.md` for the underlying formulas.

## Acceptance criteria

1. `tokenol serve` binds in < 500 ms on a cold start with 1000 JSONL files.
2. First paint happens within 200 ms of page load in Chrome DevTools on localhost.
3. During active Claude Code use, the burn rate updates at most 5 s after a new assistant event is appended to disk (at default tick).
4. Idle back-off engages after 30 s of no changes and reverts on the next change.
5. Settings changes (tick, reference) take effect within one tick without requiring a page reload.
6. Session drill-down renders 400 turns in < 300 ms after `/api/session/<id>` responds.
7. Backend memory stays under 150 MB with 1000 cached JSONL files parsed.
8. All existing `tokenol` CLI tests still pass; new modules have ≥ 80 % line coverage.
9. Lighthouse "Performance" score ≥ 95 on the static shell.
10. The dashboard is visually distinct enough that a reviewer describes it without the words "dashboard", "card", or "modern". Good descriptors: "terminal", "editorial", "instrument panel", "ticker".

## Estimated size

~1800 LOC backend + ~1200 LOC frontend + ~400 LOC tests. Ordered task list above; each step self-contained.
