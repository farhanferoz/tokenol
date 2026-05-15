# Cost Visibility Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface USD cost alongside tokens on the Breakdown page, and let Overview overlay any two metrics on a single chart with dual axes.

**Architecture:** Backend enriches `/api/breakdown/by-project` and `/api/breakdown/by-model` with cost fields — no new endpoints. Frontend gets two distinct features: a per-chart `TOKENS / $` toggle on three Breakdown charts, and a cyclic 2-of-N selector on Overview metric pills that drives a dual-axis renderer in `chart.js`. Per-user selections persist in `localStorage`.

**Tech Stack:** Python 3.12+, FastAPI, pytest, ruff (`uv run pytest` / `uv run ruff check`). Vanilla JavaScript + Chart.js v4 (no bundler, no JS test harness). HTML/CSS hand-rolled in `serve/static/`.

**Spec:** `docs/superpowers/specs/2026-05-15-cost-visibility-design.md`

**Release-gate reminder (from project memory):** `uv run ruff check src tests && uv run pytest` BOTH pass before any push. No AI-attribution trailers in commits.

---

## File Map

**Backend (Python)**
- Modify `src/tokenol/serve/app.py`
  - `_bucket_turns` (lines 62–90) — also accumulate `input_cost`, `output_cost`, `cache_read_cost`, `cache_creation_cost`.
  - `/api/breakdown/by-project` (lines 591–617) — surface `input_cost`, `output_cost` per project.
  - `/api/breakdown/by-model` (lines 619–641) — surface `cost_usd` and `cost_share` per model.
- Modify `tests/test_serve_app.py` — extend `test_breakdown_by_project_returns_project_array` and `test_breakdown_by_model_returns_model_array` with cost assertions and oracle cross-checks.

**Frontend (HTML/CSS/JS)**
- Modify `src/tokenol/serve/static/breakdown.html` — add `[TOKENS] [$]` pill pair markup to three chart cards.
- Modify `src/tokenol/serve/static/breakdown.js` — three `_unit` state vars; pill wiring; field swap on toggle; `localStorage` persistence.
- Modify `src/tokenol/serve/static/index.html` — add legend `<span>` below each Overview metric-pill row.
- Modify `src/tokenol/serve/static/app.js` — new `_wireMetricPills` cyclic helper; pair-shaped `_hMetric` / `_dMetric` state; new `_LS_H_METRIC` / `_LS_D_METRIC` `localStorage` keys; legend render; pass secondary series into painter.
- Modify `src/tokenol/serve/static/chart.js` — line-chart factory gains optional `secondary` series with right axis.
- Modify `src/tokenol/serve/static/styles.css` — add `--series-secondary` CSS variable; `.pill.secondary` selected-secondary state.

---

## Phase A — Backend: cost in Breakdown payloads

### Task 1: Add cost accumulation to `_bucket_turns`

**Files:**
- Modify: `src/tokenol/serve/app.py:62-90`
- Test: `tests/test_serve_app.py` (extend two existing tests in Tasks 2–3; this task itself touches the helper directly and is covered by those endpoint tests)

- [ ] **Step 1: Read the current helper to confirm context**

Run: `sed -n '62,90p' src/tokenol/serve/app.py`
Expected output is the `_bucket_turns` function as shown in the file map.

- [ ] **Step 2: Modify `_bucket_turns` to also accumulate cost**

Replace the function body (lines 62–90 in `src/tokenol/serve/app.py`) with:

```python
def _bucket_turns(
    sessions: list,
    since,
    key_fn,
) -> dict[str, dict[str, float]]:
    """Group non-interrupted turns into buckets and sum the usage + cost fields.

    `key_fn` receives `(session, turn)` and returns the bucket key; handlers
    pass a lambda that closes over whatever grouping dict they precomputed
    (e.g. `cwd_by_sid`). Returns a dict mapping each key to a sub-dict with
    token totals (`input`, `output`, `cache_read`, `cache_creation`) and
    per-component cost totals in USD (`input_cost`, `output_cost`,
    `cache_read_cost`, `cache_creation_cost`). Callers may ignore unused
    fields.
    """
    from tokenol.metrics.cost import cost_for_turn
    buckets: dict[str, dict[str, float]] = {}
    for s in sessions:
        for t in s.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            key = key_fn(s, t)
            b = buckets.setdefault(key, {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                "input_cost": 0.0, "output_cost": 0.0,
                "cache_read_cost": 0.0, "cache_creation_cost": 0.0,
            })
            b["input"] += t.usage.input_tokens
            b["output"] += t.usage.output_tokens
            b["cache_read"] += t.usage.cache_read_input_tokens
            b["cache_creation"] += t.usage.cache_creation_input_tokens
            tc = cost_for_turn(t.model, t.usage)
            b["input_cost"] += tc.input_usd
            b["output_cost"] += tc.output_usd
            b["cache_read_cost"] += tc.cache_read_usd
            b["cache_creation_cost"] += tc.cache_creation_usd
    return buckets
```

- [ ] **Step 3: Run existing breakdown tests to confirm the helper change is backward-compatible**

