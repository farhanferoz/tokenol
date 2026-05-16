# Tool Mix attribution-mode toggle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-position attribution-mode pill (PRO-RATA / EXCL CACHE-READ) to the Tool Mix panel on `/breakdown`, with the second mode redistributing `cache_read_usd` 100% to the non-tool residual. Backend extracts the inline aggregation into a new `state.build_breakdown_tools()` function gaining a `mode=` parameter; frontend gains a third pill group that re-fetches with the chosen mode. Default behaviour is unchanged.

**Architecture:** Single-PR change on `feature/per-tool-cost` (no new branch). Backend: extract `api_breakdown_tools` inline loop into `state.build_breakdown_tools(turns, *, mode='prorata') -> list[dict]`; add module-level helper `_recompute_excl_cache_read(turn)`. Frontend: new pill row in `breakdown.html`, new `_bdToolMode` state + localStorage in `breakdown.js`, `fetchTools(range, mode)` signature update, hide pills when in TOKENS unit mode. No persistence, parser, or other-panel changes.

**Tech Stack:** Python 3.13, FastAPI, DuckDB-backed Turn model, pytest+httpx for async endpoint tests, vanilla ES modules + localStorage for the frontend. `uv` for dependency management.

**Spec:** `docs/superpowers/specs/2026-05-16-attribution-mode-toggle-design.md` (commit `5396bef`).

---

## File map

- **Create / modify (source):**
  - Modify `src/tokenol/serve/state.py` — add `_recompute_excl_cache_read(turn)` and `build_breakdown_tools(turns, *, mode='prorata') -> list[dict]`.
  - Modify `src/tokenol/serve/app.py` — `api_breakdown_tools` accepts `mode=`, validates it, delegates to `state.build_breakdown_tools`, returns `{range, mode, tools}`.
  - Modify `src/tokenol/serve/static/breakdown.html` — add `<span class="pill-row" id="bd-tools-mode-pills">` with two pills.
  - Modify `src/tokenol/serve/static/breakdown.js` — add `_bdToolMode` state, `_LS_BD_TOOL_MODE` key, extend `_wireUnitPills` with optional `dataAttr` parameter, wire new pill group, update `fetchTools` signature, update `refreshTools` to pass mode, hide pills when unit is `tokens`.

- **Modify (tests):**
  - `tests/test_serve_state.py` — new section for `_recompute_excl_cache_read` + `build_breakdown_tools`.
  - `tests/test_serve_app.py` — new endpoint tests covering mode echo, default, invalid, excl_cache_read total, empty window.

- **Modify (meta):**
  - `pyproject.toml`, `src/tokenol/__init__.py`, `uv.lock` — bump version `0.6.0` → `0.6.1` at the end.
  - `CHANGELOG.md` — new `0.6.1` section.
  - `RESUME.md` (gitignored, repo root) — updated at session end (not part of any commit).

---

## Task 1: `_recompute_excl_cache_read` helper

**Files:**
- Modify: `src/tokenol/serve/state.py` (add helper near `_accumulate_tool_costs`, ~line 1367)
- Test: `tests/test_serve_state.py` (append at end of file)

### Step 1: Write the failing tests

Append to `tests/test_serve_state.py`:

```python
# ---- _recompute_excl_cache_read tests ---------------------------------

from tokenol.metrics.cost import cost_for_turn  # noqa: E402  (imports gathered at top of file in real edit)
from tokenol.serve.state import _recompute_excl_cache_read  # noqa: E402


def _turn_with_costs(usage: Usage, model: str, tool_costs: dict[str, ToolCost],
                     *, unattr_input=0.0, unattr_output=0.0, unattr_cost=0.0,
                     ts: datetime | None = None) -> Turn:
    ts = ts or datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)
    return Turn(
        dedup_key=f"k-{ts.isoformat()}",
        timestamp=ts,
        session_id="s1",
        model=model,
        usage=usage,
        is_sidechain=False,
        stop_reason="tool_use",
        tool_costs=tool_costs,
        unattributed_input_tokens=unattr_input,
        unattributed_output_tokens=unattr_output,
        unattributed_cost_usd=unattr_cost,
    )


def test_recompute_excl_cache_read_drops_cache_read_from_input_pool():
    """A turn with 60% tool byte-share on input and 40% on output should see
    its cache_read_usd flow entirely to unattributed; tool cost becomes
    in_share * (input_usd + cache_creation_usd) + out_share * output_usd."""
    usage = Usage(
        input_tokens=1_000,
        output_tokens=10_000,
        cache_read_input_tokens=900_000,
        cache_creation_input_tokens=99_000,
    )
    # Pool = 1_000_000; 60% tool input share => 600_000 input_tokens stored.
    # 40% tool output share => 4_000 output_tokens stored.
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=600_000.0,
                         output_tokens=4_000.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    expected_read = (
        0.6 * (turn_cost.input_usd + turn_cost.cache_creation_usd)
        + 0.4 * turn_cost.output_usd
    )
    assert result.keys() == {"Read"}
    assert result["Read"] == pytest.approx(expected_read, rel=1e-9)


def test_recompute_excl_cache_read_handles_zero_input_pool():
    """input_token_pool == 0 should not raise; in_share is 0."""
    usage = Usage(input_tokens=0, output_tokens=100,
                  cache_read_input_tokens=0, cache_creation_input_tokens=0)
    tool_costs = {
        "Edit": ToolCost(tool_name="Edit", input_tokens=0.0,
                         output_tokens=80.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    # Only output side contributes; out_share = 80/100 = 0.8.
    assert result["Edit"] == pytest.approx(0.8 * turn_cost.output_usd, rel=1e-9)


def test_recompute_excl_cache_read_handles_zero_output_tokens():
    """output_tokens == 0 (rare but possible) should not raise; out_share is 0."""
    usage = Usage(input_tokens=1_000, output_tokens=0,
                  cache_read_input_tokens=9_000, cache_creation_input_tokens=0)
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=5_000.0,
                         output_tokens=0.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)
    # in_share = 5000/10000 = 0.5; out_share = 0.
    expected = 0.5 * (turn_cost.input_usd + turn_cost.cache_creation_usd)
    assert result["Read"] == pytest.approx(expected, rel=1e-9)


def test_recompute_excl_cache_read_empty_tool_costs():
    usage = Usage(input_tokens=1_000, output_tokens=1_000,
                  cache_read_input_tokens=5_000, cache_creation_input_tokens=0)
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs={})
    assert _recompute_excl_cache_read(turn) == {}


def test_recompute_excl_cache_read_linger_only_tool():
    """Tool with positive input share but zero output share (lingered from
    a prior turn) gets a non-zero cost on the input side alone."""
    usage = Usage(input_tokens=0, output_tokens=100,
                  cache_read_input_tokens=10_000, cache_creation_input_tokens=0)
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=3_000.0,
                         output_tokens=0.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    # in_share = 3000/10000 = 0.3; out_share = 0; cache_creation is 0.
    # Tool cost = 0.3 * (input_usd + 0) + 0 * output_usd = 0.3 * input_usd.
    expected = 0.3 * turn_cost.input_usd
    assert result["Read"] == pytest.approx(expected, rel=1e-9)
```

