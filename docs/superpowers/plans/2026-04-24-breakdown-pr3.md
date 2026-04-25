# Breakdown tab — PR3 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the **Tools section** of `/breakdown` — a full-width *Tool mix* horizontal bar chart (top-10 + `others`) — plus the invasive ingest change required to feed it (per-turn tool-name capture). Fold in the PR2 final-review deferrals: per-chart sidecar unification, a shared bucket-and-sum helper for the `/api/breakdown/*` endpoints, a multi-project/multi-model/multi-tool test fixture, and live prefs plumbing for the cache-health thresholds.

**Architecture:** The parser learns to record `tool_use` block names as `Counter[str]` and threads that through `RawEvent` → `Turn` → `SessionRollup`. A new `build_tool_mix(session_rollups, top_n)` aggregator ranks tool names globally and collapses the tail into a single `others` row. A new `GET /api/breakdown/tools` endpoint filters by range and returns `[{tool, count}]`. The frontend adds a Tools section to `breakdown.html` with a Chart.js horizontal bar chart, plumbs it into the existing SSE-driven `refreshAll` pipeline, and moves the `_byProjectCwdB64` / `_byProjectHitRate` / `_byModelNames` sidecars onto each chart instance. Cache-health thresholds are fetched once from `/api/prefs` on page load and the project-bar caption re-renders to match.

**Tech Stack:** Python 3.13 + `collections.Counter` · FastAPI · pytest / pytest-asyncio · vanilla ES modules · Chart.js 4.4.7 (already loaded in PR1) · existing CSS design tokens in `src/tokenol/serve/static/styles.css`.

---

## Scope

**In scope for this PR:**

- Parser extension: `RawEvent.tool_names: Counter[str]`, populated from `message.content[*]` `tool_use` blocks.
- `Turn.tool_names: Counter[str]`, wired through `builder.build_turns`.
- `SessionRollup.tool_mix: Counter[str]`, summed across turns in `build_session_rollup`.
- `metrics/rollups.py:build_tool_mix(session_rollups, top_n=10) -> list[dict]` with top-N + `others` collapse.
- `GET /api/breakdown/tools?range=…` endpoint.
- Shared `_bucket_turns(sessions, key_fn, since, fields)` helper in `app.py`; refactor `by-project` and `by-model` to use it alongside the new endpoint.
- New `tests/fixtures/multi.jsonl` with 3 sessions, 2 projects, 2 models, and 4 distinct tool names.
- Tools section in `breakdown.html`: section heading + full-width `.breakdown-grid--1` panel.
- `renderToolMix(...)` — Chart.js horizontal `bar` chart (`indexAxis: 'y'`), palette[0], tooltip with call counts.
- SSE-tick in-place update for the new panel (wired into `refreshAll`).
- Move `_byProjectCwdB64` / `_byProjectHitRate` / `_byModelNames` onto `chart.$tokenol` sidecars (per-chart, not module globals).
- Fetch `/api/prefs` once on page load and replace hard-coded `HIT_PCT_GREEN` / `HIT_PCT_RED` constants with values from `prefs.thresholds`. Re-render the Tokens-by-Project caption after prefs arrive.
- Python unit / integration tests for parser, rollups, and the new endpoint.

**Out of scope (explicitly deferred):**

- Drill-down from the Tool mix chart. Tools are not a first-class entity in tokenol's routing model; clicking a bar is a no-op.
- Sidechain filtering in the tools aggregation. Match the existing `by-project` / `by-model` convention: exclude interrupted turns only.
- Persisting tool-mix preferences across server restarts.
- Any changes to the Overview page, drill-down pages, or the PR1/PR2 charts beyond the sidecar refactor.
- Renaming `tool_use_count` — keep it untouched, populated from `sum(tool_names.values())` for backwards compat.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/tokenol/ingest/parser.py` | Modify | Replace `_count_tool_blocks` with `_extract_tool_blocks(content) -> tuple[Counter[str], int, int]` that returns `(tool_names, tool_use_total, tool_error_count)`. Unnamed `tool_use` blocks drop out of `tool_names` but still count in `tool_use_total` so the legacy `tool_use_count` field preserves its semantics. Add `tool_names` kwarg on every `RawEvent(...)` call. |
| `src/tokenol/model/events.py` | Modify | Add `tool_names: Counter[str] = field(default_factory=Counter)` to `RawEvent` and `Turn`. Import `Counter`. |
| `src/tokenol/ingest/builder.py` | Modify | Pass `ev.tool_names` through to `Turn(...)`. |
| `src/tokenol/metrics/rollups.py` | Modify | Add `tool_mix: Counter[str] = field(default_factory=Counter)` to `SessionRollup`. Populate in `build_session_rollup`. Add `build_tool_mix(session_rollups, top_n=10) -> list[dict]`. |
| `src/tokenol/serve/app.py` | Modify | Add `/api/breakdown/tools` endpoint. Extract `_bucket_turns` helper used by all three existing aggregating endpoints. |
| `src/tokenol/serve/static/breakdown.html` | Modify | Append a `Tools` section heading + a `.breakdown-grid.breakdown-grid--1` with one full-width panel containing `<canvas id="chart-tools">`. |
| `src/tokenol/serve/static/breakdown.js` | Modify | `fetchTools` + `renderToolMix` + wire into `refreshAll`. Unify existing sidecar arrays onto `chart.$tokenol`. Fetch `/api/prefs` once, replace `HIT_PCT_GREEN` / `HIT_PCT_RED` constants with module-level vars, re-render the project caption after prefs load. |
| `src/tokenol/serve/static/styles.css` | Modify | Add `.breakdown-grid--1 { grid-template-columns: 1fr }` (stacks by default on narrow viewport). |
| `tests/fixtures/multi.jsonl` | Create | 3-session multi-project / multi-model / multi-tool fixture. |
| `tests/test_parser.py` | Modify | Tool-name extraction tests (single, multi, unnamed, mixed, zero). |
| `tests/test_metrics.py` | Modify | `build_tool_mix` ranking and top-N + `others` tests. |
| `tests/test_serve_app.py` | Modify | `/api/breakdown/tools` shape, top-N, unknown range, interrupted exclusion. |

**No new source files beyond the test fixture.** No new dependencies.

---

## Task 1: Capture tool names in parser

Extend the parser so each `RawEvent` carries a `Counter[str]` of tool names used in that turn. Keep `tool_use_count` intact for downstream consumers that haven't been migrated (it becomes `sum(tool_names.values())`).

**Files:**
- Modify: `src/tokenol/model/events.py`
- Modify: `src/tokenol/ingest/parser.py`
- Test: `tests/test_parser.py`

- [ ] **Step 1: Write failing parser tests**

Append to `tests/test_parser.py`:

```python
from collections import Counter


