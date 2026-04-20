# tokenol dashboard

A local, always-on browser dashboard that mirrors `tokenol live` / `tokenol daily` and updates in real time as Claude Code writes new events to disk.

```
tokenol serve [--port 8787] [--tick 5s] [--reference 50] [--all-projects] [--open]
```

---

## Panels

### Burn gauge

The hero panel. A 180° arc shows the current burn rate as a fraction of the configurable maximum.

| Element | Meaning |
|---|---|
| Amber fill | Burn rate at the selected lookback window (1 m / 5 m / 15 m / 60 m) |
| Cool-blue dashed arc | Projected rate at window end |
| Red zone | Past the reference threshold |
| Needle | Current rate |
| **Projected** number | Estimated total cost if the current rate holds for the rest of the 5-hour window |
| **⚠ over limit** badge | Projected cost already exceeds the reference threshold |
| Window line | Elapsed and remaining time in the current 5-hour window |

The **burn history** sparkline below the gauge shows $/hr over the last 60 minutes with a dashed reference line.

Lookback toggle in the header (`1m / 5m / 15m / 60m`) controls which burn-rate field the gauge and title bar display.

### Today

Totals for the current UTC day: cost, turn count, output tokens, cache hit %, and $/kW (cost per 1,000 output tokens). The bar chart shows hourly cost — the current hour is highlighted in amber.

If no SSE update arrives for 5 minutes, the panel fades to indicate stale data. The numbers are still correct as of the last update.

See [METRICS.md](METRICS.md) for `$/kW` and `Hit%` definitions.

### Last N days

Daily cost sparkline and $/kW drift line over a selectable window (7 / 14 / 30 / 90 days). Shows total spend, best day, and worst day for the selected range.

### Top sessions

The most expensive sessions in the selected range (24 h / 7 d / 14 d), sorted by cost. Shows session ID, model, cost, turn count, max input context, and verdict pill. Click any row to open the **session drill-down page**.

Verdict pills:

| Pill | Trigger |
|---|---|
| `runaway` | Any 5-hour window ≥ $50 |
| `ctx-creep` | Max input ≥ 500 k tokens and growing ≥ 2 k/turn |
| `tool-errs` | ≥ 10 tool uses with > 30% error rate |
| `sidechain` | Sidechain session costing > $5 |
| `ok` | None of the above |

### Models

Cost share by model for the selected range, shown as proportional bars. Each model gets a stable colour derived from its name.

### Top projects

Per-project rollup (by `cwd`) for the selected range: cost, session count, cache reuse %. Hover the project name to see the full path.

### Cost heatmap

A 14 × 24 grid — one row per day, one column per UTC hour. Amber intensity encodes hourly spend. Hover a cell to see the exact cost. Useful for spotting "always blows up at 3 am" patterns (long-running overnight agents).

### Live feed

The 20 most recent assistant turns across all sessions, newest first. Each row shows UTC time, model, session ID (short), turn cost, output token count, and chips for sidechain (`sc`) and tool-use count.

New rows animate in. If no turns arrive for 30 seconds the panel fades and shows **IDLE — no activity**; it restores immediately on the next event.

The **hide sidechain** setting (in the drawer) filters subagent turns from this panel only — totals elsewhere are unaffected.

---

## Session drill-down

Open by clicking any row in Top sessions, or by navigating to `/session/<id>`.

| Section | Content |
|---|---|
| Header strip | Session ID, verdict, time range, model, cwd |
| Totals | Total cost, turns, tool uses, tool errors |
| Turn chart | uPlot line chart: input+creation tokens (amber), cache read (dim), output (blue) over time. Click a point to highlight the corresponding table row. |
| Tool-use timeline | SVG swim-lane; amber ticks = tool calls, red ticks = errors. Click a tick to highlight the row. |
| Turn table | All turns, paged at 100. Sortable by time, cost, input, tool count, error count. |

The drill-down is a single fetch — it is not live. Use the browser refresh button to update.

---

## Settings drawer

Open with the `≡` button, the `s` key, or `?`. All settings persist in `localStorage` under `tokenol:prefs:v1`.

| Setting | Default | Notes |
|---|---|---|
| Reference threshold | $50 | Also POSTed to the server; affects the gauge alarm zone and `⚠` badge |
| Burn lookback | 5 m | Controls the gauge needle and title bar rate |
| Daily range | 14 d | Slice of `daily_90d` shown in the Last N days panel |
| Sessions / Projects / Models range | 24 h | Combined range for all three panels |
| Tick interval | 5 s | Also POSTed to the server; takes effect within one tick |
| Idle back-off | on | Server-side: after 30 s silence the tick multiplies by 3 (min 15 s), resuming on the next change |
| Hide sidechain in feed | off | Filters subagent turns from the live feed only |
| Reduce motion | auto | `auto` follows `prefers-reduced-motion`; `on` forces all animations off |

**Reset to defaults** removes `tokenol:prefs:v1` from `localStorage` and resets tick and reference on the server.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `g` | Scroll to burn gauge |
| `l` | Scroll to live feed |
| `/` | Scroll to top sessions |
| `s` or `?` | Open settings drawer |
| `Esc` | Close settings drawer |

---

## Architecture notes

- **Zero build.** Plain ES2022 modules, no bundler. `uPlot` loaded from CDN.
- **SSE only.** The server pushes a full snapshot on first connect, then shallow diffs (only changed top-level keys) on every tick. The client merges diffs into local state and re-renders only the affected panels.
- **Parse cache.** Keyed by `(path, size, mtime_ns)`. On each tick only the 1–2 files actively being written are re-parsed; unchanged files hit the cache. Safe with thousands of JSONL files.
- **Idle back-off.** After 30 s with no file changes the server multiplies the effective tick by 3 (capped at 15 s). Resumes at the configured tick on the next change.
- **Localhost only.** No authentication, no remote deployment.

For metric definitions (`$/kW`, `Hit%`, `CacheE`, verdicts) see [METRICS.md](METRICS.md).