Run: `uv run pytest tests/test_serve_app.py -k breakdown -v`
Expected: all `breakdown` tests still pass (the new sub-dict keys don't break any caller; the endpoint payloads haven't changed yet — they still ignore the new fields).

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/app.py
git commit -m "feat(breakdown): accumulate per-component cost in _bucket_turns"
```

---

### Task 2: Surface cost in `/api/breakdown/by-project`

**Files:**
- Modify: `src/tokenol/serve/app.py:591-617`
- Test: `tests/test_serve_app.py:837-882` (extend the existing test)

- [ ] **Step 1: Extend the by-project test with cost-field assertions (failing first)**

Replace the body of `test_breakdown_by_project_returns_project_array` in `tests/test_serve_app.py` (starting at line 837) with:

```python
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
    for key in [
        "project", "cwd", "cwd_b64",
        "input", "output", "cache_hit_rate",
        "input_cost", "output_cost",
    ]:
        assert key in p, f"Missing field: {key}"
    # Billable-token sort is descending.
    billable = [pp["input"] + pp["output"] for pp in data["projects"]]
    assert billable == sorted(billable, reverse=True)
    # cache_hit_rate is a decimal or null.
    assert p["cache_hit_rate"] is None or 0.0 <= p["cache_hit_rate"] <= 1.0
    # Costs are non-negative floats.
    assert isinstance(p["input_cost"], (int, float)) and p["input_cost"] >= 0
    assert isinstance(p["output_cost"], (int, float)) and p["output_cost"] >= 0

    # Oracle cross-check: per-project token sums match raw snapshot totals.
    snap = app.state.snapshot_result
    assert snap is not None, "snapshot should be cached after the endpoint call"
    expected_input = sum(
        t.usage.input_tokens
        for s in snap.sessions for t in s.turns
        if not t.is_interrupted
    )
    expected_output = sum(
        t.usage.output_tokens
        for s in snap.sessions for t in s.turns
        if not t.is_interrupted
    )
    assert sum(p["input"] for p in data["projects"]) == expected_input
    assert sum(p["output"] for p in data["projects"]) == expected_output

    # Cost oracle: per-project input_cost + output_cost sums match
    # cost_for_turn() applied to each non-interrupted turn.
    from tokenol.metrics.cost import cost_for_turn
    expected_input_cost = sum(
        cost_for_turn(t.model, t.usage).input_usd
        for s in snap.sessions for t in s.turns
        if not t.is_interrupted
    )
    expected_output_cost = sum(
        cost_for_turn(t.model, t.usage).output_usd
        for s in snap.sessions for t in s.turns
        if not t.is_interrupted
    )
    assert abs(sum(p["input_cost"] for p in data["projects"]) - expected_input_cost) < 1e-9
    assert abs(sum(p["output_cost"] for p in data["projects"]) - expected_output_cost) < 1e-9
```

- [ ] **Step 2: Run the test and verify it FAILS**

Run: `uv run pytest tests/test_serve_app.py::test_breakdown_by_project_returns_project_array -v`
Expected: FAIL with `Missing field: input_cost` (the endpoint doesn't return it yet).

- [ ] **Step 3: Update `/api/breakdown/by-project` to include cost fields**

In `src/tokenol/serve/app.py` find the `api_breakdown_by_project` handler (around line 591). In the `projects.append({...})` block (around lines 604–612), add the two cost fields. The final block reads:

```python
        projects.append({
            "project": Path(cwd).name if cwd != "(unknown)" else "(unknown)",
            "cwd": cwd,
            "cwd_b64": encode_cwd(cwd) if cwd != "(unknown)" else None,
            "input": b["input"],
            "output": b["output"],
            "input_cost": b["input_cost"],
            "output_cost": b["output_cost"],
            "cache_hit_rate": hit_rate,
        })
```

- [ ] **Step 4: Run the test and verify it PASSES**

Run: `uv run pytest tests/test_serve_app.py::test_breakdown_by_project_returns_project_array -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(breakdown): include per-project cost in /api/breakdown/by-project"
```

---

### Task 3: Surface cost in `/api/breakdown/by-model`

**Files:**
- Modify: `src/tokenol/serve/app.py:619-641`
- Test: `tests/test_serve_app.py:896-937` (extend the existing test)

- [ ] **Step 1: Extend the by-model test with cost-field assertions**

Replace the body of `test_breakdown_by_model_returns_model_array` in `tests/test_serve_app.py` (starting at line 896) with:

```python
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
        for key in ["model", "input", "output", "share", "cost_usd", "cost_share"]:
            assert key in m, f"Missing field: {key}"
        # Shares sum to ~1 (floating point tolerance).
        total = sum(mm["share"] for mm in data["models"])
        assert abs(total - 1.0) < 1e-6
        # Cost shares sum to ~1 too, when any model has cost; else they are all 0.
        cost_total = sum(mm["cost_usd"] for mm in data["models"])
        cost_share_total = sum(mm["cost_share"] for mm in data["models"])
        if cost_total > 0:
            assert abs(cost_share_total - 1.0) < 1e-6
        else:
            assert cost_share_total == 0
        # Sort desc by billable tokens.
        billable = [mm["input"] + mm["output"] for mm in data["models"]]
        assert billable == sorted(billable, reverse=True)

        # Oracle cross-check: sums of per-model input/output match raw totals.
        snap = app.state.snapshot_result
        assert snap is not None
        expected_input = sum(
            t.usage.input_tokens for t in snap.turns if not t.is_interrupted
        )
        expected_output = sum(
            t.usage.output_tokens for t in snap.turns if not t.is_interrupted
        )
        assert sum(mm["input"] for mm in data["models"]) == expected_input
        assert sum(mm["output"] for mm in data["models"]) == expected_output

        # Cost oracle: per-model cost_usd sum matches cost_for_turn() applied
        # to each non-interrupted turn.
        from tokenol.metrics.cost import cost_for_turn
        expected_cost = sum(
            cost_for_turn(t.model, t.usage).total_usd
            for t in snap.turns if not t.is_interrupted
        )
        assert abs(cost_total - expected_cost) < 1e-9
