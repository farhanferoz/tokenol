# tokenol — Claude Code Usage & Efficiency Audit Tool

A publishable open-source toolkit for Claude Code power users to audit their
JSONL session logs: track cost and cache health, detect context blow-ups,
and quantify per-model / per-session / per-project efficiency. Ships with a
local web dashboard for live monitoring.

*Status: Planning. Not yet implemented.*
*Name: `tokenol` — play on Tylenol (painkiller for your token bill).
Availability on PyPI + GitHub to be confirmed before repo init.*

---

## 1. Positioning

**What it is:** A local, zero-network CLI + Python library + local web
dashboard that parses the `~/.claude*/projects/**/*.jsonl` logs Claude Code
writes, normalizes them into an event model, and produces multi-axis
efficiency reports and live views.

**What it is not:** It is *not* a replacement for `ccusage`. `ccusage` is a
canonical token/cost aggregator written in TypeScript; we cannot import it
from Python, so `tokenol` reimplements the aggregation pieces it needs and
adds finer granularity (per-session behavior metrics, 5h-window pressure,
sidechain attribution, context blow-up detection, live dashboard) that
`ccusage` does not expose. Optional cross-check: if `ccusage` is on PATH,
`tokenol` shells out and reconciles totals in a diagnostic row. Not a hard
dependency.

**Target users:** Claude Code users on Max/Pro plans who want to understand
*why* their sessions got expensive, *when* context blew up, and *which*
projects / models / sessions are eating their quota.

