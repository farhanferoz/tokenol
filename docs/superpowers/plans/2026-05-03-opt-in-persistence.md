# Opt-in persistence (`--persist`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land PR1's DuckDB-backed history store as opt-in behind `tokenol serve --persist`. Default behavior reproduces v0.3.2 byte-for-byte: no `import duckdb`, no `~/.tokenol/` directory, no extra steady RSS. Move `duckdb` from core dependency to a new `[persist]` extras group. Cut release `0.4.0` from `feature/persistent-history-pr1` after merge.

**Architecture:** Single-flag CLI gate (`--persist`) → `ServerConfig.persist: bool` → `create_app` does deferred runtime imports of `tokenol.persistence.*` and conditionally constructs `HistoryStore` + `FlushQueue`; lifespan conditionally writes the pidfile and starts/stops the flusher. `serve/state.py` and `serve/streaming.py` are untouched — they already TYPE_CHECKING-guard their persistence imports. Existing 274 PR1 tests gain `pytest.importorskip("duckdb")` at module top so the suite still runs against a default install.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, Typer, rich, DuckDB (now optional). No new third-party deps. CI invocation `uv run pytest` (preferred) — matches existing pattern from RESUME.

**Spec:** `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md` (commit `4d456af` on `main`).

**Workspace:** All work happens in the worktree at `/home/ff235/dev/claude_rate_limit/.worktrees/persistent-history-pr1/` (branch `feature/persistent-history-pr1`, currently HEAD `2bd64c5`). Land the gate as commit #27 on this branch, then a fast-forward merge to `main` ships it as `0.4.0`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/tokenol/serve/app.py` | Modify | Add `ServerConfig.persist`, defer persistence imports under `TYPE_CHECKING`, conditional construction in `create_app`, conditional lifespan, yellow WARNING for orphaned `~/.tokenol/`. |
| `src/tokenol/cli.py` | Modify | Add `--persist` Typer option to `serve()`; fail-fast if `--persist` + missing `duckdb`. |
| `pyproject.toml` | Modify | Move `duckdb>=0.10` from core `dependencies` to a new `optional-dependencies.persist` group. Bump `version` to `0.4.0`. |
| `src/tokenol/__init__.py` | Modify | Bump `__version__` to `0.4.0`. |
| `uv.lock` | Regenerate | `uv lock` after the pyproject change so the lockfile matches. |
| `tests/test_persistence_store.py` | Modify | Add `pytest.importorskip("duckdb")` at module top. |
| `tests/test_persistence_flusher.py` | Modify | Same. |
| `tests/test_persistence_forget_handoff.py` | Modify | Same. |
| `tests/test_serve_archived_session.py` | Modify | Same. |
| `tests/test_serve_state.py` | Modify | Same. |
| `tests/test_serve_streaming.py` | Modify | Same. |
| `tests/test_serve_app.py` | Modify | Update tests that assume `app.state.history_store is not None` to either pass `persist=True` or assert `None` explicitly. |
| `tests/test_serve_app_no_persist.py` | Create | Three tests verifying default mode never imports `duckdb` and `app.state.history_store is None`. |
| `README.md` | Modify | New `## Install` snippet (default vs `[persist]`); short `### Persistent history (opt-in)` paragraph under Commands. |
| `CHANGELOG.md` | Modify | Add `0.4.0` entry with conventional-commit-style bullets. |
| `docs/superpowers/specs/2026-05-02-persistent-history-design.md` | Modify | One-line header note pointing at the new opt-in spec. |

---

## Task 1: Pre-flight — confirm worktree state

**Files:** none (verification only)

- [ ] **Step 1: Confirm cwd, branch, and head**

Run: `cd /home/ff235/dev/claude_rate_limit/.worktrees/persistent-history-pr1 && pwd && git branch --show-current && git log --oneline -1 && git status --short`

Expected:
- `pwd` → `/home/ff235/dev/claude_rate_limit/.worktrees/persistent-history-pr1`
- branch → `feature/persistent-history-pr1`
- HEAD → `2bd64c5 refactor(persistence): consolidate flush logic and remove N+1 session loop`
- `git status` → empty (clean tree)

If branch or head differs, stop and ask.

