# Changelog

All notable changes to tokenol are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Changed

- **A server with a history store no longer keeps every parsed transcript event resident.** On the
  store-backed derivation path each file's events are consumed once — a changed file is re-parsed and
  the delta deduplicated by key, and session drill-down re-opens the transcript from disk — yet they
  went through the parse cache, which retained them for the life of the process. On the first tick
  every file on disk is an edge file, so the whole corpus was held for a reader that did not exist.
  Measured at 0.41 MB per transcript file, about 2.1 GB across the 5,109 files of the corpus this was
  profiled on.
- **The warm tier is hydrated as the complement of the hot tier, not the whole store.** The hot tier
  holds every persisted row at or after the cutoff it was hydrated at and only ever grows, so rows in
  that range were rebuilt into `Turn` objects only to be dropped again as duplicates by the merge. On
  the profiled store that was 286,553 of 383,420 rows: 604 MB of the 711 MB a full hydration cost.
  Both tiers now take a single cutoff value computed once by the caller, so they partition the store
  exactly. `HistoryStore` gains `hydrate_since` and `hydrate_before` for the two halves;
  `hydrate_hot` is unchanged for callers and now delegates to `hydrate_since`.
- **The hydrated warm tier is kept while in use and released after two minutes without use**, rather
  than rebuilt every two minutes for as long as anyone was watching. The old list stayed referenced
  while its replacement was built, which is where peak memory came from. Every read counts as use,
  including one served from the merged-snapshot cache. Note that the main dashboard polls an endpoint
  which reads the warm tier, so with a browser tab open the cache stays in use by design and the
  memory is reclaimed only once the tabs are closed.
- **Cold start derives one transcript at a time.** Every edge file's events used to be accumulated
  into a single list before one derivation at the end. On the first tick every file on disk is an
  edge file, so the whole corpus of parsed events was alive at the same moment — and about half of
  that peak never returns to the operating system, because the allocator does not give back
  fragmented arenas. The peak is now bounded by the largest single file. Results are unchanged: a
  dedup key never spans two files, and a file is still derived as one batch.
- **A turn already in the store is never rebuilt, whatever its age.** The in-memory dedup set was
  seeded from the hot window alone, so every persisted turn older than the window was re-derived
  from the transcript, appended to the hot tier, queued for flush and finally rejected by the
  database as a key conflict — 96,867 turns on the store this was measured against, taking the
  whole journey in order to be discarded at the last step. `HistoryStore.dedup_keys()` answers the
  full set in one query, at roughly 15 MB per 100,000 rows.
- **Per-file parse marks now survive a restart** (`~/.tokenol/parse-marks.json`), so a server that
  restarts re-reads only the transcripts that actually changed. The marks were already used to skip
  unchanged files, but were reset on every start, so the fast path was never available exactly when
  it would have helped most. Two rules keep them honest: a mark is written only once the flusher
  confirms every queued turn is *written* to the store rather than merely dequeued, so a crash can
  never leave a mark that outruns the data; and marks are read and written only under `--persist`,
  because a plain `serve` has no writer and skipping a file there would drop its turns. Writes are
  limited to one per minute and only when a mark changed, so an idle server writes nothing.

### Fixed

- **Forgetting a session, project or date range left the deleted turns in the warm-tier cache**, so
  historical totals could keep counting them for up to two minutes after the deletion had been applied
  to the store and the in-memory tier. The forget path now clears that cache in the same tick.
- **An empty history store no longer silences every transcript.** A parse mark asserts that a file's
  turns are already stored. If the store is deleted or replaced while the marks file survives, that
  assertion is false for every mark, and honouring them would skip every transcript and serve an
  empty dashboard indefinitely with nothing to say why. An empty store with non-empty marks is now
  treated as the contradiction it is: the marks are discarded, the file removed, and a warning
  logged. `forget --all` already cleared them on its own path.

### Measured

On the corpus these were profiled against (5,109 transcript files, 383,420 persisted turns, 180 MB
store), a `--persist` server before and after, same machine, same load:

| | before | after |
|---|---:|---:|
| resident, steady | 4,539 MB | 2,357 MB |
| resident, peak | 5,028 MB | 2,492 MB |
| swapped out | 74 MB | 0 MB |

A 48% reduction in steady-state resident memory and 50% at peak. Caveat on the comparison: the
"before" figure is a process that had been up 2.8 days, the "after" one settled over 7 minutes, so
the after figure is a settled reading rather than a long-run one.

The cold-start changes were measured separately, against a true cold start of the previous code on
the same corpus — which turned out to peak higher than any settled reading had shown, because the
whole-corpus transient exists only while the first tick is running:

| | before | after |
|---|---:|---:|
| time to first request served, no marks on disk | 333 s | 117 s |
| time to first request served, marks present | n/a | 17 s |
| resident, peak during cold start | 3,223 MB | 1,298 MB |
| resident, steady | 2,317 MB | ~1,250 MB |
| swapped out | 92 MB | 0 MB |

A 60% reduction at peak, 46% steady, and a 95% reduction in the time a restart takes to become
useful. The 17-second figure was measured with two-second polling, so the true value lies between
15 and 17 seconds; what remains is process startup and hydrating the hot window out of the
database, neither of which these changes touch.

Correctness was checked against the store rather than assumed. The dashboard reported 385,229 turns
all-time and 288,362 over 90 days, a difference of 96,867; the store holds exactly 96,867 turns older
than 90 days. The two tiers therefore partition the data exactly, with nothing lost at the boundary
and nothing double-counted.

### Notes

- DuckDB's buffer pool was measured at 83.5 MiB against its 954 MiB cap on a 180 MB store, so that cap
  is not a constraint and lowering it would save nothing.
- The per-layer figures above count Python-attributed live objects. Resident memory falls by less,
  because the allocator does not return every freed arena to the operating system.

## 2026-09-04 — older RESUME pitfalls rolled down

- **2026-05-03:** Co-Authored-By trailers slipped into 61 commits; fixed via `git filter-branch` + retag + force-push. Verify every commit before push, not just the message you typed.
- **2026-05-02:** DuckDB `executemany` + `ON CONFLICT DO NOTHING` + JSON columns OOM'd at ~89k rows (24+ GiB). Fix pattern (`memory_limit`, `temp_directory`, `preserve_insertion_order=false`, ≤1000-row chunks) lives in `persistence/store.py`.
- **2026-04-25:** lint debt slipped into a release tag (ruff skipped, pytest run) — codified as the three-part pre-release gate below.

## 0.7.5 — 2026-09-04

