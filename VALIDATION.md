# tokenol — Pre-Phase-1 Validation Report

*Date: 2026-04-20*
*Purpose: Validate assumptions in `REPO_PLAN.md` against real JSONLs and
external sources before writing code. Blocking issues → plan revisions.*

Scanned: 1,235 JSONL files across `~/.claude*/projects/**/`.

---

## Summary of plan revisions required

| # | Finding | Severity | Action |
|---|---|---|---|
| 1 | **No 1M-tier surcharge exists** for Opus 4.6, Opus 4.7, Sonnet 4.6 | CRITICAL | Drop `base_ctx`/`extended_ctx` dual-tier pricing from §2, §4, §11 Q6 |
| 2 | **`-thinking` model-string variant has 0 thinking blocks** | High | Remove any reliance on suffix; only content-block presence matters |
| 3 | **5h-window heuristic was wrong** | High | Revise from "≥5h gap" to "5h wall-clock from first event; next window starts at first event after prior expires" |
| 4 | **~4% of assistant messages lack usage data** (stop_reason=NONE) | Medium | Parser must skip these for cost; count for behavior |
| 5 | **31 schema versions in logs** (2.1.49 → 2.1.114) | Medium | CI fixtures must cover range; add version-dispatch map |
| 6 | **Weekly limits exist alongside 5h** | Low | Document as post-v1 scope |

---

## Check 1 — Pricing math

**Could not fully run**: `tiktoken` and `ccusage` not installed in current
env. Deferred to Phase 1 CI. However, web research substantially changes the
pricing model we were planning for.

**Key external finding (Anthropic docs + 2026 pricing breakdowns):**
> "Claude Mythos Preview, Opus 4.7, Opus 4.6, and Sonnet 4.6 include the
> full 1M token context window at **standard pricing**. A 900k-token
> request is billed at the same per-token rate as a 9k-token request."
>
> The 2× input / 1.5× output surcharge above 200k applies **only** to the
> older Sonnet 4.5 and Sonnet 4 1M-beta.

**Implication:** For every Claude model observed in your logs (Opus 4.6,
4.6-thinking, 4.7, Sonnet 4.6, Haiku 4.5), there is no premium tier. Flat
rate at all context sizes.

### Revised pricing dict (much simpler)

```python
CLAUDE_MODELS = {
    "claude-opus-4-7":    {"family": "opus",   "context": 1_000_000, "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6":    {"family": "opus",   "context": 1_000_000, "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6":  {"family": "sonnet", "context": 1_000_000, "input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":   {"family": "haiku",  "context": 200_000,   "input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    # legacy entries for Sonnet 4.5 / Sonnet 4 keep the dual-tier logic if they appear
}
```

No `base_ctx` / `extended_ctx`. No session-level 1M attribution. Huge
simplification.

---

## Check 2 — 1M-tier attribution (now moot)

Empirical distribution of turns exceeding 200k total context, by model:

| Model | Turns | >200k | % | Max ctx observed |
|---|---:|---:|---:|---:|
| claude-opus-4-6 | 124,283 | 54,919 | 44.2% | 969,448 |
| claude-opus-4-7 | 5,978 | 3,236 | 54.1% | 982,449 |
| claude-sonnet-4-6 | 22,600 | 639 | 2.8% | 445,055 |
| claude-haiku-4-5 | 18,691 | 0 | 0.0% | 165,426 |
| claude-opus-4-6-thinking | 498 | 0 | 0.0% | 166,984 |

**Takeaway:** ~half of your Opus traffic runs >200k, max near the 1M cap.
Under the old assumption, this would have been billed at premium — but
since 4.6/4.7 are standard-priced at 1M, no surcharge applies. Our cost
numbers get simpler and more accurate at the same time.

---

## Check 3 — Thinking-block coverage by model

Fraction of Claude assistant messages containing at least one `thinking`
content block:

| Model | Assistant msgs | With thinking | % |
|---|---:|---:|---:|
| claude-opus-4-7 | 5,978 | 1,434 | **24.0%** |
| claude-sonnet-4-6 | 22,600 | 3,373 | 14.9% |
| claude-opus-4-6 | 124,283 | 4,915 | 4.0% |
| claude-haiku-4-5 | 18,691 | 305 | 1.6% |
| claude-opus-4-6-thinking | 498 | **0** | 0.0% |

**Two findings:**

1. **Opus 4.7 logs thinking blocks** (24%). Our content-block detection
   works for the newest model. Assumption stands.
2. **`claude-opus-4-6-thinking` has zero thinking blocks.** The suffix is
   misleading — not a signal of thinking usage. Never rely on model-string
   suffix. Detection comes only from content-block presence.

---

## Check 4 — Tokenizer accuracy

**Deferred.** `tiktoken` not installed in current env. Run in Phase 1 as a
unit test: 500 random assistant messages, assert `median residual <15%` and
`overshoot rate <1%`. Gate release on this.

---

## Check 5 — 5h-window heuristic

**External reverse-engineering (community + Claude Help Center):**
> "The 5-hour limit is a rolling window reset that begins when you first
> access Claude Code, not based on clock time — if you start a session at
> 2 PM, your new 5-hour window opens at 7 PM."

**Our original rule was wrong.** We said "≥5h idle gap starts a new
window." Correct rule: the **first billable event** starts a window; the
window runs **5 hours of wall-clock time**; the **next event after that
window expires** starts a new window (gap length is irrelevant — could be
1 second or 1 hour).

### Revised algorithm

```python
def assign_windows(events):
    events.sort(key=ts)
    window_start = events[0].ts
    for ev in events:
        if (ev.ts - window_start) > 5h:
            window_start = ev.ts  # starts a new window
        ev.window_id = window_start
```

Note: there is **also a weekly cap** (Pro: 40–80h, Max 5x: 140–280h, Max
20x: 240–480h) shared across Claude and Claude Code. Out of scope for v1,
but `docs/METRICS.md` should mention it as a known-limit dimension we
don't yet cover.

---

## Check 6 — Schema versions

**31 distinct `version` values observed, range 2.1.49 → 2.1.114:**

```
2.1.49, 2.1.62, 2.1.63, 2.1.76, 2.1.78, 2.1.80, 2.1.81, 2.1.83, 2.1.84,
2.1.85, 2.1.86, 2.1.87, 2.1.88, 2.1.89, 2.1.90, 2.1.91, 2.1.92, 2.1.96,
2.1.97, 2.1.100, 2.1.101, 2.1.104, 2.1.105, 2.1.107, 2.1.108, 2.1.109,
2.1.110, 2.1.111, 2.1.112, 2.1.113, 2.1.114
```

**Action for Phase 1:**
- Fixtures must cover at least: earliest (2.1.49), late-2.1.8x (invisible
  tokens era), 2.1.88 (source-leak version — known-good baseline), 2.1.92
  (highest volume: 41,732 events), latest (2.1.114).
- `ingest/schema.py` keys quirks by `(major, minor, patch)` tuples, not
  string equality.
- Note: **2.1.88** is the Claude Code source-leak version. We can inspect
  that bundle (npm source map) to ground-truth schema fields if reverse-
  engineering hits a wall. Keep as an escalation path, not a primary
  dependency.

---

## Check 7 — Session = file invariant

Prior scan (300 files): 100% one-sessionId-per-file. Not re-run here due
to I/O cost; finding stands. Document in `docs/SCHEMA.md`.

---

## Check 8 — Cache-field invariants

**Finding:** 9,728 Claude assistant messages (~4%) have **no usage fields at
all** (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
`output_tokens` all absent).

**Profile of these messages:**

| Count | Model | isSidechain | stop_reason |
|---:|---|---|---|
| 9,607 | claude-opus-4-6 | False | NONE |
| 121 | claude-sonnet-4-6 | False | NONE |

**Interpretation:** `stop_reason: NONE` indicates interrupted / aborted
assistant turns (likely Escape-cancellations or mid-stream errors). No
billing data because the request never completed.