Move the two new `import` lines (and `from tokenol.metrics.cost import cost_for_turn` if absent) into the existing import block at the top of `tests/test_serve_state.py` instead of leaving them inline near the new section.

- [ ] **Step 2: Run the new tests — expect ImportError**

Run: `uv run pytest tests/test_serve_state.py::test_recompute_excl_cache_read_drops_cache_read_from_input_pool -v`
Expected: FAIL with `ImportError: cannot import name '_recompute_excl_cache_read' from 'tokenol.serve.state'`.

- [ ] **Step 3: Implement `_recompute_excl_cache_read`**

In `src/tokenol/serve/state.py`, after the existing imports near line 37 (no new import needed — `cost_for_turn` is already imported), insert the helper before `_accumulate_tool_costs` (around line 1365):

```python
def _recompute_excl_cache_read(turn: Turn) -> dict[str, float]:
    """Per-tool cost under the 'exclude cache_read' attribution mode.

    cache_read_usd is dropped from the per-tool input pool entirely; its share
    that would have gone to each tool flows into the non-tool residual computed
    by the caller. See docs/superpowers/specs/2026-05-16-attribution-mode-toggle-design.md.
    """
    usage = turn.usage
    input_token_pool = (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
    )
    output_token_count = usage.output_tokens
    turn_cost = cost_for_turn(turn.model, usage)
    input_pool_excl = turn_cost.input_usd + turn_cost.cache_creation_usd

    out: dict[str, float] = {}
    for name, tc in turn.tool_costs.items():
        in_share = (tc.input_tokens / input_token_pool) if input_token_pool else 0.0
        out_share = (tc.output_tokens / output_token_count) if output_token_count else 0.0
        out[name] = in_share * input_pool_excl + out_share * turn_cost.output_usd
    return out
```

- [ ] **Step 4: Run the new tests — expect PASS**

Run: `uv run pytest tests/test_serve_state.py -k "recompute_excl_cache_read" -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite to confirm nothing else broke**

Run: `uv run pytest -q`
Expected: 316 passed (311 baseline + 5 new).

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/state.py tests/test_serve_state.py
git commit -m "feat(serve): add _recompute_excl_cache_read attribution helper

Pure function: given a Turn, returns per-tool cost under the
'exclude cache_read' attribution mode. cache_read_usd is dropped
from the per-tool input pool entirely; the caller is responsible
for routing its share to the unattributed residual.

Unit-tested for the standard case, zero-input-pool and
zero-output-tokens edges, empty tool_costs, and the linger-only
case where a tool has input share but no output share."
```

---

## Task 2: `build_breakdown_tools` — pro-rata extraction + regression guard

**Files:**
- Modify: `src/tokenol/serve/state.py` (add new function after `_recompute_excl_cache_read`)
- Modify: `src/tokenol/serve/app.py` (refactor `api_breakdown_tools` to call the new function)
- Test: `tests/test_serve_state.py` (append)

### Step 1: Capture the current inline-loop behaviour as a regression-guard test

Append to `tests/test_serve_state.py`:

