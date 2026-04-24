# Breakdown tab — PR1 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the new `/breakdown` page with tab navigation, a 4-card scorecard, page-level period pills with per-tab memory, and the Time section (two Chart.js bar charts) — the first of three PRs rolling out the Breakdown tab from `docs/superpowers/specs/2026-04-24-breakdown-tab-design.md` (commit `b48e208`).

**Architecture:** Add a top-level FastAPI route `/breakdown` served from a new static HTML page. Two new read-only JSON endpoints under `/api/breakdown/*` that read from the existing in-memory `SnapshotResult` cache. Frontend is a new ES module (`breakdown.js`) that loads Chart.js 4.4.7 via CDN with SRI, configures globals once from the existing CSS design tokens, and renders the scorecard + two time-series bar charts. One new cost helper (`cache_saved_usd`) in `metrics/cost.py`.

**Tech Stack:** FastAPI · pytest / pytest-asyncio · vanilla ES modules · Chart.js 4.4.7 (new) · existing CSS design tokens in `src/tokenol/serve/static/styles.css:2–23`.

---

## Scope

**In scope for this PR:**

- `GET /breakdown` HTML route and the page shell.
- `GET /api/breakdown/summary?range=…` endpoint (scorecard data).
- `GET /api/breakdown/daily-tokens?range=…` endpoint (feeds both time charts).
- `cache_saved_usd(turns)` helper in `metrics/cost.py`.
- 4-card scorecard: Activity · Billable tokens · Cache · Est. Cost (with cache-saved badge).
- Page header with "Breakdown" title, subtitle, and right-aligned period pills (`7D · 30D · 90D · All`, default `30D`).
- Nav tabs in the shared topbar (`Overview | Breakdown`) with `data-page` attribute driving CSS.
- Two Time-section Chart.js bar charts: Daily billable tokens (stacked) and Daily cache re-use (single-series).
- Per-chart right-aligned cost subheadings (total + avg).
- Per-tab period memory via `sessionStorage['tokenol.breakdown.period']`.
- SSE-triggered refresh when on Breakdown.
- Python unit + integration tests. No frontend tests (this repo has none).

**Out of scope (explicitly deferred to PR2/PR3):**

