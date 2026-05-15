# Per-Tool Cost Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute USD cost per-tool (causal model: byte-share on output + lingering input) and surface it on Breakdown, tool detail, project detail, and model detail pages.

**Architecture:** Parser walks each session's JSONL in order, maintaining a `tool_use_id → tool_name` map and per-tool running byte tallies; at each assistant turn, it distributes that turn's four cost components (input + output + cache_read + cache_creation) by byte share and emits `tool_costs: dict[str, ToolCost]` on the Turn. Aggregation lives in `metrics/rollups.py`; API endpoints are extended in-place; one shared `renderRankedBars` component in `components.js` powers five chart instances across four pages.

**Tech Stack:** Python 3.12+, FastAPI, pytest, ruff (`uv run pytest` / `uv run ruff check`). Vanilla JavaScript + Chart.js v4 (no bundler, no JS test harness). HTML/CSS hand-rolled in `serve/static/`.

**Spec:** `docs/superpowers/specs/2026-05-15-per-tool-cost-design.md`

**Release-gate reminder (from project memory):** `uv run ruff check src tests && uv run pytest -q` BOTH pass before any push. No AI-attribution trailers in commits. Maintain `RESUME.md` after meaningful sessions.

**Branch / worktree:** Work on `feature/per-tool-cost` in `.worktrees/per-tool-cost/`. Create with `git worktree add .worktrees/per-tool-cost -b feature/per-tool-cost`. Clean up after merge per project memory.

---

## File Map

**Backend (Python)**

- Modify `src/tokenol/model/events.py` — add `ToolCost` dataclass; extend `RawEvent` and `Turn` with `tool_costs: dict[str, ToolCost]` + `unattributed_input_tokens`, `unattributed_output_tokens`, `unattributed_cost_usd`.
- Modify `src/tokenol/ingest/parser.py` — `parse_file` becomes stateful per session: maintains `tool_use_id → tool_name` map, running byte tallies, compaction-reset heuristic; attaches `tool_costs` to each assistant event.
- Modify `src/tokenol/ingest/builder.py` — propagate `tool_costs` + unattributed fields from `RawEvent` to `Turn`.
- Modify `src/tokenol/metrics/rollups.py` — add `ToolCostRollup`, `build_tool_cost_rollups`, `build_tool_cost_daily`; extend `ProjectRollup` / `ModelRollup` with `tool_costs`. Add a `_rank_dict_with_others` sibling to the existing `_rank_counter_with_others`.
- Modify `src/tokenol/serve/app.py` — extend `/api/breakdown/tools` payload; extend `/api/tool/{name}`, `/api/project/{cwd_b64}`, `/api/model/{name}` payloads.
- Modify `src/tokenol/serve/state.py` if/when the snapshot/index needs new fields surfaced.
- Add `tests/fixtures/per_tool_basic.jsonl` — synthetic session for golden-file test.
- Add `tests/test_per_tool_cost.py` — parser + rollup unit tests for this feature.
- Modify `tests/test_serve_app.py` — endpoint shape assertions.

**Frontend (HTML/CSS/JS)**

- Modify `src/tokenol/serve/static/components.js` — add `renderRankedBars(container, rows, opts)` plus dim "unattributed" row handling.
- Modify `src/tokenol/serve/static/styles.css` — bar-row component CSS.
- Modify `src/tokenol/serve/static/breakdown.js` + `breakdown.html` — wire Tool Mix to `data-bdunit="cost"` and `renderRankedBars`.
- Modify `src/tokenol/serve/static/tool.html` + `tool.js` — add daily-cost chart, scorecard band, cost-by-project + cost-by-model bar charts.
- Modify `src/tokenol/serve/static/project.html` + `project.js` — add cost-by-tool + cost-by-model bar charts.
- Modify `src/tokenol/serve/static/model.html` + `model.js` — add cost-by-tool bar chart.

**Docs**

- Modify `CHANGELOG.md` — 0.6.0 section.
- Modify `RESUME.md` — note feature completion.

---

## Conceptual notes for the implementer

**Byte-share math, all four cost components.** Per `metrics/cost.py`, every turn has `input_usd + output_usd + cache_read_usd + cache_creation_usd`. The three input-side components share one byte-share split. Don't attribute only `input_usd`.

**Running tallies own per-session state.** Both halves of a tool exchange linger in context — the assistant's `tool_use` block AND the user's `tool_use_result` block. Both go into `bytes_in_context_by_tool[tool_name]`.

**Attribution happens *before* folding the turn's content into tallies.** A turn doesn't self-attribute its own output bytes on the input side — that's already covered by output-side attribution.

**Compaction is heuristic, not marked.** `metrics/patterns.py` already uses a 0.8 drop ratio. The parser reuses it: if `input_tokens + cache_read + cache_creation` on turn T+1 is < 20% of the session's running peak, reset both running tallies.

---

## Task 1: Add `ToolCost` dataclass and extend `RawEvent` / `Turn`

**Files:**
- Modify: `src/tokenol/model/events.py`
- Test: `tests/test_per_tool_cost.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_per_tool_cost.py`:

```python
"""Per-tool cost attribution: parser, rollups, and API."""

from collections import Counter
from datetime import datetime, timezone

from tokenol.model.events import RawEvent, ToolCost, Turn, Usage


def test_toolcost_dataclass_shape():
    tc = ToolCost(tool_name="Read", input_tokens=12.5, output_tokens=3.2, cost_usd=0.0042)
    assert tc.tool_name == "Read"
    assert tc.input_tokens == 12.5
    assert tc.output_tokens == 3.2
    assert tc.cost_usd == 0.0042


def test_rawevent_has_tool_costs_default_empty():
    ev = RawEvent(
        source_file="x.jsonl",
        line_number=1,
        event_type="assistant",
        session_id="s1",
        request_id="r1",
        message_id="m1",
        uuid="u1",
        timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
        usage=Usage(input_tokens=100, output_tokens=10),
        model="claude-opus-4-7",
        is_sidechain=False,
        stop_reason="end_turn",
    )
    assert ev.tool_costs == {}
    assert ev.unattributed_input_tokens == 0.0
    assert ev.unattributed_output_tokens == 0.0
    assert ev.unattributed_cost_usd == 0.0


def test_turn_has_tool_costs_default_empty():
    t = Turn(
        dedup_key="m1:r1",
        timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc),
        session_id="s1",
        model="claude-opus-4-7",
        usage=Usage(input_tokens=100, output_tokens=10),
        is_sidechain=False,
        stop_reason="end_turn",
    )
    assert t.tool_costs == {}
    assert t.unattributed_cost_usd == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v
```