def test_parse_captures_tool_names_single_use(tmp_path):
    import json
    p = tmp_path / "tools.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": "s1", "requestId": "r1", "uuid": "e1", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {
            "id": "m1", "role": "assistant", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "name": "Read", "input": {}},
            ],
        },
    }) + "\n")

    events = list(parse_file(p))
    assert events[0].tool_names == Counter({"Read": 1})
    assert events[0].tool_use_count == 1


def test_parse_captures_tool_names_multiple_per_turn(tmp_path):
    import json
    p = tmp_path / "tools_multi.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": "s1", "requestId": "r1", "uuid": "e1", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {
            "id": "m1", "role": "assistant", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "name": "Read",  "input": {}},
                {"type": "tool_use", "name": "Read",  "input": {}},
                {"type": "tool_use", "name": "Edit",  "input": {}},
                {"type": "text",     "text": "done"},
            ],
        },
    }) + "\n")

    events = list(parse_file(p))
    assert events[0].tool_names == Counter({"Read": 2, "Edit": 1})
    assert events[0].tool_use_count == 3


def test_parse_skips_unnamed_tool_block(tmp_path):
    """`tool_use` blocks without a `name` field are junk — skip them but don't crash."""
    import json
    p = tmp_path / "tools_unnamed.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": "s1", "requestId": "r1", "uuid": "e1", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {
            "id": "m1", "role": "assistant", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "input": {}},  # no "name" key
                {"type": "tool_use", "name": "",  "input": {}},  # empty name
                {"type": "tool_use", "name": "Bash", "input": {}},
            ],
        },
    }) + "\n")

    events = list(parse_file(p))
    assert events[0].tool_names == Counter({"Bash": 1})
    # tool_use_count still counts all tool_use blocks, per existing semantics.
    assert events[0].tool_use_count == 3


def test_parse_zero_tools(tmp_path):
    """A text-only assistant turn yields an empty Counter, not None."""
    events = list(parse_file(FIXTURES / "basic.jsonl"))
    assert events[0].tool_names == Counter()
    assert events[0].tool_use_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_parser.py -v`
Expected: FAIL — `AttributeError: 'RawEvent' object has no attribute 'tool_names'`.

- [ ] **Step 3: Add `tool_names` field to `RawEvent`**

Edit `src/tokenol/model/events.py`. Add the import near the top:

```python
from collections import Counter
```

In the `RawEvent` dataclass, right after the existing `tool_use_count` / `tool_error_count` block (around line 49), add:

```python
    # Tool names (parsed from message.content tool_use blocks)
    tool_names: Counter[str] = field(default_factory=Counter)
```

- [ ] **Step 4: Extend parser to extract tool names**

Edit `src/tokenol/ingest/parser.py`. Replace the existing `_count_tool_blocks` function:

```python
def _extract_tool_blocks(content: list) -> tuple[Counter[str], int, int]:
    """Return (tool_names, tool_use_total, tool_error_count) from a content list.

    Named `tool_use` blocks are keyed by their `name` in the Counter. Unnamed
    or empty-name blocks are skipped from `tool_names` but still bump
    `tool_use_total` so the legacy `tool_use_count` field preserves its
    "count every tool_use block" semantics.
    """
    tool_names: Counter[str] = Counter()
    tool_use_total = 0
    tool_error = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            tool_use_total += 1
            name = block.get("name")
            if isinstance(name, str) and name:
                tool_names[name] += 1
        elif btype == "tool_result" and block.get("is_error") is True:
            tool_error += 1
    return tool_names, tool_use_total, tool_error
```

Also add the `Counter` import at the top of `parser.py`:

```python
from collections import Counter
```

Now update `parse_file` to use the new helper. Replace the existing block:

```python
            tool_use_count, tool_error_count = _count_tool_blocks(content)
```

with:

```python
            tool_names, tool_use_count, tool_error_count = _extract_tool_blocks(content)
```

And add `tool_names=tool_names,` to the `RawEvent(...)` constructor call in `parse_file` (next to `tool_use_count=tool_use_count`).

- [ ] **Step 5: Run parser tests**

Run: `.venv/bin/python -m pytest tests/test_parser.py -v`
Expected: all green, including the four new tests.

- [ ] **Step 6: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: 194 + 4 = 198 passing. (If any non-parser test fails, investigate — most likely a stale RawEvent constructor somewhere.)

- [ ] **Step 7: Commit**

```bash
git add src/tokenol/model/events.py src/tokenol/ingest/parser.py tests/test_parser.py
git commit -m "feat: capture tool names per turn in parser