- Tokens-by-project grouped bars, cache-health dots, model-mix donut (PR2).
- Tool mix bar + parser changes (PR3).
- uPlot → Chart.js migration of Overview charts.
- Tool-name parser edge-case policies (belongs to PR3's plan).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/tokenol/metrics/cost.py` | Modify | Add `cache_saved_usd(turns)` helper. Nothing else. |
| `src/tokenol/serve/app.py` | Modify | Add `@app.get("/breakdown")` HTML route. Add `/api/breakdown/summary` and `/api/breakdown/daily-tokens` endpoints. |
| `src/tokenol/serve/static/index.html` | Modify | Add `data-page="overview"` on `<body>`. Add nav-tabs markup in `.topbar-row1` (between brand and icons). Add Chart.js SRI script tag — N/A on Overview (leave unchanged script loads). Hide global period pills via CSS (done in styles.css). |
| `src/tokenol/serve/static/breakdown.html` | Create | Page shell: topbar (brand + nav-tabs + icons), page heading row (title + subtitle + period pills), scorecard grid (4 `.scorecard-card`), Time section with two `.breakdown-panel` shells containing `<canvas>`. |
| `src/tokenol/serve/static/breakdown.js` | Create | Period-pill state, data fetching, scorecard render, Chart.js globals configuration, both time charts, SSE refresh listener. |
| `src/tokenol/serve/static/styles.css` | Modify | Add `.nav-tabs`, `.nav-tab`, `.breakdown-page` scope, `.scorecard`, `.scorecard-card`, `.breakdown-section`, `.breakdown-panel`, `.chart-subheading`, `.page-heading`. Hide `.topbar-controls .pill-group` when `body[data-page="breakdown"]`. |
| `tests/test_metrics.py` | Modify | Add three `cache_saved_usd` unit tests. |
| `tests/test_serve_app.py` | Modify | Add three endpoint tests: `/breakdown` returns HTML, `/api/breakdown/summary` shape + range filtering, `/api/breakdown/daily-tokens` shape + zero-fill. |

**No new Python source files.** One new `.html`, one new `.js`.

---

## Pre-implementation: pin Chart.js SRI

Chart.js is loaded from jsdelivr with Subresource Integrity. The engineer **must compute the real SRI hash** at implementation time — do not paste a hash from this document (none is provided; any hash I'd put here is a placeholder).

- [ ] **Step 0: Compute Chart.js 4.4.7 SRI hash**

Run:
```bash
curl -sL https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js | openssl dgst -sha384 -binary | openssl base64 -A
```

Save the output — it is the value for `integrity="sha384-<hash>"` later in Task 10. If the curl command fails (no network, jsdelivr outage), retry or fetch the hash from https://www.srihash.org/ using the same URL.

---

## Task 1: `cache_saved_usd` helper in `metrics/cost.py`

Counterfactual savings from cache reads: for each turn, the difference between what its `cache_read_input_tokens` *would have cost at full input price* and what they *actually cost at cache-read price*. Drives the Cost scorecard card's "cache saved ≈ $X" badge and the Daily cache re-use subheading.

**Files:**
- Modify: `src/tokenol/metrics/cost.py` (add function; existing file ends at line 145)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_metrics.py`:

```python
# ---- cache_saved_usd --------------------------------------------------------

from tokenol.metrics.cost import cache_saved_usd
from tokenol.model.events import Turn, Usage
from datetime import datetime, timezone


def _turn_with_cache_read(model: str | None, cache_read: int) -> Turn:
    return Turn(
        dedup_key=f"t-{cache_read}",
        timestamp=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        session_id="s",
        model=model,
        usage=Usage(
            input_tokens=0, output_tokens=0,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=0,
        ),
        is_sidechain=False,
        stop_reason="end_turn",
    )


def test_cache_saved_usd_zero_reads_returns_zero():
    turns = [_turn_with_cache_read("claude-sonnet-4-6", 0)]
    assert cache_saved_usd(turns) == 0.0


def test_cache_saved_usd_known_model_sonnet_four_six():
    # Sonnet 4.6 pricing: input = $3.00/M, cache_read = $0.30/M.
    # 1_000_000 cache read tokens → (1.00M × $3.00) − (1.00M × $0.30) = $2.70 saved.
    turns = [_turn_with_cache_read("claude-sonnet-4-6", 1_000_000)]
    assert cache_saved_usd(turns) == pytest.approx(2.70, rel=1e-6)


def test_cache_saved_usd_unknown_model_contributes_zero():
    # Completely unrecognised model: registry.resolve returns (None, tags).
    # Those turns contribute 0, others still counted.
    known = _turn_with_cache_read("claude-sonnet-4-6", 1_000_000)
    unknown = _turn_with_cache_read("claude-nonsense-99", 1_000_000)
    assert cache_saved_usd([known, unknown]) == pytest.approx(2.70, rel=1e-6)


def test_cache_saved_usd_none_model_contributes_zero():
    turns = [_turn_with_cache_read(None, 500_000)]
    assert cache_saved_usd(turns) == 0.0


def test_cache_saved_usd_sums_across_turns_and_models():
    # Opus 4.7: input $5.00/M, cache_read $0.50/M. 200k reads → (0.2 × 5) − (0.2 × 0.5) = $0.90
    # Sonnet 4.6: 500k reads → (0.5 × 3) − (0.5 × 0.3) = $1.35
    # Total: $2.25
    turns = [
        _turn_with_cache_read("claude-opus-4-7",   200_000),
        _turn_with_cache_read("claude-sonnet-4-6", 500_000),
    ]
    assert cache_saved_usd(turns) == pytest.approx(2.25, rel=1e-6)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_metrics.py::test_cache_saved_usd_zero_reads_returns_zero -v
```
Expected: `ImportError: cannot import name 'cache_saved_usd'`.

- [ ] **Step 3: Implement `cache_saved_usd`**

Add to `src/tokenol/metrics/cost.py`, after `cost_for_turn` and before `@dataclass class DailyRollup` (around line 46):

```python
from collections.abc import Iterable


def cache_saved_usd(turns: Iterable[Turn]) -> float:
    """Sum of cache-read counterfactual savings across *turns*, in USD.

    For each turn with a resolvable model: computes what its cache_read tokens
    would have cost at that model's full input price, minus what they actually
    cost at its cache_read price. Turns with model=None, an unknown model, or
    zero cache_read_input_tokens contribute 0.
    """
    total = 0.0
    for turn in turns:
        cache_read = turn.usage.cache_read_input_tokens
        if not turn.model or cache_read == 0:
            continue
        entry, _tags = registry.resolve(turn.model)
        if entry is None:
            continue
        full_input_usd = cache_read * entry["input"] / _M
        actual_cache_usd = cache_read * entry["cache_read"] / _M
        total += full_input_usd - actual_cache_usd
    return total
```

Note: `Iterable` import goes near the top of the file alongside existing `from datetime import …` lines. Do **not** move the existing `from tokenol.model import registry` — it's already imported at line 9.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_metrics.py -k cache_saved -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/metrics/cost.py tests/test_metrics.py
git commit -m "feat: add cache_saved_usd counterfactual helper

Per-turn: (cache_read_tokens × input_price) − actual cache_read_cost,
summed across turns. Drives the Breakdown Cost scorecard's cache-saved
badge and the Daily cache re-use subheading."
```

---

## Task 2: `/api/breakdown/summary` endpoint

Scorecard data source. Reads from the cached `SnapshotResult` (turns + sessions), applies `range` filtering in memory, returns per-period totals plus `cache_saved_usd`.

**Files:**
- Modify: `src/tokenol/serve/app.py:267` (append new endpoint after existing `api_search`)
- Test: `tests/test_serve_app.py`

**JSON response shape:**
```json
{
  "range": "30d",
  "sessions": 800,
  "turns": 45294,
  "input_tokens": 7700000,
  "output_tokens": 17000000,
  "cache_read_tokens": 2200000000,
  "cache_creation_tokens": 135100000,
  "cost_usd": 5938.05,
  "cache_saved_usd": 18700.00
}
```

All ranges use the existing `range_since` helper (already used by `/api/daily`, see `app.py:226`). Valid values: `7d | 30d | 90d | all`. Any other value → HTTP 400.

- [ ] **Step 1: Write failing test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_summary_returns_scorecard_fields(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/summary?range=all")

    assert resp.status_code == 200
    data = resp.json()
    for key in [
        "range", "sessions", "turns",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "cost_usd", "cache_saved_usd",
    ]:
        assert key in data, f"Missing field: {key}"
    assert data["range"] == "all"
    assert data["sessions"] >= 1
    assert data["turns"] >= 1
    assert isinstance(data["cost_usd"], (int, float))
    assert isinstance(data["cache_saved_usd"], (int, float))


@pytest.mark.asyncio
async def test_breakdown_summary_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/summary?range=14d")

    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_serve_app.py::test_breakdown_summary_returns_scorecard_fields -v
```
Expected: 404 (endpoint doesn't exist).

- [ ] **Step 3: Implement endpoint**

Add to `src/tokenol/serve/app.py` just before the final `return app`. `range_since` is already imported at `app.py:32`. Add this new import at the top of the file alongside the other `tokenol.metrics.cost` usage (grep `from tokenol.metrics.cost` first; the existing import may be elsewhere):

```python
from tokenol.metrics.cost import cache_saved_usd
```

```python
    @app.get("/api/breakdown/summary")
    async def api_breakdown_summary(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None
        turns = [t for t in result.turns if (since is None or t.timestamp.date() >= since)]
        sessions = [
            s for s in result.sessions
            if any(t.timestamp.date() >= since for t in s.turns) if since is not None
        ] if since is not None else list(result.sessions)

        return JSONResponse({
            "range": range,
            "sessions": len(sessions),
            "turns": len(turns),
            "input_tokens": sum(t.usage.input_tokens for t in turns),
            "output_tokens": sum(t.usage.output_tokens for t in turns),
            "cache_read_tokens": sum(t.usage.cache_read_input_tokens for t in turns),
            "cache_creation_tokens": sum(t.usage.cache_creation_input_tokens for t in turns),
            "cost_usd": sum(t.cost_usd for t in turns),
            "cache_saved_usd": cache_saved_usd(turns),
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_serve_app.py -k breakdown_summary -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat: add /api/breakdown/summary endpoint

Range-filtered totals for scorecard: sessions, turns, token buckets,
cost, and cache_saved_usd. Reads from existing snapshot cache."
```

---

## Task 3: `/api/breakdown/daily-tokens` endpoint

Drives both Time charts (stacked Daily billable tokens + single-series Daily cache re-use). One endpoint, one response; frontend reshapes per chart.

**Files:**
- Modify: `src/tokenol/serve/app.py` (append after Task 2's endpoint)
- Test: `tests/test_serve_app.py`

**JSON response shape:**
```json
{
  "range": "30d",
  "days": [
    {
      "date": "2026-03-25",
      "input": 1200000,
      "output": 2300000,
      "cache_creation": 42000,
      "cache_read": 89000000,
      "cost_usd": 8.41
    }
  ]
}
```

Uses existing `rollup_by_date` (`src/tokenol/metrics/cost.py:79`), which already zero-fills missing days when `since` is given. For `range=all`, no zero-fill.

- [ ] **Step 1: Write failing test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_daily_tokens_returns_day_array(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/daily-tokens?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "days" in data
    assert len(data["days"]) >= 1
    day = data["days"][0]
    for key in ["date", "input", "output", "cache_creation", "cache_read", "cost_usd"]:
        assert key in day, f"Missing field: {key}"
    # Dates are ISO strings (YYYY-MM-DD).
    assert len(day["date"]) == 10 and day["date"][4] == "-" and day["date"][7] == "-"


@pytest.mark.asyncio
async def test_breakdown_daily_tokens_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/daily-tokens?range=14d")

    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_serve_app.py -k breakdown_daily_tokens -v
```
Expected: 404.

- [ ] **Step 3: Implement endpoint**

At the top of `src/tokenol/serve/app.py` add (if not already imported):
```python
from tokenol.metrics.cost import rollup_by_date
```

Then, after the `api_breakdown_summary` endpoint, add:

```python
    @app.get("/api/breakdown/daily-tokens")
    async def api_breakdown_daily_tokens(request: Request, range: str = "30d"):
        if range not in ("7d", "30d", "90d", "all"):
            raise HTTPException(
                status_code=400,
                detail="range must be 7d, 30d, 90d, or all",
            )
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None
        turns = [t for t in result.turns if (since is None or t.timestamp.date() >= since)]
        rollups = rollup_by_date(turns, since=since) if since else rollup_by_date(turns)

        return JSONResponse({
            "range": range,
            "days": [
                {
                    "date": r.date.isoformat(),
                    "input": r.input_tokens,
                    "output": r.output_tokens,
                    "cache_creation": r.cache_creation_tokens,
                    "cache_read": r.cache_read_tokens,
                    "cost_usd": r.cost_usd,
                }
                for r in rollups
            ],
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_serve_app.py -k breakdown_daily_tokens -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat: add /api/breakdown/daily-tokens endpoint

Per-day token bucket rollup feeding both Time charts. Zero-fills
missing days via rollup_by_date's existing since-param behavior."
```

---

## Task 4: `/breakdown` HTML route

Mirror of existing `/` → `index.html` pattern. The HTML itself arrives in Task 6; this task just wires the route.

**Files:**
- Modify: `src/tokenol/serve/app.py` (insert near other HTML routes around line 105)
- Test: `tests/test_serve_app.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_route_returns_html(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/breakdown")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/test_serve_app.py::test_breakdown_route_returns_html -v
```
Expected: 404 or FileNotFoundError — either counts as failing.

- [ ] **Step 3: Create placeholder `breakdown.html`**

So the route has something to return while we build up the real page in later tasks:

Create `src/tokenol/serve/static/breakdown.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>tokenol — breakdown</title>
</head>
<body data-page="breakdown">
  <p>Breakdown — placeholder, under construction.</p>
</body>
</html>
```

- [ ] **Step 4: Add the route**

In `src/tokenol/serve/app.py`, immediately after the existing `index_page` route (around line 91), add:

```python
    @app.get("/breakdown", include_in_schema=False)
    async def breakdown_page():
        return FileResponse(str(STATIC_DIR / "breakdown.html"))
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
pytest tests/test_serve_app.py::test_breakdown_route_returns_html -v
```
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py src/tokenol/serve/static/breakdown.html tests/test_serve_app.py
git commit -m "feat: add /breakdown route and placeholder page"
```

---

## Task 5: Topbar tab nav + `data-page` attribute on `index.html`

Introduce the shared nav element. Both Overview and Breakdown will use the same markup; CSS (Task 7) styles the active one. On Overview nothing else changes.

**Files:**
- Modify: `src/tokenol/serve/static/index.html`

- [ ] **Step 1: Add `data-page` attribute to `<body>`**

In `src/tokenol/serve/static/index.html` line 15, change:
```html
<body>
```
to:
```html
<body data-page="overview">
```

- [ ] **Step 2: Add nav-tabs markup in topbar-row1**

In `src/tokenol/serve/static/index.html`, between the `.brand` div (line 119) and `.topbar-icons` (line 120), insert:

```html
      <nav class="nav-tabs" aria-label="Primary">
        <a href="/" class="nav-tab is-active" aria-current="page">Overview</a>
        <a href="/breakdown" class="nav-tab">Breakdown</a>
      </nav>
```

- [ ] **Step 3: Sanity-check existing Overview still renders**

Run:
```bash
pytest tests/test_serve_app.py -k snapshot -v
```
Expected: still passes — we didn't touch anything JS-visible. Visually, the nav is unstyled until Task 7; that's fine.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/index.html
git commit -m "feat: add data-page attr and nav-tabs in Overview topbar"
```

---

## Task 6: Full `breakdown.html` page shell

Replace the placeholder from Task 4 with the real page markup. No JS yet — page will render a static scaffold when viewed.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html`

- [ ] **Step 1: Replace file contents**

Overwrite `src/tokenol/serve/static/breakdown.html` with:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>tokenol — breakdown</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><path d='M16 2 A14 14 0 0 1 30 16' stroke='%23a66408' stroke-width='4' fill='none' stroke-linecap='round'/></svg>">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/assets/styles.css">

  <!-- Chart.js 4.4.7 — UMD build, pinned with SRI. Replace <HASH> with the
       sha384 base64 string computed in Pre-implementation Step 0. -->
  <script defer
          src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"
          integrity="sha384-<HASH>"
          crossorigin="anonymous"></script>
</head>
<body data-page="breakdown">

<div class="app">

  <header class="topbar">
    <div class="topbar-row1">
      <div class="brand">tokenol <span class="dim">— breakdown</span></div>
      <nav class="nav-tabs" aria-label="Primary">
        <a href="/" class="nav-tab">Overview</a>
        <a href="/breakdown" class="nav-tab is-active" aria-current="page">Breakdown</a>
      </nav>
      <div class="topbar-icons">
        <span class="sse-dot" id="sse-dot" title="SSE connecting…"></span>
        <span id="wall-clock" aria-live="off">––:––</span>
      </div>
    </div>
  </header>

  <div class="page-heading">
    <div>
      <h1 class="page-title">Breakdown</h1>
      <div class="page-subtitle">tokens by day, project, model, and tool</div>
    </div>
    <div class="pill-row" id="breakdown-period-pills" role="group" aria-label="Period">
      <span data-range="7d">7D</span>
      <span data-range="30d" class="on">30D</span>
      <span data-range="90d">90D</span>
      <span data-range="all">All</span>
    </div>
  </div>

  <section class="scorecard" aria-label="Totals">
    <article class="scorecard-card" id="sc-activity">
      <div class="sc-label">Activity</div>
      <div class="sc-primary" id="sc-activity-primary">—</div>
      <div class="sc-sub"     id="sc-activity-sub">—</div>
    </article>
    <article class="scorecard-card" id="sc-tokens">
      <div class="sc-label">Billable tokens</div>
      <div class="sc-primary" id="sc-tokens-primary">—</div>
      <div class="sc-sub"     id="sc-tokens-sub">—</div>
    </article>
    <article class="scorecard-card" id="sc-cache">
      <div class="sc-label">Cache</div>
      <div class="sc-primary" id="sc-cache-primary">—</div>
      <div class="sc-sub"     id="sc-cache-sub">—</div>
    </article>
    <article class="scorecard-card" id="sc-cost">
      <div class="sc-label">Est. Cost</div>
      <div class="sc-primary" id="sc-cost-primary">—</div>
      <div class="sc-sub good" id="sc-cost-sub">—</div>
    </article>
  </section>

  <div class="section-heading breakdown-section-heading">
    <h2>Time</h2>
  </div>
  <div class="breakdown-grid breakdown-grid--2">
    <section class="breakdown-panel" aria-labelledby="bp-daily-work-title">
      <div class="breakdown-panel-heading">
        <h3 id="bp-daily-work-title">Daily billable tokens</h3>
        <span class="chart-subheading" id="bp-daily-work-sub"></span>
      </div>
      <div class="breakdown-chart"><canvas id="chart-daily-work" height="200"></canvas></div>
    </section>
    <section class="breakdown-panel" aria-labelledby="bp-daily-cache-title">
      <div class="breakdown-panel-heading">
        <h3 id="bp-daily-cache-title">Daily cache re-use</h3>
        <span class="chart-subheading" id="bp-daily-cache-sub"></span>
      </div>
      <div class="breakdown-chart"><canvas id="chart-daily-cache" height="200"></canvas></div>
    </section>
  </div>

</div>

<script type="module" src="/assets/breakdown.js"></script>

</body>
</html>
```

**Important:** replace `sha384-<HASH>` with the hash computed in pre-implementation Step 0. Do not leave `<HASH>` as a literal.

- [ ] **Step 2: Sanity check**

Start the dev server and visit `http://127.0.0.1:8080/breakdown` — the page should render with the scorecard showing "—" everywhere and empty chart areas. No JS errors in the console beyond "Failed to load /assets/breakdown.js" (we haven't created it yet). If you see "integrity check failed" for Chart.js, the SRI hash is wrong — recompute it.

Run:
```bash
pytest tests/test_serve_app.py::test_breakdown_route_returns_html -v
```
Expected: still passes.

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/breakdown.html
git commit -m "feat: Breakdown page shell with scorecard and Time section

Includes Chart.js 4.4.7 UMD with SRI. breakdown.js module not yet
present — scorecard shows placeholder dashes."
```

---

## Task 7: CSS for nav tabs, scorecard, and Breakdown sections

Adds all new classes used in Tasks 5 and 6. Appends to `styles.css`; does not modify any existing rule.

**Files:**
- Modify: `src/tokenol/serve/static/styles.css` (append at end, currently 797 lines)

- [ ] **Step 1: Append new rules**

Append to the end of `src/tokenol/serve/static/styles.css`:

```css
/* ===== Nav tabs (shared across Overview/Breakdown) ===== */
.nav-tabs {
  display: flex;
  gap: 2px;
  margin: 0 16px;
  align-self: stretch;
  align-items: center;
}
.nav-tab {
  font-family: var(--font-mono);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  text-decoration: none;
  color: var(--mute-2);
  padding: 8px 14px;
  border-bottom: 2px solid transparent;
}
.nav-tab:hover { color: var(--fg); }
.nav-tab.is-active { color: var(--fg); border-bottom-color: var(--amber); }

/* Hide the global period pills when on the Breakdown page —
 * Breakdown carries its own page-level pill group. */
body[data-page="breakdown"] .topbar-controls .pill-group { display: none; }

/* ===== Breakdown: page heading ===== */
.page-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 24px;
  padding: 24px 32px 12px;
  border-bottom: 1px solid var(--rule);
}
.page-title {
  font-family: var(--font-serif);
  font-weight: 400;
  font-size: 34px;
  line-height: 1;
  margin: 0;
}
.page-subtitle {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--mute);
  margin-top: 4px;
}

/* ===== Breakdown: scorecard ===== */
.scorecard {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  padding: 16px 32px 8px;
}
.scorecard-card {
  background: var(--bg-raised);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 12px 16px;
}
.scorecard-card .sc-label {
  font-family: var(--font-mono);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mute-2);
}
.scorecard-card .sc-primary {
  font-family: var(--font-serif);
  font-size: 28px;
  line-height: 1.1;
  margin-top: 2px;
  color: var(--fg);
}
.scorecard-card .sc-primary .sc-unit {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--mute-2);
  margin-left: 2px;
}
.scorecard-card .sc-sub {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--mute-2);
  margin-top: 4px;
}
.scorecard-card .sc-sub.good { color: var(--green); }

/* ===== Breakdown: sections and panels ===== */
.breakdown-section-heading { margin-top: 8px; }
.breakdown-grid {
  padding: 0 32px 24px;
  display: grid;
  gap: 16px;
}
.breakdown-grid--2 { grid-template-columns: 1fr 1fr; }

.breakdown-panel {
  background: var(--bg-raised);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 12px 16px 16px;
}
.breakdown-panel-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 1px solid var(--rule);
  padding-bottom: 8px;
  margin-bottom: 12px;
}
.breakdown-panel-heading h3 {
  font-family: var(--font-serif);
  font-weight: 400;
  font-size: 19px;
  margin: 0;
}
.chart-subheading {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--mute);
}
.breakdown-chart { position: relative; min-height: 200px; }

