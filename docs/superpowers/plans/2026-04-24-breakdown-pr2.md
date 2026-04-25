# Breakdown tab — PR2 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the **Breakdowns section** of `/breakdown` — a 2-wide *Tokens by project* grouped bar chart with cache-health dots and per-bar drill-down, and a 1-wide *Model mix* doughnut with per-slice drill-down. Backed by two new read-only endpoints under `/api/breakdown/*`.

**Architecture:** Two new FastAPI endpoints read the existing in-memory `SnapshotResult.sessions` / `.turns` and apply range filtering in memory, mirroring the pattern already established in PR1 (`api_breakdown_summary`, `api_breakdown_daily_tokens`). No rollup pre-computation is re-used — both endpoints aggregate directly from range-filtered turns, because the cached snapshot's built-in rollups are period-scoped to whatever the last snapshot fetch requested. Frontend appends two Chart.js renderers to the existing `breakdown.js`, reusing `tokenolPalette()`, `configureChartDefaults()`, and the SSE refresh wiring shipped in PR1. A small custom Chart.js plugin draws the cache-health dots beneath project-axis ticks, since Chart.js tick callbacks can only return strings.

**Tech Stack:** FastAPI · pytest / pytest-asyncio · vanilla ES modules · Chart.js 4.4.7 (already loaded in PR1, no version bump) · existing CSS design tokens in `src/tokenol/serve/static/styles.css`.

---

## Scope

**In scope for this PR:**