```

- [ ] **Step 2: Run the test and verify it FAILS**

Run: `uv run pytest tests/test_serve_app.py::test_breakdown_by_model_returns_model_array -v`
Expected: FAIL with `Missing field: cost_usd`.

- [ ] **Step 3: Update `/api/breakdown/by-model` to include cost fields**

In `src/tokenol/serve/app.py` find the `api_breakdown_by_model` handler (around line 619). Replace the model-bucketing/append loop with one that totals cost across all buckets and computes `cost_share`. The body of the handler becomes:

```python
    @app.get("/api/breakdown/by-model")
    async def api_breakdown_by_model(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = range_since(range, date.today()) if range != "all" else None

        buckets = _bucket_turns(
            result.sessions, since,
            key_fn=lambda _s, t: t.model or "(unknown)",
        )

        total_billable = sum(b["input"] + b["output"] for b in buckets.values()) or 1
        total_cost = sum(
            b["input_cost"] + b["output_cost"]
            + b["cache_read_cost"] + b["cache_creation_cost"]
            for b in buckets.values()
        )
        models = []
        for name, b in buckets.items():
            billable = b["input"] + b["output"]
            cost_usd = (
                b["input_cost"] + b["output_cost"]
                + b["cache_read_cost"] + b["cache_creation_cost"]
            )
            models.append({
                "model": name,
                "input": b["input"],
                "output": b["output"],
                "share": billable / total_billable,
                "cost_usd": cost_usd,
                "cost_share": (cost_usd / total_cost) if total_cost > 0 else 0,
            })
        models.sort(key=lambda m: m["input"] + m["output"], reverse=True)
        return JSONResponse({"range": range, "models": models})
```

- [ ] **Step 4: Run the test and verify it PASSES**

Run: `uv run pytest tests/test_serve_app.py::test_breakdown_by_model_returns_model_array -v`
Expected: PASS.

- [ ] **Step 5: Lint + full test sweep**

Run: `uv run ruff check src tests && uv run pytest`
Expected: ruff clean, all 279+ tests pass (existing count from RESUME).

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(breakdown): include per-model cost + cost_share in /api/breakdown/by-model"
```

---

## Phase B — Breakdown frontend: TOKENS / $ toggle

> **Manual verification protocol for all Phase B and C tasks:**
> Start dev server: `uv run tokenol serve --port 8787`
> Open `http://127.0.0.1:8787/breakdown` (Phase B) or `http://127.0.0.1:8787/` (Phase C) in a browser. Hard-reload (Ctrl-Shift-R) between iterations to bypass cached static assets. Keep DevTools console open to catch any JS errors.

### Task 4: Add `--series-secondary` CSS variable and unit-toggle pill styling

**Files:**
- Modify: `src/tokenol/serve/static/styles.css`

- [ ] **Step 1: Inspect existing CSS variables to find palette + pill rules**

Run: `grep -n "^:root\|--\|\.pill" src/tokenol/serve/static/styles.css | head -40`
This shows where palette variables and `.pill-row > span` rules live. Note the existing line colour variable name (likely `--amber` / `--gold` per usage in `app.js`'s `_FAMILY_COLOR`).

- [ ] **Step 2: Add the secondary series colour**

Inside the `:root` block in `src/tokenol/serve/static/styles.css`, add:

```css
  --series-secondary: #5b7a8a;  /* slate-blue, used for overlay second series */
```

If a `--cool` variable already exists with a similar value, alias to it instead:

```css
  --series-secondary: var(--cool);
```

(Pick whichever option keeps the palette DRY — `grep -n "cool" src/tokenol/serve/static/styles.css` to check.)

- [ ] **Step 3: Add `.pill-row .secondary` styling**

Anywhere in `styles.css` after the existing `.pill-row span.on` rule (find it with `grep -n "\.on" src/tokenol/serve/static/styles.css`), add:

```css
/* Overlay state: outlined in the secondary series colour. */
.pill-row span.secondary {
  color: var(--series-secondary);
  border: 1px solid var(--series-secondary);
  background: transparent;
}
```

- [ ] **Step 4: Manual verification**

Reload `/breakdown` and `/`. No visual change expected (no element has `.secondary` yet). Confirm no CSS errors in DevTools console.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/static/styles.css
git commit -m "feat(ui): introduce --series-secondary palette + .pill secondary state"
```

---

### Task 5: Add TOKENS / $ pill markup to three Breakdown chart cards

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html`

- [ ] **Step 1: Identify the three card containers**

Run: `grep -n "Daily Billable Tokens\|Tokens by Project\|Model Mix" src/tokenol/serve/static/breakdown.html`
Note the line numbers — these are the three card titles to enrich.

- [ ] **Step 2: Insert the pill pair next to each title**

For each of the three chart cards (Daily Billable Tokens, Tokens by Project, Model Mix), locate the card's header row (the `<div>` containing the `<h3>` title and the small "total/avg" caption). Append a new pill row inside the header right-side group. Example for Daily Billable Tokens:

```html
<span class="pill-row" id="bd-time-unit-pills" role="group" aria-label="Unit">
  <span data-bdunit="tokens" class="on">Tokens</span>
  <span data-bdunit="cost">$</span>
</span>
```

The three IDs to use:
- `bd-time-unit-pills` — Daily Billable Tokens card
- `bd-project-unit-pills` — Tokens by Project card
- `bd-model-unit-pills` — Model Mix card

(If the existing chart-card header structure differs from the assumed `<div>` wrapper, place the new `<span class="pill-row">` immediately after the existing title element and before the "total/avg" caption span — match the placement that visually reads "title  [pills]  caption".)

- [ ] **Step 3: Manual verification**

Reload `/breakdown`. Confirm `[Tokens] [$]` pills appear on the three chart cards, with `Tokens` visually selected. Clicking them does nothing yet — that's Task 6.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.html
git commit -m "feat(breakdown): add TOKENS/\$ pill row markup to three chart cards"
```

---

### Task 6: Wire Daily Billable Tokens TOKENS / $ toggle

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Locate the existing Daily Billable Tokens renderer**

Run: `grep -n "daily-tokens\|chart-daily-tokens\|_renderDailyTokens\|fetchDailyTokens" src/tokenol/serve/static/breakdown.js`
Note the function that fetches `/api/breakdown/daily-tokens` and renders the stacked bar chart.

- [ ] **Step 2: Add the `_LS_BD_TIME_UNIT` constant and `_bdTimeUnit` state**

Near the top of `src/tokenol/serve/static/breakdown.js` (after any existing `const` declarations), add:

```javascript
const _LS_BD_TIME_UNIT    = 'tokenol.breakdown.timeUnit';
const _LS_BD_PROJECT_UNIT = 'tokenol.breakdown.projectUnit';
const _LS_BD_MODEL_UNIT   = 'tokenol.breakdown.modelUnit';

let _bdTimeUnit    = localStorage.getItem(_LS_BD_TIME_UNIT)    || 'tokens';
let _bdProjectUnit = localStorage.getItem(_LS_BD_PROJECT_UNIT) || 'tokens';
let _bdModelUnit   = localStorage.getItem(_LS_BD_MODEL_UNIT)   || 'tokens';
```

- [ ] **Step 3: Add a `_wireUnitPills` helper**

Near the bottom of `src/tokenol/serve/static/breakdown.js` (or near other helpers), add:

```javascript
function _wireUnitPills(groupId, lsKey, getCurrent, setCurrent, onChange) {
  const group = document.getElementById(groupId);
  if (!group) return;
  // Sync initial DOM state to persisted value.
  group.querySelectorAll('[data-bdunit]').forEach(b => {
    b.classList.toggle('on', b.dataset.bdunit === getCurrent());
  });
  group.querySelectorAll('[data-bdunit]').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.bdunit;
      if (next === getCurrent()) return;
      setCurrent(next);
      localStorage.setItem(lsKey, next);
      group.querySelectorAll('[data-bdunit]').forEach(b =>
        b.classList.toggle('on', b.dataset.bdunit === next),
      );
      onChange(next);
    });
  });
}
```

- [ ] **Step 4: Update the frontend renderer to switch unit**

`/api/breakdown/daily-tokens` already includes `cost_usd` per day, so no backend change is needed for this chart.

In `breakdown.js`, in the daily-tokens chart renderer (identified in Step 1), change the dataset construction to switch on `_bdTimeUnit`:

- TOKENS mode: existing behavior — stacked bars of `input`, `output`, `cache_creation` per day.
- `$` mode: a single non-stacked bar per day using `d.cost_usd` as the bar height. Drop the per-component split in this mode (per-component cost isn't returned by the endpoint, and the existing card legend (`total $X · avg $Y/d`) already represents per-day total).

Concrete change to the dataset construction:

```javascript
const useCost = _bdTimeUnit === 'cost';
const datasets = useCost
  ? [{
      label: 'cost',
      data: data.days.map(d => d.cost_usd),
      backgroundColor: 'var(--amber)',
      stack: 'cost',
    }]
  : [
      { label: 'input',          data: data.days.map(d => d.input),          backgroundColor: COLOR_INPUT,  stack: 'tokens' },
      { label: 'output',         data: data.days.map(d => d.output),         backgroundColor: COLOR_OUTPUT, stack: 'tokens' },
      { label: 'cache created',  data: data.days.map(d => d.cache_creation), backgroundColor: COLOR_CACHE,  stack: 'tokens' },
    ];