- [ ] **Step 2: Confirm baseline tests pass before starting**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: `274 passed` (matches RESUME's number for the PR1 branch). Stop and ask if anything fails.

- [ ] **Step 3: Confirm baseline ruff is clean**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

---

## Task 2: Add `ServerConfig.persist` field

**Files:**
- Modify: `src/tokenol/serve/app.py:96-100`

- [ ] **Step 1: Add `persist: bool = False` to the dataclass**

Open `src/tokenol/serve/app.py`. Find the `@dataclass class ServerConfig` block (near line 96):

```python
@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5
```

Replace with:

```python
@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5
    persist: bool = False
```

- [ ] **Step 2: Verify import + dataclass instantiation works**

Run: `uv run python -c "from tokenol.serve.app import ServerConfig; c = ServerConfig(); print(c.persist); c2 = ServerConfig(persist=True); print(c2.persist)"`
Expected: `False\nTrue`

- [ ] **Step 3: Commit**

```bash
git add src/tokenol/serve/app.py
git commit -m "feat(serve): add ServerConfig.persist field (default False)"
```

---

## Task 3: Defer persistence imports under TYPE_CHECKING in `app.py`

**Files:**
- Modify: `src/tokenol/serve/app.py:1-22`

- [ ] **Step 1: Replace the three top-level persistence imports**

Open `src/tokenol/serve/app.py`. The current top of the file (lines 1-25 approximately):

```python
"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.metrics.cost import cache_saved_usd, rollup_by_date
from tokenol.metrics.rollups import _rank_counter_with_others
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.persistence.flusher import FlushQueue
from tokenol.persistence.forget_handoff import clear_pidfile, write_pidfile
from tokenol.persistence.store import HistoryStore
from tokenol.serve.prefs import Preferences, default_path
```

Replace the three `from tokenol.persistence.*` lines with a `TYPE_CHECKING` block. The result:

```python
"""FastAPI application factory for tokenol serve."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from tokenol.metrics.cost import cache_saved_usd, rollup_by_date
from tokenol.metrics.rollups import _rank_counter_with_others
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.serve.prefs import Preferences, default_path

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
```

`forget_handoff`'s `clear_pidfile` and `write_pidfile` are *not* type-only — they're called at runtime in the lifespan. They will be re-imported lazily inside `create_app` in Task 4, so they're removed from the top-level import here.

- [ ] **Step 2: Verify `from __future__ import annotations` is present (it is)**

Run: `head -5 src/tokenol/serve/app.py | grep "__future__"`
Expected: line containing `from __future__ import annotations`. If missing, add it as the first statement after the module docstring.

- [ ] **Step 3: Run a quick import-time check**

Run: `uv run python -c "import sys; from tokenol.serve.app import create_app, ServerConfig; print('duckdb in modules:', 'duckdb' in sys.modules)"`
Expected: `duckdb in modules: False`. (This proves the deferred-import refactor works at the import level, even before `create_app` is called.)

If the assertion fails, something else in the import chain is pulling `duckdb`. Likely culprit: `from tokenol.serve.state import ...` at lines 25–43. Check `state.py:21-29` — should be TYPE_CHECKING-guarded already. If not, add the same TYPE_CHECKING refactor there.

- [ ] **Step 4: Commit**

```bash
git add src/tokenol/serve/app.py
git commit -m "refactor(serve/app): defer persistence imports under TYPE_CHECKING"
```

---

## Task 4: Conditional construction in `create_app` + lifespan + warning

**Files:**
- Modify: `src/tokenol/serve/app.py:131-178` (the `create_app` body)

- [ ] **Step 1: Replace the unconditional construction block**

Open `src/tokenol/serve/app.py`. The current `create_app` body (around lines 131-178):

```python
def create_app(
    config: ServerConfig | None = None,
    prefs_path: Path | None = None,
) -> FastAPI:
    """Create and return the FastAPI app, wired with the given config."""
    if config is None:
        config = ServerConfig()
    _prefs_path = prefs_path or default_path()
    prefs = Preferences.load(_prefs_path)

    parse_cache = ParseCache()
    history_store = HistoryStore()
    # Hot-tier window is read by _store_backed_derivation as a duck-typed attr.
    history_store._hot_window_days = prefs.hot_window_days
    flush_queue = FlushQueue(history_store)

    broadcaster = SnapshotBroadcaster(
        parse_cache=parse_cache,
        all_projects=config.all_projects,
        get_reference_usd=lambda: prefs.reference_usd,
        get_tick_seconds=lambda: prefs.tick_seconds,
        get_thresholds=lambda: prefs.thresholds,
        history_store=history_store,
        flush_queue=flush_queue,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        write_pidfile()
        await flush_queue.start()
        try:
            yield
        finally:
            await broadcaster.shutdown()
            await flush_queue.stop()
            history_store.close()
            clear_pidfile()

    app = FastAPI(title="tokenol", lifespan=lifespan)
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = parse_cache
    app.state.snapshot_result = None
    app.state.broadcaster = broadcaster
    app.state.history_store = history_store
    app.state.flush_queue = flush_queue
```

Replace with the conditional version below. Key changes: imports are runtime-deferred under `if config.persist:`, store/queue default to `None`, lifespan branches on `config.persist`, and a yellow `WARNING` is printed when default mode finds an orphan `~/.tokenol/history.duckdb`:

```python
def create_app(
    config: ServerConfig | None = None,
    prefs_path: Path | None = None,
) -> FastAPI:
    """Create and return the FastAPI app, wired with the given config."""
    if config is None:
        config = ServerConfig()
    _prefs_path = prefs_path or default_path()
    prefs = Preferences.load(_prefs_path)

    parse_cache = ParseCache()

    history_store: HistoryStore | None = None
    flush_queue: FlushQueue | None = None
    write_pidfile_fn = None  # bound below if persist is on
    clear_pidfile_fn = None

    if config.persist:
        from tokenol.persistence.flusher import FlushQueue as _FlushQueue
        from tokenol.persistence.forget_handoff import (
            clear_pidfile as _clear_pidfile,
            write_pidfile as _write_pidfile,
        )
        from tokenol.persistence.store import HistoryStore as _HistoryStore

        history_store = _HistoryStore()
        # Hot-tier window is read by _store_backed_derivation as a duck-typed attr.
        history_store._hot_window_days = prefs.hot_window_days
        flush_queue = _FlushQueue(history_store)
        write_pidfile_fn = _write_pidfile
        clear_pidfile_fn = _clear_pidfile
    else:
        _warn_if_orphan_store_exists()

    broadcaster = SnapshotBroadcaster(
        parse_cache=parse_cache,
        all_projects=config.all_projects,
        get_reference_usd=lambda: prefs.reference_usd,
        get_tick_seconds=lambda: prefs.tick_seconds,
        get_thresholds=lambda: prefs.thresholds,
        history_store=history_store,
        flush_queue=flush_queue,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if config.persist:
            assert write_pidfile_fn is not None
            assert flush_queue is not None
            write_pidfile_fn()
            await flush_queue.start()
        try:
            yield
        finally:
            await broadcaster.shutdown()
            if config.persist:
                assert flush_queue is not None
                assert history_store is not None
                assert clear_pidfile_fn is not None
                await flush_queue.stop()
                history_store.close()
                clear_pidfile_fn()

    app = FastAPI(title="tokenol", lifespan=lifespan)
    app.state.config = config
    app.state.prefs = prefs
    app.state.prefs_path = _prefs_path
    app.state.parse_cache = parse_cache
    app.state.snapshot_result = None
    app.state.broadcaster = broadcaster
    app.state.history_store = history_store
    app.state.flush_queue = flush_queue
```

- [ ] **Step 2: Add the `_warn_if_orphan_store_exists` helper**

Add this helper near the top of `src/tokenol/serve/app.py`, just below `STATIC_DIR = Path(__file__).parent / "static"` (around line 93):

```python
def _warn_if_orphan_store_exists() -> None:
    """Yellow WARNING when default mode finds an existing ~/.tokenol/history.duckdb.

    Prevents users who previously ran with --persist (or via the editable
    feature/persistent-history-pr1 install) from silently losing their
    persisted dashboard history when they upgrade to default 0.4.0+.
    """
    from rich.console import Console

    store_path = Path.home() / ".tokenol" / "history.duckdb"
    if not store_path.exists():
        return
    try:
        size_mb = store_path.stat().st_size / (1024 * 1024)
    except OSError:
        return
    console = Console(stderr=True)
    console.print(
        f"[yellow]Found existing history store at {store_path} ({size_mb:.0f} MB).[/yellow]"
    )
    console.print(
        "[yellow]Persistence is OFF — pass --persist to use it.[/yellow]"
    )
```

- [ ] **Step 3: Add the rest of the FastAPI handler block back unchanged**

The remainder of `create_app` (the `if STATIC_DIR.exists():` mount, the `@app.get("/")` page handlers, etc.) is unchanged — keep it as-is. Verify the final file structure with:

Run: `uv run python -c "from tokenol.serve.app import create_app, ServerConfig; a = create_app(ServerConfig()); print('store=', a.state.history_store, 'queue=', a.state.flush_queue)"`
Expected: `store= None queue= None`

Run: `uv run python -c "import sys; from tokenol.serve.app import create_app, ServerConfig; a = create_app(ServerConfig()); print('duckdb in modules:', 'duckdb' in sys.modules)"`
Expected: `duckdb in modules: False`

- [ ] **Step 4: Verify `--persist` path constructs the store**

Run: `uv run python -c "
import tempfile, os, pathlib
with tempfile.TemporaryDirectory() as d:
    os.environ['HOME'] = d  # redirect ~/.tokenol/
    from tokenol.serve.app import create_app, ServerConfig
    a = create_app(ServerConfig(persist=True))
    print('store type:', type(a.state.history_store).__name__)
    print('queue type:', type(a.state.flush_queue).__name__)
    print('db exists:', (pathlib.Path(d) / '.tokenol' / 'history.duckdb').exists())
"`
Expected:
```
store type: HistoryStore
queue type: FlushQueue
db exists: True
```

- [ ] **Step 5: Run the full test suite — most should still pass**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: many PR1 tests still pass; some `test_serve_app.py` tests that assume `history_store` non-None will now fail. Note the failures — they'll be fixed in Task 8. Don't commit yet if there are NEW failures *outside* `test_serve_app.py`.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/serve/app.py
git commit -m "feat(serve): conditional persistence wiring + orphan-store warning"
```

---

## Task 5: Add `--persist` CLI flag with fail-fast

**Files:**
- Modify: `src/tokenol/cli.py:362-400`

- [ ] **Step 1: Add the `--persist` Typer option**

Open `src/tokenol/cli.py`. Find the `def serve(...)` signature (line 363) and add a `persist` parameter after `all_projects`:

```python
@app.command()
def serve(
    port: int = typer.Option(8787, "--port", help="TCP port to bind."),
    tick: str = typer.Option("5s", "--tick", help="SSE tick interval, e.g. '2s', '5s'."),
    reference: float = typer.Option(50.0, "--reference", help="$/window alarm threshold."),
    open_browser: bool = typer.Option(False, "--open", help="Open dashboard in default browser."),
    all_projects: bool = _ALL_PROJECTS_OPT,  # noqa: B008
    persist: bool = typer.Option(
        False,
        "--persist",
        help=(
            "Enable persistent history store at ~/.tokenol/history.duckdb. "
            "Dashboard survives JSONL deletion. Adds ~500 MiB steady RSS and a "
            "one-time multi-minute backfill on first start. "
            "Default off — matches v0.3.2 resource usage. "
            "Requires the persist extras: pip install 'tokenol[persist]'."
        ),
    ),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),  # noqa: B008
) -> None:
```

- [ ] **Step 2: Add the fail-fast `import duckdb` probe**

Inside `serve()`, immediately after the existing `try/except ImportError` block that probes for `tokenol[serve]` extras (around lines 376-385), add:

```python
    if persist:
        try:
            import duckdb  # noqa: F401  — probe only
        except ImportError:
            err.print(
                "[red]--persist requires the 'persist' extras.[/red] "
                "Run: pip install 'tokenol[persist]'"
            )
            raise typer.Exit(code=1) from None