```python
# ---- build_breakdown_tools tests --------------------------------------

from tokenol.serve.state import build_breakdown_tools  # noqa: E402  (move into top imports)


def _btt_turns_fixture() -> list[Turn]:
    """Three synthetic turns: two assistant turns invoking real tools,
    one with an UNKNOWN_TOOL cost slice (folds into unattributed).
    Spans a small window so build_breakdown_tools can rank + collapse."""
    t0 = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)

    def _u(inp, out, cr, cc):
        return Usage(input_tokens=inp, output_tokens=out,
                     cache_read_input_tokens=cr, cache_creation_input_tokens=cc)

    return [
        Turn(
            dedup_key="k0", timestamp=t0, session_id="s1",
            model="claude-opus-4-7", usage=_u(100, 200, 800, 0),
            is_sidechain=False, stop_reason="tool_use",
            tool_use_count=2, tool_names=Counter({"Read": 1, "Bash": 1}),
            tool_costs={
                "Read": ToolCost(tool_name="Read", input_tokens=500.0,
                                 output_tokens=80.0, cost_usd=2.50),
                "Bash": ToolCost(tool_name="Bash", input_tokens=300.0,
                                 output_tokens=40.0, cost_usd=1.25),
            },
            unattributed_input_tokens=100.0, unattributed_output_tokens=80.0,
            unattributed_cost_usd=0.75,
        ),
        Turn(
            dedup_key="k1", timestamp=t1, session_id="s1",
            model="claude-opus-4-7", usage=_u(50, 300, 1000, 500),
            is_sidechain=False, stop_reason="end_turn",
            tool_use_count=1, tool_names=Counter({"Read": 1}),
            tool_costs={
                "Read": ToolCost(tool_name="Read", input_tokens=900.0,
                                 output_tokens=180.0, cost_usd=3.10),
            },
            unattributed_input_tokens=650.0, unattributed_output_tokens=120.0,
            unattributed_cost_usd=1.05,
        ),
        Turn(
            dedup_key="k2", timestamp=t2, session_id="s2",
            model="claude-sonnet-4-6", usage=_u(20, 80, 200, 0),
            is_sidechain=False, stop_reason="end_turn",
            tool_use_count=0, tool_names=Counter(),
            tool_costs={
                # __unknown__: unmatched tool_result bytes; folds into unattributed.
                "__unknown__": ToolCost(tool_name="__unknown__",
                                        input_tokens=100.0, output_tokens=0.0,
                                        cost_usd=0.40),
            },
            unattributed_input_tokens=120.0, unattributed_output_tokens=80.0,
            unattributed_cost_usd=0.55,
        ),
    ]


def test_build_breakdown_tools_prorata_matches_legacy_inline_loop():
    """Regression guard: build_breakdown_tools(turns, mode='prorata') must
    produce the same tools list (per-tool cost_usd, count, last_active, ordering,
    __unattributed__ residual) as the inline loop in api_breakdown_tools today."""
    turns = _btt_turns_fixture()

    # Reproduce the inline loop's behaviour to lock in the expected output.
    from tokenol.ingest.parser import UNATTRIBUTED_TOOL, UNKNOWN_TOOL
    from tokenol.metrics.rollups import _rank_dict_with_others
    from collections import Counter as _C
    cost_by_tool: dict[str, float] = {}
    tokens_by_tool: _C[str] = _C()
    unattr_cost = 0.0
    last_active: dict[str, datetime] = {}
    for t in turns:
        tokens_by_tool.update(t.tool_names)
        for name, tc in t.tool_costs.items():
            if name == UNKNOWN_TOOL:
                unattr_cost += tc.cost_usd
                continue
            cost_by_tool[name] = cost_by_tool.get(name, 0.0) + tc.cost_usd
            if name in t.tool_names and (name not in last_active or t.timestamp > last_active[name]):
                last_active[name] = t.timestamp
        unattr_cost += t.unattributed_cost_usd
    expected = _rank_dict_with_others(cost_by_tool, top_n=10)
    head = {r["name"] for r in expected if r["name"] != "other"}
    tail_calls = sum(c for n, c in tokens_by_tool.items() if n not in head)
    for row in expected:
        name = row["name"]
        if name == "other":
            row["count"] = tail_calls
        elif name in tokens_by_tool:
            row["count"] = tokens_by_tool[name]
        if name in last_active:
            row["last_active"] = last_active[name].isoformat()
        row["cost_usd"] = row.pop("value")
    expected.append({"name": UNATTRIBUTED_TOOL, "cost_usd": unattr_cost})

    got = build_breakdown_tools(turns, mode="prorata")
    assert got == expected
```

- [ ] **Step 2: Run the regression test — expect ImportError**

Run: `uv run pytest tests/test_serve_state.py::test_build_breakdown_tools_prorata_matches_legacy_inline_loop -v`
Expected: FAIL with `ImportError: cannot import name 'build_breakdown_tools'`.

- [ ] **Step 3: Add `build_breakdown_tools` to `state.py` (pro-rata-only first)**

In `src/tokenol/serve/state.py`, immediately after `_recompute_excl_cache_read` (added in Task 1), insert:

```python
def build_breakdown_tools(
    turns: list[Turn], *, mode: str = "prorata",
) -> list[dict]:
    """Build the ranked tool list for GET /api/breakdown/tools.

    Walks *turns* once, accumulating per-tool cost, invocation counts, and
    last-active timestamps. The `mode` parameter selects which attribution
    formula computes the per-tool cost contribution:

    - ``"prorata"`` (default) — sums the stored ``tc.cost_usd`` field; the
      unattributed residual is the sum of stored ``t.unattributed_cost_usd``.
    - ``"excl_cache_read"`` — recomputes per-tool cost via
      ``_recompute_excl_cache_read``; the unattributed residual is
      ``turn_total_usd - sum(per_tool_cost_for_real_tools)``.

    Returns the ranked tools list (top-10 + 'other' tail + ``__unattributed__``
    row). The caller wraps it in the response envelope.

    See docs/superpowers/specs/2026-05-16-attribution-mode-toggle-design.md.
    """
    cost_by_tool: dict[str, float] = {}
    tokens_by_tool: Counter[str] = Counter()
    unattr_cost = 0.0
    last_active: dict[str, datetime] = {}

    for t in turns:
        if t.is_interrupted:
            continue
        tokens_by_tool.update(t.tool_names)

        if mode == "excl_cache_read":
            recomputed = _recompute_excl_cache_read(t)
            turn_total = cost_for_turn(t.model, t.usage).total_usd
            real_sum = 0.0
            for name, cost in recomputed.items():
                if name == UNKNOWN_TOOL:
                    continue
                cost_by_tool[name] = cost_by_tool.get(name, 0.0) + cost
                real_sum += cost
                if name in t.tool_names and (
                    name not in last_active or t.timestamp > last_active[name]
                ):
                    last_active[name] = t.timestamp
            unattr_cost += turn_total - real_sum
        else:  # "prorata" (default; also the fallback for unknown values)
            for name, tc in t.tool_costs.items():
                if name == UNKNOWN_TOOL:
                    unattr_cost += tc.cost_usd
                    continue
                cost_by_tool[name] = cost_by_tool.get(name, 0.0) + tc.cost_usd
                if name in t.tool_names and (
                    name not in last_active or t.timestamp > last_active[name]
                ):
                    last_active[name] = t.timestamp
            unattr_cost += t.unattributed_cost_usd

    ranked = _rank_dict_with_others(cost_by_tool, top_n=10)
    head_names = {row["name"] for row in ranked if row["name"] != "other"}
    tail_call_sum = sum(c for n, c in tokens_by_tool.items() if n not in head_names)
    for row in ranked:
        name = row["name"]
        if name == "other":
            row["count"] = tail_call_sum
        elif name in tokens_by_tool:
            row["count"] = tokens_by_tool[name]
        if name in last_active:
            row["last_active"] = last_active[name].isoformat()
        row["cost_usd"] = row.pop("value")
    ranked.append({"name": UNATTRIBUTED_TOOL, "cost_usd": unattr_cost})

    return ranked
```