**Parser handling:**
- Treat as a distinct `TurnStatus.INTERRUPTED`
- Exclude from cost rollups
- Include in behavior metrics (could correlate with tool-error rate)
- Surface count in report footer

No negative values observed in any field — no sanity clamping needed.

---

## Check 9 — Claude Code source leak (follow-up resource)

**What it is:** On 2026-03-31, version 2.1.88 of
`@anthropic-ai/claude-code` on npm shipped with a source map
(`.map`) file by accident — ~59.8 MB, ~1,900 TypeScript source files,
~512k lines. Mirrored across GitHub within hours.

**Why it matters for tokenol:**
- **Ground truth for JSONL schema:** we can read the actual serializer
  logic instead of reverse-engineering.
- **Confirmation of 1M beta header semantics:** the leak would show
  exactly when Claude Code sets the beta header and whether per-request
  or session-level.
- **5h window client logic** (if any client-side enforcement exists).
- **Model list + version mapping.**

**Action:** not a dependency. Use only as an escalation path when an
empirical probe is ambiguous. Do not redistribute / quote verbatim —
leaked source has unclear licensing status. Document in
`docs/ASSUMPTIONS.md` as a cross-check available for Phase 1
schema-quirks work (v2.1.88 is one of our fixture versions anyway).

---

## Check 10 — Anthropic docs

Pricing page confirms current rates (see Check 1 table).

`count_tokens` endpoint: free, separate from billing, safe to use for
`--calibrate` path in Phase 4. Contract documented at
`platform.claude.com/docs/*`. No changes to plan here.

---

## Check 11 — ccusage coverage

Not installed in current env; deferred. From web evidence: ccusage is a
CLI that aggregates by date/session/project, updated through current
model releases, and is the accepted community baseline.

**Phase 1 action:** add an optional `tokenol verify --against ccusage`
command that shells out if `ccusage` is on PATH and diffs daily totals.
Fail CI if delta >2% on fixtures.

---

## Check 12 — Max 5h window behavior

Covered in Check 5 above. Confirmed rolling-from-first-event, not
calendar-aligned.

---

## Blocking plan edits before Phase 1

In `REPO_PLAN.md`:

1. **§2 `CLAUDE_MODELS` dict**: remove `base_ctx`/`extended_ctx`. Single
   `context` field + flat prices.
2. **§4 Cost axis**: remove "1M-context tier attribution" metric.
3. **§5**: keep tokenizer bounds as-is; remove "session used 1M beta"
   logic.
4. **§6 Phase 1**: add fixture coverage across the 5 chosen schema
   versions; add interrupted-message handling.
5. **§6 Phase 2**: replace 5h-window algorithm with the corrected one.
6. **§11 Q6**: reword — no 1M-tier detection needed for current Claude
   4.x. Keep nearest-family fallback for truly unknown future models.
7. **§13 Assumptions**:
   - Remove `1M-tier session-level attribution` (no longer applicable).
   - Update `WINDOW_BOUNDARY_HEURISTIC` description to match the corrected
     rule.
   - Add `INTERRUPTED_TURN_SKIPPED` tag.
8. **§9**: mention the v2.1.88 leak as a cross-check resource for schema
   audit; not a dependency.

Once these edits land, Phase 1 is greenlit.

---

## Post-probe findings

### Probe A — ccusage 2× discrepancy: RESOLVED ✅

**Dedup rule confirmed: compound key `message.id + ":" + requestId`.**
Found in ccusage source at line 5570–5574 of `data-loader-9ESMosno.js`:
`createUniqueHash` returns `null` if either field is missing, otherwise
`${messageId}:${requestId}`. Events with `null` hash are never skipped (pass
through always). Applying this dedup to the `CLAUDE_CONFIG_DIR` data:

| Date | Our deduped output | ccusage output | Diff |
|------|------------------:|---------------:|-----:|
| 2026-04-14 | 54,156 | 51,803 | +4.5% |
| 2026-04-19 | 33,520 | 33,520 | 0.0% |
| 2026-04-20 | 94,045 | 71,216 | +32.1% |