### Fixed
- **A `--persist` server could hold the store lock long past its advertised shutdown deadline, blocking the next start.** `FlushQueue.stop()` began its 30-second budget only *after* awaiting the in-flight drain, and the deadline is checked between batches rather than during one — so a real shutdown cost `(in-flight batch) + 30s + (one more batch)`. Observed at ~90 seconds against a populated store, well after the checkpoint had already landed; the next `tokenol serve --persist` then failed with a DuckDB lock conflict. The budget now runs from the moment `stop()` is called, and shutdown drains in smaller batches so the final one overruns by less. The in-flight write itself cannot be interrupted — it runs on an executor thread, and `close()` takes the same lock it holds — so the honest guarantee is `stop_timeout` plus at most one batch, and the docstring now says so instead of promising a flat 30 seconds. Measured after the fix: lock released in 35s, with a restart acquiring it immediately.
- **Starting a second tokenol printed a DuckDB traceback instead of saying tokenol was already running.** The failure ended in `IO Error: Could not set lock on file …`, which never mentions tokenol. It now reports one line naming the process that holds the store. This was never a data hazard — DuckDB's lock is exclusive and cross-process, so the second process dies at connect having written nothing, and `turns.dedup_key` makes re-flushing the same turns a no-op — but the error gave no way to know that. Non-lock IO errors (full disk, bad permissions) are untouched.
- **The dashboard multiplied SSE reconnect attempts while its server was down.** `EventSource` fires `onerror` repeatedly, and each one queued another reconnect timer, so attempts stacked up instead of backing off once.

## 0.7.4 — 2026-09-04

> **This release also delivers everything in 0.7.3.** Version 0.7.3 was tagged on 2026-08-18 but never
> published to PyPI, so anyone installing from PyPI is upgrading from **0.7.2** and receives both
> releases' changes at once. The 0.7.3 section below still applies to you.

> **Historical figures will move if you use `--persist`.** Persisted rows are now re-priced on read from
> their stored tokens by the current price table, rather than returning the cost computed when they were
> written. Existing stores will show different — corrected — history after upgrading. Rows written before
> schema v4 did not capture the 5-minute/1-hour cache-creation split and default that column to 0, so they
> price exactly as before on that axis while picking up every other correction.

### Performance
- **A plain `tokenol serve` burned a full CPU core continuously, and four separate causes were responsible.** Measured on a 3,897-file / 258,856-turn corpus at the default 5-second tick, with a browser attached: **99% of one core before, 2–14% after.** Reported totals are unchanged.
  - *The cheap derivation path was reachable only with `--persist`.* `build_snapshot_full` picks an incremental path when given a store and otherwise re-derives every turn from every cached event — and the store was passed only under `--persist`. So the default mode always took the expensive path. Its memo is keyed on a frozenset of every JSONL's `(path, size, mtime)`, and on an active machine something changes within 8 seconds, so it missed on essentially every tick: 2.4–5.3s of CPU per 5s tick. Deriving incrementally only needs to *read* a store, which has nothing to do with whether we are also writing to one, so the read-only handle now serves.
  - *The warm-tier hydration had no single-flight guard.* The check and the fill were unguarded, and the Breakdown page fires six endpoints at once, so one page load hydrated the whole 91,131-row store six separate times instead of once.
  - *The merged snapshot was cached against `generated_at`*, which is regenerated every tick — so the merge was discarded before it could ever be reused, and the next request re-deduped and re-sorted the union of ~350,000 turns. It is now keyed on the data rather than on the build.
  - *The corpus was scanned twice per tick and each file stat'ed three times.* The SSE gate globs and stats every file to decide whether to build; the build then globbed them again and edge selection stat'ed each once more. The gate's key set already carries the mtimes, so it is threaded through: `find_jsonl_files` fell from ~20% of samples to 3.8%.
- **Model resolution is memoized.** `registry.resolve()` runs a regex substitution and a family-prefix scan, and is called once per turn per request — on a 345,378-turn corpus, over a million calls per Breakdown refresh to distinguish about ten distinct model strings, at 10.8% of server CPU. Resolution still returns a fresh tag list per call, so no caller can mutate a shared cached value.
- **The DuckDB connection lock no longer spans row conversion.** `hydrate_hot` held it across the fetch *and* the ~2s of building 91,131 `Turn` objects, stalling the flusher and every other reader throughout. It now covers only the two queries.

### Fixed
- **A growing session file leaked one parse-cache entry per append.** `ParseCache` keys entries on `(path, size, mtime_ns)`, so every append to a live transcript minted a new entry — and nothing removed the old one, whose value is that file's entire parsed event list. The only eviction, `purge()`, runs on a derivation path that store-backed servers never take. The leak grew with session activity rather than corpus size: a server measured 1.71 GB at 3.6 minutes and 2.2 GB at 42 minutes on identical load. After the fix, twenty samples over 38 minutes stayed flat between 1.72 and 1.78 GB. A file has one current version; superseded ones are now dropped.
- **A delete request could be consumed by a server that could not honour it.** The forget hook gated on "is there a store?", which stopped implying "may we write?" once the default mode began opening a store read-only. The request file is unlinked *before* the store is touched, so a read-only server destroyed the request and then failed inside DuckDB, swallowed by a broad `except` — a delete that reported nothing and did nothing. The hook now requires a writable store, and `forget()` refuses a read-only one at the boundary, as `flush()` already did.
- **`--persist` could not shut down, and died mid-write when it was killed.** `_drain_once` took the entire pending list in one uninterruptible executor call, so a first backfill ran for minutes inside a single task. Two runs on a 345k-turn corpus each had to be SIGKILLed after 7 minutes, one leaving 101 MB of an expected ~150 MB store — a data-integrity failure, not merely a slow exit. Drains are now bounded to 5,000 turns per call, the loop keeps draining while a backlog remains, and `stop()` works to a 30-second deadline and *logs the count* of anything it could not write (those turns are re-derived from JSONL on next start, if it has not been pruned).
- **`compaction_reinflation` reported cycles that never happened.** The detector compared consecutive raw context sizes across `session.turns`, which interleaves the orchestrator's turns with every sub-agent's. Compaction acts on the orchestrator's context; a sub-agent carries its own. A large sub-agent turn beside a small orchestrator turn — the normal shape of any concurrent-agent session — matched the peak/drop/reinflate signature exactly. It now reads the main thread only, carrying original positions through so reported turn indices are unchanged.
- **Pricing corrections never reached persisted rows.** Each row stored the cost computed when it was flushed, and the read path returned that number verbatim — so today's Opus 5 and Fable 5.1 entries would not have touched a single persisted turn. Rows are now priced from their stored tokens by the current table. Schema v4 keeps the 5m/1h cache-creation split, so the recompute has the same inputs the live path does; rows written before v4 default that column to 0 and price exactly as they did before, but still pick up every other correction. **Historical figures will move after this change** — that is the correction landing on rows priced by an older table.

### Added
- **`tokenol serve` now reads an existing history store without `--persist`.** Reading the warm tier and writing it were the same switch, which meant the only way to see persisted history was to run a flusher thread inside the server — the half that costs CPU and, before the connection lock, crashed the process. They are now separable: a plain `serve` opens any existing `~/.tokenol/history.duckdb` **read-only** and merges it, while `--persist` is solely about keeping that store current. Measured on a real store: `range=all` went from $42,790 across 111 days to $51,644 across 154, recovering $8,854 and reaching back to 2026-02-28, with no writer in the process. A missing, corrupt, or write-locked store degrades to "no warm tier" rather than failing startup.
- **Read-only opens tolerate older schemas.** A store opened read-only cannot be migrated, and pre-v2 files lack every column migrations v2–v4 added — which would have made the warm tier unreadable on exactly the old stores holding the history worth recovering. The read path now substitutes each missing column's default, reproducing what the migration would have written.