Verify `Counter` is already imported at the top of `state.py` (it is — used elsewhere). `_rank_dict_with_others`, `cost_for_turn`, `UNATTRIBUTED_TOOL`, `UNKNOWN_TOOL` are all already imported.

- [ ] **Step 4: Refactor `api_breakdown_tools` to call the new function**

In `src/tokenol/serve/app.py`, replace the body of `api_breakdown_tools` (lines 701–752) with:

```python
    @app.get("/api/breakdown/tools")
    async def api_breakdown_tools(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = _current_snapshot_result(request)
        since = (
            range_since(range, datetime.now(tz=timezone.utc).date())
            if range != "all"
            else None
        )
        filtered = [
            t for t in result.turns
            if not t.is_interrupted and (since is None or t.timestamp.date() >= since)
        ]
        tools = state.build_breakdown_tools(filtered, mode="prorata")
        return JSONResponse({"range": range, "tools": tools})
```

Notes:
- The inline loop filtered `is_interrupted` and `since`. `build_breakdown_tools` also skips interrupted turns internally; we pre-filter here so the `since` constraint stays at the call site (the function is mode-agnostic and turn-window-agnostic).
- Imports needed: `state` is already imported at the top of `app.py`. No new imports.
- This step does **not** add the `mode` query parameter yet — that's Task 4.

- [ ] **Step 5: Run the regression test — expect PASS**

Run: `uv run pytest tests/test_serve_state.py::test_build_breakdown_tools_prorata_matches_legacy_inline_loop -v`
Expected: PASS.

- [ ] **Step 6: Run the existing endpoint tests to confirm no behavioural regression**

Run: `uv run pytest tests/test_serve_app.py -k "breakdown_tools or breakdown" -v`
Expected: existing tests pass unchanged.

- [ ] **Step 7: Full suite + ruff**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: 317 passed (316 + 1 new test), ruff clean.

- [ ] **Step 8: Commit**

```bash
git add src/tokenol/serve/state.py src/tokenol/serve/app.py tests/test_serve_state.py
git commit -m "refactor(serve): extract /api/breakdown/tools loop into state.build_breakdown_tools

Pure extraction — the function returns the same tools list (per-tool
cost_usd, count, last_active, ordering, __unattributed__ residual)
that the inline api_breakdown_tools loop produced. Locked in by a
regression test that compares the new function against a reproduced
inline-loop reference output.

Preparation for the attribution-mode toggle: build_breakdown_tools
gains a 'mode' keyword param now; the per-mode branch is wired in
the next commit."
```

---

## Task 3: `build_breakdown_tools` — excl_cache_read mode

