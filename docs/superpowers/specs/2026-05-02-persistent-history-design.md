# Persistent history — design

> **Update 2026-05-03:** shipped opt-in only via `--persist`. See `2026-05-03-opt-in-persistence-design.md` for the gating design and rationale.

A durable on-disk store for tokenol's derived analytics so the dashboard survives deletion or rotation of the source `~/.claude*/projects/**/*.jsonl` files. Today every metric is recomputed from JSONL on each cold start and every restart re-parses the full history; deleting a JSONL silently drops its data from the dashboard. This design adds a single-file DuckDB store of derived `Turn` and `Session` rows (no message content), backing the existing in-memory model so the live dashboard is functionally identical with full history regardless of which JSONLs still exist on disk.

## Goals

1. Dashboard renders the same charts, tiles, breakdowns, and drill-downs whether the source JSONLs exist or not.
2. Cold-start time is bounded by the size of the configurable hot window (default 90 days), not by total history length.
3. Per-tick CPU goes down, not up — the in-memory model is appended to instead of rebuilt from scratch when JSONLs change.
4. No verbatim user/assistant content is persisted. Privacy: deleting a JSONL drops the words; the metrics survive unless the user explicitly forgets them.
5. Surgical user control over the store via a `tokenol forget` CLI for session, project, age, or full-store removal.

## Non-goals

- Storing raw `RawEvent` lines or message content. Per-turn drill-down text snippets (`user_prompt`, `assistant_preview`, per-tool-call name+ok pairs) become unavailable for archived sessions; everything quantitative is preserved. This is the explicit privacy contract.
- Multi-host / multi-user / network-attached persistence. The store is per-machine, single-process.
- Migration of existing JSONLs into the store as a one-shot import. The store fills organically as the broadcaster ticks; users with months of existing JSONLs see them ingested over the first few minutes of running the new build.
- A general-purpose query API or external warehouse export. The store is private to tokenol's runtime.
- Replacing the existing in-memory `ParseCache`. It stays as the per-process speed cache for *unchanged JSONLs in the current run*; the DuckDB store is the durable layer beneath it.

## Architecture

The store is a single DuckDB database at `~/.tokenol/history.duckdb`. It backs the existing in-memory `Turn`/`Session` model rather than replacing it. The dashboard code paths (`build_snapshot_full`, drill-downs, breakdowns) are largely unchanged: they continue to operate on `list[Turn]` and `list[Session]` held on `app.state`. What changes is *how* those lists are populated and how new turns flow back to disk.

```
            ┌─────────────────────────────────────┐
            │  ~/.claude*/projects/**/*.jsonl     │   live source files
            └─────────────────────────────────────┘
                            │
              parse only files newer than
              max(persisted_ts) per session
                            ▼
            ┌─────────────────────────────────────┐
            │  ParseCache + _build_turns_…        │   per-process speed cache
            └─────────────────────────────────────┘
                            │ new Turns / Sessions
                            ▼
       ┌───────────────────────────────────────────────┐
       │  In-memory hot tier (last N days, default 90) │   what every endpoint reads
       └───────────────────────────────────────────────┘
                ▲                      │
                │ hydrate              │ append + flush
                │ on startup           ▼
            ┌─────────────────────────────────────┐
            │  ~/.tokenol/history.duckdb          │   warm tier + durable record
            │  turns | sessions | meta            │
            └─────────────────────────────────────┘
                            ▲
                            │ lazy SQL for "all-time" range queries
                            │
                ┌───────────────────────┐
                │  FastAPI handlers     │
                └───────────────────────┘
```

### Hot / warm tiering

- **Hot tier (in memory):** rolling last `hot_window_days` turns and their sessions. Default 90, user-configurable via the existing prefs file. Bounds RAM at "last N days of activity" regardless of total history. Every `build_snapshot_full` and drill-down handler reads from this tier as today.
- **Warm tier (DuckDB on disk):** every persisted turn, including the hot tier (the hot tier is a strict subset). When a handler asks for `range=all` or for a session/project whose age exceeds the hot window, it issues a DuckDB query directly rather than expecting in-memory hits.

The hot/warm boundary is one configuration value, not two storage systems. The store always has everything; "hot tier" just means "what we eagerly hold in Python objects."

### Runtime model