// y-axis ticks callback:
const tickFmt = useCost ? (v => '$' + v.toFixed(0)) : fmtTok;
```

Use whatever the existing colour constants are named in `breakdown.js` (substitute `COLOR_INPUT` etc. with the real names — visible in the existing dataset construction).

The chart needs `options.scales.y.ticks.callback` updated to `tickFmt` on each render. If the chart is reused (`_chartDailyTokens.update(...)`), update both `data.datasets` and `options.scales.y.ticks.callback` before calling `.update('none')`.

- [ ] **Step 5: Wire the pill row**

In the init block at the bottom of `breakdown.js` (where other pill rows are wired), add:

```javascript
_wireUnitPills('bd-time-unit-pills', _LS_BD_TIME_UNIT,
  () => _bdTimeUnit,
  v  => { _bdTimeUnit = v; },
  () => _fetchAndRenderDailyTokens(),  // substitute the actual fn name from Step 1
);
```

- [ ] **Step 6: Manual verification**

Start: `uv run tokenol serve --port 8787`
Open `http://127.0.0.1:8787/breakdown`. Click `$` on the Daily Billable Tokens card. Expected: bars become a single un-stacked bar per day, y-axis shows `$NN` ticks. Click `Tokens`: stacked input/output/cache_created bars return. Refresh the page: the last-selected mode persists.

- [ ] **Step 7: Lint + tests**