Parser now extracts tool_use block names as Counter[str] on RawEvent.
Unnamed tool_use blocks don't crash — they're counted in the legacy
tool_use_count but skipped from tool_names. Backwards-compatible: every
existing consumer of tool_use_count keeps its value."
```

---

## Task 2: Thread `tool_names` through `Turn`

Wire the new field from `RawEvent` through `build_turns` into `Turn`. One line in the model, one line in the builder.

**Files:**
- Modify: `src/tokenol/model/events.py`
- Modify: `src/tokenol/ingest/builder.py`
- Test: `tests/test_parser.py` (existing tests already cover via integration)

- [ ] **Step 1: Write a failing builder test**

Append to `tests/test_parser.py`:

```python
def test_builder_propagates_tool_names(tmp_path):
    """tool_names on RawEvent must survive through build_turns to Turn."""
    import json
    from tokenol.ingest.builder import build_turns
    p = tmp_path / "sess.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-04-14T10:00:00Z",
        "sessionId": "s1", "requestId": "r1", "uuid": "e1", "isSidechain": False,
        "model": "claude-opus-4-7",
        "message": {
            "id": "m1", "role": "assistant", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [
                {"type": "tool_use", "name": "Grep", "input": {}},
                {"type": "tool_use", "name": "Grep", "input": {}},
            ],
        },
    }) + "\n")

    turns = build_turns([p])
    assert len(turns) == 1
    assert turns[0].tool_names == Counter({"Grep": 2})
    assert turns[0].tool_use_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_parser.py::test_builder_propagates_tool_names -v`
Expected: FAIL — `Turn` has no `tool_names` attribute.

- [ ] **Step 3: Add `tool_names` to `Turn`**

Edit `src/tokenol/model/events.py`. In the `Turn` dataclass (line 59), after `tool_error_count: int = 0`, add:

```python
    tool_names: Counter[str] = field(default_factory=Counter)
```

(The `Counter` import was added in Task 1.)

- [ ] **Step 4: Wire it in `build_turns`**

Edit `src/tokenol/ingest/builder.py`. In the `Turn(...)` constructor (around line 28), add:

```python
                tool_names=ev.tool_names,
```

next to `tool_use_count=ev.tool_use_count,`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_parser.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/model/events.py src/tokenol/ingest/builder.py tests/test_parser.py
git commit -m "feat: propagate tool_names through build_turns to Turn"
```

---

## Task 3: `SessionRollup.tool_mix` + `build_tool_mix` helper

Aggregate tool names up through the session rollup and expose a top-N ranking helper that optionally collapses the tail into `others`.

**Files:**
- Modify: `src/tokenol/metrics/rollups.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_metrics.py`:

```python
from collections import Counter

from tokenol.metrics.rollups import build_session_rollup, build_tool_mix
from tokenol.model.events import Session, Turn, Usage


def _turn_with_tools(tool_names: dict[str, int]) -> Turn:
    from datetime import datetime, timezone
    return Turn(
        dedup_key="k",
        timestamp=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        session_id="s1",
        model="claude-opus-4-7",
        usage=Usage(input_tokens=1, output_tokens=1),
        is_sidechain=False,
        stop_reason="tool_use",
        tool_use_count=sum(tool_names.values()),
        tool_names=Counter(tool_names),
    )


def test_session_rollup_sums_tool_mix():
    s = Session(
        session_id="s1", source_file="x.jsonl", is_sidechain=False, cwd="/p",
        turns=[
            _turn_with_tools({"Read": 2, "Edit": 1}),
            _turn_with_tools({"Read": 1, "Bash": 3}),
        ],
    )
    sr = build_session_rollup(s)
    assert sr.tool_mix == Counter({"Bash": 3, "Read": 3, "Edit": 1})


def test_build_tool_mix_ranks_descending():
    srs = [
        # Fabricate minimal SessionRollups via build_session_rollup on empty-usage
        # sessions; easier to just construct Session and roll up.
        Session(session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
                turns=[_turn_with_tools({"Read": 5, "Edit": 2})]),
        Session(session_id="b", source_file="b.jsonl", is_sidechain=False, cwd="/p",
                turns=[_turn_with_tools({"Edit": 3, "Bash": 1})]),
    ]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=10)

    assert result == [
        {"tool": "Read", "count": 5},
        {"tool": "Edit", "count": 5},
        {"tool": "Bash", "count": 1},
    ]


def test_build_tool_mix_collapses_tail_to_others():
    srs = [Session(
        session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
        turns=[_turn_with_tools({
            "Read": 10, "Edit": 8, "Bash": 6, "Grep": 4, "Glob": 3,
            "Write": 2, "Task": 1,
        })],
    )]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=3)

    # Top-3 kept, remaining 4 tools collapsed into one "others" row.
    assert result[0] == {"tool": "Read", "count": 10}
    assert result[1] == {"tool": "Edit", "count": 8}
    assert result[2] == {"tool": "Bash", "count": 6}
    assert result[3] == {"tool": "others", "count": 4 + 3 + 2 + 1}
    assert len(result) == 4


def test_build_tool_mix_no_others_when_under_top_n():
    srs = [Session(
        session_id="a", source_file="a.jsonl", is_sidechain=False, cwd="/p",
        turns=[_turn_with_tools({"Read": 2, "Edit": 1})],
    )]
    rollups = [build_session_rollup(s) for s in srs]
    result = build_tool_mix(rollups, top_n=10)

    assert result == [{"tool": "Read", "count": 2}, {"tool": "Edit", "count": 1}]
    assert all(row["tool"] != "others" for row in result)


def test_build_tool_mix_empty():
    assert build_tool_mix([], top_n=10) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_metrics.py -v -k tool_mix`
Expected: FAIL — `tool_mix` not on SessionRollup; `build_tool_mix` not importable.

- [ ] **Step 3: Add `tool_mix` field**

Edit `src/tokenol/metrics/rollups.py`. In the `SessionRollup` dataclass, after `tool_error_count: int` (line 49), add:

```python
    tool_mix: Counter[str] = field(default_factory=Counter)
```

- [ ] **Step 4: Populate it in `build_session_rollup`**

In `build_session_rollup`, near the existing `tool_use_count = sum(...)` line (around line 124), add:

```python
    tool_mix: Counter[str] = Counter()
    for t in session.turns:
        tool_mix.update(t.tool_names)
```