On startup:
1. Open the DuckDB file (create + apply schema if missing).
2. Hydrate hot tier: `SELECT … FROM turns WHERE ts >= now() - hot_window_days` and the corresponding `sessions` rows. Construct `Turn` and `Session` objects in memory. Single SQL round-trip per table.
3. Stat all live JSONLs. For each session with a live JSONL whose mtime is newer than the persisted `last_ts` for that session_id, schedule it for parsing. For sessions with no live JSONL, mark them as `archived=True` in memory.
4. Start the broadcaster. The first tick parses the scheduled "edge" files and merges new turns into the hot tier.

Per tick (broadcaster):
1. Existing `compute_active_keys` probe runs unchanged. If nothing changed *and* heartbeat hasn't fired *and* no pending forget request exists, skip.
2. Parse only the JSONLs whose mtime is newer than the persisted `last_ts` for their session. The existing `ParseCache` continues to short-circuit unchanged-this-run files within the process.
3. `_build_turns_and_sessions` runs over the *new* events only and produces incremental `Turn`/`Session` deltas. Today's full re-derivation is replaced by an append.
4. New turns flow into the hot tier and into a flush queue.
5. If the flush queue holds ≥100 turns OR ≥30 seconds have passed since the last flush, a background `asyncio.Task` invokes `HistoryStore.flush(...)` via `run_in_executor` (the same off-loop pattern already used for `build_project_detail`). The flush issues a single `INSERT … ON CONFLICT DO NOTHING` batch (idempotent on `dedup_key`) and an UPSERT for the affected `sessions` rows in one transaction.
6. Pending-forget probe: `forget_handoff.take_forget_request()` is called once per tick. When a request is present (rare path), the broadcaster invokes `HistoryStore.forget(...)` via the same write connection, evicts the affected session_ids from the in-memory hot tier, and removes the request file. The next snapshot reflects the eviction without a process restart.

On graceful shutdown (lifespan hook): force-drain the flush queue.

### Crash and consistency model

The JSONL is the durable substrate; DuckDB is the derived view. If the process dies between flushes, the un-flushed turns are still on disk in their JSONL. On next start, those JSONLs are re-parsed (mtime > persisted last_ts) and the derived turns are re-inserted; the dedup key collision means it's a no-op for already-flushed turns and a fresh insert for the missed window. The only way to permanently lose persisted data is process-crash *and* user-deletes-the-JSONL before next start, which we accept as vanishingly unlikely. No fsync-per-tick is required.

DuckDB's WAL handles file-level crash safety. If the database file is corrupted unrecoverably, tokenol falls back to today's behavior: parse all available JSONLs, rebuild from scratch, replace the corrupt file with a fresh one. Logged at WARN.

## Schema

```sql
CREATE TABLE meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
-- rows: schema_version=1, created_at=…, last_flush_ts=…

CREATE TABLE sessions (
    session_id            VARCHAR PRIMARY KEY,
    source_file           VARCHAR,           -- last-known JSONL path (may no longer exist)
    cwd                   VARCHAR,
    is_sidechain          BOOLEAN NOT NULL,
    first_ts              TIMESTAMP NOT NULL,
    last_ts               TIMESTAMP NOT NULL,
    turn_count            INTEGER NOT NULL,  -- denormalized for cheap session listings
    inserted_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sessions_last_ts ON sessions(last_ts);
CREATE INDEX idx_sessions_cwd     ON sessions(cwd);

CREATE TABLE turns (
    dedup_key             VARCHAR PRIMARY KEY, -- "message_id:request_id" or fallback uuid
    ts                    TIMESTAMP NOT NULL,
    session_id            VARCHAR NOT NULL,    -- not enforced as FK; flush inserts turns+sessions in one transaction
    model                 VARCHAR,
    input_tokens          BIGINT NOT NULL,
    output_tokens         BIGINT NOT NULL,
    cache_read_tokens     BIGINT NOT NULL,
    cache_creation_tokens BIGINT NOT NULL,
    cost_usd              DOUBLE  NOT NULL,
    is_sidechain          BOOLEAN NOT NULL,
    is_interrupted        BOOLEAN NOT NULL,
    stop_reason           VARCHAR,
    tool_use_count        INTEGER NOT NULL,
    tool_error_count      INTEGER NOT NULL,
    tool_names            JSON,                -- {"Read": 3, "Bash": 1}
    assumptions           JSON,                -- ["DEDUP_PASSTHROUGH", …]
    inserted_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_turns_ts      ON turns(ts);
CREATE INDEX idx_turns_session ON turns(session_id);
```