Run: `uv run ruff check src tests && uv run pytest`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat(breakdown): TOKENS/\$ toggle for Daily Billable Tokens chart"
```

---

### Task 7: Wire Tokens by Project TOKENS / $ toggle

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Locate the by-project renderer**

Run: `grep -n "by-project\|_chartByProject" src/tokenol/serve/static/breakdown.js`
The chart is constructed at the spot identified in spec section "Data shape" — two side-by-side bars (input + output) per project.

- [ ] **Step 2: Switch the datasets on toggle**

The chart already has two datasets — `input` and `output` (verified in `breakdown.js`'s by-project chart construction). Modify the dataset data source to read either `p.input`/`p.output` (TOKENS mode) or `p.input_cost`/`p.output_cost` (`$` mode), based on `_bdProjectUnit`. Similar pattern to Task 6 Step 6:

```javascript
const useCost = _bdProjectUnit === 'cost';
const inputData  = projects.map(p => useCost ? p.input_cost  : p.input);
const outputData = projects.map(p => useCost ? p.output_cost : p.output);
// y-axis ticks callback: useCost ? v => '$' + v.toFixed(2) : fmtTok
```

The "top 10 of 18 · 97% of billable" caption stays token-based per spec — do not recompute it for `$` mode.

- [ ] **Step 3: Wire the pill row**

In the init block at the bottom of `breakdown.js`, add:

```javascript
_wireUnitPills('bd-project-unit-pills', _LS_BD_PROJECT_UNIT,
  () => _bdProjectUnit,
  v  => { _bdProjectUnit = v; },
  () => _fetchAndRenderByProject(),  // actual fn name from Step 1
);
```

- [ ] **Step 4: Manual verification**

Reload `/breakdown`. Toggle `$` on the Tokens by Project card. Expected: bars rescale to dollar amounts; y-axis shows `$NN`. Click `Tokens` to revert. Refresh the page: last selection persists. Cache-hit dots remain rendered regardless of mode.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat(breakdown): TOKENS/\$ toggle for Tokens by Project chart"
```

---

### Task 8: Wire Model Mix TOKENS / $ toggle

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Locate the Model Mix doughnut renderer**

Run: `grep -n "by-model\|_chartByModel\|doughnut\|Model Mix" src/tokenol/serve/static/breakdown.js`
The doughnut is sized by `m.input + m.output` per slice (token share). In `$` mode it sizes by `m.cost_usd`.

- [ ] **Step 2: Switch slice sizing on toggle**

Modify the value-extractor used by the doughnut so it returns either `m.input + m.output` (TOKENS) or `m.cost_usd` ($), based on `_bdModelUnit`. The legend (the model-name list with colour swatches) stays the same — only the slice sizes change. Tooltip should show the active unit:

```javascript
const useCost = _bdModelUnit === 'cost';
const values  = models.map(m => useCost ? m.cost_usd : (m.input + m.output));
const tooltipFmt = useCost
  ? (m, share) => `${m.model}: $${m.cost_usd.toFixed(2)} (${(share * 100).toFixed(1)}%)`
  : (m, share) => `${m.model}: ${fmtTok(m.input + m.output)} (${(share * 100).toFixed(1)}%)`;
```

(Hook into whatever tooltip-callback pattern the existing chart uses — likely Chart.js `plugins.tooltip.callbacks.label`.)

- [ ] **Step 3: Wire the pill row**

In the init block at the bottom of `breakdown.js`:

```javascript
_wireUnitPills('bd-model-unit-pills', _LS_BD_MODEL_UNIT,
  () => _bdModelUnit,
  v  => { _bdModelUnit = v; },
  () => _fetchAndRenderByModel(),
);
```

- [ ] **Step 4: Manual verification**

Reload `/breakdown`. Toggle `$` on the Model Mix card. Expected: doughnut slice proportions shift (e.g., opus often dominates more by cost than by tokens). Tooltip on hover shows dollar value in `$` mode and token count in `Tokens` mode. Refresh persists.

- [ ] **Step 5: Lint + tests**