Then pass it into the `SessionRollup(...)` constructor (around line 144):

```python
        tool_mix=tool_mix,
```

next to the `tool_use_count=` / `tool_error_count=` kwargs.

- [ ] **Step 5: Implement `build_tool_mix`**

Append to `src/tokenol/metrics/rollups.py` (below `build_project_rollups`, before `build_model_rollups` if it exists, or at end of file):

```python
def build_tool_mix(
    session_rollups: list[SessionRollup],
    top_n: int = 10,
) -> list[dict]:
    """Rank tool-name call counts across all sessions.

    Returns `[{tool, count}]` sorted count-desc. If more than `top_n` distinct
    tools are present, the tail collapses into a single `{tool: "others",
    count: <sum of tail>}` row. Returns `[]` on empty input.
    """
    total: Counter[str] = Counter()
    for sr in session_rollups:
        total.update(sr.tool_mix)

    if not total:
        return []

    ranked = total.most_common()
    head = ranked[:top_n]
    tail = ranked[top_n:]

    rows = [{"tool": name, "count": count} for name, count in head]
    if tail:
        rows.append({"tool": "others", "count": sum(c for _, c in tail)})
    return rows
```

- [ ] **Step 6: Run metrics tests**

Run: `.venv/bin/python -m pytest tests/test_metrics.py -v`
Expected: all green.

- [ ] **Step 7: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/tokenol/metrics/rollups.py tests/test_metrics.py
git commit -m "feat: SessionRollup.tool_mix + build_tool_mix top-N aggregator"
```

---

## Task 4: Multi-project / multi-model / multi-tool fixture

Introduces realistic test data so the new Tools endpoint (and subsequent multi-project assertions) have more than one row to work with. The existing `basic.jsonl` has one project and one model, which masks top-N / `others` collapse bugs.

**Files:**
- Create: `tests/fixtures/multi.jsonl`
- Test: parser smoke test in `tests/test_parser.py`

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/multi.jsonl` with three sessions across two projects and two models:

```jsonl
{"type":"system","timestamp":"2026-04-14T10:00:00Z","sessionId":"sess-a","uuid":"sa0","isSidechain":false,"cwd":"/home/u/projA"}
{"type":"assistant","timestamp":"2026-04-14T10:01:00Z","sessionId":"sess-a","requestId":"req-a1","uuid":"ea1","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"msg-a1","role":"assistant","stop_reason":"tool_use","usage":{"input_tokens":1000,"output_tokens":200,"cache_read_input_tokens":500,"cache_creation_input_tokens":100},"content":[{"type":"tool_use","name":"Read","input":{}},{"type":"tool_use","name":"Read","input":{}},{"type":"tool_use","name":"Bash","input":{}}]}}
{"type":"assistant","timestamp":"2026-04-14T10:02:00Z","sessionId":"sess-a","requestId":"req-a2","uuid":"ea2","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"msg-a2","role":"assistant","stop_reason":"end_turn","usage":{"input_tokens":2000,"output_tokens":300,"cache_read_input_tokens":1000,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"done"}]}}
{"type":"system","timestamp":"2026-04-14T11:00:00Z","sessionId":"sess-b","uuid":"sb0","isSidechain":false,"cwd":"/home/u/projB"}
{"type":"assistant","timestamp":"2026-04-14T11:01:00Z","sessionId":"sess-b","requestId":"req-b1","uuid":"eb1","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"msg-b1","role":"assistant","stop_reason":"tool_use","usage":{"input_tokens":1500,"output_tokens":400,"cache_read_input_tokens":200,"cache_creation_input_tokens":300},"content":[{"type":"tool_use","name":"Edit","input":{}},{"type":"tool_use","name":"Edit","input":{}},{"type":"tool_use","name":"Edit","input":{}},{"type":"tool_use","name":"Read","input":{}}]}}
{"type":"system","timestamp":"2026-04-14T12:00:00Z","sessionId":"sess-c","uuid":"sc0","isSidechain":false,"cwd":"/home/u/projA"}
{"type":"assistant","timestamp":"2026-04-14T12:01:00Z","sessionId":"sess-c","requestId":"req-c1","uuid":"ec1","isSidechain":false,"model":"claude-sonnet-4-6","message":{"id":"msg-c1","role":"assistant","stop_reason":"tool_use","usage":{"input_tokens":800,"output_tokens":150,"cache_read_input_tokens":400,"cache_creation_input_tokens":50},"content":[{"type":"tool_use","name":"Grep","input":{}},{"type":"tool_use","name":"Read","input":{}}]}}
```

Totals across all three sessions, non-interrupted:
- Projects: `/home/u/projA` (3 turns), `/home/u/projB` (1 turn)
- Models: `claude-opus-4-7` (3 turns), `claude-sonnet-4-6` (1 turn)
- Tools: `Read` (4), `Edit` (3), `Bash` (1), `Grep` (1)

- [ ] **Step 2: Write a smoke test**

Append to `tests/test_parser.py`:

```python
def test_multi_fixture_shape():
    """Sanity-check the multi.jsonl fixture's expected totals."""
    from tokenol.ingest.builder import build_sessions, build_turns
    p = FIXTURES / "multi.jsonl"
    turns = build_turns([p])
    sessions = build_sessions(turns, paths=[p])

    assert len(sessions) == 3
    assert {s.cwd for s in sessions} == {"/home/u/projA", "/home/u/projB"}
    assert {t.model for t in turns} == {"claude-opus-4-7", "claude-sonnet-4-6"}

    total_tools: Counter[str] = Counter()
    for t in turns:
        total_tools.update(t.tool_names)
    assert total_tools == Counter({"Read": 4, "Edit": 3, "Bash": 1, "Grep": 1})
```

- [ ] **Step 3: Run test**