Cost is stored *both* as the computed `cost_usd` and via the input token columns + `model`. If pricing changes later, a `tokenol recompute-costs` migration re-runs `cost_for_turn` against stored token counts and updates `cost_usd` in place. Token counts and model are the immutable inputs; cost is a derived-but-cached column.

`tool_names` and `assumptions` use DuckDB's native JSON type so they round-trip cleanly to/from the Python `Counter[str]` and `list[AssumptionTag]` shapes without a normalized child table. These columns are read-mostly; the JSON overhead is fine at this volume.

## Components

### `tokenol/persistence/store.py` (new)

The single module that owns the DuckDB connection and the schema lifecycle.

- `class HistoryStore`: thin wrapper around a write connection. Holds a single open `duckdb.Connection`. Public methods:
  - `hydrate_hot(window_days: int) -> tuple[list[Turn], list[Session]]`
  - `last_ts_by_session() -> dict[str, datetime]` — high-water marks used by the edge-parse filter. Read once at startup, then maintained in memory and re-derived from each flush.
  - `flush(turns: list[Turn], sessions: list[Session]) -> None` — single batched INSERT … ON CONFLICT DO NOTHING for turns; UPSERT for sessions (update last_ts, turn_count, source_file).
  - `query_turns(since: date | None, until: date | None, project: str | None = None, model: str | None = None) -> list[Turn]` — used by `range=all` handlers.
  - `query_session(session_id: str) -> Session | None` — used by drill-down when a session is in the warm tier only.
  - `forget(session_ids: list[str]) -> int` — removes both `turns` and `sessions` rows; returns deleted-row count for CLI feedback.
  - `migrate() -> None` — applies schema migrations based on `meta.schema_version`.

Store path resolution: `TOKENOL_HISTORY_PATH` env var if set, otherwise `~/.tokenol/history.duckdb`. The directory is created on first open with mode `0700` and the database file is opened with mode `0600` to keep metrics private to the user account, matching how `~/.claude*/projects/` is treated.
- `class ReadConnection`: a per-thread read-only connection issued via context manager for FastAPI handlers that hit the warm tier. Closed on exit.
- Module-level `get_store()` factory that the app's `create_app` calls once and stores on `app.state.history_store`.

The store is intentionally not async. All DB calls run via `asyncio.run_in_executor(None, …)` from the broadcaster and from any handler that touches the warm tier — same pattern already used for `build_project_detail`.

### `tokenol/persistence/flusher.py` (new)

A small queue-and-batch component that lives alongside the broadcaster.

- `class FlushQueue`: thread-safe queue of pending `Turn` deltas plus per-session "high-water mark" updates.
- A background `asyncio.Task` started in the broadcaster's lifespan that wakes every 30s (or when the queue size crosses a threshold) and runs `HistoryStore.flush(...)` in an executor.
- Force-flush on shutdown via the same lifespan that already handles `broadcaster.shutdown()`.

### `tokenol/serve/state.py` (modified)

- `ParseCache.purge` no longer drops turns from the in-memory derived list when a JSONL disappears. Today, deletion of a JSONL silently removes its events from the snapshot. After this change, deletion is irrelevant to display: the in-memory hot tier and DuckDB still hold the turns.
- `_build_turns_and_sessions` is restructured so it can run incrementally on a delta of events and *append* to the existing turn/session lists, rather than rebuilding from a full event corpus. The existing whole-corpus signature is kept for tests and the no-store fallback path.
- `build_snapshot_full` no longer triggers a full re-derivation when JSONLs change; it asks the parse-cache+store stack for the current hot tier.
- The `Session` dataclass (`tokenol/model/events.py`) gains an `archived: bool = False` field, set to `True` when no live JSONL backs the session at discovery time. The session-detail handler reads this to decide whether to attempt `_parse_turn_snippets`.

### `tokenol/ingest/discovery.py` (modified)

- New helper `select_edge_paths(paths: list[Path], last_ts_by_session: dict[str, datetime]) -> list[Path]`. Filters discovered JSONLs to those whose mtime is newer than the persisted high-water mark for their session, or whose session_id has no persisted entry. Returns the full list when `last_ts_by_session` is empty (first-run case) or `None` (store unavailable / disabled).

### `tokenol/serve/app.py` (modified)