### Fixed
- **`tokenol serve --persist` segfaulted the whole process under normal use.** A `duckdb.DuckDBPyConnection` is not safe for concurrent use, and `HistoryStore` shared one connection across threads with no synchronisation: the flusher writes from its executor thread while dashboard requests read from theirs. The two race inside `ClientContext::PendingQuery` and the process dies with SIGSEGV — no traceback, no log line, just a coredump. Captured 2026-09-04: `#0 __memcpy_avx_unaligned_erms (libc.so.6)` / `#1 duckdb::ClientContext::PendingQuery(...)` on a flusher thread while the event loop held the GIL. This is the long-standing "`--persist` segfaults" bug, which was never a DuckDB version mismatch. Every use of the connection is now serialised on an `RLock`, scoped per transaction rather than per `flush()` call so a large flush yields between chunks instead of stalling the dashboard for its whole duration.
- **The Breakdown page ignored the persisted history entirely, so every long-range total was silently truncated.** The warm tier (`--persist`) exists so history outlives Claude Code's pruning of old transcripts, but the merge was wired into only two endpoints — `/api/project/{cwd}` and `/api/daily` — and both gated it behind `range=all`. All six `/api/breakdown/*` endpoints read the hot tier alone, so the Breakdown page's ACTIVITY / BILLABLE TOKENS / CACHE / EST. COST tiles, the daily charts and every by-project, by-model, tool and skill rollup reported "all the history that happens to survive on disk" as if it were all the history. Measured on a real store: April 2026 read $790 from JSONL against $6,513 of persisted turns for the same month, an 8x understatement. Every endpoint that answers a historical question now folds in the warm tier through one shared helper, for **all** ranges rather than only `all` — a 30- or 90-day window reaches past the JSONL horizon just as easily once pruning has run. Overlap is resolved on `dedup_key`, and the merged result is cached against the snapshot's `generated_at` so a page firing six requests pays for it once.
- **A single session started in the home directory collapsed every project into one bucket.** Project grouping rolls a nested cwd up to its shortest active ancestor, so `/dev/proj/backend` reports under `/dev/proj`. A session whose cwd was the home directory itself (`cd ~ && claude`) made the home directory a proper ancestor of every project on the machine, and the whole dashboard — the project filter, Tokens by Project, per-project rollups — collapsed to a single entry named after the user. Container directories (a home directory, a filesystem root, a mount point, and anything above them) are now excluded as roll-up *targets*; a session that genuinely ran in one still appears as its own project. Matched by path shape rather than against `$HOME`, since cwds are ingested from other machines too.
- **Fable 5.1 cache reads overcharged 4x.** `claude-fable-5-1` had no table entry and fell back to `claude-fable-5`, which reads cache at the standard 0.1x of input ($1.00/MTok). Fable 5.1 and Mythos 5.1 read at 0.025x ($0.25/MTok) — the only models that deviate from the 0.1x rule. Cache reads dominate an agent workload, so the fallback inflated Fable 5.1 spend by roughly 4x.

### Performance
- **Warm-tier reads no longer rebuild the whole store per request.** The merge hydrated every persisted turn on each call and then issued one DuckDB round-trip *per* warm session — 300+ queries on a real store, each contending with the flusher for the connection lock, enough to push a breakdown request past a 25-second timeout. It now goes through `hydrate_hot`, which answers both in two queries, and caches the result for 120s since the warm tier only changes when the flusher writes.