@media (max-width: 900px) {
  .scorecard { grid-template-columns: repeat(2, 1fr); }
  .breakdown-grid--2 { grid-template-columns: 1fr; }
}
```

- [ ] **Step 2: Visual sanity**

Reload `/breakdown` — scorecard now renders as 4 cards with the cream/serif look; dashes visible. Nav tabs in topbar styled with the Overview tab underlined.

Reload `/` — nav tabs now visible in the topbar; Overview still highlighted. Verify the global period pills are **still visible** on Overview (they are hidden only on `body[data-page="breakdown"]`).

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/styles.css
git commit -m "style: Breakdown nav-tabs, page-heading, scorecard, panels

All new rules use existing CSS design tokens — no ad-hoc hex values.
Hides the global topbar period pills when on the Breakdown page."
```

---

## Task 8: `breakdown.js` — skeleton + period pill state

Empty module that loads, reads sessionStorage, wires up pill clicks, and logs the selected range. Full data-fetch arrives in Task 9.

**Files:**
- Create: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Create the file**

Create `src/tokenol/serve/static/breakdown.js`:

```javascript
// Breakdown page entry point. Loaded as an ES module by breakdown.html.
//
// Responsibilities (added across PR1):
//  - Period pill state (sessionStorage, independent of Overview)
//  - Scorecard fetch + render
//  - Chart.js global defaults (from CSS design tokens)
//  - Two Time-section charts
//  - SSE-driven refresh

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const SS_PERIOD = 'tokenol.breakdown.period';
const VALID_RANGES = new Set(['7d', '30d', '90d', 'all']);

function getPeriod() {
  const v = sessionStorage.getItem(SS_PERIOD);
  return VALID_RANGES.has(v) ? v : '30d';
}
function setPeriod(p) { sessionStorage.setItem(SS_PERIOD, p); }

// ---------------------------------------------------------------------------
// Pill wiring
// ---------------------------------------------------------------------------

function wirePeriodPills() {
  const group = document.getElementById('breakdown-period-pills');
  if (!group) return;
  // Sync initial highlight to stored value.
  const cur = getPeriod();
  for (const span of group.querySelectorAll('[data-range]')) {
    span.classList.toggle('on', span.dataset.range === cur);
    span.addEventListener('click', () => {
      const r = span.dataset.range;
      if (!VALID_RANGES.has(r)) return;
      setPeriod(r);
      for (const s of group.querySelectorAll('[data-range]')) {
        s.classList.toggle('on', s === span);
      }
      refreshAll();
    });
  }
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

async function refreshAll() {
  // Placeholder — filled in Task 9 onwards.
  console.log('[breakdown] refresh range=', getPeriod());
}

wirePeriodPills();
refreshAll();
```