Run: `uv run ruff check src tests && uv run pytest`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat(breakdown): TOKENS/\$ toggle for Model Mix doughnut"
```

---

## Phase C — Overview: dual-metric overlay

### Task 9: Add chart.js dual-axis line-chart support

**Files:**
- Modify: `src/tokenol/serve/static/chart.js`

- [ ] **Step 1: Inspect the current line-chart factory**

Run: `cat src/tokenol/serve/static/chart.js`
Identify the function(s) that build the hourly and daily line charts (likely a single `_makeLineChart` or two near-identical builders). Note the contract: how labels, data, and y-axis formatters are passed in.

- [ ] **Step 2: Extend the line-chart factory signature**

Change the signature to accept an optional `secondary` series. The contract becomes:

```javascript
// Before: makeLineChart(canvas, { labels, data, yUnit, formatter, scale, label })
// After:  makeLineChart(canvas, { primary: {labels, data, yUnit, formatter, scale, label},
//                                  secondary?: {data, yUnit, formatter, label} })
```

The `secondary.data` shares `primary.labels` (same x-axis). The factory adds a right y-axis `yAxisID: 'y2'` when `secondary` is present:

```javascript
const datasets = [
  { label: primary.label, data: primary.data, yAxisID: 'y',
    borderColor: 'var(--amber)', borderWidth: 2, ... },
];
if (secondary) {
  datasets.push({
    label: secondary.label, data: secondary.data, yAxisID: 'y2',
    borderColor: 'var(--series-secondary)', borderWidth: 1.5,
    borderDash: [], pointRadius: 2,
    ...withOpacity(0.85),
  });
}
const scales = {
  x: { ... existing ... },
  y: { type: primary.scale, ticks: { callback: primary.formatter }, position: 'left',
       title: { display: !!secondary, text: primary.label, color: 'var(--amber)' } },
};
if (secondary) {
  scales.y2 = { type: 'linear', position: 'right',
    ticks: { callback: secondary.formatter, color: 'var(--series-secondary)' },
    title: { display: true, text: secondary.label, color: 'var(--series-secondary)' },
    grid: { drawOnChartArea: false } };
}
```

Critically, when `secondary` is absent the resulting chart options must be **byte-equivalent** to today's output. Implement as: build the base `options` object first, then conditionally append `y2` and the secondary dataset.

- [ ] **Step 3: Backward-compat shim for existing callers**

The existing callers in `app.js` and `day.js` pass the old flat shape (`{labels, data, ...}`, not `{primary: {...}}`). Add a small adapter at the top of the factory:

```javascript
function makeLineChart(canvas, opts) {
  // Backward-compat: flat opts → wrap as primary.
  if (!opts.primary) {
    opts = { primary: opts };
  }
  const { primary, secondary } = opts;
  // ... rest of factory uses primary / secondary
}
```

This keeps Phase B and earlier Phase C tasks shippable independently.

- [ ] **Step 4: Manual verification (regression)**

Start the server, open `/`. Visually confirm the Hour-By-Hour and Daily History line charts render unchanged. Toggle HIT% → $/KW → CTX. No regressions, no console errors.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/static/chart.js
git commit -m "feat(chart): line-chart factory accepts optional secondary series + right axis"
```

---

### Task 10: Add cyclic state machine + localStorage for Overview metric pills

**Files:**
- Modify: `src/tokenol/serve/static/app.js`

- [ ] **Step 1: Add new localStorage keys and state**

Near the existing `_LS_H_SCALE` / `_LS_D_SCALE` declarations (around lines 408–409 in `app.js`), add:

```javascript
const _LS_H_METRIC = 'tokenol.hourly.metric';
const _LS_D_METRIC = 'tokenol.daily.metric';
```

Change the initialization of `_hMetric` (around line 427) and `_dMetric` (around line 691) from:

```javascript
let _hMetric    = 'hit_pct';
```

to:

```javascript
// Persist as 'primary' or 'primary,secondary'. Returns {primary, secondary?}.
function _parseMetricState(raw) {
  if (!raw) return { primary: 'hit_pct' };
  const parts = raw.split(',').filter(Boolean);
  return parts.length === 2
    ? { primary: parts[0], secondary: parts[1] }
    : { primary: parts[0] || 'hit_pct' };
}
function _formatMetricState(st) {
  return st.secondary ? `${st.primary},${st.secondary}` : st.primary;
}

let _hMetric = _parseMetricState(localStorage.getItem(_LS_H_METRIC));
```

Same pattern for `_dMetric`:

```javascript
let _dMetric = _parseMetricState(localStorage.getItem(_LS_D_METRIC));
```

- [ ] **Step 2: Find every read of `_hMetric` / `_dMetric` and update**

`_hMetric` and `_dMetric` are now objects, not strings. Every existing read site needs `.primary`. Search-and-fix:

Run: `grep -n "_hMetric\|_dMetric" src/tokenol/serve/static/app.js`

For each match, decide:
- If it's used as a single-metric identity (e.g., `_hMetric === 'hit_pct'`), change to `_hMetric.primary === 'hit_pct'`.
- If passed to a function expecting a string (e.g., `_scaleFor(_hMetric, ...)`), change to `_scaleFor(_hMetric.primary, ...)`.
- If passed as a query-param value (e.g., `URLSearchParams({metric: _hMetric})`), change to `_hMetric.primary` — the server still expects the single-metric value.

This is the highest-risk step in the plan because there are ~10 read sites across `app.js`. Make all changes in one pass and confirm by re-grepping for `_hMetric.toString` / `_hMetric +` / similar coercions — none should remain.

- [ ] **Step 3: Add the cyclic `_wireMetricPills` helper**

Below `_wireRange` (around line 900), add:

```javascript
function _wireMetricPills(groupId, lsKey, getState, setState, onChange) {
  const group = document.getElementById(groupId);
  if (!group) return;
  const renderPills = () => {
    const st = getState();
    group.querySelectorAll('[data-metric]').forEach(b => {
      const m = b.dataset.metric;
      b.classList.remove('on', 'secondary');
      if (m === st.primary) b.classList.add('on');
      else if (m === st.secondary) b.classList.add('secondary');
    });
  };
  renderPills();
  group.querySelectorAll('[data-metric]').forEach(btn => {
    btn.addEventListener('click', () => {
      const m  = btn.dataset.metric;
      const cur = getState();
      let next;
      if (m === cur.primary) {
        // primary → secondary (the other slot becomes primary if it existed)
        next = cur.secondary
          ? { primary: cur.secondary, secondary: m }
          : { primary: m };
        // Edge case: if only one pill was selected, going primary→secondary
        // with nothing to promote means we collapse to single-series of the same.
        if (!cur.secondary) next = { primary: m };
      } else if (m === cur.secondary) {
        // secondary → unselected (primary stays)
        next = { primary: cur.primary };
      } else {
        // Unselected click → becomes primary; old primary becomes secondary.
        next = cur.primary
          ? { primary: m, secondary: cur.primary }
          : { primary: m };
      }
      setState(next);
      localStorage.setItem(lsKey, _formatMetricState(next));
      renderPills();
      onChange(next);
    });
  });
}
```

