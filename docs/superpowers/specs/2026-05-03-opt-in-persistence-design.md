# Opt-in persistence (`--persist`) — design

Ship `feature/persistent-history-pr1`'s DuckDB-backed history store as an opt-in feature behind a `--persist` flag on `tokenol serve`. Default behavior — `tokenol serve` with no extra flags — reproduces released v0.3.2 byte-for-byte: no `import duckdb`, no `~/.tokenol/` directory created, no extra steady-state RSS, no flusher task. Users who want the dashboard to survive JSONL deletion or rotation pass `--persist` and accept the measured cost (≈+500 MiB steady RSS, multi-minute one-time backfill, ≈30 MB durable disk on the developer's full corpus; see `2026-05-03-cold-start-bench-design.md` for the measurement methodology and `_local/cold_start_*/report.md` for the medium-tier numbers).

## Goals

1. Default `tokenol serve` matches v0.3.2 in import surface, RSS, CPU, and disk usage. Verified by an automated test that asserts `"duckdb" not in sys.modules` after `create_app(ServerConfig())` returns.
2. `tokenol serve --persist` reproduces today's `feature/persistent-history-pr1` behavior end-to-end, including the JSONL-deletion-survival contract from `2026-05-02-persistent-history-design.md`.
3. The flip is single-flag and surface-level: no schema change, no migration step, no prefs key. Adding the flag turns the feature on; removing it turns the feature off (the existing `~/.tokenol/` keeps its data and resumes when the flag returns).
4. Existing PR1 tests keep passing without semantic changes — they construct `HistoryStore` directly or call `create_app` with `persist=True`.

## Non-goals

- A prefs.json key for persistence. Adding `prefs.persist: bool` later is fine if demand surfaces; we don't add it preemptively.
- Auto-detection of an existing `~/.tokenol/history.duckdb`. If a user previously ran with `--persist`, removed the flag, and then wants persistence back, they re-add `--persist`. We do not silently re-enable persistence based on disk state — that would be surprising magic.
- Removing PR1's persistence code or downgrading any of its capabilities. The store, flusher, forget-handoff, hot/warm tiering, archived-session detail UI, and warm-tier merge for `/api/daily` and `/api/project` all stay. The only difference is whether they're wired up at startup.
- Per-tenant or per-project persistence toggles. The flag is process-wide.
- Hot-reload of the flag without restart. Toggling persistence requires a restart of `tokenol serve`.

## Architecture

The change is concentrated in three files: `src/tokenol/cli.py` (one new flag), `src/tokenol/serve/app.py` (the gate + deferred imports), and a new test file. `serve/state.py`, `serve/streaming.py`, and the `tokenol.persistence.*` modules are untouched — PR1 already designed those for the `HistoryStore | None` case to support CLI report tools and existing tests, and both already wrap their persistence imports in `TYPE_CHECKING` so they do not transitively load `duckdb` at import time. (`streaming.py:227` does a function-level import of `take_forget_request` from `forget_handoff`, but that path only executes when `flush_queue` is non-None, so the default mode never triggers it.)

```
            ┌──────────────────────────┐
            │  tokenol serve [--persist] │
            └──────────────────────────┘
                         │
                         ▼
            ┌────────────────────────────┐
            │ ServerConfig(persist=…)    │
            └────────────────────────────┘
                         │
                         ▼
                 ┌──────────────────┐
                 │  create_app()    │
                 └─────────┬────────┘
                           │
        config.persist?    │
              ┌────────────┴───────────┐
              │                        │
        True (opt-in)            False (default)
              │                        │
              ▼                        ▼
   import + construct           skip imports
   HistoryStore, FlushQueue;     entirely; pass
   pidfile; flusher.start;       None to broadcaster;
   close on lifespan exit         no pidfile; no
                                  flusher; no DB dir
              │                        │
              └────────────┬───────────┘
                           ▼
                   FastAPI app with
                   app.state.history_store
                   ∈ {HistoryStore, None}
```

### CLI surface

`src/tokenol/cli.py` adds one Typer option to `serve()`:

```python
persist: bool = typer.Option(
    False,
    "--persist",
    help=(
        "Enable persistent history store at ~/.tokenol/history.duckdb. "
        "Dashboard survives JSONL deletion. Adds ≈500 MiB steady RSS and a "
        "one-time multi-minute backfill on first start; durable disk ≈30 MB. "
        "Default off — matches v0.3.2 resource usage."
    ),
),
```

The value flows through unchanged to `ServerConfig(..., persist=persist)`.

### `ServerConfig`

`src/tokenol/serve/app.py` adds one field:

```python
@dataclass
class ServerConfig:
    all_projects: bool = False
    reference_usd: float = 50.0
    tick_seconds: int = 5
    persist: bool = False
```

Default `False` means existing callers (`create_app(ServerConfig())` in tests, in `cli.py`'s legacy path, anywhere) get the v0.3.2 behavior automatically.

### Module-level imports

The three offending top-level imports at `src/tokenol/serve/app.py:20-22` are removed and replaced with type-only imports inside a `TYPE_CHECKING` guard:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.store import HistoryStore
```

`forget_handoff` does not import `duckdb` (verified) but lives in the same package, so its module load would still trigger the package `__init__.py`. We move its import too, into the `if config.persist:` branch, to keep the package untouched in the default path.

No further changes to `serve/state.py` or `serve/streaming.py` — both already TYPE_CHECKING-guard their persistence imports (verified at `state.py:21-29` and `streaming.py:23-27`). The implementation only needs to bring `app.py` into the same shape.

### `create_app` lifespan

```python
def create_app(config=None, prefs_path=None):
    config = config or ServerConfig()
    prefs = Preferences.load(prefs_path or default_path())
    parse_cache = ParseCache()

    history_store = None
    flush_queue = None
    if config.persist:
        from tokenol.persistence.flusher import FlushQueue
        from tokenol.persistence.forget_handoff import clear_pidfile, write_pidfile
        from tokenol.persistence.store import HistoryStore

        history_store = HistoryStore()
        history_store._hot_window_days = prefs.hot_window_days
        flush_queue = FlushQueue(history_store)

    broadcaster = SnapshotBroadcaster(
        ...,
        history_store=history_store,
        flush_queue=flush_queue,
    )

    @asynccontextmanager
    async def lifespan(_app):
        if config.persist:
            write_pidfile()
            await flush_queue.start()
        try:
            yield
        finally:
            await broadcaster.shutdown()
            if config.persist:
                await flush_queue.stop()
                history_store.close()
                clear_pidfile()
```

`pidfile`, `flush_queue.start/stop`, `history_store.close` are all skipped when `persist=False`. The existing handler-side conditionals (`if request.app.state.history_store is not None` in `/api/daily`, `/api/project`, `_build_and_cache_snapshot`) already fall through to the in-memory parse-only path — the same path v0.3.2 takes today.

## Data flow

For `--persist` runs: identical to today's PR1 behavior. JSONL → ParseCache → in-memory hot tier + queued for flush → DuckDB store → warm-tier reads on `range=all`. Deleting a JSONL drops the snippet text but keeps the metrics.

For default runs: identical to v0.3.2. JSONL → ParseCache → in-memory model → snapshot. Deleting a JSONL silently drops the data from the dashboard. No `~/.tokenol/` directory exists; nothing is persisted.

The two modes never interact within a single process. Switching modes between runs is safe in both directions:
- Default → `--persist`: first start does the backfill into a new (or existing) `~/.tokenol/history.duckdb`. Same path as a fresh PR1 install.
- `--persist` → default: `~/.tokenol/` is left untouched on disk; nothing reads from it. Re-adding `--persist` later resumes from the existing file.

## Error handling

- **`--persist` fails to construct `HistoryStore`** (disk-permission denied, corrupt DB file, DuckDB import failure): startup fails fast with a clear error message naming the path and suggesting either fixing the permission or running without `--persist`. Do not silently fall back to default mode — that would mask broken state and surprise the user with missing persistence.
- **Default mode encounters an old `~/.tokenol/`**: log a single info-level line on startup ("found existing history store at ~/.tokenol/history.duckdb; pass --persist to use it") and continue. No data loss, no surprise.
- **Test code that constructs `create_app()` without arguments**: gets `persist=False` automatically. Tests that need persistence pass `ServerConfig(persist=True)` explicitly.

## Testing

New file: `tests/test_serve_app_no_persist.py`. Three tests:

1. `test_default_create_app_does_not_import_duckdb`: spawn a subprocess running `python -c "from tokenol.serve.app import create_app, ServerConfig; create_app(ServerConfig()); import sys; assert 'duckdb' not in sys.modules"`. Subprocess is required because the parent test process likely already imported `duckdb` via earlier persistence tests.
2. `test_default_app_state_has_no_store`: `app = create_app(ServerConfig())`; assert `app.state.history_store is None` and `app.state.flush_queue is None`.
3. `test_persist_true_constructs_store`: `app = create_app(ServerConfig(persist=True))` against a tmp-dir-redirected `~/.tokenol/`; assert `app.state.history_store is not None`, `~/.tokenol/history.duckdb` exists.

Modified files:
- `tests/test_serve_app.py` — any test that asserts `app.state.history_store is not None` is updated to either pass `persist=True` or assert `None` if it was implicitly relying on PR1's default-on behavior.
- `tests/test_serve_state.py`, `tests/test_archived_session.py` — same treatment.

End-to-end smoke (manual or via the existing cold-start bench): `tokenol serve` (no flag) on the developer's full corpus reproduces v0.3.2's measured profile (≈500 MiB steady, no DuckDB file). `tokenol serve --persist` reproduces today's PR1 profile (≈1.0–1.1 GiB steady, ≈30 MB DuckDB file).

## Docs

- `README.md` gains a `### Persistent history (opt-in)` subsection under "Commands" with a 2-3 sentence description of `--persist` plus the cost summary (link to this spec for details).
- `CHANGELOG.md` next-release entry: `feat(serve): persistent history landed as opt-in via --persist (default off matches v0.3.2 resource use)`.
- `docs/superpowers/specs/2026-05-02-persistent-history-design.md` gets a short header note: `**Update 2026-05-03:** shipped opt-in only via --persist. See 2026-05-03-opt-in-persistence-design.md for the gating design and rationale.`

## Pitfalls

- **TYPE_CHECKING string literals.** Once `HistoryStore` and `FlushQueue` are TYPE_CHECKING-only at module scope, any non-TYPE_CHECKING annotation that names them needs to be a string literal (`"HistoryStore | None"`) or wrapped in `from __future__ import annotations`. `app.py`, `state.py`, and `streaming.py` all already use `from __future__ import annotations` at the top (verified), so annotations work without changes.
- **Import side effects in `tokenol.persistence.__init__`.** Currently empty (1 line per `wc -l` earlier); adding any module-level import there in the future would defeat the deferral. Add a comment to `tokenol/persistence/__init__.py` flagging this so future contributors don't quietly re-trigger the cost.
- **Existing PR1 tests.** PR1 ships 274 tests, 52 of which exercise the persistence path. Most should keep passing because they construct `HistoryStore` directly. The handful that go through `create_app` need an explicit `persist=True` — review with `grep -lE "create_app\(" tests/`.
- **`forget` handoff via pidfile.** The `forget_handoff` module writes/clears a pidfile signalling that a `tokenol forget` invocation can request a live forget over the running server. With persistence off, no pidfile is written — `tokenol forget` running against a default-mode server simply finds no pidfile and proceeds against the (nonexistent) DuckDB file directly, which is the correct behavior.
- **`pyproject.toml` `duckdb` dep.** The dependency stays — it's needed when `--persist` is on. Default users still install the wheel that includes `duckdb`. The deferral only avoids the *runtime* cost of `import duckdb`, not the disk cost of the wheel. (Splitting into an extras group like `tokenol[persist]` is a separate decision; not in scope here.)