- `GET /api/breakdown/by-project?range=…` — per-project billable tokens + cache_hit_rate.
- `GET /api/breakdown/by-model?range=…` — per-model billable tokens + share.
- Breakdowns section in `breakdown.html` (section heading + 2:1 grid with two panels).
- `renderByProject(...)` — Chart.js grouped `bar` chart with 8-px cache-health dot per tick, drill-down on click.
- `renderByModel(...)` — Chart.js `doughnut` chart with top-6 + `others` collapse, drill-down on click (except `others`).
- Python unit / integration tests for both endpoints.
- SSE-tick refresh already covers both panels (they're wired into `refreshAll`).

**Out of scope (explicitly deferred):**

- Tool mix chart and any parser changes — that's PR3.
- Wiring the cache-dot thresholds to live prefs (`/api/prefs`). PR2 hard-codes the `DEFAULTS` values (`hit_rate_good_pct = 95`, `hit_rate_red_pct = 85`). A later task can plumb prefs into `breakdown.js`.
- Paginated legend for >6 models. PR2 collapses the tail into an `others` slice so the legend caps at 7 entries.
- Changing the Time section's charts.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/tokenol/serve/app.py` | Modify | Add `/api/breakdown/by-project` and `/api/breakdown/by-model` endpoints after the existing `api_breakdown_daily_tokens` (around line 378). Add `encode_cwd` to the import block from `tokenol.serve.state`. |
| `src/tokenol/serve/static/breakdown.html` | Modify | Append a new `<div class="section-heading">Breakdowns</div>` block and a `.breakdown-grid.breakdown-grid--2-1` with two panels (project chart canvas + model donut canvas) after the existing Time grid (`</div>` at line 90). |
| `src/tokenol/serve/static/breakdown.js` | Modify | Append two fetchers, two renderers, the cache-health dots plugin, and update `refreshAll` to include both new calls. |
| `src/tokenol/serve/static/styles.css` | Modify | Append `.breakdown-grid--2-1` grid-template (2fr 1fr), tighten media-query to stack it at < 900 px. |
| `tests/test_serve_app.py` | Modify | Add four tests: by-project shape + unknown-range rejection, by-model shape + unknown-range rejection. |

**No new source files.** No new dependencies.

---

## Task 1: `/api/breakdown/by-project` endpoint

Aggregates turns by session cwd under a range filter, returning billable tokens (excluding interrupted turns) and `cache_hit_rate` per project. Projects without a cwd are emitted as `"(unknown)"` with `cwd_b64: null` so the frontend can render them but skip the drill-down click.

**Files:**
- Modify: `src/tokenol/serve/app.py` (append endpoint after `api_breakdown_daily_tokens`, line 378)
- Test: `tests/test_serve_app.py`

**JSON response shape:**
```json
{
  "range": "30d",
  "projects": [
    {
      "project": "automl",
      "cwd": "/Users/ff235/dev/automl",
      "cwd_b64": "L1VzZXJzL2ZmMjM1L2Rldi9hdXRvbWw",
      "input": 1200000,
      "output": 2300000,
      "cache_hit_rate": 0.984
    }
  ]
}
```

Sorted by billable tokens (`input + output`) descending. `cache_hit_rate` matches the formula used by `build_project_rollups`: `cache_read / (cache_read + cache_creation + input)`. Returns `None` when denominator is zero (JSON `null`).

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_by_project_returns_project_array(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/by-project?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "projects" in data
    assert len(data["projects"]) >= 1
    p = data["projects"][0]
    for key in ["project", "cwd", "cwd_b64", "input", "output", "cache_hit_rate"]:
        assert key in p, f"Missing field: {key}"
    # Billable-token sort is descending.
    billable = [pp["input"] + pp["output"] for pp in data["projects"]]
    assert billable == sorted(billable, reverse=True)
    # cache_hit_rate is a decimal or null.
    assert p["cache_hit_rate"] is None or 0.0 <= p["cache_hit_rate"] <= 1.0


@pytest.mark.asyncio
async def test_breakdown_by_project_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/by-project?range=14d")

    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -k breakdown_by_project -v
```
Expected: 404 on the positive test, something like `method not allowed` on the negative (both count as failing — endpoint does not exist).

- [ ] **Step 3: Import `encode_cwd` in `app.py`**

In `src/tokenol/serve/app.py`, the import block at lines 19–34 imports several names from `tokenol.serve.state`. Add `encode_cwd` alongside `decode_cwd`:

```python
from tokenol.serve.state import (
    VALID_METRICS,
    ParseCache,
    SnapshotResult,
    build_daily_panel,
    build_day_detail,
    build_hourly_panel,
    build_model_detail,
    build_models_panel,
    build_project_detail,
    build_recent_activity_panel,
    build_search_results,
    build_snapshot_full,
    decode_cwd,
    encode_cwd,
    range_since,
)
```

- [ ] **Step 4: Implement the endpoint**

In `src/tokenol/serve/app.py`, immediately after the `api_breakdown_daily_tokens` endpoint (ends at line 377), add:

```python
    @app.get("/api/breakdown/by-project")
    async def api_breakdown_by_project(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None

        buckets: dict[str, dict[str, int]] = {}
        for s in result.sessions:
            cwd = s.cwd or "(unknown)"
            for t in s.turns:
                if since is not None and t.timestamp.date() < since:
                    continue
                if t.is_interrupted:
                    continue
                b = buckets.setdefault(cwd, {
                    "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                })
                b["input"] += t.usage.input_tokens
                b["output"] += t.usage.output_tokens
                b["cache_read"] += t.usage.cache_read_input_tokens
                b["cache_creation"] += t.usage.cache_creation_input_tokens

        projects = []
        for cwd, b in buckets.items():
            denom = b["cache_read"] + b["cache_creation"] + b["input"]
            hit_rate = (b["cache_read"] / denom) if denom > 0 else None
            projects.append({
                "project": Path(cwd).name if cwd != "(unknown)" else "(unknown)",
                "cwd": cwd,
                "cwd_b64": encode_cwd(cwd) if cwd != "(unknown)" else None,
                "input": b["input"],
                "output": b["output"],
                "cache_hit_rate": hit_rate,
            })
        projects.sort(key=lambda p: p["input"] + p["output"], reverse=True)
        return JSONResponse({"range": range, "projects": projects})
```

Note: `Path` is already imported at the top (line 8). No new imports beyond the `encode_cwd` added in Step 3.

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -k breakdown_by_project -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat: add /api/breakdown/by-project endpoint

Per-project billable tokens and cache_hit_rate under a range filter.
Excludes interrupted turns. Emits '(unknown)' with null cwd_b64 for
sessions without a cwd so the frontend can skip the drill-down click."
```

---

## Task 2: `/api/breakdown/by-model` endpoint

Billable-token share per model, with interrupted turns excluded. Cache tokens are deliberately omitted from the share computation so the chart matches the *Billable tokens* scorecard math established in PR1.

**Files:**
- Modify: `src/tokenol/serve/app.py` (append after Task 1's endpoint)
- Test: `tests/test_serve_app.py`

**JSON response shape:**
```json
{
  "range": "30d",
  "models": [
    {
      "model": "claude-sonnet-4-6",
      "input": 6500000,
      "output": 14000000,
      "share": 0.8304
    },
    {
      "model": "claude-opus-4-7",
      "input": 1200000,
      "output": 3000000,
      "share": 0.1696
    }
  ]
}
```

`share = (input + output) / sum_of_(input+output)_across_all_models`. Sorted by billable tokens descending. Unknown/absent model names bucket to `"(unknown)"`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_by_model_returns_model_array(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/by-model?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "models" in data
    assert len(data["models"]) >= 1
    m = data["models"][0]
    for key in ["model", "input", "output", "share"]:
        assert key in m, f"Missing field: {key}"
    # Shares sum to ~1 (floating point tolerance).
    total = sum(mm["share"] for mm in data["models"])
    assert abs(total - 1.0) < 1e-6
    # Sort desc by billable tokens.
    billable = [mm["input"] + mm["output"] for mm in data["models"]]
    assert billable == sorted(billable, reverse=True)


@pytest.mark.asyncio
async def test_breakdown_by_model_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/by-model?range=14d")

    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -k breakdown_by_model -v
```
Expected: 404 (positive test) / 404 (negative test) — both fail.

- [ ] **Step 3: Implement the endpoint**

In `src/tokenol/serve/app.py`, immediately after `api_breakdown_by_project`, add:

```python
    @app.get("/api/breakdown/by-model")
    async def api_breakdown_by_model(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None

        buckets: dict[str, dict[str, int]] = {}
        for t in result.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            name = t.model or "(unknown)"
            b = buckets.setdefault(name, {"input": 0, "output": 0})
            b["input"] += t.usage.input_tokens
            b["output"] += t.usage.output_tokens

        total_billable = sum(b["input"] + b["output"] for b in buckets.values()) or 1
        models = []
        for name, b in buckets.items():
            billable = b["input"] + b["output"]
            models.append({
                "model": name,
                "input": b["input"],
                "output": b["output"],
                "share": billable / total_billable,
            })
        models.sort(key=lambda m: m["input"] + m["output"], reverse=True)
        return JSONResponse({"range": range, "models": models})
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -k breakdown_by_model -v
```
Expected: 2 passed.

- [ ] **Step 5: Sanity-check the full test file**

Run:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -v
```
Expected: all previous tests still pass; 4 new PR2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat: add /api/breakdown/by-model endpoint

Per-model billable tokens (input + output) and share. Cache tokens
excluded from share math to match the Billable tokens scorecard."
```

---

## Task 3: Breakdowns section HTML + 2:1 grid CSS

Extends the page shell with the section group that hosts the two new charts, and adds a CSS modifier class for its 2:1 column layout.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html`
- Modify: `src/tokenol/serve/static/styles.css`

- [ ] **Step 1: Append Breakdowns section HTML**

In `src/tokenol/serve/static/breakdown.html`, immediately after the closing `</div>` of the Time grid (line 90, `</div>` closing `.breakdown-grid.breakdown-grid--2`), insert:

```html
  <div class="section-heading breakdown-section-heading">
    <h2>Breakdowns</h2>
  </div>
  <div class="breakdown-grid breakdown-grid--2-1">
    <section class="breakdown-panel" aria-labelledby="bp-by-project-title">
      <div class="breakdown-panel-heading">
        <h3 id="bp-by-project-title">Tokens by project</h3>
        <span class="chart-subheading" id="bp-by-project-sub"></span>
      </div>
      <div class="breakdown-chart"><canvas id="chart-by-project" height="260"></canvas></div>
    </section>
    <section class="breakdown-panel" aria-labelledby="bp-by-model-title">
      <div class="breakdown-panel-heading">
        <h3 id="bp-by-model-title">Model mix</h3>
        <span class="chart-subheading" id="bp-by-model-sub"></span>
      </div>
      <div class="breakdown-chart"><canvas id="chart-by-model" height="260"></canvas></div>
    </section>
  </div>
```

Place it before the closing `</div>` of `<div class="app">`.

- [ ] **Step 2: Append grid modifier CSS**

Append to `src/tokenol/serve/static/styles.css` (after the existing `.breakdown-grid--2 { grid-template-columns: 1fr 1fr; }` rule — find it via `grep -n 'breakdown-grid--2 ' src/tokenol/serve/static/styles.css`):

```css
.breakdown-grid--2-1 { grid-template-columns: 2fr 1fr; }
```

Then extend the existing responsive block (search for `@media (max-width: 900px)` in the file) by adding a line that stacks the new grid at narrow widths. Locate the existing rule:

```css
@media (max-width: 900px) {
  .scorecard { grid-template-columns: repeat(2, 1fr); }
  .breakdown-grid--2 { grid-template-columns: 1fr; }
}
```

and change it to:

```css
@media (max-width: 900px) {
  .scorecard { grid-template-columns: repeat(2, 1fr); }
  .breakdown-grid--2 { grid-template-columns: 1fr; }
  .breakdown-grid--2-1 { grid-template-columns: 1fr; }
}
```

- [ ] **Step 3: Visual sanity (no JS yet)**

Start the dev server and reload `/breakdown`:

```bash
.venv/bin/tokenol serve
```

Expected: the page now shows a **Breakdowns** section heading and two empty panels below the Time section — left panel wider than right panel at ≥ 900 px viewport, stacked at < 900 px. No JS errors in the console (the canvases stay empty because the renderers arrive in Tasks 4–5).

Also:
```bash
.venv/bin/python -m pytest tests/test_serve_app.py -k breakdown_route -v
```
Expected: the HTML route test still passes.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.html src/tokenol/serve/static/styles.css
git commit -m "feat: Breakdowns section shell with 2:1 grid

Two empty panels (Tokens by project, Model mix) ready for Chart.js
renderers. New .breakdown-grid--2-1 modifier keyed to 2fr 1fr,
stacks 1 column below 900 px."
```

---

## Task 4: Tokens-by-project grouped bars + cache-health dots plugin

Renders the project chart as a grouped `bar` (input + output per project) with a small colored dot drawn beneath each tick indicating cache health. Clicking a bar drills to `/project/{cwd_b64}` when available.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add cache-health threshold constants and color resolver**

Append to `breakdown.js`, just below the existing `tokenolPalette()` function (around line 101):

```javascript
// ---------------------------------------------------------------------------
// Cache-health thresholds (hard-coded from metrics/thresholds.DEFAULTS).
// PR2 ships these inline; a later task can fetch /api/prefs for live values.
// ---------------------------------------------------------------------------

const HIT_PCT_GREEN = 95.0;
const HIT_PCT_RED = 85.0;

function healthColorForHitRate(rate) {
  // `rate` is a decimal in [0, 1] or null/undefined.
  if (rate == null) return cssVar('--mute');
  const pct = rate * 100;
  if (pct >= HIT_PCT_GREEN) return cssVar('--green');
  if (pct >= HIT_PCT_RED) return cssVar('--amber');
  return cssVar('--alarm');
}
```

- [ ] **Step 2: Add the cache-health dots Chart.js plugin**

Append to `breakdown.js`, below the cache-health section:

```javascript
// ---------------------------------------------------------------------------
// Cache-health dots plugin for Chart.js.
//
// Chart.js tick callbacks can only return strings, so we can't inject a dot
// into the tick label. Instead we register a per-chart plugin that draws an
// 8 px colored circle aligned to each x-tick, just below the axis baseline.
//
// Usage: register via `plugins: [cacheHealthDotsPlugin]` and pass
// `options.plugins.cacheHealthDots.colors = [...]` aligned to the chart's
// x-axis tick order.
// ---------------------------------------------------------------------------

const cacheHealthDotsPlugin = {
  id: 'cacheHealthDots',
  afterDatasetsDraw(chart) {
    const opts = chart.options.plugins && chart.options.plugins.cacheHealthDots;
    if (!opts || !Array.isArray(opts.colors)) return;
    const xScale = chart.scales.x;
    if (!xScale) return;
    const ctx = chart.ctx;
    // Rotated 45° labels sit between xScale.bottom and roughly xScale.bottom+30.
    // Dots are drawn in a dedicated band below that. Keep in sync with
    // `layout.padding.bottom` on the chart options.
    const y = xScale.bottom + 38;
    ctx.save();
    for (let i = 0; i < xScale.ticks.length; i++) {
      const color = opts.colors[i];
      if (!color) continue;
      const x = xScale.getPixelForTick(i);
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    }
    ctx.restore();
  },
};
```

- [ ] **Step 3: Add the by-project fetcher and renderer**

Append to `breakdown.js`, below the plugin:

```javascript
// ---------------------------------------------------------------------------
// Breakdowns-section charts
// ---------------------------------------------------------------------------

async function fetchByProject(range) {
  const resp = await fetch(`/api/breakdown/by-project?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`by-project ${resp.status}`);
  return resp.json();
}

const BY_PROJECT_TOP_N = 10;

let _chartByProject = null;
let _byProjectCwdB64 = [];   // parallel to chart's x-axis order, for click-drill

function renderByProject(data) {
  const pal = tokenolPalette();
  // Cap to a readable number of bars; tail is dropped (not collapsed) so the
  // chart stays legible. Subheading notes how many were shown vs. total.
  const projects = data.projects.slice(0, BY_PROJECT_TOP_N);
  const labels = projects.map(p => p.project);
  const dotColors = projects.map(p => healthColorForHitRate(p.cache_hit_rate));
  _byProjectCwdB64 = projects.map(p => p.cwd_b64);

  const datasets = [
    { label: 'input',  data: projects.map(p => p.input),  backgroundColor: pal[0] },
    { label: 'output', data: projects.map(p => p.output), backgroundColor: pal[1] },
  ];

  const shownTotal = projects.reduce((s, p) => s + p.input + p.output, 0);
  const allTotal = data.projects.reduce((s, p) => s + p.input + p.output, 0);
  const subEl = document.getElementById('bp-by-project-sub');
  if (data.projects.length > BY_PROJECT_TOP_N) {
    const pct = Math.round((shownTotal / Math.max(allTotal, 1)) * 100);
    subEl.textContent = `top ${BY_PROJECT_TOP_N} of ${data.projects.length} · ${pct}% of billable`;
  } else {
    subEl.textContent = `${data.projects.length} project${data.projects.length === 1 ? '' : 's'}`;
  }

  const canvas = document.getElementById('chart-by-project');

  if (_chartByProject) {
    _chartByProject.data.labels = labels;
    for (let i = 0; i < datasets.length; i++) {
      _chartByProject.data.datasets[i].data = datasets[i].data;
      _chartByProject.data.datasets[i].backgroundColor = datasets[i].backgroundColor;
    }
    _chartByProject.options.plugins.cacheHealthDots.colors = dotColors;
    _chartByProject.update('none');
    return;
  }

  _chartByProject = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    plugins: [cacheHealthDotsPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { bottom: 46 } },  // room for rotated labels + dot band
      scales: {
        x: {
          ticks: { maxRotation: 45, minRotation: 45, autoSkip: false, padding: 4 },
        },
        y: { beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
      },
      plugins: {
        legend: { position: 'top', align: 'end' },
        cacheHealthDots: { colors: dotColors },
      },
      onClick: (_evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const b64 = _byProjectCwdB64[idx];
        if (b64) window.location.href = `/project/${b64}`;
      },
    },
  });
}
```

- [ ] **Step 4: Wire into `refreshAll`**

Find the existing `refreshAll` (currently around line 258 in `breakdown.js`). Modify its body to fetch and render the project chart alongside the Time-section charts:

Before:
```javascript
async function refreshAll() {
  const range = getPeriod();
  try {
    await whenChartReady();
    configureChartDefaults();
    const [summary, daily] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
  } catch (err) {
    console.error('[breakdown] refresh failed', err);
  }
}
```

After:
```javascript
async function refreshAll() {
  const range = getPeriod();
  try {
    await whenChartReady();
    configureChartDefaults();
    const [summary, daily, byProject] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
      fetchByProject(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
    renderByProject(byProject);
  } catch (err) {
    console.error('[breakdown] refresh failed', err);
  }
}
```

- [ ] **Step 5: Visual sanity**

Reload `/breakdown` with the dev server running:

- Grouped bar chart renders with two series (input amber, output alarm-red) per project.
- Below each project label, a small colored dot indicates cache health (green / amber / red / grey-mute).
- Subheading shows either `N projects` or `top 10 of N · X% of billable`.
- Clicking any bar whose project has a cwd drills to `/project/{cwd_b64}` (verify by clicking one — should load the project page).
- Switching period pills re-renders with the new range.
- SSE tick: append a turn to a fixture and wait — the chart should update in place without flicker.

Open DevTools Console: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Tokens-by-project grouped bars with cache-health dots

Grouped input/output per project, top 10 by billable. Custom Chart.js
plugin draws an 8 px colored dot below each x-tick indicating cache
hit-rate health vs. the 95/85 thresholds. Click-drill to /project."
```

---

## Task 5: Model mix doughnut chart

Renders the model-share doughnut from `/api/breakdown/by-model`, collapsing the tail into an `others` slice when there are more than six models so the legend stays readable. Clicks drill to `/model/{name}` for named slices.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add the by-model fetcher and renderer**

Append to `breakdown.js`, below `renderByProject`:

```javascript
async function fetchByModel(range) {
  const resp = await fetch(`/api/breakdown/by-model?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`by-model ${resp.status}`);
  return resp.json();
}

const BY_MODEL_TOP_N = 6;

let _chartByModel = null;
let _byModelNames = [];  // parallel to chart labels; 'others' entry is null

function collapseModels(models) {
  // Keep top N−1, collapse the tail into 'others' only if it would exceed N.
  if (models.length <= BY_MODEL_TOP_N) {
    return models.map(m => ({ name: m.model, value: m.input + m.output, isOthers: false }));
  }
  const head = models.slice(0, BY_MODEL_TOP_N - 1).map(m => ({
    name: m.model, value: m.input + m.output, isOthers: false,
  }));
  const tailValue = models.slice(BY_MODEL_TOP_N - 1).reduce((s, m) => s + m.input + m.output, 0);
  head.push({ name: 'others', value: tailValue, isOthers: true });
  return head;
}

function renderByModel(data) {
  const pal = tokenolPalette();
  const collapsed = collapseModels(data.models);
  const labels = collapsed.map(c => c.name);
  const values = collapsed.map(c => c.value);
  const colors = collapsed.map((_, i) => pal[i % pal.length]);
  _byModelNames = collapsed.map(c => (c.isOthers ? null : c.name));

  document.getElementById('bp-by-model-sub').textContent =
    `${data.models.length} model${data.models.length === 1 ? '' : 's'}`;

  const canvas = document.getElementById('chart-by-model');

  if (_chartByModel) {
    _chartByModel.data.labels = labels;
    _chartByModel.data.datasets[0].data = values;
    _chartByModel.data.datasets[0].backgroundColor = colors;
    _chartByModel.update('none');
    return;
  }

  _chartByModel = new window.Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: colors, borderWidth: 1, borderColor: cssVar('--bg-raised') }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '60%',
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label(ctx) {
              const total = ctx.dataset.data.reduce((s, v) => s + v, 0) || 1;
              const v = ctx.parsed;
              const pct = ((v / total) * 100).toFixed(1);
              return `${ctx.label}: ${fmtTok(v)} billable (${pct}%)`;
            },
          },
        },
      },
      onClick: (_evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const name = _byModelNames[idx];
        if (name) window.location.href = `/model/${encodeURIComponent(name)}`;
      },
    },
  });
}
```

- [ ] **Step 2: Wire into `refreshAll`**

Extend `refreshAll` to fetch and render the model donut. Modify the existing version (after Task 4's changes):

Before:
```javascript
    const [summary, daily, byProject] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
      fetchByProject(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
    renderByProject(byProject);
```

After:
```javascript
    const [summary, daily, byProject, byModel] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
      fetchByProject(range),
      fetchByModel(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
    renderByProject(byProject);
    renderByModel(byModel);
```

- [ ] **Step 3: Visual sanity**

Reload `/breakdown` with the dev server running:

- Model mix donut renders with 1–6 slices (or 6 including a collapsed `others`).
- Legend sits below the donut.
- Hovering a slice shows `modelname: X.XM billable (YY.Y%)`.
- Clicking a non-`others` slice drills to `/model/{name}`. Clicking `others` does nothing (verify by console — no navigation, no error).
- Switching period pills re-renders.
- SSE tick updates in place.

Open DevTools Console: no errors.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Model mix doughnut chart

Billable-token share per model. Collapses >6 models into top 5 + an
'others' slice so the legend stays readable. Click-drill to /model
for named slices; others slice is non-navigating."
```

---

## Task 6: Manual end-to-end verification

Not code — a scripted walkthrough before opening the PR.

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ -v
```
Expected: all tests pass, including the 4 new PR2 tests. No regressions vs. the PR1 state.

- [ ] **Step 2: Start the dev server**

```bash
.venv/bin/tokenol serve
```

- [ ] **Step 3: Walk the Breakdowns section on `/breakdown`**

- Period pills `7D · 30D · 90D · All` — each re-fetches both new endpoints and re-renders the two charts.
- Project bar chart:
  - Two series visible (input + output, semantic amber/alarm colors).
  - Up to 10 project bars. Subheading reads `N projects` or `top 10 of N · X% of billable`.
  - Each x-tick has a colored dot below it. Projects with hit-rate ≥ 95% → green; 85–94% → amber; < 85% → alarm-red; null → grey-mute.
  - Clicking a bar drills to `/project/{cwd_b64}` — page loads correctly.
  - Clicking a bar whose cwd is `(unknown)` does nothing (no navigation, no error).
- Model donut:
  - Slices sum to 100%.
  - Up to 6 slices, including a collapsed `others` slice if > 6 distinct models existed.
  - Tooltip reads `modelname: X.XM billable (YY.Y%)`.
  - Clicking a named slice drills to `/model/{name}`.
  - Clicking the `others` slice is a no-op.

- [ ] **Step 4: SSE in-place update**

With the dev server running, append a new assistant turn to a fixture file (or wait for a real session tick). Within one SSE tick both charts should update in place — no canvas flicker, no re-layout jank.

- [ ] **Step 5: Edge cases**

- Empty snapshot: point the server at an empty `~/.claude*` temp dir. Both endpoints should return `projects: []` / `models: []` and the charts should render empty (Chart.js handles the "no data" case gracefully — confirm no console errors).
- Single project, single model: both charts still render (1 bar / 1 slice). Subheading shows `1 project` / `1 model`.

- [ ] **Step 6: Tab nav and per-tab state**

- Click `Overview` tab → Overview loads with its original chart set unchanged. Global period pills still visible.
- Click `Breakdown` tab → scorecard and all four charts re-render with the last-used period.
- Reload the page — period pill state still `sessionStorage`-persisted.

- [ ] **Step 7: Responsive sanity**

Narrow viewport below 900 px: Breakdowns grid stacks to a single column (project chart on top, model donut below). No clipping or overflow.

- [ ] **Step 8: No console errors**

DevTools console open for the entire walkthrough — zero errors, zero warnings related to Chart.js, fetches, or the cache-health dots plugin.

---

## Self-review checklist

Run before declaring PR2 ready:

- [ ] `.venv/bin/python -m pytest tests/` is green.
- [ ] Both new endpoints reject unknown `range` values with HTTP 400.
- [ ] No new Python source files; only `app.py` and `test_serve_app.py` were modified on the backend.
- [ ] No hard-coded hex values — cache-health dot colors resolve from `--green` / `--amber` / `--alarm` / `--mute` CSS tokens at render time.
- [ ] `BY_PROJECT_TOP_N = 10` and `BY_MODEL_TOP_N = 6` are declared as named constants, not magic numbers sprinkled in logic.
- [ ] Commit messages follow repo convention (`feat:`), no Co-Authored-By lines.
- [ ] Both charts update in place on SSE tick (no `destroy()` calls in the renderers' update path).

## What PR3 inherits from PR2

- The `/api/breakdown/*` endpoint pattern (in-memory filter over `SnapshotResult.turns` / `.sessions`, `range` validation, wrapped JSON response with `range` echo).
- The SSE-tick-driven `refreshAll` pipeline — PR3 only needs to add `fetchTools` + `renderToolMix` and append to the `Promise.all`.
- `tokenolPalette()` color cycle for the horizontal bars.
- The click-to-drill pattern (though tools probably won't drill anywhere in PR3).