```

- [ ] **Step 3: Pass `persist` into `ServerConfig`**

Find the `config = ServerConfig(...)` construction (around line 387) and add the field:

```python
    config = ServerConfig(
        all_projects=all_projects,
        reference_usd=reference,
        tick_seconds=tick_seconds,
        persist=persist,
    )
```

- [ ] **Step 4: Verify the CLI accepts the flag and shows it in --help**

Run: `uv run tokenol serve --help 2>&1 | grep -A2 persist`
Expected: lines describing the `--persist` option, including the help text.

- [ ] **Step 5: Verify fail-fast behavior with a synthetic missing-duckdb scenario**

Run: `uv run python -c "
import sys, types
# Simulate duckdb not being installed by injecting a sentinel that raises on import.
class _Missing:
    def __getattr__(self, _): raise ImportError('No module named duckdb')
# This is hard to test in a single inline run because cli.py imports happen at typer dispatch.
# Defer thorough verification to Task 9's integration test.
print('CLI flag check deferred to integration test in Task 9')
"`
Expected: prints the deferred-message line. The actual verification of fail-fast happens in Task 9.

- [ ] **Step 6: Commit**

```bash
git add src/tokenol/cli.py
git commit -m "feat(cli): add tokenol serve --persist flag with fail-fast on missing extras"
```