**Non-goals (v1):**
- No remote API dependency. Everything is local.
- No telemetry / opt-in analytics.
- **Thinking-split metrics deferred.** The Anthropic billing record in
  JSONL gives a single `output_tokens` number per turn; the thinking vs
  visible split is not stored. Validation showed 98% of logged thinking
  blocks have empty text, and no local tokenizer (tiktoken p50k_base,
  tiktoken cl100k_base, Claude Code's own `length/4` heuristic) comes
  within ±40% of the billed count. Reliable thinking % would require the
  Anthropic `count_tokens` API (free but requires a separate API key and
  breaks the local-only guarantee). Deferred to post-v1; see §13 for the
  planned approach if we revisit.
- **Claude models only in v1.** Observed Claude Code JSONLs also contain
  non-Claude model rows (e.g. `gemini-3-flash`, `gemini-3.1-pro-high`).
  These are parsed and counted but shown as **unpriced** in v1.
  Multi-provider pricing in v0.2.
- No causal claims about "which configuration delivers better work" —
  observational data is too confounded.

---

## 2. High-level architecture

```
tokenol/
├── src/tokenol/
│   ├── __init__.py
│   ├── cli.py                  # typer entry point
│   ├── enums.py                # Model, BlockType, SchemaVersion, BlowUpVerdict, AssumptionTag
│   ├── ingest/
│   │   ├── discovery.py        # honor CLAUDE_CONFIG_DIR; default scan all ~/.claude* dirs
│   │   ├── parser.py           # JSONL → Event (typed dataclass); dedup by (message.id, requestId)
│   │   └── schema.py           # pass-through; version surfaced as metadata, not dispatch
│   ├── model/
│   │   ├── events.py           # Event, Turn, Session, Project dataclasses
│   │   ├── pricing.py          # CLAUDE_MODELS dict: flat pricing + context window
│   │   └── registry.py         # ModelRegistry: resolve model string → entry + fallback
│   ├── metrics/
│   │   ├── base.py             # Metric[T] protocol; Rollup base class
│   │   ├── context.py          # max_turn_input, growth, cache reuse ratio
│   │   ├── cost.py             # cost decomposition from billed fields
│   │   ├── behavior.py         # tool-error rate, sidechain share, interrupted turns
│   │   └── windows.py          # 5-hour Max-window alignment (wall-clock from first event)
│   ├── assumptions.py          # AssumptionTag enum + recorder; per-row + footer + stderr
│   ├── report/
│   │   ├── text.py             # ANSI/plain text tables (default)
│   │   ├── json_out.py         # machine-readable JSON
│   │   └── html.py             # single-file static HTML
│   └── web/
│       ├── server.py           # FastAPI app + /api routes (127.0.0.1 only)
│       ├── ingester.py         # watchdog-based JSONL tailer → SQLite/DuckDB
│       ├── store.py            # DuckDB historical + in-memory ring buffer
│       └── static/             # HTMX + Chart.js (no build step)
├── tests/
│   ├── fixtures/               # synthetic JSONL samples
│   ├── test_parser.py
│   ├── test_metrics.py
│   └── test_dedup.py           # regression for message.id:requestId dedup rule
├── docs/
│   ├── METRICS.md              # every metric defined + formula
│   ├── SCHEMA.md               # observed JSONL schema versions + invariants
│   ├── PRIVACY.md              # what we read, what we don't
│   ├── ASSUMPTIONS.md          # catalog of heuristics + error modes
│   └── COMPARISON.md           # tokenol vs ccusage
├── examples/
│   └── redacted_sample.jsonl   # small anonymized log for demo
├── _local/                     # gitignored — user's private scripts / notes
├── README.md
├── CHANGELOG.md
├── LICENSE                     # MIT
├── pyproject.toml
├── .github/workflows/ci.yml    # lint + tests on 3.10/3.11/3.12
└── REPO_PLAN.md                # this file, removed at v0.1.0 release
```

**OO principles enforced throughout:**
- Enums (`Model`, `BlockType`, `SchemaVersion`, `BlowUpVerdict`,
  `AssumptionTag`) — no scattered string equality.
- `ModelRegistry` is the single source of truth for pricing + context
  windows + family. No module reimplements model-string dispatch.
- Shared `Rollup` base class for projects / models / (model, project)
  views.
- Metric classes implement a `Metric[T]` protocol with `compute(events) -> T`
  and `format(value)`.

---

## 3. Data model

**Event**: one line of a JSONL file, parsed.
**Turn**: one (user, assistant) exchange. Deduplicated by
`message.id + ":" + requestId` (matches ccusage behavior exactly —
validated against three reference dates with 0–4.5% residual diff).
**Session**: one `sessionId`, one JSONL file (verified 1:1 across 300 files).
Sidechains/subagents live in `subagents/` subdirs with their own
sessionIds, attributed as children of the main session.
**Project**: all sessions under one `cwd` or one `~/.claude*` config dir.
**Window (5h)**: Max-plan rate-limit window. Boundary = first billable
event starts a window; window runs 5h wall-clock; next event after expiry
starts a new window. (Not gap-based.)

**Parser invariants (validated):**
- `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
  `output_tokens` always co-present when any is — or all absent (interrupted
  turns, `stop_reason: NONE`). Interrupted turns excluded from cost,
  counted for behavior.
- `isSidechain=True` iff file lives under `subagents/` subdir.
- Events with `null` `message.id` or `null` `requestId` pass through dedup
  (matches ccusage).
- Schema version is pass-through metadata. Parser is permissive; no
  version-based dispatch unless a future break demands it.

---

## 4. Metrics catalog (v1)

Full definitions live in `docs/METRICS.md`. Summary:

### Context axis
- **Max turn input** — largest single-turn `input + cache_read + cache_creation` per session
- **Context growth rate** — slope of per-turn input tokens over turn index
- **Cache reuse ratio** — `reads / (reads + creates)`. Flags sessions <50%
  as cache-inefficient.
- **Non-cached input ratio** — how much input is *not* cached

### Cost axis
- **Per-model token split** within each row (all four usage fields)
- **Cache-adjusted effective $/turn** — what you'd have paid uncached
- Flat pricing per model; context window size from `ModelRegistry`. No
  separate 1M-tier surcharge logic (current Claude 4.x models price
  flat at all context sizes per Anthropic docs).

### Behavior / health
- **Tool-use error rate** per session
- **Sidechain share** — `subagent_tokens / main_session_tokens`
- **Interrupted-turn rate** — fraction of assistant events with
  `stop_reason: NONE` (billing data absent)
- **Turn density** — tokens per turn distribution (p50 / p95 / max)
- **5-hour window pressure** — peak tokens consumed within any active 5h
  window

### Blow-up verdicts
Per-session one-line verdict:
`OK`, `CONTEXT_CREEP`, `SIDECHAIN_HEAVY`, `TOOL_ERROR_STORM`, `RUNAWAY_WINDOW`.

### Report views (first-class CLI commands)

- **`tokenol live --last <duration>`** — recent-window burn-rate view.
  Required flag: `--last 5m | 45m | 2h | 30s`. Shows turns,
  input/output/cache tokens, cost, active sessions, extrapolated 5h-window
  consumption at current rate. Flags if projected consumption exceeds 100%
  before reset.
- **`tokenol daily [--since 14d]`** — daily aggregates.
- **`tokenol hourly [DATE]`** — hourly breakdown.
- **`tokenol sessions [--top N --sort max_input]`** — per-session detail.
- **`tokenol projects [--since 14d]`** — per-project rollup with
  context-token share + cost share columns.
- **`tokenol models [--since 14d]`** — per-model rollup: turns, tokens by
  type, cost, tool-error rate.
- **`tokenol serve [--port 8765]`** — launch the local dashboard (§6 Phase 4.5).
- **`tokenol verify [--against ccusage]`** — diagnostic: diffs our totals
  against ccusage's if installed. Fails CI if delta >2% on fixtures.
- **`tokenol pivot --rows project --cols model --metric cost`** — generic
  2-D pivot (post-v1 if complexity warrants).

---

## 5. Token accounting

All token counts come directly from `message.usage` in the JSONL (billed
values from the Anthropic API). No local tokenization needed for any v1
metric:

- `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens` are read verbatim
- Cost = per-model rate × each count
- No attempt to split `output_tokens` into thinking vs visible (see
  §1 non-goals + §13 future work)

---

## 6. Implementation phases

### Phase 1 — Foundation (week 1)
- Repo scaffold: `pyproject.toml`, `ruff` + `pytest`, CI matrix (3.10–3.12)
- `enums.py` + `ModelRegistry` with `CLAUDE_MODELS` dict populated through
  Opus 4.7, flat pricing per family, context window per model
- `ingest.parser`: permissive JSONL → Event parser
- `ingest.discovery`: honor `CLAUDE_CONFIG_DIR`, default scan all
  `~/.claude*` dirs
- **Dedup rule**: `message.id + ":" + requestId`; pass-through on missing
- `model.events`: Event/Turn/Session/Project dataclasses
- `metrics.cost`: read from billed fields; correct Haiku 4.5 + Opus 4.7
  pricing
- `assumptions.py`: recorder infrastructure + CLI flags (`--strict`,
  `--show-assumptions`, `--log-level`)
- `report.text`: daily + hourly tables
- `tokenol verify`: optional ccusage cross-check; fails on >2% delta
- Synthetic fixtures covering: thinking block, tool_use, text, sidechain,
  rate_limit marker, duplicate-requestId dedup, interrupted turn
- `_local/` cleanup: move existing `cache_hit_analysis.py`,
  `claude_cost_efficiency.py`, `comprehensive_report.py`,
  `hourly_cache_analysis.py`, `hourly_comprehensive_report.py`,
  `efficiency_report*.txt`, and legacy `.md` notes into `_local/`
  (gitignored). Triage pass on whether any notes graduate to `docs/`.

**Exit criterion**: `tokenol daily --since 14d` produces output matching
ccusage within 2% for a given `CLAUDE_CONFIG_DIR`, across 5+ reference
dates. Assumptions footer shows which heuristics fired.

### Phase 2 — Context detection + multi-lens views (week 2)
- `metrics.context`: all context metrics
- `metrics.windows`: 5h window alignment (wall-clock from first event)
- `tokenol live --last Nm` — primary "am I burning too fast?" tool
- `tokenol sessions --top N --sort max_input`
- `tokenol projects`
- `tokenol models`
- Blow-up verdict badges per session
- Sidechain attribution

**Exit criterion**: can answer (a) *"in the last 20 minutes, am I burning
too fast?"*, (b) *"which 5 sessions caused my $512 day?"*, (c) *"which
project + model dominated my last 14 days?"* — each in one command.

### Phase 3 — Polish & publish (week 3)
- HTML single-file static report (`report.html`)
- `pipx install tokenol` works end-to-end
- README with screenshots, examples, FAQ
- CHANGELOG + v0.1.0 release tag
- PRIVACY.md, COMPARISON.md, SCHEMA.md, ASSUMPTIONS.md
- Publish to PyPI + GitHub release

**Exit criterion**: a new user can `pipx install tokenol && tokenol daily`
and get a useful report in <30 seconds on a typical `~/.claude/` dir.

### Phase 4 — Local dashboard (week 4)
- `tokenol serve --port 8765` launches local FastAPI server
- **Live panel**: burn-rate sparkline, 5h-window gauge, active sessions,
  configurable window slider (1m / 5m / 15m / 60m)
- **Today + rolling tiles**: today / 24h / 7d / 30d with period-over-period
  deltas
- **Session drill-down**: table → per-turn timeline
- **Breakdowns**: project / model / (project, model)
- **Context health panel**: turn-input histogram, cache reuse trend
- Backend: watchdog tailer → DuckDB (historical) + in-memory ring buffer
  (last 5h). Cold start: <30s one-time index. Steady-state refresh: <5ms
  per new event.
- Frontend: HTMX + Chart.js (no build step), dark-mode-first. Frontend
  design skill invoked here (not before) for tile hierarchy, color
  semantics, sparkline density.

**Exit criterion**: `tokenol serve` runs, dashboard loads in a browser,
live panel updates as new events append to any active JSONL.

### Phase 5 — Optional / post-v1
- Watch mode for CLI (`tokenol watch` tails active JSONLs to stdout)
- Config-file defaults (`~/.config/tokenol/config.toml`)
- Prometheus exporter
- Comparison view across two time ranges (`--compare LAST_WEEK`)
- Multi-provider pricing (Gemini, etc.)
- **Thinking-split metrics** (§13)

---

## 7. Testing strategy

- **Unit**: every metric from small synthetic fixtures with hand-computed
  expected values.
- **Dedup regression**: fixture with known-duplicate `(message.id, requestId)`
  pairs; asserts tokenol count equals the unique count.
- **Golden files**: known-good text reports for 3–5 representative fixtures.
- **Property tests** (hypothesis): parser never crashes on fuzzed JSONL.
- **ccusage parity**: `tokenol verify` run against fixtures, CI fails on
  >2% delta.
- **No live API** in any test.
- **Privacy-safe fixtures**: synthetic; no real user data.
- **Assumption regression**: every fixture asserts which `AssumptionTag`
  set fires. Protects against silent heuristic drift.

---

## 8. Privacy & safety

- Tool reads only `~/.claude*/projects/**/*.jsonl` and equivalents.
- **Never** writes or uploads anything by default.
- JSONL contents include user prompts, file contents, tool results — highly
  sensitive. Reports aggregate token counts only; no message text is
  emitted unless `--debug-sample` is set (documented).
- Dashboard binds to `127.0.0.1` only; no remote listener.

---

## 9. Compatibility matrix

Claude Code JSONL schema drifts across versions (observed v2.1.49 →
v2.1.114, 31 distinct versions across the user's logs). Parser is
permissive — `version` is pass-through metadata, surfaced in reports so
users can see which log versions contributed. Schema-version dispatch only
introduced if a future change breaks permissiveness.

Known behaviors worth noting in `docs/SCHEMA.md`:
- Pre-2.1: flat `usage` on event (not observed in current user's logs; keep
  permissive handling if encountered)
- 2.1+: nested `message.usage`
- Sidechains under `subagents/` subdir; 1:1 with `isSidechain=True`
- "Invisible tokens" v2.1.100 regression: surface as a diagnostic if
  detected (per-turn hidden-input-delta vs prior version baseline)
- `-thinking` model-string suffix (e.g. `claude-opus-4-6-thinking`) is
  inconsistent across versions; never used as a signal

**Reference resource:** The Claude Code v2.1.88 npm source leak
(2026-03-31) is available online as a schema ground-truth cross-check if
empirical probing hits a wall. Not a dependency — do not vendor or
redistribute.

---

## 10. Publishing checklist

- [ ] Confirm `tokenol` available on PyPI + GitHub
- [ ] MIT LICENSE
- [ ] README: what/why, install, 60-second example, ccusage comparison,
      privacy note, contributing, license
- [ ] PyPI metadata: classifiers, keywords, project URLs
- [ ] GitHub repo: issue templates, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`
- [ ] v0.1.0 tag + GitHub release with changelog
- [ ] Short announcement post / gist with example output

---

## 11. Open questions — resolved

1. **Name.** `tokenol` (Tylenol play). Confirm PyPI+GitHub availability
   before repo init.
2. **Python floor.** 3.10. Gives `match` for future schema dispatch if we
   ever need it.
3. **CLAUDE.md.** Yes, ship one. Written in terse dev-notes voice so it
   reads as human-authored.
4. **Existing scripts.** Move to `./_local/` (gitignored). Not shipped, not
   visible in cloned repo.
5. **Effort / thinking detection.** No. Not in JSONL, no reliable local
   estimation. Deferred post-v1 (§13).
6. **1M-context tier.** No special handling needed. All current Claude 4.x
   models (Opus 4.6, 4.7, Sonnet 4.6) price flat at up to 1M context per
   Anthropic docs. `ModelRegistry` stores one context-window value per
   model + flat per-token rates. Unknown future models inherit from
   nearest-family sibling with a warning tag.
7. **ccusage comparison.** Ship `tokenol verify --against ccusage` as a
   diagnostic; cross-check in CI with 2% tolerance.

---

## 12. What we're NOT doing (and why)

- **Thinking-split metrics in v1.** Can't get reliable thinking-token
  counts without the Anthropic `count_tokens` API (free but requires a
  separate API key + crosses the local-only promise). 98% of logged
  thinking blocks have empty text in JSONL; local tokenizers are ±40% off.
  Deferred — see §13.
- **Quality evaluation of model output.** Too confounded without
  controlled experiments.
- **Live rate-limit prediction / alerting.** Out of scope; compose our
  JSON output with your own alerting.
- **Re-implementing ccusage fully.** We cross-check against it where
  useful.
- **Supporting non-Claude-Code logs** (Claude.ai, Workbench exports) in v1.
- **Multi-provider pricing in v1.** Gemini and other models appear in
  JSONLs but are shown unpriced.
- **Local tokenizer of any kind.** tiktoken p50k (−61% median error),
  cl100k (−64%), Claude Code's own `length/4` (−39%) all fail.

---

## 13. Future work — thinking-split metrics (post-v1)

Recorded for context when we revisit.

**Why it's hard:**
- Billing emits a single `output_tokens` per turn — no split in JSONL.
- Claude Code strips the `thinking` block text in 98% of logged messages
  (keeps type + signature only).
- No local tokenizer matches Claude's tokenizer closely enough. Best
  alternative tested: Claude Code's own `length/4` internal heuristic →
  still −39% off at median on visible-only content with ±58% p95 swings.
- Anthropic's `count_tokens` API would give exact counts but: (a) requires
  a separate API key (Max OAuth can't authenticate it), (b) crosses the
  local-only design promise, (c) needs per-hash caching to stay cheap at
  scale.

**Minimum viable path if we revisit:**
1. `--calibrate` flag + separate `ANTHROPIC_API_KEY`.
2. Send only visible content blocks (text + tool_use) to `/v1/messages/count_tokens`.
3. Subtract from billed `output_tokens` to get exact thinking tokens.
4. Cache `count_tokens` results per content-block hash in
   `~/.cache/tokenol/count_tokens.sqlite` to avoid re-billing.
5. Hide thinking metrics entirely behind `--calibrate`; default run never
   mentions them.

**If we don't revisit:** acceptable. The core value of tokenol (cost,
cache health, blow-up detection, 5h-window pressure, per-project /
per-model breakdowns) stands without thinking metrics.

---

## 14. Assumptions catalog

Every report surfaces which of these fired via: (a) per-row `assumptions`
column, (b) report footer summary, (c) stderr at `--log-level debug`.
`--strict` mode refuses fallbacks and errors out instead.

| Tag | Heuristic | Why needed | Error mode |
|---|---|---|---|
| `WINDOW_BOUNDARY_HEURISTIC` | 5h window starts at first billable event; runs 5h wall-clock; next event after expiry starts a new window | Anthropic's exact server-side rule isn't published; matches community reverse-engineering | Drifts if Anthropic uses fixed-UTC or overlapping-rolling windows |
| `UNKNOWN_MODEL_FALLBACK` | Unknown Claude model inherits pricing + context from nearest-known family sibling | No machine-readable pricing feed; new models appear before we update | Warned per model; slight mispricing until registry updated |
| `DEDUP_PASSTHROUGH` | Events with `null` `message.id` or `null` `requestId` pass through dedup (matches ccusage behavior) | Cannot form the compound hash key | Rare; logged per event |
| `INTERRUPTED_TURN_SKIPPED` | Assistant messages with no `usage` fields (stop_reason=NONE) excluded from cost | Request never completed; no billing data | None — correctly excludes |
| `GEMINI_UNPRICED` | Non-Claude models (gemini-*) parsed but not priced | Multi-provider pricing is post-v1 | Cost rows show `—` for these models |

CLI flags:
- `--strict` — refuse any assumption fallback; error out.
- `--show-assumptions` — verbose per-row listing of which tags fired.
- `--log-level debug` — stderr JSON lines, one per assumption-driven
  decision.

---

*Next action after approval:* confirm `tokenol` name availability, then
scaffold Phase 1 starting with `enums.py` + `ModelRegistry`.