(Note: the spec's table says "Primary click → becomes secondary, other becomes primary if it existed". The edge case where only one pill is selected and you click it — the spec doesn't define this. The implementation above collapses to keep it selected as primary, which means clicking the only selected pill is a no-op. That matches existing behavior, where clicking the active single-select pill is also a no-op.)

- [ ] **Step 4: Replace `_wireRange('hourly-metric-pills', ...)` and `_wireRange('daily-metric-pills', ...)` with `_wireMetricPills`**

Lines ~654 and ~786 in `app.js`. Replace:

```javascript
_wireRange('hourly-metric-pills', m => { _hMetric = m; _fetchHourly(); });
```

with:

```javascript
_wireMetricPills('hourly-metric-pills', _LS_H_METRIC,
  () => _hMetric,
  st => { _hMetric = st; },
  () => _fetchHourly(),
);
```

Same for daily:

```javascript
_wireMetricPills('daily-metric-pills', _LS_D_METRIC,
  () => _dMetric,
  st => { _dMetric = st; },
  () => _fetchDaily(),
);
```

- [ ] **Step 5: Manual verification — single-series still works**

Reload `/`. Hour-By-Hour and Daily History should open on `HIT%` (no prior `localStorage` entry). Click `$/KW`: it becomes the only "on" pill — gold filled. Click `CTX`: now CTX is gold, $/KW becomes outlined (secondary). Click CTX again: it becomes outlined, $/KW becomes gold. Click $/KW (now gold) once: collapses back to single $/KW. **At this point the chart still only renders the primary series** — Task 11 wires the secondary into the chart.

Refresh the page. Confirm the last-selected pair restores from `localStorage`.

- [ ] **Step 6: Lint + tests**

Run: `uv run ruff check src tests && uv run pytest`
Expected: green (no Python changes here; ruff is a safety check).

- [ ] **Step 7: Commit**

```bash
git add src/tokenol/serve/static/app.js
git commit -m "feat(overview): cyclic primary/secondary metric pill selector + localStorage"
```

---

### Task 11: Render the secondary series on Hour-By-Hour and Daily History

**Files:**
- Modify: `src/tokenol/serve/static/app.js`
- Modify: `src/tokenol/serve/static/index.html`

- [ ] **Step 1: Add legend slot in `index.html`**

In `src/tokenol/serve/static/index.html`, immediately after each metric-pill `<span class="pill-row">` (one for hourly at line ~209, one for daily at line ~245), add:

```html
<span class="tl-legend" id="hourly-legend" hidden></span>
```

and

```html
<span class="tl-legend" id="daily-legend" hidden></span>
```

Add a CSS rule in `styles.css`:

```css
.tl-legend {
  font-size: 0.85em;
  color: var(--mute, #888);
  margin-left: 0.5em;
}
.tl-legend .swatch {
  display: inline-block;
  width: 0.8em;
  height: 0.15em;
  vertical-align: middle;
  margin: 0 0.25em;
}
```

- [ ] **Step 2: Build a `_renderLegend` helper in `app.js`**

```javascript
const _METRIC_LABEL = {
  hit_pct: 'Hit%', cost_per_kw: '$/kW', ctx_ratio: 'Ctx',
  cache_reuse: 'Cache reuse', output: 'Output', cost: 'Cost',
};
function _renderLegend(elId, state) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!state.secondary) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML =
    `<span class="swatch" style="background:var(--amber)"></span>` +
    `${_METRIC_LABEL[state.primary]} (left)` +
    `<span class="swatch" style="background:var(--series-secondary); margin-left:1em"></span>` +
    `${_METRIC_LABEL[state.secondary]} (right)`;
}
```

- [ ] **Step 3: Update `_fetchHourly` to fetch + plot both series**

The current `_fetchHourly` requests `/api/hourly?metric=<primary>`. When `_hMetric.secondary` is present, issue a second request for the secondary metric (parallel `Promise.all`):

```javascript
async function _fetchHourly() {
  const p1 = fetch(`/api/hourly?` + new URLSearchParams({ day: ..., metric: _hMetric.primary }))
    .then(r => r.json());
  const p2 = _hMetric.secondary
    ? fetch(`/api/hourly?` + new URLSearchParams({ day: ..., metric: _hMetric.secondary }))
        .then(r => r.json())
    : Promise.resolve(null);
  const [d1, d2] = await Promise.all([p1, p2]);
  _paintHourly(d1, d2);
  _renderLegend('hourly-legend', _hMetric);
}
```

(Match the existing `_fetchHourly` parameter list — `day`, project / model filters, range. The structure shown is illustrative.)

`_paintHourly` (or whatever the existing painter is named) accepts an optional `secondary` argument and forwards it to the `makeLineChart` factory's `secondary` key from Task 9. The data shape for `secondary` is identical to `primary` minus `labels` (shared x-axis).

- [ ] **Step 4: Update `_fetchDaily` symmetrically**

Same pattern as Step 3. Note: there's a `_snapshotToDailyData(daily, metric)` fast-path for `hit_pct` + `range='30d'`. The fast-path is single-metric only — if `_dMetric.secondary` is set, skip the fast-path and always fetch.

```javascript
const canUseSnapshot = _dMetric.primary === 'hit_pct'
  && !_dMetric.secondary
  && _dRange === '30d'
  // ... existing conditions
;
```

- [ ] **Step 5: Manual verification — overlay renders**