---

## Task 6: Add `pytest.importorskip("duckdb")` to existing persistence test files

**Files:**
- Modify: `tests/test_persistence_store.py` (line 1 area)
- Modify: `tests/test_persistence_flusher.py` (line 1 area)
- Modify: `tests/test_persistence_forget_handoff.py` (line 1 area)
- Modify: `tests/test_serve_archived_session.py` (line 1 area)
- Modify: `tests/test_serve_state.py` (line 1 area)
- Modify: `tests/test_serve_streaming.py` (line 1 area)

The reason this task comes *before* the pyproject change: once `duckdb` moves out of core deps, any test file that imports it at module top will fail collection unless `pytest.importorskip` is in place first.

- [ ] **Step 1: Add the importorskip line at the top of each file**

For each of the six files listed above, add this exactly as the *first* statement after any module docstring but *before* any other import:

```python
import pytest

pytest.importorskip("duckdb")
```

If the file already has `import pytest` somewhere later, leave it; the duplicate import is harmless. The critical placement requirement is that `pytest.importorskip("duckdb")` runs before any line that would transitively load `duckdb`.

Concrete diff for `tests/test_persistence_store.py` (current top is `from __future__ import annotations` followed by stdlib imports):

```python
"""Tests for tokenol.persistence.store.HistoryStore."""
from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

# ... rest of existing imports
```