**Files:**
- (No source changes — the mode branch was added in Task 2's `state.py` edit.)
- Test: `tests/test_serve_state.py` (append)

### Step 1: Write the failing tests

Append to `tests/test_serve_state.py`:

```python
def test_build_breakdown_tools_excl_cache_read_total_invariant():
    """Sum of per-tool cost_usd + __unattributed__ cost_usd under
    mode='excl_cache_read' must equal sum of cost_for_turn().total_usd
    across the non-interrupted turns (total cost is mode-invariant)."""
    turns = _btt_turns_fixture()
    expected_total = sum(
        cost_for_turn(t.model, t.usage).total_usd for t in turns if not t.is_interrupted
    )

    got = build_breakdown_tools(turns, mode="excl_cache_read")
    got_total = sum(row["cost_usd"] for row in got)
    assert got_total == pytest.approx(expected_total, rel=1e-9)


def test_build_breakdown_tools_excl_cache_read_shifts_cache_read_to_unattributed():
    """The __unattributed__ row in excl_cache_read mode must be strictly larger
    than in prorata mode by an amount equal to (cache_read_usd that previously
    flowed to tools)."""
    turns = _btt_turns_fixture()
    pro = {row["name"]: row["cost_usd"] for row in build_breakdown_tools(turns, mode="prorata")}
    exc = {row["name"]: row["cost_usd"] for row in build_breakdown_tools(turns, mode="excl_cache_read")}

    assert exc["__unattributed__"] > pro["__unattributed__"]
    # Real-tool buckets shrink (cache_read share leaves them).
    for name in ("Read", "Bash"):
        if name in pro and name in exc:
            assert exc[name] <= pro[name] + 1e-12, f"{name} should not grow under excl mode"


def test_build_breakdown_tools_non_cost_fields_are_mode_invariant():
    """count, last_active, tool_count, and tool ordering must be identical
    between prorata and excl_cache_read modes (only cost_usd is recomputed)."""
    turns = _btt_turns_fixture()
    pro = build_breakdown_tools(turns, mode="prorata")
    exc = build_breakdown_tools(turns, mode="excl_cache_read")

    assert [r["name"] for r in pro] == [r["name"] for r in exc]
    for p, e in zip(pro, exc, strict=True):
        for key in ("count", "last_active", "tool_count"):
            if key in p or key in e:
                assert p.get(key) == e.get(key), f"{p['name']} {key} drift"


def test_build_breakdown_tools_excludes_interrupted_turns():
    """Interrupted turns must contribute to neither mode."""
    t_ok = _btt_turns_fixture()[0]
    t_interrupt = Turn(
        dedup_key="k-int",
        timestamp=datetime(2026, 5, 14, 13, 0, tzinfo=timezone.utc),
        session_id="s1", model="claude-opus-4-7", usage=Usage(),
        is_sidechain=False, stop_reason=None, is_interrupted=True,
        tool_use_count=1, tool_names=Counter({"Read": 1}),
    )
    got_pro = build_breakdown_tools([t_ok, t_interrupt], mode="prorata")
    got_exc = build_breakdown_tools([t_ok, t_interrupt], mode="excl_cache_read")
    # The Read row's count should be 1 (from the one healthy turn) in both.
    by_name_pro = {r["name"]: r for r in got_pro}
    by_name_exc = {r["name"]: r for r in got_exc}
    assert by_name_pro["Read"]["count"] == 1
    assert by_name_exc["Read"]["count"] == 1
```

- [ ] **Step 2: Run the new tests — expect PASS**

Run: `uv run pytest tests/test_serve_state.py -k "build_breakdown_tools" -v`
Expected: 5 passed (1 regression from Task 2 + 4 new).

(The mode branch was wired in Task 2's `state.py` edit; these tests exercise it.)

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: 321 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_serve_state.py
git commit -m "test(serve): cover build_breakdown_tools excl_cache_read mode

Four tests: total-cost mode-invariance, unattributed grows by the
cache_read share that previously went to tools, non-cost fields
(count, last_active, ordering) are mode-invariant, and interrupted
turns are excluded in both modes."
```

---

## Task 4: Wire `mode` query parameter into `/api/breakdown/tools`

**Files:**
- Modify: `src/tokenol/serve/app.py` (`api_breakdown_tools` accepts `mode`)
- Test: `tests/test_serve_app.py` (append)

### Step 1: Write the failing endpoint tests

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_tools_mode_excl_cache_read(tmp_path: Path) -> None:
    """GET /api/breakdown/tools?mode=excl_cache_read returns 200, echoes mode,
    and produces a total (sum of tools + unattributed) matching the summary."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=all&mode=excl_cache_read")
            summary = await client.get("/api/breakdown/summary?range=all")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "excl_cache_read"
    total_attributed = sum(t["cost_usd"] for t in body["tools"])
    assert total_attributed == pytest.approx(summary.json()["cost_usd"], rel=1e-6)


@pytest.mark.asyncio
async def test_breakdown_tools_default_mode_is_prorata(tmp_path: Path) -> None:
    """No mode param == mode=prorata; both produce equal cost_usd per tool
    and echo 'prorata' in the response envelope."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            no_mode = await client.get("/api/breakdown/tools?range=all")
            with_mode = await client.get("/api/breakdown/tools?range=all&mode=prorata")

    a, b = no_mode.json(), with_mode.json()
    assert a["mode"] == "prorata" and b["mode"] == "prorata"
    # Compare per-tool costs by name (ordering already mode-invariant).
    assert [t["name"] for t in a["tools"]] == [t["name"] for t in b["tools"]]
    for ta, tb in zip(a["tools"], b["tools"], strict=True):
        assert ta["cost_usd"] == tb["cost_usd"]


@pytest.mark.asyncio
async def test_breakdown_tools_invalid_mode_falls_back_to_prorata(tmp_path: Path) -> None:
    """Unknown mode value silently falls back to 'prorata' (matches the existing
    range-fallback pattern); response echoes the effective mode."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=all&mode=bogus")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "prorata"


@pytest.mark.asyncio
async def test_breakdown_tools_empty_window(tmp_path: Path) -> None:
    """A range covering no turns returns an empty tools list (or just an
    __unattributed__ row with cost 0); both modes handle it without errors."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 7d window with no turns in the past week (fixture is dated 2024-09).
            r_pro = await client.get("/api/breakdown/tools?range=7d")
            r_exc = await client.get("/api/breakdown/tools?range=7d&mode=excl_cache_read")

    assert r_pro.status_code == 200 and r_exc.status_code == 200
    # No real tools; __unattributed__ row carries 0.0.
    for body in (r_pro.json(), r_exc.json()):
        non_unattr = [t for t in body["tools"] if t["name"] != "__unattributed__"]
        assert non_unattr == []
        unattr = [t for t in body["tools"] if t["name"] == "__unattributed__"]
        assert unattr == [] or unattr[0]["cost_usd"] == 0.0
```

- [ ] **Step 2: Run the new endpoint tests — expect FAIL on `mode` field**

Run: `uv run pytest tests/test_serve_app.py -k "breakdown_tools_mode or breakdown_tools_default or breakdown_tools_invalid or breakdown_tools_empty_window" -v`
Expected: FAIL — the endpoint doesn't accept `mode` yet; response doesn't contain `mode` key.

- [ ] **Step 3: Wire `mode` into the endpoint**

In `src/tokenol/serve/app.py`, replace the `api_breakdown_tools` body again (the one written in Task 2 step 4) with:

```python
    @app.get("/api/breakdown/tools")
    async def api_breakdown_tools(
        request: Request, range: str = "30d", mode: str = "prorata",
    ):
        _validate_breakdown_range(range)
        # Silent fallback for unknown mode — mirrors the range fallback convention.
        if mode not in ("prorata", "excl_cache_read"):
            mode = "prorata"
        result = _current_snapshot_result(request)
        since = (
            range_since(range, datetime.now(tz=timezone.utc).date())
            if range != "all"
            else None
        )
        filtered = [
            t for t in result.turns
            if not t.is_interrupted and (since is None or t.timestamp.date() >= since)
        ]
        tools = state.build_breakdown_tools(filtered, mode=mode)
        return JSONResponse({"range": range, "mode": mode, "tools": tools})
```

- [ ] **Step 4: Run the endpoint tests — expect PASS**

Run: `uv run pytest tests/test_serve_app.py -k "breakdown_tools_mode or breakdown_tools_default or breakdown_tools_invalid or breakdown_tools_empty_window" -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: 325 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(serve): /api/breakdown/tools accepts mode= query param

Adds an optional mode= query parameter to /api/breakdown/tools.
Valid values: 'prorata' (default) and 'excl_cache_read'. Unknown
values fall back to 'prorata' silently, matching the existing
range-fallback convention. The response always echoes the effective
mode in a top-level 'mode' field (additive, schema-compatible).

Endpoint tests cover: excl_cache_read total invariance against
/api/breakdown/summary, default==prorata equivalence, invalid mode
fallback, and empty-window handling under both modes."
```

---

## Task 5: Frontend — HTML pill group

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html` (Tool Mix panel header)

### Step 1: Add the new pill row between period and unit pills

Open `src/tokenol/serve/static/breakdown.html`. Find the Tool Mix panel header (around line 142–155):

```html
        <h3 id="bp-tools-title">Tool Mix</h3>
        <span class="pill-row" id="bd-tools-period-pills" role="group" aria-label="Period">
          <span data-range="7d">7D</span>
          <span data-range="30d" class="on">30D</span>
          <span data-range="90d">90D</span>
          <span data-range="all">All</span>
        </span>
        <span class="pill-row" id="bd-tools-unit-pills" role="group" aria-label="Unit">
          <span data-bdunit="tokens">Tokens</span>
          <span data-bdunit="cost" class="on">$</span>
        </span>
        <span class="chart-subheading" id="bp-tools-sub"></span>
```

Insert a new pill row between the period and unit groups so the final markup reads:

```html
        <h3 id="bp-tools-title">Tool Mix</h3>
        <span class="pill-row" id="bd-tools-period-pills" role="group" aria-label="Period">
          <span data-range="7d">7D</span>
          <span data-range="30d" class="on">30D</span>
          <span data-range="90d">90D</span>
          <span data-range="all">All</span>
        </span>
        <span class="pill-row" id="bd-tools-mode-pills" role="group" aria-label="Attribution mode">
          <span data-bdmode="prorata" class="on"
                title="Pro-rata: every dollar of input + cache + output is split by visible-byte share across tools.">Pro-rata</span>
          <span data-bdmode="excl_cache_read"
                title="Cache-read excluded: cost of carrying tool output across turns goes to non-tool, not the tool.">Excl cache-read</span>
        </span>
        <span class="pill-row" id="bd-tools-unit-pills" role="group" aria-label="Unit">
          <span data-bdunit="tokens">Tokens</span>
          <span data-bdunit="cost" class="on">$</span>
        </span>
        <span class="chart-subheading" id="bp-tools-sub"></span>
```

The label text uses sentence case (`Pro-rata` / `Excl cache-read`) to match the other pill rows (`7D / 30D / Tokens / $`); the spec's `PRO-RATA / EXCL CACHE-READ` are conceptual labels — actual UI uses normal capitalisation so this row reads consistently with the others. CSS already styles pills uppercase-ish via existing rules; verify visual fit in the smoke step (Task 9).

- [ ] **Step 2: Commit (HTML only, no functional change yet)**

```bash
git add src/tokenol/serve/static/breakdown.html
git commit -m "feat(ui): add attribution-mode pill group to Tool Mix panel header

New pill row #bd-tools-mode-pills with two options: 'Pro-rata' (default,
active) and 'Excl cache-read'. Title-attribute tooltips explain each
formula. Wiring follows in the next commit."
```

---

## Task 6: Frontend — state, fetch, wiring

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

### Step 1: Add the localStorage key, state variable, and helper extension

Open `src/tokenol/serve/static/breakdown.js`.

**Edit 1 (around line 35):** add `_bdToolMode` next to the existing `_bdToolUnit` state.

Find:

```javascript
let _bdToolUnit    = localStorage.getItem(_LS_BD_TOOL_UNIT)    || 'cost';
```

Replace with:

```javascript
let _bdToolUnit    = localStorage.getItem(_LS_BD_TOOL_UNIT)    || 'cost';
const _LS_BD_TOOL_MODE = 'tokenol.breakdown.toolMode';
let _bdToolMode    = localStorage.getItem(_LS_BD_TOOL_MODE)    || 'prorata';
```

(Const + let mirror the existing patterns. Keep the key declaration adjacent so future readers can spot the family.)

**Edit 2 (function `_wireUnitPills` near line 638):** add an optional `dataAttr` parameter so it can drive either `data-bdunit` or `data-bdmode`.

Find:

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

Replace with:

```javascript
function _wireUnitPills(groupId, lsKey, getCurrent, setCurrent, onChange, dataAttr = 'bdunit') {
  const group = document.getElementById(groupId);
  if (!group) return;
  const selector = `[data-${dataAttr}]`;
  const readVal = (el) => el.dataset[dataAttr];
  // Sync initial DOM state to persisted value.
  group.querySelectorAll(selector).forEach(b => {
    b.classList.toggle('on', readVal(b) === getCurrent());
  });
  group.querySelectorAll(selector).forEach(btn => {
    btn.addEventListener('click', () => {
      const next = readVal(btn);
      if (next === getCurrent()) return;
      setCurrent(next);
      localStorage.setItem(lsKey, next);
      group.querySelectorAll(selector).forEach(b =>
        b.classList.toggle('on', readVal(b) === next),
      );
      onChange(next);
    });
  });
}
```

The default value `'bdunit'` means the four existing call sites continue to work unchanged.

**Edit 3 (function `fetchTools` near line 411):** add a `mode` argument and thread it into the URL.

Find:

```javascript
async function fetchTools(range) {
  const resp = await fetch(`/api/breakdown/tools?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`tools ${resp.status}`);
  return resp.json();
}
```

Replace with:

```javascript
async function fetchTools(range, mode) {
  const url = `/api/breakdown/tools?range=${encodeURIComponent(range)}&mode=${encodeURIComponent(mode)}`;
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`tools ${resp.status}`);
  return resp.json();
}
```

**Edit 4 (function `refreshTools` near line 738):** pass `_bdToolMode` to `fetchTools`.

Find:

```javascript
async function refreshTools() {
  try {
    renderToolMix(await fetchTools(_bdToolPeriod));
  } catch (err) { console.error('[breakdown] tools refresh', err); }
}
```

Replace with:

```javascript
async function refreshTools() {
  try {
    renderToolMix(await fetchTools(_bdToolPeriod, _bdToolMode));
  } catch (err) { console.error('[breakdown] tools refresh', err); }
}
```

**Edit 5 (the initial `refreshAll` call near line 715):** also update the parallel fetch in `refreshAll`.

Find:

```javascript
      fetchTools(_bdToolPeriod),
```

Replace with:

```javascript
      fetchTools(_bdToolPeriod, _bdToolMode),
```

**Edit 6 (wiring section near line 792):** wire the new mode pills. Insert this block immediately after the existing `_wireUnitPills('bd-tools-unit-pills', …)` call (line 792–796):

```javascript
_wireUnitPills('bd-tools-mode-pills', _LS_BD_TOOL_MODE,
  () => _bdToolMode,
  v  => { _bdToolMode = v; },
  () => { refreshTools(); },
  'bdmode',
);
```

- [ ] **Step 2: Hard-refresh the running server and verify the pills work in the browser**

Run: from the worktree, `uv run python -m tokenol serve --port 8787` (or rely on the user's already-running server if they prefer; the static file is served fresh on every load).

Open `http://127.0.0.1:8787/breakdown` in the browser. Tool Mix panel should now show three pill groups: period (7D/30D/90D/All), attribution mode (Pro-rata / Excl cache-read), and unit (Tokens/$). The Pro-rata pill should be highlighted (active class). Click "Excl cache-read" — the panel should refetch (briefly), the bars should shrink, and the subtitle's `$X tool cost` should drop while `$Y non-tool` should grow.

Reload the page (Cmd-R / Ctrl-R). The attribution-mode pill should persist as "Excl cache-read" across the reload.

Click "Pro-rata" to switch back. Confirm the numbers return to today's values.

Confirm scorecards, daily charts, by-project, by-model, and the tool detail page (`/tool/Read`) are unchanged when toggling — the mode pill is panel-local.

- [ ] **Step 3: Run linting**

Run: `uv run ruff check src tests`
Expected: clean.

(There is no JS linting in this repo; the smoke test in step 2 is the verification.)

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat(ui): wire attribution-mode pill group on Tool Mix panel

Adds _bdToolMode state (default 'prorata') persisted to
localStorage at tokenol.breakdown.toolMode. fetchTools now takes
(range, mode) and threads both into the /api/breakdown/tools call.

_wireUnitPills gains an optional dataAttr parameter so it can drive
either data-bdunit or data-bdmode selection. The four existing call
sites keep their default 'bdunit' behaviour; the new call site for
bd-tools-mode-pills passes 'bdmode'.

Clicking a mode pill refetches via refreshTools() and re-renders the
Tool Mix panel only — no other panel reacts to the toggle."
```

---

## Task 7: Frontend — hide mode pills when unit is TOKENS

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

### Step 1: Hide / restore the mode pill row when unit toggles

In `breakdown.js`, find the existing `_wireUnitPills('bd-tools-unit-pills', _LS_BD_TOOL_UNIT, …)` wiring (line ~792). The `onChange` callback there is `() => renderToolMix(null)`. We need an additional side effect when the unit changes: toggle the visibility of `#bd-tools-mode-pills`.

Replace the existing block:

```javascript
_wireUnitPills('bd-tools-unit-pills', _LS_BD_TOOL_UNIT,
  () => _bdToolUnit,
  v  => { _bdToolUnit = v; },
  () => renderToolMix(null),
);
```

With:

```javascript
function _syncToolModePillsVisibility() {
  const el = document.getElementById('bd-tools-mode-pills');
  if (!el) return;
  el.style.display = _bdToolUnit === 'cost' ? '' : 'none';
}

_wireUnitPills('bd-tools-unit-pills', _LS_BD_TOOL_UNIT,
  () => _bdToolUnit,
  v  => { _bdToolUnit = v; },
  () => { _syncToolModePillsVisibility(); renderToolMix(null); },
);

// Apply initial visibility on page load.
_syncToolModePillsVisibility();
```

- [ ] **Step 2: Smoke-test in the browser**

Reload `http://127.0.0.1:8787/breakdown`.

1. Default state: unit is `$`, mode pill group is visible. ✓
2. Click `Tokens` — mode pill group disappears. Bars switch to token counts. ✓
3. Click `$` — mode pill group reappears with the previously-selected mode. ✓
4. Reload page in `Tokens` state — mode pill group stays hidden on load (visibility derives from `_bdToolUnit`). ✓

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat(ui): hide attribution-mode pills when Tool Mix is in tokens unit

Attribution mode is a cost-only concept; token counts aren't
redistributed across modes. Showing a disabled control would
invite confusion, so we hide the pill group entirely when the
unit pills are on 'tokens'. Switching back to '\$' restores the
group with the persisted selection."
```

---

## Task 8: Version bump + CHANGELOG

**Files:**
- Modify: `pyproject.toml` (version)
- Modify: `src/tokenol/__init__.py` (`__version__`)
- Regenerate: `uv.lock`
- Modify: `CHANGELOG.md`

### Step 1: Bump version 0.6.0 → 0.6.1

In `pyproject.toml`, find:

```toml
version = "0.6.0"
```

Replace with:

```toml
version = "0.6.1"
```

In `src/tokenol/__init__.py`, find:

```python
__version__ = "0.6.0"
```

Replace with:

```python
__version__ = "0.6.1"
```

- [ ] **Step 2: Regenerate uv.lock**

Run: `uv lock`
Expected: lock file updates the tokenol version reference.

- [ ] **Step 3: Add CHANGELOG entry**

In `CHANGELOG.md`, find the existing `## 0.6.0` section and insert a new section **above** it:

```markdown
## 0.6.1

### Added

- **Attribution mode toggle on the Tool Mix panel.** Two-position pill group in the panel header lets you switch between the existing pro-rata cost split and a new "exclude cache-read" lens. The second mode routes cache_read_usd 100% to the non-tool residual instead of distributing it pro-rata across visible tool bytes, answering "what do tools cost excluding the cost of keeping their output around for subsequent turns?" Selection persists in localStorage; hidden when the panel is displaying token counts (mode is a cost-only concept).
- `mode=` query parameter on `GET /api/breakdown/tools` — accepts `prorata` (default) or `excl_cache_read`. Unknown values fall back to `prorata` silently. The response echoes the effective `mode` in a new top-level field.
- `state.build_breakdown_tools(turns, *, mode='prorata') -> list[dict]` — extracts the previously-inline aggregation loop from `api_breakdown_tools` into a unit-testable module-level function.

### Changed

- `_wireUnitPills` (in `breakdown.js`) gains an optional `dataAttr` parameter so a single helper can drive both `data-bdunit` and `data-bdmode` pill groups. Existing call sites unchanged.

### Notes

- No persistence changes — the mode toggle is purely a presentation-layer reinterpretation of already-stored per-turn `tool_costs` data.
- No changes to other panels — scorecards, daily charts, by-project, by-model, and the tool detail page (`/tool/{name}`) all stay on pro-rata regardless of the toggle.
```

- [ ] **Step 4: Run full pytest + ruff for the final verification**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: 325 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/tokenol/__init__.py uv.lock CHANGELOG.md
git commit -m "chore(release): 0.6.1 — Tool Mix attribution-mode toggle"
```

---

## Task 9: Final integration smoke + RESUME.md

**Files:**
- Modify: `/home/ff235/dev/claude_rate_limit/RESUME.md` (gitignored, repo root — outside this worktree)

### Step 1: Final manual smoke

Confirm in a browser at `http://127.0.0.1:8787/breakdown`:

1. Tool Mix shows three pill groups (period, attribution mode, unit) and the subtitle in $ mode.
2. Clicking "Excl cache-read" — bars shrink, subtitle's tool cost drops, non-tool grows. Sum stays roughly constant.
3. Reload — mode persists.
4. Switch unit to Tokens — mode pills hide.
5. Switch back to $ — mode pills reappear with previously-selected mode.
6. `/tool/Read` (tool detail page) shows the same Read cost regardless of mode (out of scope confirmation).
7. Hover each mode pill — tooltip explanation shows.

### Step 2: Sanity-check the running server is on 0.6.1

Run: `curl -s http://127.0.0.1:8787/api/version 2>/dev/null || curl -s http://127.0.0.1:8787/api/snapshot | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('config'))"`

(If the server is still running the 0.6.0 build, restart it: kill the old process and start `uv run python -m tokenol serve --port 8787` from the worktree.)

### Step 3: Final pytest + ruff (one more time, post-CHANGELOG)

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: 325 passed, ruff clean.

### Step 4: Confirm commit ladder

Run: `git log --oneline 5396bef..HEAD`
Expected (8 commits since the spec amendment):

```
<sha> chore(release): 0.6.1 — Tool Mix attribution-mode toggle
<sha> feat(ui): hide attribution-mode pills when Tool Mix is in tokens unit
<sha> feat(ui): wire attribution-mode pill group on Tool Mix panel
<sha> feat(ui): add attribution-mode pill group to Tool Mix panel header
<sha> feat(serve): /api/breakdown/tools accepts mode= query param
<sha> test(serve): cover build_breakdown_tools excl_cache_read mode
<sha> refactor(serve): extract /api/breakdown/tools loop into state.build_breakdown_tools
<sha> feat(serve): add _recompute_excl_cache_read attribution helper
```

### Step 5: Update RESUME.md

Edit `/home/ff235/dev/claude_rate_limit/RESUME.md` (outside the worktree) — bump branch status to "0.6.1 ready", note the new commits and test count, flag any remaining smoke-test feedback. Lean update; refer to CHANGELOG and the spec for detail.

### Step 6: Hand back to the user

The branch is now 0.6.1-ready. The release sequence (push → tag → PyPI publish → merge → worktree cleanup) is the same as the 0.6.0 ladder (commit f888675 / b6d9234 reference points) and is the user's call — do not auto-push.

---

## Self-review

(Performed inline after writing — fixes applied as found.)

**Spec coverage check:**

| Spec requirement | Plan task |
|---|---|
| Modes table (prorata + excl_cache_read formulas) | Task 1, Task 3 (implements both branches) |
| `_recompute_excl_cache_read(turn)` helper | Task 1 |
| `state.build_breakdown_tools(turns, *, mode)` extraction | Task 2 |
| Endpoint `mode=` query parameter | Task 4 |
| Endpoint echoes `mode` in response envelope | Task 4 |
| Invalid mode falls back to `prorata` silently | Task 4 (endpoint test + impl) |
| Pro-rata path unchanged (regression guard) | Task 2 (regression test) |
| `_accumulate_tool_costs` not modified, not called | Task 2 (uses `_rank_dict_with_others` + inline accumulation) |
| `UNKNOWN_TOOL` folding preserved under both modes | Task 2 / Task 3 (both branches handle `__unknown__`) |
| Linger-only tools surface under both modes | Task 3 (excl_cache_read iterates `t.tool_costs.items()`) |
| Header layout: three pill groups | Task 5 |
| `localStorage` key `tokenol.breakdown.toolMode` | Task 6 |
| Pills hidden in TOKENS unit mode | Task 7 |
| Tooltips on pills | Task 5 (HTML `title=`) |
| Subtitle wording unchanged | Task 5 / Task 6 (no subtitle change needed) |
| Unit tests 1–8 | Task 1 (tests 1–5), Task 2 (test 8), Task 3 (tests 6–7) |
| Endpoint tests 9–12 | Task 4 |
| Frontend manual smoke 13–17 | Task 6 (steps 2), Task 7 (step 2), Task 9 (step 1) |
| Version bump + CHANGELOG | Task 8 |

**Placeholder scan:** no "TBD", "TODO", "handle edge cases", or "similar to Task N". Code blocks contain actual code with real names and signatures.

**Type / signature consistency:**
- `_recompute_excl_cache_read(turn: Turn) -> dict[str, float]` — same signature in spec, Task 1 impl, Task 2 (call site inside `build_breakdown_tools`).
- `build_breakdown_tools(turns: list[Turn], *, mode: str = "prorata") -> list[dict]` — same signature in spec, Task 2 impl, Task 3 tests, Task 4 endpoint call site.
- `fetchTools(range, mode)` — added in Task 6 (signature change) and used in Task 6 step 2 wiring and the `refreshAll` parallel fetch.
- `_wireUnitPills(groupId, lsKey, getCurrent, setCurrent, onChange, dataAttr='bdunit')` — extended in Task 6; used with default in four existing call sites (untouched) and with explicit `'bdmode'` in the new call site (Task 6).
- localStorage key `tokenol.breakdown.toolMode` — declared in Task 6 step 1 edit 1; referenced in the wiring call in Task 6 step 1 edit 6 via the new `_LS_BD_TOOL_MODE` const.

No drift found.