### Added
- **Explicit `claude-opus-5` and `claude-fable-5-1` pricing entries** (verified against Anthropic's pricing page 2026-09-04). Opus 5 bills identically to Opus 4.8 ($5/$25, cache $6.25 5m / $10 1h / $0.50 read), so its totals were already right, but as an unmapped model it resolved through the family fallback and was reported as an estimated price — on this corpus that covered the single largest model by spend. Both now price exactly.

### Changed
- **Sonnet 5's $2/$10 rate is now documented as standard, not introductory.** Anthropic cancelled the increase to $3/$15 that had been scheduled for 2026-09-01. The table value is unchanged; the note that told a future reader to raise it has been replaced with one telling them not to.

## 0.7.3 — 2026-08-18

### Fixed
- **Phantom Fable-5 pricing on non-Claude models.** When running Claude Code with alternative models via translation proxies or multi-provider gateways (e.g. DeepSeek, Qwen, GLM, Kimi, MiniMax, Mimo), unrecognised model names previously fell back to the table's default sibling (`claude-fable-5` at $10/M input, $50/M output), generating hundreds of dollars in phantom costs. `ModelRegistry` now strictly recognizes the Claude family (`fable`, `opus`, `sonnet`, `haiku`, and `claude-` prefixes); any non-Claude model resolves to unpriced ($0.00 marginal cost) tagged with `GEMINI_UNPRICED`.
- **Metric pollution from non-Claude models.** Non-Claude turns (which produce zero Anthropic cache-read tokens and consume no Anthropic 5-hour rate limits) are now excluded by default from CLI metrics (`daily`, `hourly`, `live`, `sessions`, `projects`) and the web dashboard (`serve`), preserving clean 98%+ cache hit rates and eliminating false rate-limit burn alarms. `tokenol models` gains `--all-models` to view all models when desired.

## 0.7.2 — 2026-07-17

### Fixed
- **1-hour cache-write tokens were priced at the 5-minute rate.** Anthropic bills prompt-cache writes at 1.25x input for a 5-minute TTL (the default) or 2x input for a 1-hour TTL (opt-in — Claude Code requests it when a session's turns are spaced further apart than 5 minutes, e.g. waiting on a long-running sub-agent), reported via a nested `usage.cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens` breakdown. tokenol only read the flat `cache_creation_input_tokens` total and priced all of it at the 5-minute rate — understating cache-write cost by up to 37.5% on any turn using 1-hour caching. Measured on real session data across two samples of differing scope: ~28% of cache-write cost understated / 66% of cache-creation tokens on the 1-hour tier (40-file sample), and 36% / 95% (full local corpus, from a heavily sub-agent-orchestration-driven workload). **The actual split is workload-shape dependent** — sessions with more sub-agent fan-out or long idle gaps between turns push more cache-writes onto the 1-hour tier than simple, sequential single-threaded sessions; don't treat either number as a universal rate. `Usage` gains `cache_creation_1h_input_tokens`; the parser now extracts the breakdown; `cost_for_turn` prices the two tiers separately. Every `ModelEntry` gains a `cache_write_1h` rate (verified against Anthropic's pricing page). **Existing `~/.tokenol/history.duckdb` rows retain their pre-fix (understated) `cost_usd`** — the raw 5m/1h split was never persisted for those turns, so a `forget` (clear history) + re-ingest from source JSONL is required to recompute them correctly; new flushes are unaffected going forward.
- **Unmapped-but-active models silently mispriced via family fallback.** `claude-sonnet-4-5`, `claude-opus-4-5`, and `claude-opus-4-1` (all still active per Anthropic's pricing page) had no table entry, so any turn on them fell back to the newest sibling in their family — e.g. Sonnet 4.5 ($3/$15) priced as Sonnet 5's introductory rate ($2/$10), or Opus 4.1 ($15/$75) priced as Opus 4.8's rate ($5/$25), a 3x underprice. Added explicit entries for `claude-sonnet-4-5(-20250929)`, `claude-opus-4-5(-20251101)`, `claude-opus-4-1(-20250805)`, plus the older `claude-opus-4(-0)(-20250514)`, `claude-sonnet-4(-0)(-20250514)`, and `claude-3-haiku-20240307` for historical-log coverage.

### Internal
- `FAMILY_FALLBACKS` simplified from `dict[str, list[str]]` to `dict[str, str]` — the list form's entries past index 0 were dead code (`registry.resolve` only ever read `[0]`).

## 0.7.1 — 2026-07-13

### Fixed
- **`claude-sonnet-5` pricing.** Added the missing pricing-table entry for `claude-sonnet-5` ($2.00 input / $10.00 output per 1M tokens, intro rate through 2026-08-31; cache $2.50 write / $0.20 read; 1M context). Previously unpriced Sonnet-5 turns silently fell back to `claude-sonnet-4-6` rates via family fallback instead of surfacing as a priced or flagged model. Also updates the `sonnet` family's newest-model fallback to Sonnet 5.

### Internal
- Removed dead `DUAL_SESSION_CONFLICT` verdict plumbing (verified unreachable — its only possible producer, `detect_dual_session_conflict`, was never wired in).
- Fixed a test time-bomb: `test_daily_insufficient_history` compared a fixture's hardcoded 2026-04-14 timestamp against real `date.today()`, which stopped triggering the intended fallback once 90 real days had passed. The test now builds its event data relative to `date.today()` instead of a fixed-date fixture.

## 0.7.0 — 2026-06-10

### Added

- **Skills cost dimension.** New first-class Skill dimension parallel to Tools/Models/Projects, driven by Claude Code's `attributionSkill` log tag. A "Skill Mix" panel sits alongside the Tool/Model/Project mixes in the Breakdown page's Breakdowns section, ranking skills by cost (incl. their sub-agent fan-out); a Skill detail page (`/skill/{name}`) shows scorecards, a 30-day daily-cost chart, cost-by-model / cost-by-project, and an **inline vs sub-agent** cost split. Model and Project detail pages gain a Cost-by-skill bar. The previously-misleading single "Skill" row in Tool Mix (trigger cost only) is dropped in favour of the real per-skill numbers.
- **Plain-language caveat notes on every Breakdown panel.** Each panel (and the Billable-tokens scorecard) gains a small ⓘ with a jargon-free explanation of what it does and doesn't measure — that per-tool and (unrecognised) model costs are estimates, that Skill Mix excludes un-skilled work and won't sum to the total, that cache re-use is money saved rather than spent.
- **Honest pricing on Model Mix.** Models whose price isn't in the table are flagged in the subheading — "estimated price" for an unrecognised Claude model (matched to a similar one) or "shown as $0" for a provider with no price — instead of silently understating cost.
- **Skill Mix shows started-but-uncharged skills.** Skills that ran without any cost billed to them are summarised ("+N started with no separate cost") rather than vanishing from the cost-ranked list.
- **Share-of-total in Tool Mix and Skill Mix.** Each panel's subheading shows what fraction of the whole it accounts for, matched to the active unit: share of total spend in the $ view, share of all billable tokens in the Tokens view. The two genuinely differ because $/token isn't uniform — e.g. tool calls are ~51% of spend but ~78% of billable tokens (input-heavy, and input is cheaper than output).
- **Claude Fable 5 pricing.** New top-tier model (`claude-fable-5`) above Opus, priced at $10.00 input / $50.00 output per 1M tokens, with a 1M-token context window. Cache rates follow the table-wide convention: $12.50 cache-write (1.25x input) and $1.00 cache-read (0.1x input). A new `fable` model family is registered ahead of `opus`/`sonnet`/`haiku`, so unknown or future-dated Fable IDs (e.g. `claude-fable-5-20260601`) fall back to Fable pricing rather than the wrong family. Cost, cache-savings, and dashboard model breakdowns pick it up with no further changes.
- **Claude Opus 4.8 pricing.** Added `claude-opus-4-8` to the model table (same rates as Opus 4.7: $5.00 / $25.00 per 1M, 1M context, $6.25 cache-write / $0.50 cache-read) and made it the newest `opus` fallback. Closes the gap where Opus 4.8 turns were priced via family fallback to Opus 4.7.
- **Context-window suffix normalization in model resolution.** Claude Code appends a `[1m]` marker to logged model IDs for the 1M-context variant (e.g. `claude-opus-4-8[1m]`). `ModelRegistry.resolve` now strips any trailing `[...]` marker before lookup, so a `claude-fable-5[1m]` turn prices as `claude-fable-5` (exact match, no assumption flag) instead of falling through to family fallback. Model-agnostic: benefits every present and future model whose logged ID carries the suffix.

## 0.6.1 — 2026-05-16

### Added

- **Attribution mode toggle on the Tool Mix panel.** Two-position pill group in the panel header lets you switch between the existing pro-rata cost split and a new "exclude cache-read" lens. The second mode routes cache_read_usd 100% to the non-tool residual instead of distributing it pro-rata across visible tool bytes, answering "what do tools cost excluding the cost of keeping their output around for subsequent turns?" Selection persists in localStorage; hidden when the panel is displaying token counts (mode is a cost-only concept).
- `mode=` query parameter on `GET /api/breakdown/tools` — accepts `prorata` (default) or `excl_cache_read`. Unknown values fall back to `prorata` silently (forward-compatible: older servers degrade gracefully when clients persist a newer mode token). The response echoes the effective `mode` in a new top-level field.
- `state.build_breakdown_tools(turns, *, mode='prorata') -> list[dict]` — extracts the previously-inline aggregation loop from `api_breakdown_tools` into a unit-testable module-level function.
- `tokenol.enums.AttributionMode` — new `str` enum (`PRORATA` / `EXCL_CACHE_READ`) so consumers can pass typed values instead of string literals to `build_breakdown_tools` and the API endpoint.

### Changed

- **`_wireUnitPills` → `_wirePillGroup`** in `breakdown.js`. Renamed and the `dataAttr` parameter is now required (no default), so each of the five pill groups passes its attribute explicitly. Reads as "wire a pill group keyed on this data-attr" instead of pretending unit and mode pills are the same concept.
- **`build_breakdown_tools` unified branch shape.** Both pro-rata and excl_cache_read modes now build a per-tool cost dict + residual pair before a single shared accumulation loop. Removes a copy-pasted last-active update block and the duplicate `cost_for_turn(...)` call that excl-mode used to make per turn.
- Per-tool docs added to README, `docs/ASSUMPTIONS.md` (heuristics catalog), and `docs/METRICS.md` (formulas + API field reference).

### Fixed

- **`_grouped_cwd_by_sid` memoization keyed on `id(sessions)` could return stale cwd remaps.** Python recycles `id()` for freed list objects, so a freshly-derived sessions list that happened to land at a recently-freed list's id would receive that prior list's cached remap — every session id missing from the old remap fell back to `"(unknown)"`. The cache key is now a content fingerprint (tuple of `(session_id, cwd)` pairs). Pre-existing latent bug surfaced by a declared-order test run during 0.6.1 release prep.
- **XSS hardening on project / session tables.** Eleven spots interpolated JSONL-derived strings (cwd, session_id) into HTML without escaping: `model.js`'s "projects using this model" cwd cell; `day.js`'s "top projects" title + cell and its "sessions" `data-id` + 8-char session-id cell; `app.js`'s "recent activity" cwd cell + the new "latest session" href; `components.js`'s shared `sessionRows` `<tr>` `data-id` + `title` + 8-char session-id cell; `project.js`'s "Top Turns" `data-sess` + 8-char session-id cell and its "Project Sessions" `data-id` + 8-char session-id cell. A pathological cwd basename or session_id like `"><img src=x onerror=…>` no longer executes. All eleven are pre-0.6.1 patterns surfaced by the release-gate security + adversarial review passes; included here to clear them before release.
- **Tool literally named `other` no longer collides with the synthetic collapse-tail row.** `_is_real_tool_name` (parser) now rejects `"other"` alongside `__unattributed__` / `__unknown__`. Without this filter, an attacker could craft a JSONL with a tool named `other` whose invocation count would be silently overwritten by the ranked-bar aggregator's tail-collapse logic.
- **`fmtUSD` rendered negative values as `$-0.50`** instead of `-$0.50`. Today no caller passes a negative — the bug was dormant — but `cost_per_kw`, `cost_usd`, and similar fields have no formal non-negativity guarantee, so a future regression would surface as garbled currency.
- **`$/kW` scorecard read `$0`** on the Overview page. `fmtUSD` (introduced in 0.6.0's "standardise dollar formatting on whole dollars" pass) was applying `Math.round` to every value, stamping out the entire sub-dollar signal — `$/kW` is inherently $0.01–$1 territory, so the scorecard, its `<$X GOOD · >$Y RED` threshold labels, and the `last hour: $…` sub-line all read `$0`. Now `fmtUSD` keeps two decimals for values in `[$0.01, $1)`, five decimals for `(0, $0.01)`, and whole dollars for `≥ $1` (the original rationale held for the $10+ table values). No call-site changes required.
- **Sub-dollar bypasses around the dashboard.** Sweep across every static JS file found four more places that bypassed the shared `fmtUSD` and would have shown `$0` for sub-dollar values: `session.js` had a local `fmtUSD = v => $${v.toFixed(2)}` (which would render a $0.003 turn cost as `$0.00`); `project.js` rendered `cost_per_kw` for both Top Turns and Project Sessions tables via inline `$${...toFixed(2)}`; `tool.js` daily-cost chart used `(v) => '$' + v.toFixed(2)` as its Y-axis tick callback; `chart.js` `Y_FMTRS.usd` (used by uPlot cost charts on Overview / Breakdown) was `$${v.toFixed(2)}`. All routed through the shared `fmtUSD` now.
- **`renderRankedBars` default formatter footgun.** The default `valueFormat` when omitted was `(n) => "$" + n.toFixed(2)`; now `fmtUSD`. No current caller relied on the default, but a future omission would have silently swallowed cents.
- **`↓0% vs 7d median` ghost arrow** on Overview tiles. When the rounded delta is exactly 0%, the tile would still render an arrow + "0%" (reading as a tiny decrease but actually flat). `_setTileDelta` now mirrors `deltaBadge`'s 0% suppression and falls through to the plain `vs <baseline> median` text.
- **Stale Tool Mix pill state on truthy-but-unknown localStorage values.** The `|| 'prorata'` fallback only triggered on falsy values, so a value left over from a prior build (or manually set) bypassed defaulting and left the pills with no selection highlighted. All five breakdown pill states now validate against a whitelist on load.

### Performance

- **`-215 MiB steady RSS` (-31 %)** for the no-persist server on a 92 K-turn / 375 K-event corpus (685 MiB → 470 MiB after-derive). Three stacked optimisations:
  1. **`slots=True` on every high-count dataclass.** `RawEvent`, `Turn`, `Usage`, `ToolCost`, `Session`, `Project` in the model layer (replicated 90 K-fold), plus the metrics-side rollup classes (`TurnCost`, `DailyRollup`, `HourlyRollup`, `SessionRollup`, `ProjectRollup`, `ModelRollup`, `DailyToolCost`), `PatternHit`, and `Window`. Saves 88 B/`Turn`, 88 B/`Usage`, ~60 B/`ToolCost` by dropping per-instance `__dict__`. **-82 MiB.**
  2. **Shared empty-container sentinels** (`EMPTY_TOOL_NAMES`, `EMPTY_TOOL_COSTS`, `EMPTY_ASSUMPTIONS` in `tokenol.model.events`). 275 K of 376 K events have no `tool_use` blocks; previously each carried a fresh empty `Counter` (80 B), empty `dict` (64 B), and on the `Turn` side every assumption-free turn carried a fresh empty `list` (56 B). All collapse to module-level singletons (audited: no in-place mutation anywhere in `src/tokenol/`). **-38 MiB.**
  3. **`sys.intern()` on low-cardinality string fields** at parse time: `source_file` (1929 unique paths × 376 K refs), `event_type` (~3 unique), `session_id` (447 unique × 92 K refs), `model` (~3 unique × 92 K refs), `stop_reason` (~5 unique), `cwd` (~50 unique). Strings from `json.loads` aren't interned by default; without sharing, the corpus carries 1.5 M near-duplicate `str` objects with ~50 B/string overhead. **-95 MiB.**

  No behavioural change — every transformation is internal to the parser + the dataclass layout. Restart the server (`uv run tokenol serve --port 8787`) to pick up the change.

### Notes

- No persistence changes — the mode toggle is purely a presentation-layer reinterpretation of already-stored per-turn `tool_costs` data.
- No changes to other panels — scorecards, daily charts, by-project, by-model, and the tool detail page (`/tool/{name}`) all stay on pro-rata regardless of the toggle.

## 0.6.0 — 2026-05-15

### Added

- **Per-tool cost attribution.** Causal model that splits a turn's four cost
  components (`input_usd + output_usd + cache_read_usd + cache_creation_usd`)
  across each tool by JSON byte share. Output side attributes by `tool_use`
  block bytes; input side attributes by lingering `tool_use` + `tool_use_result`
  bytes still in the conversation window. Compaction is detected heuristically
  (input pool drop below 20% of running peak) and resets the lingering tallies.
- **Breakdown → Tool Mix in `$` mode.** The TOKENS/$ toggle now extends to the
  Tool Mix panel; chart switches from Chart.js bars to a ranked-bar list. A
  dim italic `__unattributed__` row surfaces residual so totals reconcile to
  overall spend.
- **Tool detail page redesign** (`/tool/<name>`). 30-day daily-cost line chart,
  four scorecards (Est. Cost · Output tokens · Invocations · Top project), and
  cost-by-project + cost-by-model ranked bars replace the previous tables.
- **Project + model detail pages** gain a "Cost by tool" ranked-bar list.
- New API fields:
  - `/api/breakdown/tools` rows now include `cost_usd`, `count`, `last_active`,
    and a final `__unattributed__` sentinel row.
  - `/api/tool/{name}` adds `scorecards`, `daily_cost` (30 zero-filled points),
    `by_project`, `by_model`. Old `projects_using_tool` / `models_using_tool`
    keys removed.
  - `/api/project/{cwd_b64}` and `/api/model/{name}` add a `by_tool` block.

### Fixed (Tier 3 release-gate review)

Every finding surfaced by the Tier 3 review pipeline (6-specialist fan-out +
`fp-check` + `/second-opinion` + adversarial re-run) is fixed in this release.
No items deferred.

**Frontend correctness**

- **`breakdown.js` no longer crashes on load.** `const UNATTRIBUTED_TOOL =
  UNATTRIBUTED_TOOL;` was a temporal-dead-zone self-reference that threw
  `ReferenceError` and killed the entire Breakdown page. Replaced with the
  literal sentinel value.
- **Tool detail "30d total" subtitle now matches the chart.** `tool.js` was
  passing the all-time `scorecards.cost_usd` to the daily chart's "30d total"
  label; switched to summing the 30 daily points client-side.
- **`tool.js` model card cleanup.** Removed the `project_label` /
  `last_active` copy-paste branches that were only relevant to the
  by_project renderer.
- **XSS hardening in tool scorecards.** `tool.js` now passes interpolated
  values through `esc()` before rendering via `innerHTML`. A pathological cwd
  basename like `<img src=x onerror=…>` no longer executes (self-XSS only,
  but the fix is one import + four `esc(…)` calls).

**Aggregation correctness**

- **`by_tool` rollups now reconcile.** `_accumulate_tool_costs` and its
  callers (`build_project_detail`, `build_model_detail`) previously iterated
  only `tool_names`, dropping tools whose presence was purely linger-only
  cost. They now iterate `set(cost) | set(invs)`, so
  `sum(by_tool[].cost_usd)` matches the scorecard totals.
- **Tool detail page surfaces linger-only attribution.** `build_tool_detail`
  expanded its `tool_turns` filter to include turns where the tool appears
  in `tool_costs` even without a fresh invocation. Sentinel names
  (`__unattributed__` / `__unknown__`) explicitly return 404.
- **`__unknown__` no longer leaks as a clickable row.** `/api/breakdown/tools`
  folds `__unknown__` into the `__unattributed__` row; `_accumulate_tool_costs`
  does the same for project/model `by_tool` views.
- **`by_project` / `by_model` payloads capped at 50 entries** to bound API
  response size for users with hundreds of projects or model variations.
- **`other` row in Tool Mix now reports real call sums.** The "other" row's
  `count` (used as the bar value in tokens mode) is now the sum of tail tool
  invocations rather than the count of collapsed tools. The collapsed-tool
  count moved to a new `tool_count` field, displayed in the row label.
- **UTC-based date windows in new code paths.** `build_tool_detail` and
  `build_tool_cost_daily` now use `datetime.now(tz=timezone.utc).date()`
  instead of local `date.today()`, matching the UTC timestamps stored on
  every Turn.

**Parser correctness**

- **Compaction heuristic resets `peak_input_tokens`.** Previously a long
  session that stabilised below 20% of its historical peak kept re-triggering
  the reset on every turn, dumping all per-turn attribution into
  `__unattributed__`. Peak now resets to the new pool after a compaction
  event so the heuristic only fires on genuine context drops.
- **Sentinel tool-name collision rejected.** `_extract_tool_blocks` and
  `_output_byte_shares` now drop `tool_use` blocks whose name is
  `__unattributed__` or `__unknown__` so a hostile log can't hide cost under
  the cost-attribution sentinels.
- **Plain-string assistant content no longer dropped from byte tallies.**
  When `message.content` is a string (rare but spec-legal for short replies),
  the parser now wraps it as a single `text` block so its bytes feed the
  non-tool input pool on subsequent turns — preventing slight over-attribution
  to lingering tools.
- **`_block_bytes` catches `RecursionError`.** A deeply nested malformed
  content block would have crashed the entire `parse_file` (the previous
  `except (TypeError, ValueError)` missed `RecursionError`).
- **`_block_bytes` called once per content block.** `_output_byte_shares`
  now accepts pre-sized `(block, bytes)` pairs so each block is serialized
  exactly once per assistant turn rather than twice (output-share pass +
  context-accumulation pass).

**Performance**

- **`build_tool_cost_daily` now scoped to `tool_turns`** in
  `build_tool_detail` instead of walking the full corpus on every
  `/api/tool/{name}` request.
- **`_grouped_cwd_by_sid` memoized per snapshot.** O(C²) cwd ancestor scan
  no longer reruns on every API request; bounded LRU keyed on `id(sessions)`.

**Persistence**

- **`tool_costs` and `unattributed_*` round-trip through DuckDB.** Schema
  v2 adds `tool_costs JSON` plus three `unattributed_*` DOUBLE columns to
  the `turns` table; migration is idempotent via `ALTER … IF NOT EXISTS`.
  Existing v1 databases upgrade in place on open. Warm-tier breakdowns
  for `--persist` users on `range=all` now reconcile correctly. Older v1
  rows pre-dating this release hydrate with empty `tool_costs` until they
  age out of the window — re-ingest from the source JSONL files to backfill.

**API hardening**

- **`/api/tool/{name}` and `/api/model/{name}` accept path-segment names.**
  Switched to FastAPI's `{name:path}` converter so MCP tool names like
  `mcp__server/tool` resolve instead of 404ing; explicit validation rejects
  empty names, `..` path-traversal segments, and embedded NULs.

**Rollups**

- **`_rank_dict_with_others` is deterministic on ties.** Sort now uses
  `(-value, name)` so equal-cost entries don't shuffle "other" membership
  between runs.
- **Dead `build_tool_cost_rollups` / `ToolCostRollup` removed.**
  `state.py:_accumulate_tool_costs` was the only consumer of similar logic
  and lived in a different shape; the unused rollups version is gone.

### Notes

- DuckDB schema bumps to v2; migration is idempotent so opening an existing
  0.5.x history file upgrades in place. Per-tool token fields are floats
  (fractional after share split); aggregate reconciliation is exact to
  floating-point precision.

## 0.5.1 — 2026-05-15

### Fixes

- **`tokenol serve` now scans every `~/.claude*` directory by default.**
  Previously the dashboard honored `CLAUDE_CONFIG_DIR` and silently scoped
  itself to a single project when workspace isolation pointed the env var
  at one directory — which made Daily History look mysteriously empty for
  days when you were working in other projects. The dashboard is now
  always cross-project unless you explicitly pass `--scoped`. CLI commands
  (`daily`, `sessions`, `projects`, …) are unchanged — they still default
  to single-project with `--all-projects` / `-A` as the opt-in.
- The old `--all-projects` / `-A` flag on `serve` has been removed (the
  behavior it produced is now the default). Update any scripts that
  passed it; otherwise no action needed.

## 0.5.0 — 2026-05-15

### Features

- **Overview: dual-metric compare overlay on Hour-By-Hour and Daily History.**
  Each chart gains a small `compare` toggle pill. Toggle on → pick a second
  metric from the existing pill row and it renders on a right y-axis,
  overlaid on the primary series. The secondary line is slate-blue
  (`--series-secondary`) at 1.5 px for visual restraint; an inline legend
  below the pill row names both. LIN/LOG applies to the primary axis only;
  secondary axis is always linear. Toggle off drops the secondary and
  restores single-series view. Selection and compare state persist per chart
  in `localStorage`. First-time users land on `HIT%` single-series exactly
  as before.
- **Breakdown: per-chart `TOKENS / $` toggle on three cards.** Daily Billable
  Tokens, Tokens by Project, and Model Mix gain a small pill pair next to
  their titles. `$` mode shows actual cost stacked by component
  (input / output / cache created / cache read) so the bar height equals the
  per-bucket cost; Model Mix slice sizing switches to cost share, which
  surfaces how heavily Opus dominates cost despite being a small token
  share. Cache-hit dots, "top N of M" captions, and the existing
  $-annotated summary cards are unchanged. Each chart's mode is independent
  and persists in `localStorage`.
- **Backend payloads enriched with cost.** `/api/breakdown/by-project` returns
  per-component cost (`input_cost`, `output_cost`, `cache_creation_cost`,
  `cache_read_cost`); `/api/breakdown/by-model` returns `cost_usd` and
  `cost_share` per model; `/api/breakdown/daily-tokens` returns the same
  four per-component cost fields per day. No new endpoints, no schema
  changes. The Tools chart and the Daily Cache Re-use chart keep their
  current shapes — per-tool cost attribution would need a parser change,
  deliberately deferred (same constraint that omitted the error-rate column
  from Tool drill-down).

### Fixes

- **Chart y-axis lower bound is clamped to 0.** Non-negative tokenol metrics
  (Hit%, Output, Cost, …) no longer extend into a phantom negative-padding
  zone when the data is close to zero.
- **Right-axis labels no longer read as negatives.** Hidden the inward tick
  marks on the secondary axis — they were touching the dollar signs and
  visually reading as minus signs.
- **Secondary-metric switch rebuilds the chart instance.** uPlot fast-path
  now compares the secondary y-unit too, so switching the overlaid metric
  (e.g. Output → Cost) replaces the value formatter instead of leaving the
  old one in place.

### Internal

- `chart.js` `drawChart` (uPlot) accepts an optional `secondary` series with
  its own right y-axis (`y2`). Single-series callers pass the flat opts shape
  unchanged; dual-axis callers wrap it as `{ primary, secondary }`.
- `_bucket_turns` in `serve/app.py` accumulates per-component cost
  (`input_cost`, `output_cost`, `cache_read_cost`, `cache_creation_cost`,
  `total_cost`) alongside the existing token totals, so by-project and
  by-model endpoints reuse the same aggregation pass.
- CSS-var lookups in the breakdown palette and Overview legend are now
  memoized — design tokens are static for the page lifetime and were being
  hit several times per chart × every SSE tick.

## 0.4.1 — 2026-05-03

### Fixes
- **Daily History range pills now actually filter the chart.** `rollup_by_date`
  zero-filled the requested `[since, until]` window but never dropped turns
  dated before `since`, so 7D / 30D / 90D rendered the same full series as
  ALL. Turns outside the window are now skipped before bucketing.

## 0.4.0 — 2026-05-03

### Features
- **Persistent history that survives JSONL deletion** (opt-in via
  `--persist`). `tokenol serve --persist` backs the live in-memory
  dashboard with a single-file DuckDB store at `~/.tokenol/history.duckdb`
  (override via `TOKENOL_HISTORY_PATH`). On startup the store seeds the
  in-memory hot tier so cold start is bounded by `hot_window_days`
  (default 90), not by total history length. Each tick parses only JSONLs
  whose `mtime_ns` exceeds the per-session high-water mark — typically
  just today's active files — and appends derived turns to both memory and
  a background batch flush (every 30 s or 100 turns). Deleting a JSONL no
  longer drops its data from the dashboard; the affected sessions are
  marked `archived=True` and continue to render every quantitative panel.
  Only the per-turn modal's content snippets (user prompt, assistant
  preview, tool-call list) become unavailable for archived sessions, in
  line with the privacy intent of the deletion. Default off — `tokenol
  serve` without `--persist` matches the v0.3.2 resource profile
  byte-for-byte (no `import duckdb`, no `~/.tokenol/` directory, no extra
  steady RSS).
- `Preferences.hot_window_days` (default `90`, accepted range `1..3650`),
  exposed via the existing `/api/prefs` endpoint. Takes effect on next
  startup.
- `Session.archived: bool` field surfaced through `/api/session/{id}` and
  `/api/session/{id}/turn/{idx}`; the session-detail page renders an
  amber "Archived — text snippets unavailable" badge and hides the
  per-turn snippet block when the flag is set.
- `tokenol.persistence.forget_handoff` — a pidfile + atomic request-file
  handshake so a future `tokenol forget` CLI (PR 2) can apply deletions
  to a live serve within one tick, without requiring a restart.

### Changes
- `duckdb` moved from a core dependency to the new `[persist]` optional
  extras group. Default `pip install tokenol` no longer pulls the DuckDB
  binary wheel (~30 MB saved). Users who pass `--persist` install with
  `pip install 'tokenol[persist]'`.
- `build_snapshot_full` now accepts optional `history_store` and
  `flush_queue` arguments. When neither is supplied the legacy whole-corpus
  derivation path is used unchanged, so CLI report commands and any
  existing test that constructs a bare `ParseCache` keep working.
- Default mode prints a yellow `WARNING` at startup if it finds an existing
  `~/.tokenol/history.duckdb` (or `TOKENOL_HISTORY_PATH`), prompting the
  user to pass `--persist` if they want to use it (rather than silently
  ignoring the file).
- `select_edge_paths` now tracks per-file `mtime_ns` instead of comparing
  filesystem mtime to turn timestamps — fixes a freshness bug in the
  store-backed snapshot path where backdated turn timestamps could silently
  exclude files from re-parse.

### Notes
- See `docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md` for
  the gating-and-extras design.
- See `docs/superpowers/specs/2026-05-02-persistent-history-design.md` for
  the underlying store design.

## [0.3.2] — 2026-04-28

### Fixed
- **Dashboard fallback endpoints no longer freeze on stale turn counts.**
  The 0.3.1 `/api/snapshot` fast-path stopped refreshing
  `app.state.snapshot_result`, so every endpoint that fell back to it
  (`/api/hourly`, `/api/daily`, `/api/models`, `/api/recent`, `/api/session/*`,
  `/api/project/*`, `/api/breakdown/*`, `/api/search`, `/api/model/*`,
  `/api/tool/*`) silently froze at first-page-load turn counts. Symptom: the
  HIT% panel showed live turns but switching to $/KW, CTX, Cache Reuse, Output,
  or Cost rendered stale numbers. Fix: `SnapshotBroadcaster` now exposes its
  freshest `SnapshotResult`, and fallback endpoints prefer it over the
  app-level cache.

## [0.3.1] — 2026-04-27

### Fixed
- **Dashboard auto-update self-heals from SSE drift.** The live dashboard
  could intermittently freeze on stale tile/chart values while the SSE
  connection appeared healthy (dot green, messages arriving), requiring a
  hard reload. Reproduced reliably in long-lived browser tabs; root cause
  is browser-environmental (extension hooks, accumulated tab state, or
  long-lived `EventSource` quirks). Fix is a layered, root-cause-agnostic
  resilience set in the static client:
  - `/api/snapshot` polling backstop every 30 s while the tab is visible —
    state self-heals within 30 s even if SSE delivery silently breaks.
  - Force-reconnect on `visibilitychange → visible` when the last message
    is older than 15 s (browsers throttle background-tab timers/SSE).
  - 90 s SSE staleness watchdog (silent stalls don't always fire `onerror`).
  - Live "last update Ns ago" tooltip on the SSE dot for at-a-glance
    freshness.

### Changed
- `/api/snapshot` reuses `SnapshotBroadcaster.cached_payload(period)` when
  an SSE group is live for the requested period, avoiding a redundant full
  rebuild. Cuts the snapshot fetch from ~175 ms to ~100 ms (mostly JSON
  serialization), making the new poll backstop essentially free.

## [0.3.0] — 2026-04-27

### Changed
- **`tokenol serve` resource use slashed.** On a real session-history workload,
  steady-state RSS drops ~8× (from ~4 GiB to ~500 MiB) and idle CPU falls to
  near zero between heartbeats. Multi-tab dashboards now share a single
  background producer, so adding tabs does not multiply server CPU.
- The SSE stream (`/api/stream`) is driven by `SnapshotBroadcaster`: one task
  per `period` fans payloads out to N subscribers, each maintaining its own
  shallow-diff state. The wire format is unchanged.
- The producer now gates rebuilds on JSONL file `(path, size, mtime_ns)`
  changes, with a configurable heartbeat (default 60 s) so time-windowed
  panels (`recent_activity`, day boundaries) stay reasonably fresh. Trade-off:
  panels may lag wall-clock by up to the heartbeat between file writes.
- `ParseCache` now memoizes derived `(turns, sessions, fired)` keyed on the
  active file-key set; idle ticks skip the per-tick `_build_turns_and_sessions`
  rebuild entirely.
- `_build_turns_and_sessions` now returns the per-build assumption-fired
  `Counter` instead of mutating the global `assumption_recorder`.
- `create_app` migrated from the deprecated `@app.on_event("shutdown")` to a
  lifespan context manager.

### Removed
- **`RawEvent.raw` field.** Was populated by the parser with the full JSON
  dict (message bodies, tool I/O) for "extensibility" but read by no
  downstream code. Removing it is the dominant memory win. Code that wants
  raw JSON should re-read from disk (as `serve/session_detail.py` already
  does).

## [0.2.0] — 2026-04-25

### Added
- **Breakdowns tab** (`/breakdown`): new top-level page with three sections —
  Time (daily billable-tokens stacked bars + daily cache-reuse bars),
  Breakdowns (tokens-by-project grouped bars with cache-health dots,
  model-mix doughnut), and Tools (tool-mix horizontal bars). SSE-driven,
  in-place refresh, period-pill state persisted in sessionStorage.
- **Tool drill-down** (`/tool/<name>`): per-tool usage and error stats with
  click-through from the Tool Mix chart on the Breakdowns tab.
- **Breakdown API**: `/api/breakdown/summary`, `/api/breakdown/daily-tokens`,
  `/api/breakdown/by-project`, `/api/breakdown/by-model`,
  `/api/breakdown/tools`, `/api/tool/<name>`.
- **Cache-health thresholds** are now plumbed through `/api/prefs` and
  configurable from the Settings modal.
- **Per-turn tool names** captured in the parser and surfaced through
  `SessionRollup.tool_mix` (top-N aggregator with "others" bucket).
- **Top-nav tabs** (Overview / Breakdowns) on the dashboard topbar.

### Changed
- Chart.js defaults now derive from tokenol CSS design tokens for visual
  consistency across charts.
- `/api/breakdown/by-project` consolidates nested cwds using the same
  shortest-proper-ancestor rule as the projects rollup.

### Fixed
- Sessions are now keyed by JSONL `session_id` rather than file stem, so
  multi-session files (and renamed files) attribute turns correctly.

## [0.1.1] — 2026-04-23

### Fixed
- README screenshots now render on PyPI: switched from relative paths to
  absolute `raw.githubusercontent.com` URLs.

## [0.1.0] — 2026-04-23

Initial public release.

### Added
- **CLI** (`tokenol`): `daily`, `hourly`, `live`, `sessions`, `projects`,
  `models`, `verify`, `serve`.
- **Ingestion**: discovery across `~/.claude*` dirs (honours `CLAUDE_CONFIG_DIR`),
  JSONL parsing, compound-key deduplication (`message.id:requestId`),
  Windows cwd normalization.
- **Metrics**: cost rollups with full 4-component billing (input, output,
  cache_read, cache_creation); 5-hour rolling-window cost; context growth,
  cache hit rate, cache reuse, cost-per-kW output; session verdicts
  (`OK`, `CONTEXT_CREEP`, `RUNAWAY_WINDOW`, `TOOL_ERROR_STORM`,
  `SIDECHAIN_HEAVY`).
- **Pattern detection** on session drill-down: `idle_expiry`,
  `compaction_reinflation`, `context_ceiling_plateau`, `sidechain_explosion`,
  `tool_error_storm` with severity escalation.
- **Live dashboard** (`tokenol serve`): SSE-streamed main view with headline
  tiles (+ last-hour trajectory), hourly / daily charts (linear ↔ log
  toggle), model and project rollups, recent-activity table.
- **Drill-down pages**: `/session/<id>` (patterns, cost-per-turn small
  multiples, per-turn modal), `/project/<cwd>` (cache trend with auto
  hourly/daily bucketing, verdict distribution with tooltips),
  `/day/<date>`, `/model/<name>`.
- **Preferences**: user-editable thresholds and ranges persisted via
  `XDG_CONFIG_HOME`.
- **Project grouping**: shortest-proper-ancestor cwd rule generalizes across
  nested repos without per-user configuration.

### Tested
- 184 unit + integration tests on Python 3.10 / 3.11 / 3.12.

[0.2.0]: https://github.com/farhanferoz/tokenol/releases/tag/v0.2.0
[0.1.1]: https://github.com/farhanferoz/tokenol/releases/tag/v0.1.1
[0.1.0]: https://github.com/farhanferoz/tokenol/releases/tag/v0.1.0