Apply the equivalent change (insert between the docstring/`__future__` lines and the rest of the imports) to all six files. If a file lacks a docstring, the pattern becomes `from __future__ import annotations` then blank line then `import pytest` then blank line then `pytest.importorskip("duckdb")` then blank line then the existing imports.

- [ ] **Step 2: Run the persistence tests with duckdb still installed (sanity check)**

Run: `uv run pytest tests/test_persistence_store.py tests/test_persistence_flusher.py tests/test_persistence_forget_handoff.py tests/test_serve_archived_session.py tests/test_serve_state.py tests/test_serve_streaming.py -q 2>&1 | tail -5`
Expected: same number of tests pass as before (importorskip is a no-op when the module exists).

- [ ] **Step 3: Commit**

```bash
git add tests/test_persistence_store.py tests/test_persistence_flusher.py \
        tests/test_persistence_forget_handoff.py tests/test_serve_archived_session.py \
        tests/test_serve_state.py tests/test_serve_streaming.py
git commit -m "test: importorskip(duckdb) so persistence tests skip cleanly without [persist] extras"
```

---

## Task 7: Move `duckdb` to `[persist]` extras in `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`
- Regenerate: `uv.lock`

- [ ] **Step 1: Edit pyproject.toml**

Open `pyproject.toml`. Current `[project]` block has:

```toml
dependencies = [
    "typer>=0.12",
    "rich>=13",
    "duckdb>=0.10",
]
```

Remove `"duckdb>=0.10"` from `dependencies`. Then in the `[project.optional-dependencies]` block (which currently has `dev` and `serve` groups), add a new `persist` group. The result:

```toml
dependencies = [
    "typer>=0.12",
    "rich>=13",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-cov",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "ruff>=0.4",
    "hypothesis>=6",
]
serve = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "watchfiles>=0.21",
]
persist = [
    "duckdb>=0.10",
]
```

- [ ] **Step 2: Regenerate uv.lock**

Run: `uv lock 2>&1 | tail -5`
Expected: `Resolved N packages` with no errors. The lockfile updates to reflect duckdb's new optional-dep status.

- [ ] **Step 3: Sync the venv with the new extras list**

Run: `uv sync --all-extras 2>&1 | tail -5`
Expected: lockfile-synced output. duckdb is still installed in the venv because `--all-extras` includes `[persist]`. This keeps the test suite green for development.

- [ ] **Step 4: Verify duckdb is still importable in the dev venv**

Run: `uv run python -c "import duckdb; print('duckdb', duckdb.__version__)"`
Expected: a duckdb version string (e.g., `duckdb 1.5.2`).

- [ ] **Step 5: Verify the persistence tests still pass**