- `create_app` opens the `HistoryStore`, hydrates hot tier, attaches both to `app.state`, and starts the flusher.
- The lifespan hook force-flushes the queue and closes the store on shutdown.
- Two handlers gain a warm-tier path: `api_daily` with `range=all`, and `api_project_detail` with `range=all`. Both currently iterate over `result.turns` filtering by date; after this change they delegate to `HistoryStore.query_turns(...)` when the requested range exceeds the hot window. Other handlers are unchanged.

### `tokenol/persistence/forget_handoff.py` (new)

A tiny helper module that owns the pidfile + request-file convention used by `tokenol forget` to coordinate with a live `tokenol serve`:

- `write_pidfile()` / `clear_pidfile()` — called from serve's lifespan startup/shutdown.
- `read_live_pid() -> int | None` — checks the pidfile, validates the PID is alive (not a stale file from a crashed serve), returns the PID or `None`.
- `submit_forget_request(req: ForgetRequest) -> None` — writes the JSON request to `~/.tokenol/pending-forget.json` (atomic via tmpfile + rename).
- `take_forget_request() -> ForgetRequest | None` — called by serve's broadcaster each tick; reads + removes the request file if present.
- `ForgetRequest` is a small dataclass: `kind: Literal["session", "project", "older_than", "all"]`, `value: str | None`, `submitted_at: datetime`.

### `tokenol/cli.py` (modified)

A new `tokenol forget` subcommand backed by `HistoryStore.forget`:

- `tokenol forget --session <id>` — drop a single session's turns and metadata.
- `tokenol forget --project <cwd>` — drop everything under a working-directory.
- `tokenol forget --older-than <duration>` — accepts `7d`, `30d`, `1y`. Per-turn semantics: deletes every turn whose `ts` falls before the cutoff. Surviving sessions have their denormalized `first_ts` and `turn_count` recomputed from remaining turns in the same transaction. Sessions whose every turn was dropped have their `sessions` row removed.
- `tokenol forget --all` — wipes the store. Equivalent to deleting `~/.tokenol/history.duckdb` but goes through the connection so a running serve process notices.
- All forms print the number of affected sessions and turns and prompt for confirmation unless `--yes` is passed. When a live serve is detected via the pidfile, the CLI submits the request via `forget_handoff.submit_forget_request` and exits with a message indicating the request is queued. Otherwise it executes the deletion inline against its own write connection.

A second new subcommand `tokenol recompute-costs` re-runs `cost_for_turn` over every stored turn and updates `cost_usd` in place. Used after pricing-table changes.

### Preferences (modified)

The existing `Preferences` (`tokenol/serve/prefs.py`) gains one field:

- `hot_window_days: int = 90` — the size of the in-memory hot tier. Validated as `1 <= v <= 3650`. Persisted in the same prefs file as the existing `tick_seconds` and `thresholds`. Exposed via the `/api/prefs` GET/POST that the dashboard already speaks; the frontend can ship the UI control later.

A re-hydration is *not* triggered when this value changes mid-run; it takes effect on next startup. Documented in the validator's error message.

## Data flow

### First run after upgrade

1. `~/.tokenol/history.duckdb` does not exist. `HistoryStore.migrate()` creates it with `schema_version=1`.
2. Hot-tier hydration returns empty lists.
3. Broadcaster starts. First tick treats every JSONL as "edge" (no persisted `last_ts` per session) and parses everything.
4. The flusher drains the resulting backlog over the next minute or two as 100-turn batches; the user sees the dashboard come up immediately and the warm tier fill in the background.

### Steady-state run (warm cache present)

1. Hot-tier hydration loads ~last 90 days of turns into memory in a single SQL round-trip (sub-second to ~2s for the heaviest users).
2. Broadcaster starts; first tick parses only JSONLs whose mtime exceeds the per-session high-water mark — typically just today's active files.
3. Per tick: `_build_turns_and_sessions` derives turns from the small delta, appends to hot tier, queues for flush. Flushed every 30s/100 turns.

### JSONL deleted between runs

1. User deletes `~/.claude/projects/foo/<sid>.jsonl`.
2. On next startup, `find_jsonl_files` doesn't return that path. The session_id is in the warm tier, so it's hydrated into the hot tier (if within window) or remains in DuckDB for lazy queries. Marked `archived=True` because no live file backs it.
3. Dashboard renders the session's verdict, cost, turn rows, patterns, and per-turn quantitative fields. Session-detail UI shows an "Archived — text snippets unavailable" badge; the per-turn modal omits `user_prompt`, `assistant_preview`, and the per-call tool name+ok list.

### `range=all` query for a multi-year user