Run: `.venv/bin/python -m pytest tests/test_parser.py::test_multi_fixture_shape -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/multi.jsonl tests/test_parser.py
git commit -m "test: multi-project / multi-model / multi-tool fixture

Three sessions across two projects (projA x2, projB x1) and two models
(opus x3, sonnet x1), producing a Read/Edit/Bash/Grep tool mix. Unlocks
top-N / 'others' coverage that basic.jsonl couldn't reach."
```

---

## Task 5: `/api/breakdown/tools` endpoint

Add the tools endpoint. Like the existing `/api/breakdown/by-project` and `by-model`, it filters range in memory against `result.turns` / `.sessions`, excludes interrupted turns, and emits a capped top-10 + `others` row.

**Files:**
- Modify: `src/tokenol/serve/app.py`
- Test: `tests/test_serve_app.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_tools_returns_ranked_list(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-multi.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "multi.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "tools" in data

    # multi.jsonl has 4 distinct tools — no "others" row at default top_n=10.
    names = [row["tool"] for row in data["tools"]]
    assert names == ["Read", "Edit", "Bash", "Grep"]
    counts = [row["count"] for row in data["tools"]]
    assert counts == [4, 3, 1, 1]


@pytest.mark.asyncio
async def test_breakdown_tools_rejects_unknown_range(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=14d")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_breakdown_tools_excludes_interrupted(tmp_path: Path) -> None:
    """Interrupted turns (no usage) must not contribute to tool counts —
    matches the by-project / by-model exclusion convention."""
    dst = tmp_path / "projects" / "sess-int.jsonl"
    dst.parent.mkdir(parents=True)
    # One interrupted assistant turn with a tool_use block.
    dst.write_text(
        '{"type":"assistant","timestamp":"2026-04-14T10:00:00Z","sessionId":"s1","requestId":"r1","uuid":"e1","isSidechain":false,"model":"claude-opus-4-7",'
        '"message":{"id":"m1","role":"assistant","content":[{"type":"tool_use","name":"Read","input":{}}]}}\n'
    )

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=all")

    assert resp.status_code == 200
    assert resp.json()["tools"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_serve_app.py -v -k breakdown_tools`
Expected: FAIL (404 from the endpoint).

- [ ] **Step 3: Add the endpoint**

Edit `src/tokenol/serve/app.py`. After `api_breakdown_by_model` (around line 446), add:

```python
    @app.get("/api/breakdown/tools")
    async def api_breakdown_tools(request: Request, range: str = "30d"):
        _validate_breakdown_range(range)
        result = request.app.state.snapshot_result or _build_and_cache_snapshot(request)
        since = range_since(range, date.today()) if range != "all" else None

        total: Counter[str] = Counter()
        for t in result.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            total.update(t.tool_names)

        if not total:
            return JSONResponse({"range": range, "tools": []})

        ranked = total.most_common()
        top_n = 10
        head = ranked[:top_n]
        tail = ranked[top_n:]
        tools = [{"tool": name, "count": count} for name, count in head]
        if tail:
            tools.append({"tool": "others", "count": sum(c for _, c in tail)})
        return JSONResponse({"range": range, "tools": tools})
```

Add the `Counter` import at the top of `app.py` if not already present:

```python
from collections import Counter
```

- [ ] **Step 4: Run endpoint tests**

Run: `.venv/bin/python -m pytest tests/test_serve_app.py -v -k breakdown_tools`
Expected: all three new tests green.

- [ ] **Step 5: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: 198 + 3 + 1 = 202 passing.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat: add /api/breakdown/tools endpoint

Top-10 tool-name ranking across non-interrupted turns in the requested
range, with a collapsed 'others' row for the tail."
```

---

## Task 6: Extract `_bucket_turns` helper

The `/api/breakdown/by-project` and `by-model` handlers both iterate `result.sessions` / `result.turns` with the same shape: since-filter, interrupted-filter, bucket by key, sum a fixed set of usage fields. Collapse to a shared helper. This is PR2's final-review deferral #2.

**Files:**
- Modify: `src/tokenol/serve/app.py`
- Test: existing tests (no new — this is pure refactor)

- [ ] **Step 1: Add the helper**

Edit `src/tokenol/serve/app.py`. Before the first breakdown handler (above `api_breakdown_by_project`, around line 381), add:

```python
def _bucket_turns(
    sessions: list,
    since,
    key_fn,
) -> dict[str, dict[str, int]]:
    """Group non-interrupted turns into buckets and sum the usage fields.

    `key_fn` receives `(session, turn)` and returns the bucket key; handlers
    pass a lambda that closes over whatever grouping dict they precomputed
    (e.g. `cwd_by_sid`). Returns `{key: {"input", "output", "cache_read",
    "cache_creation"}}`. Callers may ignore unused fields.
    """
    buckets: dict[str, dict[str, int]] = {}
    for s in sessions:
        for t in s.turns:
            if since is not None and t.timestamp.date() < since:
                continue
            if t.is_interrupted:
                continue
            key = key_fn(s, t)
            b = buckets.setdefault(key, {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
            })
            b["input"] += t.usage.input_tokens
            b["output"] += t.usage.output_tokens
            b["cache_read"] += t.usage.cache_read_input_tokens
            b["cache_creation"] += t.usage.cache_creation_input_tokens
    return buckets
```

- [ ] **Step 2: Refactor `by-project` to use it**

In `api_breakdown_by_project`, replace the bucket-building block (from `buckets: dict[str, dict[str, int]] = {}` through the inner `b["cache_creation"] += ...` line) with:

```python
        buckets = _bucket_turns(
            result.sessions, since,
            key_fn=lambda s, t: cwd_by_sid.get(s.session_id, "(unknown)"),
        )
```

- [ ] **Step 3: Refactor `by-model` to use it**

In `api_breakdown_by_model`, replace the existing `buckets = {}` loop (which iterates `result.turns` directly) with a session-based call:

```python
        buckets = _bucket_turns(
            result.sessions, since,
            key_fn=lambda _s, t: t.model or "(unknown)",
        )