- [ ] **Step 2: Visual sanity**

Reload `/breakdown`, open DevTools Console. On each pill click you should see a log line. Selecting `7D`, reloading the page should still show `7D` highlighted (sessionStorage persists within the tab).

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Breakdown period-pill state in sessionStorage

Independent of Overview's localStorage-backed global period. Wires
click handlers and syncs the visual 'on' class."
```

---

## Task 9: Scorecard fetch + render

Fetches `/api/breakdown/summary` and populates the 4 scorecard cards.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add formatters and summary fetch**

Add near the top of `breakdown.js`, below the state section:

```javascript
// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function fmtInt(n) {
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

function fmtTok(n) {
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function fmtUSD(n) {
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 1000) return `$${(n).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
  return `$${n.toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// Summary / scorecard
// ---------------------------------------------------------------------------

async function fetchSummary(range) {
  const resp = await fetch(`/api/breakdown/summary?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`summary ${resp.status}`);
  return resp.json();
}

function renderScorecard(data) {
  document.getElementById('sc-activity-primary').innerHTML =
    `${fmtInt(data.sessions)} <span class="sc-unit">sessions</span>`;
  document.getElementById('sc-activity-sub').textContent =
    `${fmtInt(data.turns)} turns`;

  const billable = data.input_tokens + data.output_tokens;
  document.getElementById('sc-tokens-primary').textContent = fmtTok(billable);
  document.getElementById('sc-tokens-sub').textContent =
    `${fmtTok(data.input_tokens)} in · ${fmtTok(data.output_tokens)} out`;

  document.getElementById('sc-cache-primary').innerHTML =
    `${fmtTok(data.cache_read_tokens)} <span class="sc-unit">read</span>`;
  document.getElementById('sc-cache-sub').textContent =
    `${fmtTok(data.cache_creation_tokens)} created`;

  document.getElementById('sc-cost-primary').textContent = fmtUSD(data.cost_usd);
  document.getElementById('sc-cost-sub').textContent =
    data.cache_saved_usd > 0
      ? `cache saved ≈ ${fmtUSD(data.cache_saved_usd)}`
      : '';
}
```

- [ ] **Step 2: Wire into `refreshAll`**

Replace the placeholder `refreshAll()` body:

```javascript
async function refreshAll() {
  const range = getPeriod();
  try {
    const summary = await fetchSummary(range);
    renderScorecard(summary);
  } catch (err) {
    console.error('[breakdown] summary failed', err);
  }
}
```

- [ ] **Step 3: Sanity check**

Reload `/breakdown` — scorecard now populated with real numbers. Click `7D` and `All` pills; numbers update each time.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Breakdown scorecard fetch + render

Four-card layout: Activity, Billable tokens, Cache, Est. Cost with
cache-saved badge. Formatters for tokens (k/M/B) and USD."
```

---

## Task 10: Chart.js global defaults

Configure `Chart.defaults` once from the CSS design tokens. Every Chart.js chart on the page inherits these — no per-chart overrides of color or font.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add setup function**

Append to `breakdown.js`:

```javascript
// ---------------------------------------------------------------------------
// Chart.js configuration (run once)
// ---------------------------------------------------------------------------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Tokenol dataset color cycle, semantic.
// 0 → amber (input, primary), 1 → alarm (output), 2 → green (cache),
// 3 → cool (model axis), 4 → mute, 5 → amber-dim.
function tokenolPalette() {
  return [
    cssVar('--amber'),
    cssVar('--alarm'),
    cssVar('--green'),
    cssVar('--cool'),
    cssVar('--mute'),
    cssVar('--amber-dim'),
  ];
}

let _chartDefaultsApplied = false;
function configureChartDefaults() {
  if (_chartDefaultsApplied || typeof window.Chart === 'undefined') return;
  const Chart = window.Chart;
  Chart.defaults.font.family = "'JetBrains Mono', 'SF Mono', 'Courier New', monospace";
  Chart.defaults.font.size = 11;
  Chart.defaults.color = cssVar('--fg-2');
  Chart.defaults.borderColor = cssVar('--rule');
  Chart.defaults.plugins.tooltip.backgroundColor = cssVar('--bg-raised');
  Chart.defaults.plugins.tooltip.titleColor = cssVar('--fg');
  Chart.defaults.plugins.tooltip.bodyColor = cssVar('--fg-2');
  Chart.defaults.plugins.tooltip.borderColor = cssVar('--rule-2');
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleFont = { family: "'Instrument Serif', serif", size: 14 };
  Chart.defaults.plugins.legend.labels.color = cssVar('--fg-2');
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  _chartDefaultsApplied = true;
}

// Chart.js is loaded as a deferred UMD script; it may not be ready when this
// module first evaluates. Poll briefly on a microtask until window.Chart shows up.
async function whenChartReady() {
  if (typeof window.Chart !== 'undefined') return window.Chart;
  for (let i = 0; i < 50; i++) {
    await new Promise(r => setTimeout(r, 40));
    if (typeof window.Chart !== 'undefined') return window.Chart;
  }
  throw new Error('Chart.js did not load within 2s');
}
```

- [ ] **Step 2: Apply defaults before rendering charts**

Update `refreshAll` to call `configureChartDefaults()` after Chart.js is ready:

```javascript
async function refreshAll() {
  const range = getPeriod();
  try {
    const [summary] = await Promise.all([
      fetchSummary(range),
      whenChartReady().then(configureChartDefaults),
    ]);
    renderScorecard(summary);
  } catch (err) {
    console.error('[breakdown] refresh failed', err);
  }
}
```

- [ ] **Step 3: Sanity check**

Reload `/breakdown`. Open DevTools Console; no errors. Type `Chart.defaults.color` — should return the `--fg-2` hex (`#3d372d` or similar, read from the cream palette).

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Chart.js defaults from tokenol CSS design tokens

Sets Chart.defaults font, color, tooltip, and legend styling once
from --amber/--alarm/--green/--cool/--mute/--fg-2/--rule tokens.
Every subsequent Chart.js chart inherits — no per-chart overrides."
```

---

## Task 11: Daily billable tokens stacked bar chart

First of the two Time-section charts. Stacks: input (`--amber`), output (`--alarm`), cache_creation (`--green`).

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add fetcher and chart renderer**

Append to `breakdown.js`:

```javascript
// ---------------------------------------------------------------------------
// Time-section charts
// ---------------------------------------------------------------------------

async function fetchDailyTokens(range) {
  const resp = await fetch(`/api/breakdown/daily-tokens?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`daily-tokens ${resp.status}`);
  return resp.json();
}

let _chartDailyWork = null;

function renderDailyWork(data) {
  const pal = tokenolPalette();
  const labels = data.days.map(d => d.date);
  const datasets = [
    { label: 'input',          data: data.days.map(d => d.input),          backgroundColor: pal[0] },
    { label: 'output',         data: data.days.map(d => d.output),         backgroundColor: pal[1] },
    { label: 'cache created',  data: data.days.map(d => d.cache_creation), backgroundColor: pal[2] },
  ];

  const totalCost = data.days.reduce((s, d) => s + d.cost_usd, 0);
  const days = Math.max(1, data.days.length);
  document.getElementById('bp-daily-work-sub').textContent =
    `total ${fmtUSD(totalCost)} · avg ${fmtUSD(totalCost / days)}/d`;

  const canvas = document.getElementById('chart-daily-work');
  if (_chartDailyWork) { _chartDailyWork.destroy(); _chartDailyWork = null; }
  _chartDailyWork = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
        y: { stacked: true, beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
      },
      plugins: { legend: { position: 'top', align: 'end' } },
    },
  });
}
```

- [ ] **Step 2: Wire into `refreshAll`**

Update `refreshAll` to fetch daily-tokens and render:

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
  } catch (err) {
    console.error('[breakdown] refresh failed', err);
  }
}
```

- [ ] **Step 3: Sanity check**

Reload `/breakdown`. The left chart panel now shows stacked daily bars with legend at top-right. Subheading shows `total $X · avg $Y/d`. Switching period pills re-renders the chart with the new range. No console errors.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Daily billable tokens stacked bar chart

Three stacks (input/output/cache_creation) using tokenol palette in
semantic order. Subheading with per-period total and daily average."
```

---

## Task 12: Daily cache re-use bar chart

Second Time-section chart. Single-series, color `--green` (matches the cache_creation stack on chart 1).

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add renderer**

Append to `breakdown.js`:

```javascript
let _chartDailyCache = null;

function renderDailyCache(data) {
  const pal = tokenolPalette();
  const labels = data.days.map(d => d.date);
  const datasets = [
    { label: 'cache read', data: data.days.map(d => d.cache_read), backgroundColor: pal[2] },
  ];

  // "Savings per day" subheading — pulled from the summary endpoint, not
  // daily-tokens, so this renderer reads it from the scorecard state.
  // For PR1 we compute a simple total-reads figure and a "avg $X/d saved" only
  // when the scorecard has already populated a cache_saved_usd number.
  const totalReads = data.days.reduce((s, d) => s + d.cache_read, 0);
  const days = Math.max(1, data.days.length);
  const savedTotal = window.__breakdownCacheSaved ?? 0;
  document.getElementById('bp-daily-cache-sub').textContent =
    savedTotal > 0
      ? `total ${fmtTok(totalReads)} · avg ${fmtUSD(savedTotal / days)}/d saved`
      : `total ${fmtTok(totalReads)}`;

  const canvas = document.getElementById('chart-daily-cache');
  if (_chartDailyCache) { _chartDailyCache.destroy(); _chartDailyCache = null; }
  _chartDailyCache = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 14 } },
        y: { beginAtZero: true, ticks: { callback: v => fmtTok(v) } },
      },
      plugins: { legend: { display: false } },
    },
  });
}
```

- [ ] **Step 2: Pass cache-saved value from summary into the cache chart**

Update `renderScorecard` to stash the cache_saved figure for the cache chart subheading to read:

Find in `renderScorecard`:
```javascript
  document.getElementById('sc-cost-sub').textContent =
    data.cache_saved_usd > 0
      ? `cache saved ≈ ${fmtUSD(data.cache_saved_usd)}`
      : '';
```

Add immediately after:
```javascript
  window.__breakdownCacheSaved = data.cache_saved_usd;
```

- [ ] **Step 3: Wire into `refreshAll`**

Update the render calls in `refreshAll`:

```javascript
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
```

- [ ] **Step 4: Sanity check**

Reload `/breakdown`. Right chart panel now shows single-series cache-read bars in green. Subheading shows `total {N}B · avg $X/d saved`. Switching period pills re-renders both charts.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Daily cache re-use single-series bar chart

Uses --green to match chart 1's cache_creation stack. Subheading
shows total reads and avg \$ saved per day."
```

---

## Task 13: SSE-driven refresh

Open an EventSource to `/api/stream` (existing). On each tick, re-fetch summary + daily-tokens and re-render with the currently-selected range.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add SSE wiring**

Append to `breakdown.js`:

```javascript
// ---------------------------------------------------------------------------
// SSE refresh
//
// The existing /api/stream stream is parameterised by 'period' (today/7d/30d/all),
// not our 'range', and its payload is tailored to Overview. We ignore the payload
// and only use the message event as a tick signal, then re-fetch our own endpoints
// with the currently-selected range.
// ---------------------------------------------------------------------------

let _es = null;
let _reconnectMs = 1000;

function connectSSE() {
  if (_es) { _es.close(); _es = null; }
  _es = new EventSource('/api/stream?period=today');
  _es.onopen = () => {
    _reconnectMs = 1000;
    const dot = document.getElementById('sse-dot');
    if (dot) { dot.className = 'sse-dot connected'; dot.title = 'Live — connected'; }
  };
  _es.onmessage = () => { refreshAll().catch(err => console.error('[breakdown] sse refresh', err)); };
  _es.onerror = () => {
    if (_es) { _es.close(); _es = null; }
    const dot = document.getElementById('sse-dot');
    if (dot) { dot.className = 'sse-dot error'; dot.title = 'Live — reconnecting'; }
    setTimeout(connectSSE, _reconnectMs);
    _reconnectMs = Math.min(_reconnectMs * 2, 30_000);
  };
}

connectSSE();
```

- [ ] **Step 2: Sanity check**

Reload `/breakdown`. The `sse-dot` in the topbar turns green within 1–2 seconds. In another terminal append a new assistant turn to any fixture file under the server's scanned dir (or wait for a real Claude Code session to append); within ~30 seconds the scorecard and both charts update. No console errors on reconnect when the server is restarted.

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Breakdown SSE-driven refresh on tick

Reuses /api/stream as a tick signal, re-fetches breakdown endpoints
with the currently-selected range. Exponential-backoff reconnect."
```

---

## Task 14: Manual end-to-end verification

Not code — a scripted walkthrough before opening the PR.

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v
```
Expected: all tests pass. No new failures vs main.

- [ ] **Step 2: Start the dev server**

```bash
tokenol serve
```
Default binds to `127.0.0.1:8080`. See `src/tokenol/cli.py:362` for flags.

- [ ] **Step 3: Walk the golden path on `/breakdown`**

- Period pills default to `30D`. Click each in turn — scorecard + both charts update.
- Reload page; pill state persists (sessionStorage).
- Click **Overview** tab — Overview renders unchanged from main (except the new nav tabs visible in the topbar).
- Click **Breakdown** tab again — state restored.

- [ ] **Step 4: Edge cases**

- Zero-data case: point the server at an empty `~/.claude*` temp dir. Scorecard shows zeros, charts show empty days. No console errors.
- Insufficient-history case: with only 1 day of data, `range=90d` still returns 200 (not enforced here — unlike `/api/daily`, breakdown endpoints don't gate on history length. Confirm the chart just shows a mostly-empty 90-bar range).

- [ ] **Step 5: Responsive sanity**

Narrow the viewport below 900 px. Scorecard goes 2×2, time grid stacks 1 column. Layout holds.

- [ ] **Step 6: No console errors**

With DevTools console open for the full walkthrough — zero errors and zero warnings related to Chart.js or fetches.

---

## Self-review checklist

Run before declaring PR1 ready:

- [ ] Every task has committed cleanly (no WIP lines in `git log`).
- [ ] `pytest tests/` is green.
- [ ] Chart.js SRI hash is a real value, not `<HASH>`.
- [ ] No ad-hoc hex values in new CSS/JS — every color is a CSS var or `tokenolPalette()`.
- [ ] `/` page still works exactly as before; global period pills still visible on Overview.
- [ ] No new Python source files beyond the ones in File Structure table.
- [ ] Commit messages follow repo convention (`feat:`, `style:`, `test:`) — no Co-Authored-By lines.

## What PR2 and PR3 inherit from PR1

The patterns established here — `/api/breakdown/*` namespace, `sessionStorage['tokenol.breakdown.period']`, `tokenolPalette()` color cycle, `configureChartDefaults()`, `fetch… then render…` structure — are what PR2 (project / model breakdowns) and PR3 (tool mix + parser ingest) re-use. Get these right here and the later PRs are near-mechanical.