Expected: ImportError (`ToolCost` doesn't exist) or AttributeError.

- [ ] **Step 3: Add `ToolCost` and extend `RawEvent` / `Turn` in `src/tokenol/model/events.py`**

After the existing `Usage` dataclass, add:

```python
@dataclass
class ToolCost:
    """Attributed slice of a turn's cost for one tool."""
    tool_name: str
    input_tokens: float = 0.0        # fractional after share split
    output_tokens: float = 0.0
    cost_usd: float = 0.0            # input_usd + output_usd + cache_read_usd + cache_creation_usd shares
```

Append to `RawEvent` (after `cwd: str | None = None`):

```python
    tool_costs: dict[str, ToolCost] = field(default_factory=dict)
    unattributed_input_tokens: float = 0.0
    unattributed_output_tokens: float = 0.0
    unattributed_cost_usd: float = 0.0
```

Append to `Turn` (after `tool_names: Counter[str] = field(default_factory=Counter)`):

```python
    tool_costs: dict[str, ToolCost] = field(default_factory=dict)
    unattributed_input_tokens: float = 0.0
    unattributed_output_tokens: float = 0.0
    unattributed_cost_usd: float = 0.0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_per_tool_cost.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/model/events.py tests/test_per_tool_cost.py
git commit -m "feat(model): ToolCost dataclass + tool_costs on RawEvent/Turn"
```

---

## Task 2: Output-side byte-share attribution helper

**Files:**
- Modify: `src/tokenol/ingest/parser.py`
- Test: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_tool_cost.py`:

```python
from tokenol.ingest.parser import _output_byte_shares


def test_output_share_single_tool():
    content = [
        {"type": "text", "text": "I'll search for it."},      # ~30 bytes
        {"type": "tool_use", "id": "a", "name": "Grep",
         "input": {"pattern": "foo"}},                         # ~60 bytes
    ]
    shares, unattributed = _output_byte_shares(content)
    assert set(shares.keys()) == {"Grep"}
    assert 0 < shares["Grep"] < 1
    assert 0 < unattributed < 1
    assert abs(shares["Grep"] + unattributed - 1.0) < 1e-9


def test_output_share_multiple_tools_same_name_sum():
    content = [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
        {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/y"}},
        {"type": "tool_use", "id": "c", "name": "Grep", "input": {"pattern": "z"}},
    ]
    shares, unattributed = _output_byte_shares(content)
    assert set(shares.keys()) == {"Read", "Grep"}
    assert shares["Read"] > shares["Grep"]   # two Reads vs one Grep
    assert abs(unattributed) < 1e-9          # no text/thinking → all attributed


def test_output_share_thinking_block_unattributed():
    content = [
        {"type": "thinking", "thinking": "x" * 500},
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x"}},
    ]
    shares, unattributed = _output_byte_shares(content)
    assert "Read" in shares
    assert unattributed > shares["Read"]     # 500 bytes of thinking dwarfs the tool block


def test_output_share_empty_content():
    shares, unattributed = _output_byte_shares([])
    assert shares == {}
    assert unattributed == 1.0               # all unattributed when there's nothing to attribute to
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k _output_byte
```

Expected: ImportError on `_output_byte_shares`.

- [ ] **Step 3: Implement the helper in `src/tokenol/ingest/parser.py`**

Add near the existing `_extract_tool_blocks`:

```python
def _block_bytes(block: dict) -> int:
    """Byte-size of a content block as it'd appear in the request (JSON-serialized)."""
    try:
        return len(json.dumps(block, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _output_byte_shares(content: list) -> tuple[dict[str, float], float]:
    """Split an assistant message's content into per-tool byte shares + unattributed.

    Returns (shares_by_tool_name, unattributed_share). Sum = 1.0.

    `tool_use` blocks attribute to their `name`. `text` and `thinking` blocks
    (and anything else) go to unattributed.
    """
    tool_bytes: dict[str, int] = {}
    unattributed_bytes = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        b = _block_bytes(block)
        if block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                tool_bytes[name] = tool_bytes.get(name, 0) + b
                continue
        unattributed_bytes += b
    total = sum(tool_bytes.values()) + unattributed_bytes
    if total <= 0:
        return {}, 1.0
    shares = {name: b / total for name, b in tool_bytes.items()}
    return shares, unattributed_bytes / total
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k _output_byte
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/ingest/parser.py tests/test_per_tool_cost.py
git commit -m "feat(parser): output-side byte-share helper"
```

---

## Task 3: Cost-attribution helper using all four pricing components

**Files:**
- Modify: `src/tokenol/ingest/parser.py`
- Test: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_tool_cost.py`:

```python
from tokenol.ingest.parser import _attribute_cost
from tokenol.model.events import Usage


def test_attribute_cost_uses_all_four_components():
    """Cache-read and cache-creation must also be distributed by the input share,
    not lumped into 'unattributed'. On Opus a cache_read at $0.50/M is 10× cheaper
    than fresh input at $5/M — but it's still real cost the user wants attributed."""
    usage = Usage(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=10_000,
        cache_creation_input_tokens=2_000,
    )
    output_shares = {"Read": 0.6}                    # 60% of output attributed to Read
    input_shares = {"Read": 0.4}                     # 40% of input-side context is Read's results
    tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        "claude-opus-4-7", usage, output_shares, input_shares
    )

    assert "Read" in tool_costs
    tc = tool_costs["Read"]
    # Token slices (fractional)
    assert tc.output_tokens == 200 * 0.6
    assert tc.input_tokens == 1000 * 0.4 + 10_000 * 0.4 + 2_000 * 0.4
    # Cost slice: output_usd × 0.6 + (input + cache_read + cache_create)_usd × 0.4
    # Opus rates: input 5, output 25, cache_read 0.5, cache_write 6.25 per 1M
    expected_cost = (
        200 * 25 / 1_000_000 * 0.6
        + 1000 * 5 / 1_000_000 * 0.4
        + 10_000 * 0.5 / 1_000_000 * 0.4
        + 2_000 * 6.25 / 1_000_000 * 0.4
    )
    assert abs(tc.cost_usd - expected_cost) < 1e-9
    # Unattributed picks up the leftover 0.4 output / 0.6 input shares
    assert abs(unattr_out - 200 * 0.4) < 1e-9
    assert abs(unattr_in - (1000 + 10_000 + 2_000) * 0.6) < 1e-9


def test_attribute_cost_unknown_model_zero():
    usage = Usage(input_tokens=1000, output_tokens=200)
    tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
        None, usage, {"Read": 1.0}, {"Read": 1.0}
    )
    # No model → no pricing → zero cost. Tokens are still attributed.
    assert tool_costs["Read"].cost_usd == 0.0
    assert tool_costs["Read"].input_tokens == 1000.0
    assert tool_costs["Read"].output_tokens == 200.0
    assert unattr_cost == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k attribute_cost
```

Expected: ImportError on `_attribute_cost`.

- [ ] **Step 3: Implement the helper in `src/tokenol/ingest/parser.py`**

Add (near the other helpers):

```python
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import ToolCost


def _attribute_cost(
    model: str | None,
    usage: Usage,
    output_shares: dict[str, float],
    input_shares: dict[str, float],
) -> tuple[dict[str, ToolCost], float, float, float]:
    """Split a turn's four cost components by the given byte shares.

    Returns (tool_costs, unattributed_input_tokens, unattributed_output_tokens, unattributed_cost_usd).
    """
    turn_cost = cost_for_turn(model, usage)

    input_token_pool = (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
    )
    input_cost_pool = (
        turn_cost.input_usd + turn_cost.cache_read_usd + turn_cost.cache_creation_usd
    )

    names = set(output_shares.keys()) | set(input_shares.keys())
    tool_costs: dict[str, ToolCost] = {}
    for name in names:
        out_share = output_shares.get(name, 0.0)
        in_share = input_shares.get(name, 0.0)
        tool_costs[name] = ToolCost(
            tool_name=name,
            input_tokens=input_token_pool * in_share,
            output_tokens=usage.output_tokens * out_share,
            cost_usd=turn_cost.output_usd * out_share + input_cost_pool * in_share,
        )

    out_attributed = sum(output_shares.values())
    in_attributed = sum(input_shares.values())
    unattr_out_share = max(0.0, 1.0 - out_attributed)
    unattr_in_share = max(0.0, 1.0 - in_attributed)

    return (
        tool_costs,
        input_token_pool * unattr_in_share,
        usage.output_tokens * unattr_out_share,
        turn_cost.output_usd * unattr_out_share + input_cost_pool * unattr_in_share,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k attribute_cost
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/ingest/parser.py tests/test_per_tool_cost.py
git commit -m "feat(parser): cost-attribution helper across all four pricing components"
```

---

## Task 4: Stateful parse_file — running tallies + lingering input

**Files:**
- Modify: `src/tokenol/ingest/parser.py`
- Test: `tests/test_per_tool_cost.py`

Refactor `parse_file` to maintain per-session state for attribution. Existing tests must keep passing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_tool_cost.py`:

```python
import json
from tokenol.ingest.parser import parse_file


def _write_jsonl(tmp_path, name, lines):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return p


def test_lingering_input_attribution_across_turns(tmp_path):
    """Read returns 50 KB on turn 1; turns 2-3 have no tool calls. Read's
    input attribution should grow on turns 2+3 as its result lingers in context."""
    # Turn 1: assistant calls Read
    # User: tool_result with 50 KB content
    # Turn 2: assistant text only
    # Turn 3: assistant text only
    big_result = "x" * 50_000
    lines = [
        {
            "type": "assistant", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                             "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "uuid": "u2", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big_result}
            ]},
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:02:00Z",
            "sessionId": "s1", "requestId": "r2", "uuid": "u3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m2", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 200, "output_tokens": 30,
                          "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Got it."}],
            },
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:03:00Z",
            "sessionId": "s1", "requestId": "r3", "uuid": "u4", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m3", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 30,
                          "cache_read_input_tokens": 50_500, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Anything else?"}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]
    assert len(assistants) == 3

    # Turn 1: only output-side attribution to Read (its tool_use block);
    #         no prior tool_result, so input-side attributes nothing to Read yet.
    t1 = assistants[0]
    assert "Read" in t1.tool_costs
    assert t1.tool_costs["Read"].output_tokens > 0
    assert t1.tool_costs["Read"].input_tokens == 0  # no Read result yet on turn 1 input

    # Turn 2: Read's 50 KB tool_result is now in context.
    #         Read's input share should be near 1.0 (50 KB dominates the few bytes of prior text).
    t2 = assistants[1]
    assert "Read" in t2.tool_costs
    assert t2.tool_costs["Read"].input_tokens > 0
    assert t2.tool_costs["Read"].cost_usd > 0
    assert t2.unattributed_input_tokens < t2.tool_costs["Read"].input_tokens

    # Turn 3: still lingers
    t3 = assistants[2]
    assert "Read" in t3.tool_costs
    assert t3.tool_costs["Read"].input_tokens > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k lingering
```

Expected: FAIL (parse_file doesn't yet populate `tool_costs`).

- [ ] **Step 3: Rewrite `parse_file` in `src/tokenol/ingest/parser.py`**

Replace the existing `parse_file` body with the stateful version:

```python
def parse_file(path: Path) -> Iterator[RawEvent]:
    """Yield one RawEvent per non-blank, parseable line of *path*.

    Per-session state is maintained for per-tool cost attribution:
    - `tool_use_id_to_name` maps assistant-side tool_use IDs to tool names.
    - `bytes_in_context_by_tool` / `non_tool_bytes_in_context` are running byte tallies
      of content still in the conversation window.
    - Compaction is detected heuristically (input drop ≥80% from running peak) and
      resets both tallies.
    """
    session_id = path.stem
    is_sidechain = "subagents" in path.parts

    # Per-session attribution state
    tool_use_id_to_name: dict[str, str] = {}
    bytes_in_context_by_tool: dict[str, int] = {}
    non_tool_bytes_in_context = 0
    peak_input_tokens = 0
    COMPACTION_DROP_RATIO = 0.2  # current < 20% of peak → compaction

    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue

            event_type = ev.get("type", "")
            if not event_type:
                continue

            msg = ev.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                content = []

            tool_names, tool_use_count, tool_error_count = _extract_tool_blocks(content)
            usage = _parse_usage(msg)
            model = ev.get("model") or msg.get("model")

            cwd: str | None = ev.get("cwd") or None
            if cwd and (
                (len(cwd) >= 2 and cwd[1] == ":" and cwd[0].isalpha())
                or cwd.startswith("\\\\")
            ):
                cwd = cwd.replace("\\", "/")

            tool_costs: dict[str, ToolCost] = {}
            unattr_in = unattr_out = unattr_cost = 0.0

            if event_type == "assistant" and usage is not None:
                input_pool = (
                    usage.input_tokens
                    + usage.cache_read_input_tokens
                    + usage.cache_creation_input_tokens
                )
                # Compaction reset BEFORE this turn's attribution
                if peak_input_tokens > 0 and input_pool < COMPACTION_DROP_RATIO * peak_input_tokens:
                    tool_use_id_to_name.clear()
                    bytes_in_context_by_tool.clear()
                    non_tool_bytes_in_context = 0
                peak_input_tokens = max(peak_input_tokens, input_pool)

                # Compute attribution against current tallies (BEFORE folding this turn in)
                output_shares, _out_unattr_share = _output_byte_shares(content)
                total_ctx_bytes = sum(bytes_in_context_by_tool.values()) + non_tool_bytes_in_context
                if total_ctx_bytes > 0:
                    input_shares = {
                        name: b / total_ctx_bytes
                        for name, b in bytes_in_context_by_tool.items()
                    }
                else:
                    input_shares = {}
                tool_costs, unattr_in, unattr_out, unattr_cost = _attribute_cost(
                    model, usage, output_shares, input_shares
                )

            # Fold THIS event's content into running tallies AFTER attribution
            for block in content:
                if not isinstance(block, dict):
                    continue
                b = _block_bytes(block)
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name")
                    bid = block.get("id")
                    if isinstance(name, str) and name and isinstance(bid, str) and bid:
                        tool_use_id_to_name[bid] = name
                        bytes_in_context_by_tool[name] = (
                            bytes_in_context_by_tool.get(name, 0) + b
                        )
                    else:
                        non_tool_bytes_in_context += b
                elif btype == "tool_result":
                    bid = block.get("tool_use_id")
                    name = tool_use_id_to_name.get(bid, "__unknown__") if bid else "__unknown__"
                    bytes_in_context_by_tool[name] = (
                        bytes_in_context_by_tool.get(name, 0) + b
                    )
                else:
                    non_tool_bytes_in_context += b

            yield RawEvent(
                source_file=str(path),
                line_number=lineno,
                event_type=event_type,
                session_id=ev.get("sessionId", session_id),
                request_id=ev.get("requestId"),
                message_id=msg.get("id"),
                uuid=ev.get("uuid"),
                timestamp=_parse_timestamp(ev.get("timestamp", "")),
                usage=usage,
                model=model,
                is_sidechain=ev.get("isSidechain", is_sidechain),
                stop_reason=msg.get("stop_reason"),
                tool_use_count=tool_use_count,
                tool_error_count=tool_error_count,
                tool_names=tool_names,
                cwd=cwd,
                tool_costs=tool_costs,
                unattributed_input_tokens=unattr_in,
                unattributed_output_tokens=unattr_out,
                unattributed_cost_usd=unattr_cost,
            )
```

- [ ] **Step 4: Run all parser tests to verify it passes and nothing regressed**

```bash
uv run pytest tests/test_per_tool_cost.py tests/test_parser.py -v
```

Expected: lingering test passes; existing parser tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/ingest/parser.py tests/test_per_tool_cost.py
git commit -m "feat(parser): stateful per-session attribution + lingering input"
```

---

## Task 5: Compaction reset heuristic

**Files:**
- Modify: `tests/test_per_tool_cost.py` (no code change needed if Task 4 implemented it)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_per_tool_cost.py`:

```python
def test_compaction_resets_tallies(tmp_path):
    """Turn 1 calls Read with a 50 KB result. Turn 2 has input_tokens that drop
    sharply (compaction). Turn 2 should not attribute to Read because the
    context was reset by the compaction heuristic."""
    big_result = "x" * 50_000
    lines = [
        {
            "type": "assistant", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u1", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "tool_use",
                "usage": {"input_tokens": 10_000, "output_tokens": 20,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                             "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "uuid": "u2", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": big_result}
            ]},
        },
        # Compaction: input_tokens drops from peak (60_000) to 500 (< 20%).
        {
            "type": "assistant", "timestamp": "2026-05-15T10:02:00Z",
            "sessionId": "s1", "requestId": "r2", "uuid": "u3", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m2", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 500, "output_tokens": 30,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "Compacted then asked again."}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]

    # Pre-compaction turn — Read appears on output side
    assert "Read" in assistants[0].tool_costs

    # Post-compaction turn — running tallies were reset before attribution
    post = assistants[1]
    assert "Read" not in post.tool_costs
    assert post.unattributed_input_tokens >= 0
```

- [ ] **Step 2: Run test**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k compaction
```

Expected: PASS (the reset logic was added in Task 4). If FAIL, re-check Task 4 step 3.

- [ ] **Step 3: Commit**

```bash
git add tests/test_per_tool_cost.py
git commit -m "test(parser): compaction reset heuristic"
```

---

## Task 6: Unknown tool_use_id bucketing

**Files:**
- Modify: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_unknown_tool_use_id_goes_to_unknown_bucket(tmp_path):
    """A tool_result whose tool_use_id has no matching prior tool_use block
    (e.g. compaction lost the call) lands in __unknown__ — never crashes."""
    lines = [
        # No preceding tool_use — the tool_result is orphaned.
        {
            "type": "user", "timestamp": "2026-05-15T10:00:00Z",
            "sessionId": "s1", "uuid": "u1", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "leftover"}
            ]},
        },
        {
            "type": "assistant", "timestamp": "2026-05-15T10:01:00Z",
            "sessionId": "s1", "requestId": "r1", "uuid": "u2", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {
                "id": "m1", "role": "assistant", "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 10,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, "s1.jsonl", lines)
    events = list(parse_file(p))
    assistants = [e for e in events if e.event_type == "assistant"]
    t1 = assistants[0]
    # __unknown__ shows up as a tool name in the attribution
    assert "__unknown__" in t1.tool_costs
    assert t1.tool_costs["__unknown__"].input_tokens > 0
```

- [ ] **Step 2: Run test**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k unknown_tool_use_id
```

Expected: PASS (Task 4 already implements the `__unknown__` fallback).

- [ ] **Step 3: Commit**

```bash
git add tests/test_per_tool_cost.py
git commit -m "test(parser): unknown tool_use_id bucketing"
```

---

## Task 7: Golden-file fixture + reconciliation test

**Files:**
- Create: `tests/fixtures/per_tool_basic.jsonl`
- Modify: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Build the fixture**

Create `tests/fixtures/per_tool_basic.jsonl`:

```jsonl
{"type":"assistant","timestamp":"2026-05-15T10:00:00Z","sessionId":"sess-pt","requestId":"req-1","uuid":"u1","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"m1","role":"assistant","stop_reason":"tool_use","usage":{"input_tokens":200,"output_tokens":40,"cache_read_input_tokens":0,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"Reading."},{"type":"tool_use","id":"tu1","name":"Read","input":{"file_path":"/etc/hostname"}}]}}
{"type":"user","timestamp":"2026-05-15T10:00:10Z","sessionId":"sess-pt","uuid":"u2","isSidechain":false,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu1","content":"my-hostname\n"}]}}
{"type":"assistant","timestamp":"2026-05-15T10:00:20Z","sessionId":"sess-pt","requestId":"req-2","uuid":"u3","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"m2","role":"assistant","stop_reason":"tool_use","usage":{"input_tokens":300,"output_tokens":50,"cache_read_input_tokens":200,"cache_creation_input_tokens":0},"content":[{"type":"tool_use","id":"tu2","name":"Bash","input":{"command":"ls /tmp"}}]}}
{"type":"user","timestamp":"2026-05-15T10:00:30Z","sessionId":"sess-pt","uuid":"u4","isSidechain":false,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu2","content":"file1\nfile2\nfile3\n"}]}}
{"type":"assistant","timestamp":"2026-05-15T10:00:40Z","sessionId":"sess-pt","requestId":"req-3","uuid":"u5","isSidechain":false,"model":"claude-opus-4-7","message":{"id":"m3","role":"assistant","stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":500,"cache_creation_input_tokens":0},"content":[{"type":"text","text":"Done."}]}}
```

- [ ] **Step 2: Write the test**

Append to `tests/test_per_tool_cost.py`:

```python
def test_golden_fixture_reconciliation():
    """Three-turn fixture with Read + Bash. Per-tool cost + unattributed = total cost
    within 5% reconciliation tolerance."""
    from tokenol.metrics.cost import cost_for_turn
    events = list(parse_file(FIXTURES / "per_tool_basic.jsonl"))
    assistants = [e for e in events if e.event_type == "assistant"]
    assert len(assistants) == 3

    total_attributed_cost = 0.0
    total_unattr_cost = 0.0
    total_turn_cost = 0.0
    seen_tools: set[str] = set()

    for ev in assistants:
        for tc in ev.tool_costs.values():
            total_attributed_cost += tc.cost_usd
            seen_tools.add(tc.tool_name)
        total_unattr_cost += ev.unattributed_cost_usd
        total_turn_cost += cost_for_turn(ev.model, ev.usage).total_usd

    # Both Read and Bash were called
    assert seen_tools == {"Read", "Bash"}

    # Reconciliation: per-tool + unattributed ≈ total turn cost (within 5%)
    reconciled = total_attributed_cost + total_unattr_cost
    assert abs(reconciled - total_turn_cost) / max(total_turn_cost, 1e-9) < 0.05


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
```

(Move the `FIXTURES` line to the top of the file alongside the other module-level imports — keep this snippet self-contained but unify on next edit if you prefer.)

- [ ] **Step 3: Run test**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k golden
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/per_tool_basic.jsonl tests/test_per_tool_cost.py
git commit -m "test(parser): golden-file reconciliation fixture"
```

---

## Task 8: Wire ToolCost flow through builder.py

**Files:**
- Modify: `src/tokenol/ingest/builder.py`
- Test: `tests/test_per_tool_cost.py`

The builder converts `RawEvent` → `Turn`. New fields need to propagate.

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_builder_propagates_tool_costs(tmp_path):
    from tokenol.ingest.builder import build_sessions
    fixture = FIXTURES / "per_tool_basic.jsonl"
    sessions = build_sessions([fixture])
    assert len(sessions) == 1
    session = sessions[0]
    assert len(session.turns) == 3

    # Find the turn that called Read (first turn)
    t1 = session.turns[0]
    assert "Read" in t1.tool_costs
    assert t1.tool_costs["Read"].output_tokens > 0
    # unattributed scalars survive the build
    assert t1.unattributed_cost_usd >= 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k builder_propagates
```

Expected: FAIL — `Turn.tool_costs` is still empty because builder hasn't been wired.

- [ ] **Step 3: Modify `src/tokenol/ingest/builder.py`**

Find the `Turn(...)` construction in `build_turns` and add the new fields. The existing call already mirrors `RawEvent` field-by-field — append the four new fields:

```python
Turn(
    # ... existing fields ...
    tool_use_count=ev.tool_use_count,
    tool_error_count=ev.tool_error_count,
    tool_names=ev.tool_names,
    # NEW:
    tool_costs=ev.tool_costs,
    unattributed_input_tokens=ev.unattributed_input_tokens,
    unattributed_output_tokens=ev.unattributed_output_tokens,
    unattributed_cost_usd=ev.unattributed_cost_usd,
)
```

(Use `Read` first to find the exact lines and surrounding context; the existing call may live inside a function `build_turns`.)

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_per_tool_cost.py tests/test_parser.py -v
```

Expected: builder_propagates passes; nothing else regressed.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/ingest/builder.py tests/test_per_tool_cost.py
git commit -m "feat(builder): propagate tool_costs from RawEvent to Turn"
```

---

## Task 9: `ToolCostRollup` + `build_tool_cost_rollups`

**Files:**
- Modify: `src/tokenol/metrics/rollups.py`
- Test: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_build_tool_cost_rollups_across_turns():
    from tokenol.ingest.builder import build_sessions
    from tokenol.metrics.rollups import build_tool_cost_rollups

    sessions = build_sessions([FIXTURES / "per_tool_basic.jsonl"])
    all_turns = [t for s in sessions for t in s.turns]
    rollups = build_tool_cost_rollups(all_turns)

    by_name = {r.tool_name: r for r in rollups}
    assert "Read" in by_name and "Bash" in by_name
    assert by_name["Read"].cost_usd > 0
    assert by_name["Read"].invocations == 1
    # last_active is the turn timestamp when the tool was called
    assert by_name["Read"].last_active is not None
    # Sorted desc by cost
    assert rollups == sorted(rollups, key=lambda r: r.cost_usd, reverse=True)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k build_tool_cost_rollups
```

Expected: ImportError.

- [ ] **Step 3: Implement in `src/tokenol/metrics/rollups.py`**

Add (next to existing rollup builders):

```python
@dataclass
class ToolCostRollup:
    tool_name: str
    invocations: int            # count of turns invoking this tool
    input_tokens: float
    output_tokens: float
    cost_usd: float
    last_active: datetime | None


def build_tool_cost_rollups(turns: list[Turn]) -> list[ToolCostRollup]:
    """Aggregate per-tool cost across *turns*. Skips interrupted turns."""
    buckets: dict[str, ToolCostRollup] = {}
    for turn in turns:
        if turn.is_interrupted:
            continue
        for name, tc in turn.tool_costs.items():
            if name not in buckets:
                buckets[name] = ToolCostRollup(
                    tool_name=name, invocations=0,
                    input_tokens=0.0, output_tokens=0.0, cost_usd=0.0,
                    last_active=None,
                )
            r = buckets[name]
            r.input_tokens += tc.input_tokens
            r.output_tokens += tc.output_tokens
            r.cost_usd += tc.cost_usd
            # Invocation counts: increment iff the tool was actually called in this turn
            # (not just lingering in context). Use tool_names which is the invocation Counter.
            if name in turn.tool_names:
                r.invocations += turn.tool_names[name]
                if r.last_active is None or turn.timestamp > r.last_active:
                    r.last_active = turn.timestamp
    return sorted(buckets.values(), key=lambda r: r.cost_usd, reverse=True)
```

Ensure `datetime` is imported at the top of `rollups.py` (it likely already is).

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k build_tool_cost_rollups
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/metrics/rollups.py tests/test_per_tool_cost.py
git commit -m "feat(rollups): ToolCostRollup + build_tool_cost_rollups"
```

---

## Task 10: `build_tool_cost_daily` 30-day series

**Files:**
- Modify: `src/tokenol/metrics/rollups.py`
- Test: `tests/test_per_tool_cost.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_tool_cost_daily_zero_fills():
    from datetime import date, timedelta
    from tokenol.ingest.builder import build_sessions
    from tokenol.metrics.rollups import build_tool_cost_daily

    sessions = build_sessions([FIXTURES / "per_tool_basic.jsonl"])
    all_turns = [t for s in sessions for t in s.turns]
    today = date(2026, 5, 15)
    series = build_tool_cost_daily(all_turns, tool_name="Read", days=30, today=today)
    assert len(series) == 30
    # All days have a (date, cost_usd) shape; one day has nonzero cost (the fixture's 2026-05-15)
    nonzero = [p for p in series if p.cost_usd > 0]
    assert len(nonzero) == 1
    assert nonzero[0].date == today
```

- [ ] **Step 2: Run test**

```bash
uv run pytest tests/test_per_tool_cost.py -v -k tool_cost_daily
```

Expected: FAIL (no such function).

- [ ] **Step 3: Implement**

Append to `src/tokenol/metrics/rollups.py`:

```python
@dataclass
class DailyToolCost:
    date: date
    cost_usd: float


def build_tool_cost_daily(
    turns: list[Turn], *, tool_name: str, days: int = 30, today: date | None = None
) -> list[DailyToolCost]:
    """Per-day cost_usd for *tool_name* over the last *days* days, zero-filled."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    buckets: dict[date, float] = {start + timedelta(days=i): 0.0 for i in range(days)}
    for turn in turns:
        if turn.is_interrupted:
            continue
        tc = turn.tool_costs.get(tool_name)
        if not tc:
            continue
        d = turn.timestamp.date()
        if d in buckets:
            buckets[d] += tc.cost_usd
    return [DailyToolCost(date=d, cost_usd=c) for d, c in sorted(buckets.items())]
```

Make sure `from datetime import date, timedelta` is in the imports.

- [ ] **Step 4: Run test**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/metrics/rollups.py tests/test_per_tool_cost.py
git commit -m "feat(rollups): build_tool_cost_daily zero-filled series"
```

---

## Task 11: `_rank_dict_with_others` sibling helper

**Files:**
- Modify: `src/tokenol/metrics/rollups.py`
- Test: `tests/test_per_tool_cost.py`

The existing `_rank_counter_with_others` works on `Counter[str]`. The new $-ranked surfaces need the same shape over `dict[str, float]`.

- [ ] **Step 1: Test**

```python
def test_rank_dict_with_others_top_n_plus_other():
    from tokenol.metrics.rollups import _rank_dict_with_others
    d = {"Read": 10.0, "Bash": 7.0, "Grep": 5.0, "Edit": 3.0, "Glob": 1.5}
    out = _rank_dict_with_others(d, top_n=3)
    names = [r["name"] for r in out]
    assert names == ["Read", "Bash", "Grep", "other"]
    assert out[-1]["name"] == "other"
    assert abs(out[-1]["value"] - 4.5) < 1e-9    # Edit + Glob
    assert out[-1].get("count") == 2             # number rolled up


def test_rank_dict_with_others_skips_other_when_short():
    from tokenol.metrics.rollups import _rank_dict_with_others
    d = {"Read": 10.0, "Bash": 7.0}
    out = _rank_dict_with_others(d, top_n=5)
    names = [r["name"] for r in out]
    assert names == ["Read", "Bash"]   # no "other" row when nothing to roll up
```

- [ ] **Step 2: Run test**

Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `rollups.py`, near `_rank_counter_with_others`:

```python
def _rank_dict_with_others(values: dict[str, float], top_n: int) -> list[dict]:
    """Top-N by value, sum the rest into one 'other' row. Returns list of
    {name, value, [count]} dicts."""
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    head = ranked[:top_n]
    tail = ranked[top_n:]
    out = [{"name": name, "value": v} for name, v in head]
    if tail:
        out.append({"name": "other", "value": sum(v for _, v in tail), "count": len(tail)})
    return out
```

- [ ] **Step 4: Run test**

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/metrics/rollups.py tests/test_per_tool_cost.py
git commit -m "feat(rollups): _rank_dict_with_others sibling helper"
```

---

## Task 12: Extend `/api/breakdown/tools` with cost + unattributed

**Files:**
- Modify: `src/tokenol/serve/app.py` (around line 688)
- Test: `tests/test_serve_app.py`

- [ ] **Step 1: Write the test**

Existing tests in `test_serve_app.py` use this pattern: `_mock_dirs(tmp_path)` context manager + copy the fixture into `tmp_path / "projects" / "<sess>.jsonl"` + `create_app(ServerConfig())` + `AsyncClient(transport=ASGITransport(...))`. Mirror it. Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_breakdown_tools_includes_cost_and_unattributed(tmp_path: Path) -> None:
    """Tool Mix endpoint surfaces cost_usd per tool plus a sentinel
    __unattributed__ row so the frontend can render the dim reconciliation row."""
    dst = tmp_path / "projects" / "sess-pt.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "per_tool_basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/breakdown/tools?range=all")

    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"
    assert "tools" in data
    rows = data["tools"]
    for row in rows:
        assert "cost_usd" in row
        assert "name" in row
    sentinels = [r for r in rows if r["name"] == "__unattributed__"]
    assert len(sentinels) == 1
    assert sentinels[0]["cost_usd"] >= 0
```

- [ ] **Step 2: Run test**

Expected: FAIL — sentinel row absent, `cost_usd` not in payload.

- [ ] **Step 3: Modify `/api/breakdown/tools` in `src/tokenol/serve/app.py`**

Replace the existing body (around lines 688–703):

```python
@app.get("/api/breakdown/tools")
async def api_breakdown_tools(request: Request, range: str = "30d"):
    _validate_breakdown_range(range)
    result = _current_snapshot_result(request)
    since = range_since(range, date.today()) if range != "all" else None

    cost_by_tool: dict[str, float] = {}
    tokens_by_tool: Counter[str] = Counter()
    unattr_cost = 0.0
    last_active: dict[str, datetime] = {}

    for t in result.turns:
        if since is not None and t.timestamp.date() < since:
            continue
        if t.is_interrupted:
            continue
        tokens_by_tool.update(t.tool_names)
        for name, tc in t.tool_costs.items():
            cost_by_tool[name] = cost_by_tool.get(name, 0.0) + tc.cost_usd
            if name in t.tool_names:
                if name not in last_active or t.timestamp > last_active[name]:
                    last_active[name] = t.timestamp
        unattr_cost += t.unattributed_cost_usd

    # Top-N + other, by cost
    ranked = _rank_dict_with_others(cost_by_tool, top_n=10)
    # Enrich with invocation count + last_active where available
    for row in ranked:
        name = row["name"]
        if name in tokens_by_tool:
            row["count"] = tokens_by_tool[name]
        if name in last_active:
            row["last_active"] = last_active[name].isoformat()
        # Rename "value" → "cost_usd" for the canonical API field
        row["cost_usd"] = row.pop("value")
    ranked.append({"name": "__unattributed__", "cost_usd": unattr_cost})

    return JSONResponse({"range": range, "tools": ranked})
```

Add `from tokenol.metrics.rollups import _rank_dict_with_others` to the existing imports (or wherever `_rank_counter_with_others` is already imported from).

- [ ] **Step 4: Run all serve tests**

```bash
uv run pytest tests/test_serve_app.py -v -k tools
```

Expected: new test passes; existing tools-endpoint tests still pass (the response shape gained fields but didn't lose any).

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/app.py tests/test_serve_app.py
git commit -m "feat(api): extend /api/breakdown/tools with cost + unattributed sentinel"
```

---

## Task 13: Extend `/api/tool/{name}` with scorecards + daily + by_project + by_model

**Files:**
- Modify: `src/tokenol/serve/state.py` (`build_tool_detail` at line 1324)
- Test: `tests/test_serve_app.py`

The route handler in `app.py` (line 483) calls `build_tool_detail(name, result.turns, result.sessions)` — keep it thin. All new logic goes in `state.py`.

- [ ] **Step 1: Write the test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_tool_detail_includes_scorecards_and_breakdowns(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-pt.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "per_tool_basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/tool/Read")

    assert resp.status_code == 200
    data = resp.json()
    sc = data["scorecards"]
    assert sc["cost_usd"] > 0
    assert sc["invocations"] >= 1
    assert "output_tokens" in sc
    top = sc["top_project"]
    assert "name" in top and "cost_usd" in top and "share" in top
    daily = data["daily_cost"]
    assert len(daily) == 30
    assert all("date" in d and "cost_usd" in d for d in daily)
    bp = data["by_project"]
    assert len(bp) >= 1
    assert all({"cwd_b64", "project_label", "cost_usd", "invocations", "last_active"} <= set(p) for p in bp)
    bm = data["by_model"]
    assert len(bm) >= 1
    assert all({"name", "cost_usd", "invocations"} <= set(m) for m in bm)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_serve_app.py -v -k tool_detail
```

Expected: FAIL (new keys not in response).

- [ ] **Step 3: Modify `build_tool_detail` in `src/tokenol/serve/state.py`**

The existing function (lines 1324–1375) returns `{name, total_invocations, projects_using_tool, models_using_tool}`. Extend it to also return `{scorecards, daily_cost, by_project, by_model}`. Replace the function body:

```python
def build_tool_detail(
    name: str,
    turns: list[Turn],
    sessions: list[Session],
) -> dict | None:
    """Build the tool drill-down payload for GET /api/tool/{name}."""
    tool_turns = [
        t for t in turns
        if not t.is_interrupted and t.tool_names.get(name, 0) > 0
    ]
    if not tool_turns:
        return None

    cwd_by_sid = _grouped_cwd_by_sid(sessions)

    # Aggregate cost + token + invocation data
    total_cost = 0.0
    total_output_tokens = 0.0
    total_invocations = 0
    proj_cost: defaultdict[str, float] = defaultdict(float)
    proj_invs: defaultdict[str, int] = defaultdict(int)
    proj_last: dict[str, datetime] = {}
    model_cost: defaultdict[str, float] = defaultdict(float)
    model_invs: defaultdict[str, int] = defaultdict(int)

    for t in tool_turns:
        tc = t.tool_costs.get(name)
        if tc:
            total_cost += tc.cost_usd
            total_output_tokens += tc.output_tokens
        invs = t.tool_names.get(name, 0)
        total_invocations += invs
        cwd = cwd_by_sid.get(t.session_id, "(unknown)")
        proj_cost[cwd] += tc.cost_usd if tc else 0.0
        proj_invs[cwd] += invs
        if cwd not in proj_last or t.timestamp > proj_last[cwd]:
            proj_last[cwd] = t.timestamp
        model = t.model or "(unknown)"
        model_cost[model] += tc.cost_usd if tc else 0.0
        model_invs[model] += invs

    # Total spend across ALL turns (for share calc)
    grand_total_cost = sum(tt.cost_usd for tt in turns if not tt.is_interrupted) or 1.0

    # Top project by cost
    top_cwd = max(proj_cost.items(), key=lambda kv: kv[1], default=("(unknown)", 0.0))
    top_project = {
        "name": top_cwd[0].rsplit("/", 1)[-1] if top_cwd[0] != "(unknown)" else "—",
        "cost_usd": top_cwd[1],
        "share": top_cwd[1] / total_cost if total_cost > 0 else 0.0,
    }

    # 7-day invocations
    today = date.today()
    seven_days_ago_ts = datetime.combine(
        today - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc
    )
    invs_7d = sum(
        t.tool_names.get(name, 0) for t in tool_turns if t.timestamp >= seven_days_ago_ts
    )

    daily = build_tool_cost_daily(turns, tool_name=name, days=30)

    by_project = sorted(
        [{
            "cwd_b64": encode_cwd(cwd) if cwd != "(unknown)" else None,
            "project_label": cwd.rsplit("/", 1)[-1] if cwd != "(unknown)" else "(unknown)",
            "cost_usd": proj_cost[cwd],
            "invocations": proj_invs[cwd],
            "last_active": proj_last[cwd].isoformat(),
        } for cwd in proj_cost],
        key=lambda r: -r["cost_usd"],
    )
    by_model = sorted(
        [{"name": m, "cost_usd": model_cost[m], "invocations": model_invs[m]}
         for m in model_cost],
        key=lambda r: -r["cost_usd"],
    )

    return {
        "name": name,
        "total_invocations": total_invocations,
        "scorecards": {
            "cost_usd": total_cost,
            "output_tokens": total_output_tokens,
            "invocations": total_invocations,
            "invocations_7d": invs_7d,
            "share_of_total": total_cost / grand_total_cost,
            "top_project": top_project,
        },
        "daily_cost": [{"date": d.date.isoformat(), "cost_usd": d.cost_usd} for d in daily],
        "by_project": by_project,
        "by_model": by_model,
    }
```

Imports to ensure at the top of `state.py`: `from datetime import date, datetime, timedelta, timezone` (some may already be there), `from tokenol.metrics.rollups import build_tool_cost_daily`.

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/test_serve_app.py -v -k tool_detail
```

Expected: PASS. If any existing tests check for the old `projects_using_tool` / `models_using_tool` keys, they still pass because those keys are removed but the new keys cover the same data. Remove the now-unused old keys from `build_tool_detail` (already done in the rewrite above) — check `test_serve_app.py` for any assertions on the old keys and update.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/state.py tests/test_serve_app.py
git commit -m "feat(api): /api/tool/{name} scorecards + daily + by_project + by_model"
```

---

## Task 14: Extend `/api/project/{cwd_b64}` with `by_tool`

**Files:**
- Modify: `src/tokenol/serve/state.py` (`build_project_detail` at line 1514)
- Modify: `tests/fixtures/per_tool_basic.jsonl` (add a `system` event with `cwd` so the test is deterministic)
- Test: `tests/test_serve_app.py`

The route handler in `app.py` (line 304) calls `build_project_detail(...)`. Extend the builder, not the handler.

- [ ] **Step 1: Add a `cwd`-carrying system event to the fixture**

Prepend this line to `tests/fixtures/per_tool_basic.jsonl`:

```jsonl
{"type":"system","timestamp":"2026-05-15T09:59:00Z","sessionId":"sess-pt","uuid":"s0","isSidechain":false,"cwd":"/home/u/per-tool-fixture"}
```

(Re-verify Task 7's golden-file test still passes after adding this line — the fixture has more events but the assertions are about specific tools/turns.)

- [ ] **Step 2: Write the test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_project_detail_includes_by_tool(tmp_path: Path) -> None:
    import base64
    dst = tmp_path / "projects" / "sess-pt.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "per_tool_basic.jsonl").read_bytes())

    cwd_b64 = base64.urlsafe_b64encode(b"/home/u/per-tool-fixture").decode().rstrip("=")

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/project/{cwd_b64}")

    assert resp.status_code == 200
    data = resp.json()
    bt = data["by_tool"]
    assert len(bt) >= 1
    assert all({"name", "cost_usd", "invocations", "last_active"} <= set(r) for r in bt)
    assert bt == sorted(bt, key=lambda r: -r["cost_usd"])
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_serve_app.py -v -k by_tool
```

Expected: FAIL (`by_tool` not in response).

- [ ] **Step 4: Modify `build_project_detail` in `src/tokenol/serve/state.py`**

Open `state.py` and locate `build_project_detail` at line 1514. Inside the function, after the existing aggregation loops over `project_turns`, add:

```python
    tool_cost: defaultdict[str, float] = defaultdict(float)
    tool_invs: defaultdict[str, int] = defaultdict(int)
    tool_last: dict[str, datetime] = {}
    for t in project_turns:
        for tname, tc in t.tool_costs.items():
            tool_cost[tname] += tc.cost_usd
        for tname, count in t.tool_names.items():
            tool_invs[tname] += count
            if tname not in tool_last or t.timestamp > tool_last[tname]:
                tool_last[tname] = t.timestamp

    by_tool = sorted(
        [{
            "name": tname,
            "cost_usd": tool_cost[tname],
            "invocations": tool_invs[tname],
            "last_active": tool_last[tname].isoformat(),
        } for tname in tool_invs],
        key=lambda r: -r["cost_usd"],
    )
```

(`project_turns` is the existing local variable inside `build_project_detail` — verify the name by reading the function. If it's called something else, use that.)

Then add `"by_tool": by_tool,` to the returned dict.

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_serve_app.py -v -k by_tool
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/state.py tests/fixtures/per_tool_basic.jsonl tests/test_serve_app.py
git commit -m "feat(api): /api/project/{cwd_b64} by_tool"
```

---

## Task 15: Extend `/api/model/{name}` with `by_tool`

**Files:**
- Modify: `src/tokenol/serve/state.py` (`build_model_detail` at line 1276)
- Test: `tests/test_serve_app.py`

- [ ] **Step 1: Write the test**

Append to `tests/test_serve_app.py`:

```python
@pytest.mark.asyncio
async def test_model_detail_includes_by_tool(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-pt.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "per_tool_basic.jsonl").read_bytes())

    from httpx import ASGITransport, AsyncClient

    with _mock_dirs(tmp_path):
        app = create_app(ServerConfig())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/model/claude-opus-4-7")

    assert resp.status_code == 200
    data = resp.json()
    bt = data["by_tool"]
    assert len(bt) >= 1
    assert all({"name", "cost_usd", "invocations"} <= set(r) for r in bt)
    assert bt == sorted(bt, key=lambda r: -r["cost_usd"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_serve_app.py -v -k model_detail_includes
```

Expected: FAIL (`by_tool` not in response).

- [ ] **Step 3: Modify `build_model_detail` in `src/tokenol/serve/state.py`**

Open `state.py` and locate `build_model_detail` at line 1276. Inside the function, after the existing aggregation loop over model-scoped turns, add:

```python
    tool_cost: defaultdict[str, float] = defaultdict(float)
    tool_invs: defaultdict[str, int] = defaultdict(int)
    for t in model_turns:
        for tname, tc in t.tool_costs.items():
            tool_cost[tname] += tc.cost_usd
        for tname, count in t.tool_names.items():
            tool_invs[tname] += count

    by_tool = sorted(
        [{
            "name": tname,
            "cost_usd": tool_cost[tname],
            "invocations": tool_invs[tname],
        } for tname in tool_invs],
        key=lambda r: -r["cost_usd"],
    )
```

(`model_turns` is the existing local variable inside `build_model_detail` — verify the name. If it's called something else, use that.)

Then add `"by_tool": by_tool,` to the returned dict.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_serve_app.py -v -k model_detail_includes
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/state.py tests/test_serve_app.py
git commit -m "feat(api): /api/model/{name} by_tool"
```

---

## Task 16: `renderRankedBars` shared component

**Files:**
- Modify: `src/tokenol/serve/static/components.js`
- Modify: `src/tokenol/serve/static/styles.css`

This is a vanilla-JS component. No JS test harness in the repo, so verification is by visual smoke test in the browser (server already exists; reload to verify).

- [ ] **Step 1: Add CSS to `styles.css`**

Append:

```css
/* Ranked bar list — used on Breakdown Tool Mix, tool detail, project detail, model detail */
.ranked-bars {
  display: grid;
  grid-template-columns: 160px 1fr 80px;
  column-gap: 10px;
  row-gap: 8px;
  align-items: center;
}
.ranked-bars .rb-label {
  text-align: right;
  color: var(--fg);
}
.ranked-bars .rb-sublabel {
  opacity: 0.45;
  font-size: 10px;
  margin-top: 2px;
}
.ranked-bars .rb-track {
  height: 18px;
  background: var(--bar-track, #1a1a1a);
  border-radius: 3px;
  position: relative;
}
.ranked-bars .rb-fill {
  position: absolute;
  inset: 0;
  background: var(--accent, #a66408);
  border-radius: 3px;
  opacity: 0.85;
}
.ranked-bars .rb-value {
  text-align: right;
  color: var(--accent, #a66408);
}
.ranked-bars .rb-row.is-other .rb-fill {
  background: #5a5a5a;
  opacity: 0.75;
}
.ranked-bars .rb-row.is-other .rb-value {
  color: #bbb;
}
.ranked-bars .rb-row.is-unattributed .rb-label,
.ranked-bars .rb-row.is-unattributed .rb-value {
  opacity: 0.45;
  font-style: italic;
}
.ranked-bars .rb-row.is-unattributed .rb-fill {
  background: #3a3a3a;
  opacity: 0.55;
}
.ranked-bars a.rb-row {
  text-decoration: none;
  display: contents;
}
.ranked-bars a.rb-row:hover .rb-label {
  text-decoration: underline;
}
```

- [ ] **Step 2: Add component to `components.js`**

Append:

```javascript
/**
 * Render a ranked horizontal bar list into `container`.
 *
 * rows: [{label, sublabel?, value, href?, kind?}]
 *   - kind: undefined | "other" | "unattributed"
 *   - href: optional drill-in URL; clickable rows render as <a>
 * opts: {valueFormat?: (n) => string}
 */
export function renderRankedBars(container, rows, opts = {}) {
  const fmt = opts.valueFormat || ((n) => "$" + n.toFixed(2));
  const max = rows.reduce((m, r) => Math.max(m, Math.abs(r.value) || 0), 0) || 1;

  container.classList.add("ranked-bars");
  container.innerHTML = "";
  for (const r of rows) {
    const rowEl = r.href
      ? document.createElement("a")
      : document.createElement("div");
    rowEl.classList.add("rb-row");
    if (r.kind === "other") rowEl.classList.add("is-other");
    if (r.kind === "unattributed") rowEl.classList.add("is-unattributed");
    if (r.href) rowEl.setAttribute("href", r.href);

    const label = document.createElement("div");
    label.classList.add("rb-label");
    label.textContent = r.label;
    if (r.sublabel) {
      const sub = document.createElement("div");
      sub.classList.add("rb-sublabel");
      sub.textContent = r.sublabel;
      label.appendChild(sub);
    }

    const track = document.createElement("div");
    track.classList.add("rb-track");
    const fill = document.createElement("div");
    fill.classList.add("rb-fill");
    const pct = max > 0 ? Math.max(0, (r.value / max) * 100) : 0;
    fill.style.width = pct + "%";
    track.appendChild(fill);

    const value = document.createElement("div");
    value.classList.add("rb-value");
    value.textContent = fmt(r.value);

    rowEl.appendChild(label);
    rowEl.appendChild(track);
    rowEl.appendChild(value);
    container.appendChild(rowEl);
  }
}
```

- [ ] **Step 3: Visual smoke test**

```bash
# In one terminal
uv run tokenol serve --port 8787
# Then open http://localhost:8787/breakdown — confirm nothing broke (component
# isn't wired into any page yet, so this is just a regression check).
```

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/components.js src/tokenol/serve/static/styles.css
git commit -m "feat(ui): renderRankedBars shared component + CSS"
```

---

## Task 17: Surface 1 — Breakdown Tool Mix in $ mode

**Files:**
- Modify: `src/tokenol/serve/static/breakdown.html`
- Modify: `src/tokenol/serve/static/breakdown.js`

- [ ] **Step 1: Add `data-bdunit="cost"` to the Tool Mix panel**

In `breakdown.html`, find the `<section class="breakdown-panel" aria-labelledby="bp-tools-title">` block (around line 129–135). Add `data-bdunit="cost"` to the panel root so the existing TOKENS/$ toggle wires up automatically.

- [ ] **Step 2: Replace the chart canvas with a bar container**

Within that section, replace:

```html
<div class="breakdown-chart breakdown-chart--tall"><canvas id="chart-tools" height="320"></canvas></div>
```

with:

```html
<div class="breakdown-chart breakdown-chart--tall" id="bp-tools-bars"></div>
```

- [ ] **Step 3: Rewire the renderer in `breakdown.js`**

Find the existing Tool Mix rendering function and replace it. Import the new component at the top:

```javascript
import { renderRankedBars } from "./components.js";
```

Replace the Tool Mix render call with:

```javascript
function _renderToolMix(data, unitMode /* "tokens" | "cost" */) {
  const container = document.getElementById("bp-tools-bars");
  const rows = data.tools.map((t) => {
    let kind;
    if (t.name === "other") kind = "other";
    if (t.name === "__unattributed__") kind = "unattributed";
    const displayName = t.name === "__unattributed__" ? "unattributed" :
                        t.name === "other" ? `other (${t.count || 0})` : t.name;
    return {
      label: displayName,
      sublabel: t.last_active ? `${t.count || 0} calls · ${t.last_active.slice(0, 10)}` : undefined,
      value: unitMode === "cost" ? t.cost_usd : (t.count || 0),
      href: kind ? undefined : `/tool/${encodeURIComponent(t.name)}`,
      kind,
    };
  });
  const fmt = unitMode === "cost"
    ? (n) => "$" + n.toFixed(2)
    : (n) => n.toLocaleString();
  renderRankedBars(container, rows, { valueFormat: fmt });
}
```

Hook this into the existing toggle handler — the same place that currently swaps tokens↔cost for the other Breakdown panels via `data-bdunit="cost"`. Search `breakdown.js` for the existing handler and add a `_renderToolMix(...)` call alongside the existing per-panel re-render.

- [ ] **Step 4: Visual smoke test**

```bash
uv run tokenol serve --port 8787
```

Open `http://localhost:8787/breakdown`. Click the TOKENS/$ toggle. Tool Mix should re-render between tokens-bars and dollar-bars. The `__unattributed__` row should appear at the bottom with dim/italic styling.

- [ ] **Step 5: Commit**

```bash
git add src/tokenol/serve/static/breakdown.html src/tokenol/serve/static/breakdown.js
git commit -m "feat(ui): Breakdown Tool Mix in cost mode via renderRankedBars"
```

---

## Task 18: Surface 2 — Tool detail page (HTML scaffold)

**Files:**
- Modify: `src/tokenol/serve/static/tool.html`

The page today (59 lines) has just a name + summary + two tables. Replace its body with a daily-cost chart container, a 4-card scorecard band, and two bar-chart containers.

- [ ] **Step 1: Replace `tool.html` body**

Open `src/tokenol/serve/static/tool.html`. Keep the `<head>` and `<header>` as-is. Replace the main `<div class="app">` contents from `<div id="tool-error" ...>` through `</div>` (just before `<script>`) with:

```html
<div id="tool-error" class="tl-insufficient hidden"></div>

<div class="section-heading" style="margin-top:24px">
  <h2 id="tool-name" style="font-family:var(--font-serif);font-size:24px">—</h2>
</div>
<div class="ra-summary" id="tool-summary"></div>

<!-- Daily cost chart -->
<section class="panel-card" style="margin-top:14px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
    <div>
      <span class="panel-eyebrow">Daily cost · last 30d</span>
      <span id="tool-daily-total" style="color:var(--accent);margin-left:10px"></span>
    </div>
    <div class="dim" id="tool-daily-peak"></div>
  </div>
  <div class="panel-chart" style="height:160px">
    <canvas id="chart-tool-daily"></canvas>
  </div>
</section>

<!-- Scorecards -->
<div class="scorecard-band" id="tool-scorecards" style="margin-top:14px"></div>

<!-- Cost by project -->
<div class="section-heading"><h2>Cost by project</h2></div>
<div id="tool-by-project" class="ranked-bars-wrap"></div>
<div id="tool-no-projects" class="ra-empty hidden">No project data.</div>

<!-- Cost by model -->
<div class="section-heading"><h2>Cost by model</h2></div>
<div id="tool-by-model" class="ranked-bars-wrap"></div>
<div id="tool-no-models" class="ra-empty hidden">No model data.</div>
```

The `.panel-card`, `.panel-eyebrow`, `.scorecard-band`, `.ra-summary` classes already exist (used on Breakdown / Day pages). If `.ranked-bars-wrap` isn't already styled, drop it — the inner element gets `ranked-bars` from the component itself.

- [ ] **Step 2: Smoke-load the page**

```bash
uv run tokenol serve --port 8787
```

Open `http://localhost:8787/tool/Read`. Expect: page renders skeleton, JS fetches still 404 because the new fields aren't wired yet in `tool.js` (next task).

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/tool.html
git commit -m "feat(ui): tool detail page HTML scaffold for cost surfaces"
```

---

## Task 19: Surface 2 — Tool detail page (JS: scorecards + bar charts)

**Files:**
- Modify: `src/tokenol/serve/static/tool.js`

- [ ] **Step 1: Replace `tool.js` body**

Replace the existing module with:

```javascript
import { renderRankedBars } from "./components.js";

const $ = (id) => document.getElementById(id);
const fmtUSD = (n) => "$" + (n || 0).toFixed(2);
const fmtCompact = (n) => {
  n = n || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return n.toString();
};

function name() {
  const parts = location.pathname.split("/");
  return decodeURIComponent(parts[parts.length - 1] || "");
}

function renderScorecards(sc) {
  const total = sc.cost_usd || 0;
  const cards = [
    { label: "Est. Cost", primary: fmtUSD(total), sub: `~${(sc.share_of_total * 100).toFixed(1)}% of total spend` },
    { label: "Output tokens", primary: fmtCompact(sc.output_tokens), sub: sc.invocations ? `avg ${fmtCompact(sc.output_tokens / sc.invocations)} / call` : "" },
    { label: "Invocations", primary: sc.invocations.toString(), sub: sc.invocations_7d != null ? `7-day: ${sc.invocations_7d}` : "" },
    { label: "Top project", primary: sc.top_project.name || "—", sub: sc.top_project.cost_usd > 0 ? `${fmtUSD(sc.top_project.cost_usd)} (${(sc.top_project.share * 100).toFixed(0)}%)` : "" },
  ];
  $("tool-scorecards").innerHTML = cards.map((c) => `
    <article class="scorecard-card">
      <div class="sc-label">${c.label}</div>
      <div class="sc-primary">${c.primary}</div>
      <div class="sc-sub">${c.sub}</div>
    </article>
  `).join("");
}

function renderDailyChart(daily, totalCost) {
  $("tool-daily-total").textContent = "total " + fmtUSD(totalCost);
  // Find peak
  let peak = { date: null, cost: 0 };
  for (const d of daily) {
    if (d.cost_usd > peak.cost) peak = { date: d.date, cost: d.cost_usd };
  }
  $("tool-daily-peak").textContent = peak.date
    ? `peak ${peak.date.slice(5)} · ${fmtUSD(peak.cost)}`
    : "";

  // Reuse the same Chart.js setup as Overview's daily history.
  // The Overview chart lives in chart.js — its renderDaily(canvas, data, opts)
  // takes a series of {date, value}. Pass cost_usd as value.
  // If your chart.js exports a different name, search for it and adapt.
  const series = daily.map((d) => ({ date: d.date, value: d.cost_usd }));
  // eslint-disable-next-line no-undef
  if (window.renderToolDailyChart) {
    window.renderToolDailyChart($("chart-tool-daily"), series);
  } else {
    // Fallback: inline a minimal Chart.js line config.
    // eslint-disable-next-line no-undef
    new Chart($("chart-tool-daily"), {
      type: "line",
      data: {
        labels: series.map((p) => p.date.slice(5)),   // "MM-DD"
        datasets: [{
          data: series.map((p) => p.value),
          borderColor: "#a66408",
          backgroundColor: "rgba(166, 100, 8, 0.18)",
          fill: true,
          tension: 0.2,
          pointRadius: 0,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
          x: { ticks: { maxTicksLimit: 6 } },
        },
      },
    });
  }
}

function renderBars(containerId, rows, hrefMaker) {
  renderRankedBars(
    $(containerId),
    rows.map((r) => ({
      label: r.project_label || r.name,
      sublabel: r.last_active
        ? `${r.invocations} calls · ${r.last_active.slice(0, 10)}`
        : `${r.invocations} calls`,
      value: r.cost_usd,
      href: hrefMaker(r),
    })),
    { valueFormat: fmtUSD },
  );
}

async function load() {
  const n = name();
  $("tool-name").textContent = n;
  try {
    const resp = await fetch("/api/tool/" + encodeURIComponent(n));
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    renderScorecards(data.scorecards);
    renderDailyChart(data.daily_cost, data.scorecards.cost_usd);
    if (data.by_project.length === 0) {
      $("tool-no-projects").classList.remove("hidden");
    } else {
      renderBars("tool-by-project", data.by_project, (r) => "/project/" + r.cwd_b64);
    }
    if (data.by_model.length === 0) {
      $("tool-no-models").classList.remove("hidden");
    } else {
      renderBars("tool-by-model", data.by_model, (r) => "/model/" + encodeURIComponent(r.name));
    }
  } catch (err) {
    $("tool-error").textContent = "Failed to load: " + err.message;
    $("tool-error").classList.remove("hidden");
  }
}

load();
```

If `window.renderToolDailyChart` doesn't exist, the fallback inline Chart.js config kicks in. If you'd rather extract a real shared helper into `chart.js`, do it in a follow-up commit.

- [ ] **Step 2: Smoke test**

```bash
uv run tokenol serve --port 8787
```

Visit `http://localhost:8787/tool/Read`. Verify daily chart renders, four scorecards show, project/model bars populated. Click a project bar → drills to `/project/{cwd_b64}` (existing project page).

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/static/tool.js
git commit -m "feat(ui): tool detail page — daily chart + scorecards + bars"
```

---

## Task 20: Surface 3 — Project detail page bars

**Files:**
- Modify: `src/tokenol/serve/static/project.html`
- Modify: `src/tokenol/serve/static/project.js`

- [ ] **Step 1: Add bar containers to `project.html`**

Below whatever the page shows today (locate the bottom of the `<div class="app">` body, just before `<script>`), add:

```html
<div class="section-heading"><h2>Cost by tool</h2></div>
<div id="project-by-tool" class="ranked-bars-wrap"></div>

<div class="section-heading"><h2>Cost by model</h2></div>
<div id="project-by-model" class="ranked-bars-wrap"></div>
```

- [ ] **Step 2: Wire renderer in `project.js`**

At the top of `project.js`, add:

```javascript
import { renderRankedBars } from "./components.js";
```

After the existing data fetch completes (the function that consumes `/api/project/{cwd_b64}` payload), add:

```javascript
const fmtUSD = (n) => "$" + (n || 0).toFixed(2);

if (data.by_tool && data.by_tool.length) {
  renderRankedBars(
    document.getElementById("project-by-tool"),
    data.by_tool.map((r) => ({
      label: r.name,
      sublabel: `${r.invocations} calls · ${(r.last_active || "").slice(0, 10)}`,
      value: r.cost_usd,
      href: "/tool/" + encodeURIComponent(r.name),
    })),
    { valueFormat: fmtUSD },
  );
}

if (data.by_model && data.by_model.length) {
  renderRankedBars(
    document.getElementById("project-by-model"),
    data.by_model.map((r) => ({
      label: r.name,
      sublabel: `${r.invocations} calls`,
      value: r.cost_usd,
      href: "/model/" + encodeURIComponent(r.name),
    })),
    { valueFormat: fmtUSD },
  );
}
```

(If the existing project endpoint doesn't yet return `by_model` per-project costs, leave the second block guarded — `data.by_model` will be falsy and nothing renders.)

- [ ] **Step 3: Smoke test**

Visit `http://localhost:8787/project/<some_cwd_b64>` and confirm the new sections render.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/project.html src/tokenol/serve/static/project.js
git commit -m "feat(ui): project detail page — cost-by-tool + cost-by-model bars"
```

---

## Task 21: Surface 4 — Model detail page bar

**Files:**
- Modify: `src/tokenol/serve/static/model.html`
- Modify: `src/tokenol/serve/static/model.js`

- [ ] **Step 1: Add container to `model.html`**

Add (below existing content):

```html
<div class="section-heading"><h2>Cost by tool</h2></div>
<div id="model-by-tool" class="ranked-bars-wrap"></div>
```

- [ ] **Step 2: Wire renderer in `model.js`**

```javascript
import { renderRankedBars } from "./components.js";

// After existing fetch:
if (data.by_tool && data.by_tool.length) {
  renderRankedBars(
    document.getElementById("model-by-tool"),
    data.by_tool.map((r) => ({
      label: r.name,
      sublabel: `${r.invocations} calls`,
      value: r.cost_usd,
      href: "/tool/" + encodeURIComponent(r.name),
    })),
    { valueFormat: (n) => "$" + (n || 0).toFixed(2) },
  );
}
```

- [ ] **Step 3: Smoke test**

Visit `http://localhost:8787/model/claude-opus-4-7` and confirm the bar list renders.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/static/model.html src/tokenol/serve/static/model.js
git commit -m "feat(ui): model detail page — cost-by-tool bars"
```

---

## Task 22: End-to-end smoke + lint + final run

**Files:**
- (No code changes — verification + release gate)

- [ ] **Step 1: Full pytest run**

```bash
uv run pytest -q
```

Expected: all green.

- [ ] **Step 2: Ruff lint**

```bash
uv run ruff check src tests
```

Expected: clean.

- [ ] **Step 3: Manual end-to-end**

```bash
uv run tokenol serve --port 8787
```

Walk all four surfaces in the browser:
- `/breakdown` → toggle TOKENS/$ on Tool Mix; verify dollar bars + `__unattributed__` row + drill-in works.
- `/tool/<top-tool>` → daily chart, four scorecards, project + model bars; drill into a project.
- `/project/<cwd_b64>` → cost-by-tool + cost-by-model bars; drill into a tool.
- `/model/<model-name>` → cost-by-tool bars.

Confirm aggregate reconciliation: on the Breakdown page, the sum of tool bars in $ mode + the `unattributed` row should approximately equal the page's overall Est. Cost scorecard.

- [ ] **Step 4: Update CHANGELOG.md**

Add a 0.6.0 section at the top:

```markdown
## 0.6.0 — 2026-MM-DD

### Added
- Per-tool cost attribution (causal model: byte-share on output + lingering input).
- Breakdown → Tool Mix now respects the TOKENS/$ toggle.
- Tool detail page gains a 30-day cost chart, four scorecards, and cost-by-project + cost-by-model bar charts.
- Project and model detail pages gain a cost-by-tool bar chart.
- New `__unattributed__` row on Breakdown Tool Mix so totals reconcile to overall spend.
```

- [ ] **Step 5: Update RESUME.md**

Append the per-tool-cost feature line per existing format.

- [ ] **Step 6: Commit final**

```bash
git add CHANGELOG.md RESUME.md
git commit -m "chore(release): 0.6.0 notes — per-tool cost attribution"
```

---

## Wrap-up

After Task 22, the branch is ready for review. Release prep (version bump, tag, PyPI publish) is a separate task — follow the same pattern as 0.5.1 (commit `9911e9f`).

Worktree cleanup per project memory: after merge, `git worktree remove .worktrees/per-tool-cost && git branch -d feature/per-tool-cost && git push origin --delete feature/per-tool-cost`.