1. Handler receives `GET /api/daily?range=all`.
2. It checks: does the request span beyond `hot_window_days` from today? If yes → delegate to `HistoryStore.query_turns(since=None, until=today)`, run aggregation in DuckDB SQL, return the result. If no → use the existing in-memory path.
3. The query result is not hydrated as Python `Turn` objects — DuckDB returns the aggregated rows directly.

## Failure modes and edge cases

- **Persisted session whose JSONL reappears at a different path**: dedup_key collision means re-inserts are no-ops. The `sessions.source_file` is updated to the new path on next flush.
- **Persisted session whose JSONL was truncated**: the new file's mtime > persisted `last_ts`, so it's parsed. Turns that match existing dedup_keys are skipped; any new turns are inserted. The store never *removes* a persisted turn just because its source file shrank — this protects against accidental file overwrites.
- **DuckDB file corrupted**: WAL recovery on next start. If unrecoverable, log WARN, rename the corrupt file to `history.duckdb.corrupt-<ts>`, treat as fresh install. The user keeps their JSONLs and rebuilds.
- **`hot_window_days` shrunk**: hot tier hydrates the smaller window on next startup. Older turns stay in the warm tier untouched. No data is dropped.
- **`hot_window_days` grown beyond available history**: hydrates whatever exists. Same as today's "earliest_available" handling.
- **Two `tokenol serve` processes started against the same store**: DuckDB file lock fails on the second one with a clear error. Documented.
- **Concurrent `tokenol forget` while serve is running**: handled via a request-file handoff. Serve writes its PID to `~/.tokenol/serve.pid` on startup and clears it on graceful shutdown. `forget` checks the pidfile; if a live serve is detected, it writes the forget request to `~/.tokenol/pending-forget.json` and exits with a "queued; serve will apply within one tick" message. Serve checks for that file once per broadcaster tick (a single `os.stat`, free when absent), processes it through its own write connection, evicts the affected session_ids from the hot tier in memory, and removes the request file. If no live serve is detected, `forget` opens its own write connection and processes the request inline.

## Testing

Existing tests must continue to pass with the store enabled in a tmp directory.

New test surfaces:

- **`tests/persistence/test_store.py`**: schema migration is idempotent; `flush` is idempotent on dedup_key; `query_turns` respects date and project filters; `forget` removes both turns and sessions for matching IDs; corrupt-file recovery renames and re-creates.
- **`tests/persistence/test_flusher.py`**: queue drains every 30s; queue drains when count threshold crossed; force-flush on shutdown leaves no pending turns.
- **`tests/serve/test_state_with_store.py`**: hot-tier hydration produces the same in-memory turn list as parsing the equivalent JSONL set; per-tick incremental derivation produces the same `SnapshotResult.payload` as a full rebuild over the same events; `range=all` handler returns identical rows whether served from hot tier or DuckDB query.
- **`tests/serve/test_archived_session.py`**: deleting a JSONL whose session is in the store produces a snapshot identical to the pre-deletion one in every quantitative field; `build_turn_detail` returns blanked snippet fields and a populated source-file field; the existing tests for `_parse_turn_snippets` remain valid for sessions with live JSONLs.
- **`tests/cli/test_forget.py`**: each `--session/--project/--older-than/--all` form removes the expected rows; `--older-than` recomputes `first_ts`/`turn_count` for surviving sessions and removes sessions whose every turn was dropped; `--yes` skips the prompt; without `--yes` the prompt is required.
- **`tests/persistence/test_forget_handoff.py`**: pidfile staleness detection (PID file present but process gone is treated as no live serve); request file is processed exactly once even if serve restarts mid-tick; `submit_forget_request` is atomic against concurrent CLI invocations.
- **`tests/cli/test_recompute_costs.py`**: a price change in `tokenol/model/pricing.py` followed by `tokenol recompute-costs` updates `cost_usd` in stored turns to match the new pricing while leaving token counts and dedup keys untouched.

## Rollout

Two PRs:

1. **PR 1 — store + flusher + hot tier.** Ships the persistence layer, hydration, flushing, and the modified state/app paths. `hot_window_days` defaults to 90. No CLI commands yet. Behavior change for users: dashboard now survives JSONL deletion of any session that the upgraded build has seen at least once.
2. **PR 2 — `forget` and `recompute-costs` CLI.** Adds the user-facing controls. Ships independently because nothing in PR 1 depends on them.

Both PRs must keep `ruff` and `pytest` green per the project's release-gate convention.