```

Note: `by-model` only uses `input` and `output`, so the extra `cache_read` / `cache_creation` fields the helper computes are simply unused. That's an acceptable trade — the helper's behavior stays uniform and the Python overhead is negligible.

- [ ] **Step 4: Run all endpoint tests**

Run: `.venv/bin/python -m pytest tests/test_serve_app.py -q`
Expected: all 40+ tests green (same count as before the refactor).

- [ ] **Step 5: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as end of Task 5.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py
git commit -m "refactor: extract _bucket_turns helper for breakdown endpoints

by-project and by-model share the same since-filter + interrupted-filter
+ bucket-and-sum shape; centralize it. Tools endpoint is left alone
because it aggregates a Counter, not token sums."
```

---

## Task 7: Tools section HTML shell + CSS

Add the section heading and a full-width panel containing the new canvas.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html`
- Modify: `src/tokenol/serve/static/styles.css`

- [ ] **Step 1: Append Tools section to `breakdown.html`**

Edit `src/tokenol/serve/static/breakdown.html`. After the Breakdowns section's closing `</div>` (around line 110, just before `</div>` that closes `.app`), add:

```html
  <div class="section-heading breakdown-section-heading">
    <h2>Tools</h2>
  </div>
  <div class="breakdown-grid breakdown-grid--1">
    <section class="breakdown-panel" aria-labelledby="bp-tools-title">
      <div class="breakdown-panel-heading">
        <h3 id="bp-tools-title">Tool Mix</h3>
        <span class="chart-subheading" id="bp-tools-sub"></span>
      </div>
      <div class="breakdown-chart breakdown-chart--tall"><canvas id="chart-tools" height="320"></canvas></div>
    </section>
  </div>
```

- [ ] **Step 2: Add the grid + tall-chart CSS**

Edit `src/tokenol/serve/static/styles.css`. Next to the existing `.breakdown-grid--2-1` rule (around line 896), add:

```css
.breakdown-grid--1 { grid-template-columns: 1fr; }
```

Next to `.breakdown-chart` (around line 923), add:

```css
.breakdown-chart--tall { min-height: 320px; }
```

No media-query change needed — `.breakdown-grid--1` is already single-column on all widths.

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/breakdown.html src/tokenol/serve/static/styles.css
git commit -m "feat: Tools section shell in /breakdown"
```

---

## Task 8: `renderToolMix` + fetch wiring

Render the horizontal bar chart and wire it into the SSE-driven refresh pipeline.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add the fetcher and renderer**

Edit `src/tokenol/serve/static/breakdown.js`. Below `renderByModel` (around line 343), append:

```javascript
async function fetchTools(range) {
  const resp = await fetch(`/api/breakdown/tools?range=${encodeURIComponent(range)}`);
  if (!resp.ok) throw new Error(`tools ${resp.status}`);
  return resp.json();
}

let _chartTools = null;

function renderToolMix(data) {
  const pal = tokenolPalette();
  const tools = data.tools || [];
  const labels = tools.map(t => t.tool);
  const counts = tools.map(t => t.count);

  const subEl = document.getElementById('bp-tools-sub');
  const totalCalls = counts.reduce((s, n) => s + n, 0);
  subEl.textContent = tools.length === 0
    ? 'no tool calls'
    : `${tools.length} tool${tools.length === 1 ? '' : 's'} · ${fmtInt(totalCalls)} calls`;

  const canvas = document.getElementById('chart-tools');
  if (_chartTools) {
    _chartTools.data.labels = labels;
    _chartTools.data.datasets[0].data = counts;
    _chartTools.update('none');
    return;
  }
  _chartTools = new window.Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'calls',
        data: counts,
        backgroundColor: pal[0],
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { beginAtZero: true, ticks: { callback: v => fmtInt(v) } },
        y: { ticks: { autoSkip: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label(ctx) {
              return `${ctx.label}: ${fmtInt(ctx.parsed.x)} calls`;
            },
          },
        },
      },
    },
  });
}
```

- [ ] **Step 2: Wire into `refreshAll`**

Find the `refreshAll` function (around line 500). Extend the `Promise.all` and the render sequence:

```javascript
    const [summary, daily, byProject, byModel, tools] = await Promise.all([
      fetchSummary(range),
      fetchDailyTokens(range),
      fetchByProject(range),
      fetchByModel(range),
      fetchTools(range),
    ]);
    renderScorecard(summary);
    renderDailyWork(daily);
    renderDailyCache(daily);
    renderByProject(byProject);
    renderByModel(byModel);
    renderToolMix(tools);
```

- [ ] **Step 3: Syntax check and manual sanity**

Run: `node --check src/tokenol/serve/static/breakdown.js`
Expected: OK (no stdout).

Run: `.venv/bin/tokenol serve --open` in one terminal, then visit `http://127.0.0.1:8787/breakdown` in the browser. Tool Mix panel should render with 4 bars (Read / Edit / Bash / Grep on a fresh multi-fixture database — or whatever is in your live data).

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: Tool Mix horizontal bar chart"
```

---

## Task 9: Unify sidecar arrays onto chart instances

PR2 final-review deferral #1. The click-handler and tooltip callbacks currently reach for module-level sidecar arrays (`_byProjectCwdB64`, `_byProjectHitRate`, `_byModelNames`) whose order is parallel to the chart's x-axis. Stash them on each chart instance under a `$tokenol` namespace so the chart is self-contained.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Move the project-chart sidecars onto the instance**

In `renderByProject`, remove the module-level declarations:

```javascript
let _byProjectCwdB64 = [];
let _byProjectHitRate = [];
```

Replace the inside-function assignments:

```javascript
  _byProjectCwdB64 = projects.map(p => p.cwd_b64);
  _byProjectHitRate = projects.map(p => p.cache_hit_rate);