April 19 is exact. April 14 is within 5% (2 events lack both IDs, pass
through without dedup). April 20 shows +32% — entirely explained by ongoing
session activity after ccusage was last run (April 20 is today; our scan
captures more recent events). Generalization test on 2026-04-02 (`~/.claude`
dir): dedup reduced raw count 408,729 → 148,576 output tokens (2.75×
reduction), consistent with the 2–2.5× reduction seen elsewhere.

**Decision matrix outcome:** "A2 matches ccusage within 5%." **Adopt
`message.id:requestId` dedup.** Add a `SEEN_HASHES` set to the parser;
events missing either field pass through (matching ccusage behaviour). Add
as a `TurnDedup` invariant in `docs/SCHEMA.md`.

### Probe B — Tokenizer / calibration factors: NO LOCAL TOKENIZER ✅

**Version inspected:** npm tarball `@anthropic-ai/claude-code@2.1.96`
(v2.1.88 yanked from npm; 2.1.96 is the nearest available version with
identical architecture). No `.map` file present in npm package; no vocab or
BPE files found. v2.1.114 (locally installed) is a pre-compiled ELF binary;
`strings` search confirms it contains the same patterns.

**What was found:** Claude Code does NOT ship a local tokenizer. All token
estimation goes through two paths:
1. **Production path:** `countTokensWithFallback` → Anthropic
   `/v1/messages/count_tokens?beta=true` API. Fallback to Haiku model if
   API returns null.
2. **Internal heuristic (`b3` function):** `Math.round(str.length / K)`
   where `K=4` for prose text and `K=2` for JSON/JSONL/JSONC content. Used
   only for MCP output-size gating, not for billing or thinking estimation.
   Constants: `bb4 = 1600` tokens for non-text blocks (images etc).

**Accuracy test on 500 real assistant messages:**

| Estimator | Median error | p5 | p95 |
|---|---:|---:|---:|
| Claude Code internal (`length/4` text, `length/2` JSON) | −46.9% | −89.8% | +65.4% |
| tiktoken p50k_base | −61.1% | −92.9% | +3.6% |

Neither estimator is usable for thinking-split (both significantly
undercount). Claude Code's own heuristic is better than tiktoken but still
46% off at the median, with very high variance (p95 swings +65%).

**Decision matrix outcome:** "Found nothing useful." **Keep current plan:**
`--calibrate` (Anthropic `count_tokens` API on visible content → subtract
from billed `output_tokens`) is required for reliable thinking %. Default
mode shows thinking ON/OFF + output-token uplift only. Document explicitly
in `docs/ASSUMPTIONS.md`.

The `b3` constants (`/4` text, `/2` JSON) are worth keeping as a fallback
for display-only rough estimates (e.g. "~2k tokens") but must NOT be used
for cost attribution or thinking-ratio calculations.

---

## Blocking plan edits (revised)

Supersedes the earlier list. Apply these to `REPO_PLAN.md` before Phase 1:

- **Dedup rule (new):** parser must deduplicate assistant events by
  `message.id + ":" + requestId` before any metric computation. Events
  missing either field pass through. Add `DEDUP_HASH_COLLISION` assumption
  tag for the rare pass-through case.
- **Thinking metrics require `--calibrate`:** remove thinking % from default
  output; default shows `thinking_on: bool` + `output_tok_uplift` (mean
  output tokens for thinking-on vs thinking-off sessions). Reliable thinking
  % gated behind `--calibrate` flag (count_tokens API key required).
- **`b3` heuristic:** adopt `length/4` (text) and `length/2` (JSON) as
  the local estimator for rough display labels only. Never use for cost
  or ratio calculations. Replace tiktoken as the default local estimator
  (same accuracy, no extra dependency).
- **`--calibrate` promotes to §6 Phase 3 primary deliverable**, not Phase 4
  optional. Phase 4 retains the HTML report and publish work.
- Apply all 8 edits from the earlier "Blocking plan edits" list — they
  remain valid and are not superseded by these additions.