Reload `/`. Click `OUTPUT` then `COST` on Daily History. Expected:
- Gold OUTPUT line, slate-blue COST line.
- Two y-axes: left for OUTPUT (tokens, `k`/`M` suffix), right for COST (`$NN`).
- Legend reads `▬ Output (left)  ▬ Cost (right)`.
- Tooltip on hover shows both values.
- Click `COST` again → drops the overlay. Legend hides. Chart reverts to single gold line.

Try a wildly mismatched pair: `OUTPUT` + `HIT%`. Verify left axis is in `M` and right axis is in `%`, both auto-fitting.

Try on Hour-By-Hour with the same combinations.

- [ ] **Step 6: Lint + tests**

Run: `uv run ruff check src tests && uv run pytest`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/tokenol/serve/static/app.js src/tokenol/serve/static/index.html src/tokenol/serve/static/styles.css
git commit -m "feat(overview): render dual-axis overlay for paired metrics + inline legend"
```

---

## Phase D — Cross-feature validation

### Task 12: End-to-end regression sweep

**Files:** none (validation only)

- [ ] **Step 1: Reset browser localStorage to simulate first-time user**

In DevTools console on the Overview page:

```javascript
localStorage.removeItem('tokenol.hourly.metric');
localStorage.removeItem('tokenol.daily.metric');
localStorage.removeItem('tokenol.breakdown.timeUnit');
localStorage.removeItem('tokenol.breakdown.projectUnit');
localStorage.removeItem('tokenol.breakdown.modelUnit');
location.reload();
```

Confirm Overview opens on `HIT%` single-series; Breakdown opens on TOKENS for all three charts. Matches the "Defaults match today" goal.

- [ ] **Step 2: Visual overlay spot-checks (5 pairs)**

On Daily History, exercise: `OUTPUT+COST`, `HIT%+$/KW`, `CACHE REUSE+CTX`, `COST+OUTPUT` (verify primary/secondary order does swap left/right axes), `$/KW+CACHE REUSE` (an extreme mismatch — dollars vs ratio).

For each: confirm dual-axis labels are correct, tooltip stacks two values, secondary line is thinner + outlined pill.

- [ ] **Step 3: Hour-By-Hour overlay spot-check**

Same five pairs on Hour-By-Hour. Confirm `LIN/LOG` toggle still works (applies to primary only).

- [ ] **Step 4: Breakdown unit toggles in mixed state**

Set Daily Billable Tokens to `$`, Tokens by Project to `Tokens`, Model Mix to `$`. Refresh. Confirm all three persist independently.

- [ ] **Step 5: Backward-compat — older single-metric `localStorage` entry**

In DevTools console:

```javascript
localStorage.setItem('tokenol.hourly.metric', 'cost');
location.reload();
```

Confirm Overview opens with single-series COST on Hour-By-Hour (no overlay, parser handled the one-element case).

- [ ] **Step 6: Test suite + lint final pass**

Run: `uv run ruff check src tests && uv run pytest`
Expected: ruff clean, all tests pass.

- [ ] **Step 7: Update CHANGELOG and RESUME**

Add an entry to `CHANGELOG.md` near the top under an `Unreleased` heading (or a new minor-version heading like `0.5.0` depending on the project's bump cadence — RESUME indicates a new user-facing flag bumped 0.3 → 0.4 last release, so a new user-facing dashboard feature also justifies a minor bump):

```markdown
## 0.5.0 — Cost visibility everywhere

- Overview: Hour-By-Hour and Daily History pills now support a primary +
  secondary metric overlay (max two), rendered on dual axes. Cyclic pill
  selection: click sets primary, click again demotes to secondary, click
  a third time deselects. Selection persists in `localStorage`.
- Breakdown: Daily Billable Tokens, Tokens by Project, and Model Mix gained
  a per-chart `TOKENS / $` toggle. Backend `/api/breakdown/by-project` and
  `/api/breakdown/by-model` payloads include cost fields; `/api/breakdown/daily-tokens`
  gains per-component cost fields.
- No new endpoints; no schema changes; no new dependencies.
```

Update `RESUME.md` under "Recent shipped work" with one paragraph summarizing the same.

- [ ] **Step 8: Final commit (docs + version)**

If bumping version: also update `pyproject.toml`, `src/tokenol/__init__.py`, and run `uv lock` (per RESUME's documented release-bump checklist).

```bash
git add CHANGELOG.md RESUME.md pyproject.toml src/tokenol/__init__.py uv.lock
git commit -m "chore(release): bump to 0.5.0 — cost visibility overhaul"
```

(If holding off on the version bump, commit only the CHANGELOG + RESUME under `chore(docs): note 0.5.0 cost-visibility work in CHANGELOG and RESUME` and leave version bump for a separate release commit.)

---

## Self-review checklist (for the agent executing this plan)

After all tasks are committed, do a final mental pass:

- [ ] Defaults unchanged for a first-time user — confirmed in Task 12 Step 1.
- [ ] No new endpoints — verified by `git diff main -- 'src/tokenol/serve/app.py'` showing only modifications inside existing `api_breakdown_*` handlers and `_bucket_turns`.
- [ ] No new Python deps — `uv.lock` only changes if you bumped the version.
- [ ] All commits use the project's `feat(scope): ...` or `chore(scope): ...` style — no AI attribution trailers.
- [ ] `uv run ruff check src tests && uv run pytest` is green at HEAD.
- [ ] Manual verification of all four overlay combinations passed.
- [ ] Spec referenced (`docs/superpowers/specs/2026-05-15-cost-visibility-design.md`) — only one open question (colour pick) was resolved during Task 4 Step 2.