```

with:

```javascript
  const cwdB64 = projects.map(p => p.cwd_b64);
  const hitRate = projects.map(p => p.cache_hit_rate);
```

In the in-place update block, after `_chartByProject.update('none'); return;`, add setting the sidecar before the `update` call:

```javascript
  if (_chartByProject) {
    _chartByProject.$tokenol = { cwdB64, hitRate };
    _chartByProject.data.labels = labels;
    // ...existing dataset/plugin updates...
    _chartByProject.update('none');
    return;
  }
```

At new-chart construction, attach the sidecar just after `_chartByProject = new window.Chart(...)`:

```javascript
  _chartByProject.$tokenol = { cwdB64, hitRate };
```

In the tooltip callback, replace `_byProjectHitRate[items[0].dataIndex]` with `items[0].chart.$tokenol.hitRate[items[0].dataIndex]`.

In the `onClick` handler, replace `_byProjectCwdB64[idx]` with `_chartByProject.$tokenol.cwdB64[idx]`. (Inside `onClick`, `this` / the bound chart is easier via the closure-captured `_chartByProject`.)

- [ ] **Step 2: Move the model-chart sidecar**

In `renderByModel`, remove the module-level `let _byModelNames = [];`. Replace `_byModelNames = ...` assignments with a local `const names = ...`. On the chart instance, set `_chartByModel.$tokenol = { names }` in both the in-place-update path and the construction path. In the `onClick` handler, replace `_byModelNames[idx]` with `_chartByModel.$tokenol.names[idx]`.

- [ ] **Step 3: Syntax check**

Run: `node --check src/tokenol/serve/static/breakdown.js`
Expected: OK.

- [ ] **Step 4: Run Python suite to confirm nothing serverside broke**

(No Python changes in this task — this just reassures us nothing leaked.)
Run: `.venv/bin/python -m pytest -q`
Expected: same pass count as end of Task 6.

- [ ] **Step 5: Manual sanity**

With the dev server running, reload `/breakdown`. Click a project bar → drill-down still works. Hover a project bar → tooltip still shows `cache hit rate: XX.X%`. Click a model slice → drill-down still works. Click the `others` slice → no-op (as before).

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "refactor: move breakdown chart sidecars onto chart.\$tokenol

Parallel-to-x-axis lookup arrays (cwdB64, hitRate, names) now live on
the chart instance instead of module-level lets. Click handlers and
tooltip callbacks read from chart.\$tokenol — one source of truth per
chart, no cross-renderer bleed."
```

---

## Task 10: Plumb cache-health thresholds through `/api/prefs`

PR2 final-review deferral #4. `breakdown.js` currently hard-codes `HIT_PCT_GREEN = 95.0` and `HIT_PCT_RED = 85.0`, independent of the live prefs. `/api/prefs` already returns `thresholds.hit_rate_good_pct` and `.hit_rate_red_pct` — fetch them once on page load and use them instead.

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Replace the constants with mutable module state**

Edit `src/tokenol/serve/static/breakdown.js`. Replace:

```javascript
const HIT_PCT_GREEN = 95.0;
const HIT_PCT_RED = 85.0;
```

with:

```javascript
// Thresholds default to DEFAULTS from metrics/thresholds.py; overridden by
// /api/prefs on page load. Kept as module-level mutable so renderers see the
// latest value on SSE tick without re-plumbing.
let HIT_PCT_GREEN = 95.0;
let HIT_PCT_RED = 85.0;
```

- [ ] **Step 2: Add a one-shot prefs fetcher**

Near the other `fetch*` functions (below `fetchTools`), add:

```javascript
async function loadThresholdsFromPrefs() {
  try {
    const resp = await fetch('/api/prefs');
    if (!resp.ok) return;
    const prefs = await resp.json();
    const t = prefs.thresholds || {};
    if (Number.isFinite(t.hit_rate_good_pct)) HIT_PCT_GREEN = t.hit_rate_good_pct;
    if (Number.isFinite(t.hit_rate_red_pct))  HIT_PCT_RED   = t.hit_rate_red_pct;
  } catch (err) {
    console.warn('[breakdown] prefs fetch failed; using default thresholds', err);
  }
}
```

- [ ] **Step 3: Invalidate the caption cache so it re-renders with new thresholds**

In `renderByProjectCaption` (added earlier in this session), the `_captionRendered` guard prevents re-render. Move the guard so the caption can be re-rendered when thresholds change. Replace:

```javascript
let _captionRendered = false;
function renderByProjectCaption() {
  if (_captionRendered) return;
  const el = document.getElementById('bp-by-project-caption');
  if (!el) return;
  el.innerHTML = ...;
  _captionRendered = true;
}
```

with:

```javascript
let _captionSignature = '';
function renderByProjectCaption() {
  const el = document.getElementById('bp-by-project-caption');
  if (!el) return;
  const sig = `${HIT_PCT_GREEN}|${HIT_PCT_RED}`;
  if (sig === _captionSignature) return;
  el.innerHTML =
    `<span>cache hit rate</span>` +
    `<span class="caption-swatch caption-swatch--green"></span><span>≥${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--amber"></span><span>${HIT_PCT_RED}–${HIT_PCT_GREEN}%</span>` +
    `<span class="caption-swatch caption-swatch--alarm"></span><span>&lt;${HIT_PCT_RED}%</span>`;
  _captionSignature = sig;
}
```

- [ ] **Step 4: Call `loadThresholdsFromPrefs` before the first refresh**

At the bottom of the file, update the entry sequence. Change:

```javascript
connectSSE();
wirePeriodPills();
refreshAll();
```

to:

```javascript
connectSSE();
wirePeriodPills();
loadThresholdsFromPrefs().then(() => refreshAll());
```

This way the first `refreshAll` sees the live threshold values; SSE ticks thereafter use whatever's current in the module-level vars. (Thresholds don't change between reloads — no re-fetch on tick.)

- [ ] **Step 5: Syntax check**

Run: `node --check src/tokenol/serve/static/breakdown.js`
Expected: OK.

- [ ] **Step 6: Manual sanity**

With the dev server running, hit `/breakdown`. The caption under Tokens by Project should still read `≥95%` / `85–95%` / `<85%`. Then POST to `/api/prefs` to change the threshold:

```bash
curl -X POST http://127.0.0.1:8787/api/prefs \
  -H 'content-type: application/json' \
  -d '{"thresholds":{"hit_rate_good_pct":90,"hit_rate_red_pct":80}}'