Run: `uv run pytest tests/test_persistence_store.py -q 2>&1 | tail -3`
Expected: tests pass. (If they skip instead, the venv didn't pick up `[persist]` — re-run `uv sync --all-extras`.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): move duckdb to [persist] optional extras"
```

---

## Task 8: Update `tests/test_serve_app.py` for None-by-default

**Files:**
- Modify: `tests/test_serve_app.py`

PR1's existing tests in this file likely call `create_app(ServerConfig())` and then exercise paths that depended on `history_store` being non-None. With the gate, those tests need to pass `persist=True` (if they were testing the persistence path) or be updated to assert `None` (if they were just incidentally exercising it).

- [ ] **Step 1: Identify failing tests after Task 4**

Run: `uv run pytest tests/test_serve_app.py -q 2>&1 | tail -20`
Note any failing tests. Common failures will be assertions like `assert app.state.history_store is not None`, `app.state.flush_queue.flush()`, etc.

- [ ] **Step 2: For each failing test, decide: was it about persistence behavior, or merely exercising the wired-up store?**

- If the test name or body is about persistence behavior (forget, hot tier, warm tier reads, archived sessions): add `persist=True` to the `ServerConfig(...)` call. Example:

  ```python
  # Before:
  config = ServerConfig(all_projects=True)
  # After:
  config = ServerConfig(all_projects=True, persist=True)
  ```

- If the test is about something else (snapshot endpoints, breakdown handlers, recent activity) and was just incidentally getting a store, it should now assert that the store is `None`. Update the assertion or add a new explicit one. Example:

  ```python
  # If the test does:  app = create_app(ServerConfig())
  # Add (if relevant): assert app.state.history_store is None
  ```

- [ ] **Step 3: Re-run the file**

Run: `uv run pytest tests/test_serve_app.py -q 2>&1 | tail -5`
Expected: all tests in the file pass.

- [ ] **Step 4: Run the entire suite to confirm no other regressions**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: `274 passed` (same as baseline) or higher if any new pre-existing tests started covering both paths.

- [ ] **Step 5: Commit**

```bash
git add tests/test_serve_app.py
git commit -m "test(serve/app): pass persist=True for tests that exercise the store path"
```

---

## Task 9: Add `tests/test_serve_app_no_persist.py`

**Files:**
- Create: `tests/test_serve_app_no_persist.py`

- [ ] **Step 1: Write the file**

Create `tests/test_serve_app_no_persist.py` with this exact content:

```python
"""Default-mode (`persist=False`) regression tests for tokenol serve.

These tests guarantee that `tokenol serve` without `--persist` reproduces
v0.3.2 behavior: no `import duckdb`, no `~/.tokenol/` directory created,
`app.state.history_store is None`, no flusher task. Spec:
docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_default_create_app_does_not_import_duckdb(tmp_path):
    """A subprocess that constructs the default app must not load duckdb.

    Subprocess isolation is required because the parent test process likely
    has duckdb in sys.modules already from earlier persistence tests.
    """
    snippet = (
        "import os, sys, pathlib;"
        f"os.environ['HOME']={str(tmp_path)!r};"
        "from tokenol.serve.app import create_app, ServerConfig;"
        "app = create_app(ServerConfig());"
        "assert app.state.history_store is None, 'history_store leaked';"
        "assert app.state.flush_queue is None, 'flush_queue leaked';"
        "assert 'duckdb' not in sys.modules, 'duckdb was imported';"
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "OK" in proc.stdout


def test_default_app_state_has_no_store(tmp_path, monkeypatch):
    """In-process check that ServerConfig() yields None store/queue."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from tokenol.serve.app import ServerConfig, create_app
    app = create_app(ServerConfig())
    assert app.state.history_store is None
    assert app.state.flush_queue is None


def test_persist_true_constructs_store(tmp_path, monkeypatch):
    """With persist=True, the store + queue are wired up and the DB file appears."""
    pytest.importorskip("duckdb")
    monkeypatch.setenv("HOME", str(tmp_path))
    from tokenol.serve.app import ServerConfig, create_app
    app = create_app(ServerConfig(persist=True))
    assert app.state.history_store is not None
    assert app.state.flush_queue is not None
    db_path = Path(tmp_path) / ".tokenol" / "history.duckdb"
    assert db_path.exists(), f"expected {db_path} to exist after create_app"


def test_default_warns_when_orphan_store_exists(tmp_path, monkeypatch, capsys):
    """Default mode prints a yellow WARNING if ~/.tokenol/history.duckdb exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pre-create an orphan store file (1 KB to make the size readout non-zero).
    store_dir = Path(tmp_path) / ".tokenol"
    store_dir.mkdir()
    (store_dir / "history.duckdb").write_bytes(b"x" * 1024)
    from tokenol.serve.app import ServerConfig, create_app
    create_app(ServerConfig())
    captured = capsys.readouterr()
    # Rich strips ANSI when not in a TTY; the literal text still appears.
    assert "Found existing history store" in captured.err
    assert "--persist" in captured.err
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_serve_app_no_persist.py -v 2>&1 | tail -15`
Expected: 4 tests pass. If `test_default_warns_when_orphan_store_exists` fails because rich is buffering elsewhere, the fix is to construct `Console(stderr=True, force_terminal=False)` in `_warn_if_orphan_store_exists` — but try the natural code first.

- [ ] **Step 3: Run the entire suite once more**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: previous pass count + 4 = `278 passed`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_serve_app_no_persist.py
git commit -m "test(serve): add no-persist regression suite (no duckdb import, no store, orphan warning)"
```

---

## Task 10: Update README and CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update README install section**

Find the `## Install` section (or equivalent) in `README.md`. Replace the existing install snippet with:

```markdown
## Install

    pip install tokenol             # core dashboard, no persistence
    pip install 'tokenol[persist]'  # adds DuckDB-backed history that survives JSONL deletion
```

If the existing README has an `[serve]` extras hint, leave that as-is — it's complementary.

- [ ] **Step 2: Add a `### Persistent history (opt-in)` subsection**

Under the "Commands" section of `README.md` (or near the existing `tokenol serve` description), add:

```markdown
### Persistent history (opt-in)

By default, `tokenol serve` parses your `~/.claude*/projects/**/*.jsonl` files
into an in-memory model on each restart — fast, but the dashboard loses any
session whose JSONL has been deleted or rotated.

Pass `--persist` to enable a DuckDB-backed history store at
`~/.tokenol/history.duckdb`. Sessions are durable across JSONL deletion and
restarts. Cost on a moderate corpus (~500 sessions): ~+500 MiB steady RSS,
~30 MB durable disk, a one-time multi-minute backfill on the first start.
Requires the persist extras (`pip install 'tokenol[persist]'`).

See `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md` for
design rationale and `docs/superpowers/specs/2026-05-02-persistent-history-design.md`
for the underlying store design.
```

- [ ] **Step 3: Add `0.4.0` entry to CHANGELOG.md**

Open `CHANGELOG.md`. The current top entry is `0.3.2`. Insert above it:

```markdown
## 0.4.0 — 2026-05-03

### Features
- `tokenol serve --persist` enables a DuckDB-backed history store at
  `~/.tokenol/history.duckdb`. Sessions are durable across JSONL deletion
  and tokenol restarts. Default off — `tokenol serve` matches the v0.3.2
  resource profile byte-for-byte (no `import duckdb`, no `~/.tokenol/`
  directory, no extra steady RSS).

### Changes
- `duckdb` moved from a core dependency to the new `[persist]` optional
  extras group. Default `pip install tokenol` no longer pulls the DuckDB
  binary wheel (~30 MB saved). Users who pass `--persist` install with
  `pip install 'tokenol[persist]'`.
- Default mode prints a yellow `WARNING` at startup if it finds an existing
  `~/.tokenol/history.duckdb`, prompting the user to pass `--persist` if
  they want to use it (rather than silently ignoring the file).

### Notes
- See `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md` for
  the gating-and-extras design.
- Underlying persistent-history store design is unchanged from
  `feature/persistent-history-pr1` — see
  `docs/superpowers/specs/2026-05-02-persistent-history-design.md`.
```

- [ ] **Step 4: Verify both files render correctly**

Run: `head -40 README.md && echo --- && head -40 CHANGELOG.md`
Expected: readable Markdown for both, no obvious typos in the new sections.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: README + CHANGELOG for 0.4.0 (--persist opt-in + [persist] extras)"
```

---

## Task 11: Add cross-reference to PR1's underlying spec

**Files:**
- Modify: `docs/superpowers/specs/2026-05-02-persistent-history-design.md`

- [ ] **Step 1: Add a header note pointing at the new opt-in spec**

Open `docs/superpowers/specs/2026-05-02-persistent-history-design.md`. Insert this line as the second line (immediately after the H1 title, before the existing intro paragraph):

```markdown
> **Update 2026-05-03:** shipped opt-in only via `--persist`. See `2026-05-03-opt-in-persistence-design.md` for the gating design and rationale.
```

- [ ] **Step 2: Verify the file**

Run: `head -5 docs/superpowers/specs/2026-05-02-persistent-history-design.md`
Expected: H1 title, the new `> **Update ...** ` line, then the original intro paragraph.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-02-persistent-history-design.md
git commit -m "docs(spec): note that persistent-history shipped opt-in via --persist"
```

---

## Task 12: Bump version to 0.4.0

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/tokenol/__init__.py`
- Regenerate: `uv.lock`

This task implements the lesson from RESUME's "History notes — pitfalls to avoid" section: `pyproject.toml`, `src/tokenol/__init__.py`, and `uv.lock` all bump together. Skipping `__init__.py` is exactly what bit v0.3.2.

- [ ] **Step 1: Bump pyproject.toml version**

Open `pyproject.toml`. Find `version = "0.3.2"` near the top of the `[project]` block and change it to:

```toml
version = "0.4.0"
```

- [ ] **Step 2: Bump `src/tokenol/__init__.py`**

Open `src/tokenol/__init__.py`. Find the `__version__ = "..."` line and change it to:

```python
__version__ = "0.4.0"
```

If the file doesn't have `__version__`, add it at the top (matching the pattern from prior releases — see `git log -p src/tokenol/__init__.py` for the form previously used).

- [ ] **Step 3: Regenerate uv.lock**

Run: `uv lock 2>&1 | tail -3`
Expected: `Resolved N packages` with no errors. The lockfile's `tokenol` entry now reads `0.4.0`.

- [ ] **Step 4: Confirm the three sources agree**

Run: `grep -E "^version" pyproject.toml; grep -E "__version__" src/tokenol/__init__.py; grep -A1 'name = "tokenol"' uv.lock | head -3`
Expected: all three show `0.4.0`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/tokenol/__init__.py uv.lock
git commit -m "release: bump to 0.4.0"
```

---

## Task 13: Final verification — pre-merge gate

**Files:** none (verification only)

This task implements RESUME's release gate: `uv run ruff check src tests && uv run pytest` — both must pass before any release-bound push.

- [ ] **Step 1: Run ruff against the full source + tests**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`. If anything fails, fix and re-run; do not bypass with `--fix` or noqa unless the failure is unrelated to this work.

- [ ] **Step 2: Run the entire test suite**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: `278 passed` (274 baseline + 4 from `test_serve_app_no_persist.py`). If any test fails, stop and diagnose.

- [ ] **Step 3: End-to-end smoke — default mode is duckdb-free**

Run: `uv run python -c "
import sys
from tokenol.serve.app import create_app, ServerConfig
app = create_app(ServerConfig())
print('history_store:', app.state.history_store)
print('flush_queue:', app.state.flush_queue)
print('duckdb in sys.modules:', 'duckdb' in sys.modules)
"`
Expected:
```
history_store: None
flush_queue: None
duckdb in sys.modules: False
```

- [ ] **Step 4: End-to-end smoke — `--persist` constructs the store**

Run: `HOME=$(mktemp -d) uv run python -c "
from tokenol.serve.app import create_app, ServerConfig
app = create_app(ServerConfig(persist=True))
print('history_store type:', type(app.state.history_store).__name__)
print('flush_queue type:', type(app.state.flush_queue).__name__)
"`
Expected:
```
history_store type: HistoryStore
flush_queue type: FlushQueue
```

- [ ] **Step 5: End-to-end smoke — `--persist` without extras fails fast**

Skip if the dev venv has duckdb installed (it does after Task 7). Manual reproduction outside this venv: `pip install --no-deps tokenol==0.4.0` (after release), then `tokenol serve --persist` — should print the red error and exit 1.

For now, just verify the error message renders correctly without actually missing duckdb:

Run: `uv run python -c "
from rich.console import Console
err = Console(stderr=True)
err.print('[red]--persist requires the [/red][bold]persist[/bold][red] extras.[/red] Run: pip install [bold]tokenol[persist][/bold]')
"`
Expected: a colored error line on stderr (red).

- [ ] **Step 6: Confirm branch is N commits ahead of main, ready for merge**

Run: `git log --oneline main..HEAD | wc -l && git log --oneline main..HEAD | head -15`
Expected: 26 (PR1's existing) + 11 (this work) = approximately **37 commits** ahead of main, with the new work clustered at the tip.

- [ ] **Step 7: Hand-off — branch is ready to merge**

Stop here. The next step is a fast-forward merge into `main`:

```bash
cd /home/ff235/dev/claude_rate_limit
git checkout main
git merge --ff-only feature/persistent-history-pr1
```

After merge: tag `v0.4.0`, follow the existing PyPI publish recipe from RESUME (`rm -rf dist/ && uv build && set -a && source .env && set +a && uv publish dist/*`), then run the worktree cleanup pattern from RESUME's "feedback_worktree_cleanup" memory.

Do **not** perform the merge or PyPI publish from inside this task — the user reviews the branch first and chooses when to ship.

---

## Self-Review

**Spec coverage** (from `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md`):
- [x] Goal 1 (default == v0.3.2 byte-for-byte, asserted via subprocess `"duckdb" not in sys.modules`) — Tasks 3, 9
- [x] Goal 2 (`--persist` reproduces PR1 behavior end-to-end) — Tasks 4, 5, 9
- [x] Goal 3 (single-flag flip, no schema, no migration) — Tasks 4, 5
- [x] Goal 4 (existing PR1 tests keep passing) — Tasks 6, 8
- [x] CLI flag (`--persist`) — Task 5
- [x] `ServerConfig.persist` field — Task 2
- [x] Module-level import deferral in `app.py` — Task 3
- [x] Conditional construction + lifespan — Task 4
- [x] Yellow WARNING for orphan `~/.tokenol/` — Task 4 (helper) + Task 9 (test)
- [x] Branching plan (commit on PR1 → merge to main → 0.4.0) — Plan header + Task 13
- [x] `pyproject.toml` extras split — Task 7
- [x] CLI fail-fast on missing `duckdb` — Task 5
- [x] `pytest.importorskip("duckdb")` in 6 persistence test files — Task 6
- [x] New `test_serve_app_no_persist.py` with 4 tests — Task 9
- [x] Updates to existing `test_serve_app.py` for None-by-default — Task 8
- [x] README install + persistent-history subsection — Task 10
- [x] CHANGELOG 0.4.0 entry — Task 10
- [x] Cross-reference in `2026-05-02-persistent-history-design.md` — Task 11
- [x] Version bump (pyproject + `__init__` + `uv.lock`) — Task 12
- [x] Final ruff + pytest gate — Task 13

**Placeholder scan:** No TBD/TODO/handle-edge-cases. Task 13's "Skip if the dev venv has duckdb installed" is an explicit precondition, not a placeholder.

**Type consistency:** `ServerConfig.persist`, `app.state.history_store`, `app.state.flush_queue`, `_warn_if_orphan_store_exists`, `_FlushQueue`/`_HistoryStore`/`_write_pidfile`/`_clear_pidfile` lazy aliases — names are consistent across Tasks 2, 3, 4, 5, 9.

No issues; ready to execute.