```

Reload `/breakdown`. Caption should now read `≥90%` / `80–90%` / `<80%` and the dot colors should recompute to the new thresholds on the next render. Reset the prefs afterward:

```bash
curl -X POST http://127.0.0.1:8787/api/prefs \
  -H 'content-type: application/json' \
  -d '{"thresholds":"reset"}'
```

- [ ] **Step 7: Commit**

```bash
git add src/tokenol/serve/static/breakdown.js
git commit -m "feat: plumb cache-health thresholds through /api/prefs

Replaces the hard-coded HIT_PCT_GREEN/RED constants with values fetched
from /api/prefs on page load. Caption under Tokens by Project re-renders
when thresholds change. Defaults still match metrics/thresholds.DEFAULTS
on fetch failure."
```

---

## Task 11: Visual walkthrough

Manual verification across the whole Breakdown tab — new Tools section, refactored PR1/PR2 pieces, prefs plumbing.

**Files:**
- None (manual).

- [ ] **Step 1: Start the dev server with the multi fixture**

Point the server at a temp dir containing `tests/fixtures/multi.jsonl` as a seeded project, or just use your live `~/.claude*` directory. Launch:

```bash
.venv/bin/tokenol serve --open
```

- [ ] **Step 2: Tools section shape**

On `/breakdown`:
- Tools section heading is visible and matches the style of "Time" and "Breakdowns".
- Tool Mix panel spans full width under Breakdowns.
- Horizontal bars, ranked descending by call count.
- Subheading reads `N tools · K calls` (or `no tool calls` if the data is empty).
- If there are > 10 distinct tools, the 11th row reads `others` and aggregates the tail.
- Tooltip on hover reads `toolname: N calls`.

- [ ] **Step 3: Period pills re-render tools**

Click 7D / 30D / 90D / All. Tool Mix updates in place; no flicker, no canvas destroy.

- [ ] **Step 4: SSE tick in place**

Leave the tab idle; within one SSE tick the tool chart updates without recreating the canvas (watch DOM in DevTools → Elements to confirm the `<canvas id="chart-tools">` node persists).

- [ ] **Step 5: Regression: PR1 / PR2 still work**

Re-verify the PR2 walkthrough items after the Task 9 sidecar refactor:
- Project bars: cache-health dots under ticks, click drills to `/project/{cwd_b64}`, tooltip shows `cache hit rate: XX.X%`.
- Model donut: named-slice click drills to `/model/{name}`, `others` slice no-op.
- Daily billable / Daily cache re-use: unchanged.

- [ ] **Step 6: Narrow viewport**

Below 900 px: Breakdowns grid stacks; Tools section stays single-column (was single-column to begin with). No overflow.

- [ ] **Step 7: Prefs plumbing**

POST a threshold override (see Task 10 Step 6). Confirm the caption updates on reload and the dot colors recompute to the new thresholds.

- [ ] **Step 8: No console errors**

DevTools console open for the entire walkthrough — zero errors, zero warnings related to Chart.js, fetches, or the new prefs fetcher.

---

## Self-review checklist

Run before declaring PR3 ready (and before the whole-branch final review):

- [ ] `.venv/bin/python -m pytest tests/` is green.
- [ ] `node --check src/tokenol/serve/static/breakdown.js` is green.
- [ ] `/api/breakdown/tools` rejects unknown `range` values with HTTP 400.
- [ ] `/api/breakdown/tools` excludes interrupted turns from tool counts.
- [ ] `build_tool_mix` collapses the tail into `others` when `len(tools) > top_n`; otherwise emits no `others` row.
- [ ] `tests/fixtures/multi.jsonl` is committed and referenced by at least the parser smoke test and the new tools endpoint test.
- [ ] No hard-coded hex values in new CSS/HTML/JS — Tool Mix bars use `tokenolPalette()[0]` which resolves via `cssVar()`.
- [ ] `_bucket_turns` is the single source of truth for the by-project / by-model bucket-and-sum (or a comment explains why any endpoint opted out).
- [ ] No module-level `let _byProjectCwdB64 = []` / `_byProjectHitRate = []` / `_byModelNames = []` remain — sidecars live on `chart.$tokenol`.
- [ ] `HIT_PCT_GREEN` / `HIT_PCT_RED` are mutable module-level `let`s, not `const`s, and are populated from `/api/prefs` on page load.
- [ ] `tool_use_count` still exists on every `RawEvent` / `Turn` and equals `sum(tool_names.values())` for turns with named tools (plus any unnamed-block count, per `_extract_tool_blocks` semantics).
- [ ] All commits on `breakdown-pr1` follow repo convention (`feat:` / `refactor:` / `test:`); no Co-Authored-By lines.

## What PR3 completes

After PR3, the whole Breakdown tab is done and `breakdown-pr1` is ready for its whole-branch review + `superpowers:finishing-a-development-branch` merge into `main`:

- All five charts from the design spec render: Daily Billable Tokens, Daily Cache Re-use, Tokens by Project (with cache-health dots), Model Mix, Tool Mix.
- Parser emits per-turn tool names without breaking any existing `tool_use_count` consumer.
- All four `/api/breakdown/*` endpoints share a common validator (`_validate_breakdown_range`) and the token-summing ones share `_bucket_turns`.
- Cache-health thresholds are live-configurable via `/api/prefs`.
- No sidecar arrays leak between chart renderers.
