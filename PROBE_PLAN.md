# tokenol — Remaining Validation Probes

*Handoff doc for the agent executing the last two blocking probes before
`REPO_PLAN.md` is frozen for Phase 1.*

**Rules:**
1. Do **only** the two probes below. Do not expand scope.
2. Each probe has a **decision matrix** — pick one outcome and record it.
   Do not invent third options.
3. When a command returns unexpected output, re-run with more detail — do
   **not** guess the answer.
4. Report results by updating `VALIDATION.md` with a new §"Post-probe
   findings" section. Do not rewrite other sections.
5. Model: Sonnet 4.6 at medium effort.
6. Budget: 60 minutes. If a probe exceeds 30 min without a conclusion,
   record "INCONCLUSIVE" with specifics, stop, and hand back.

---

## Probe A — ccusage 2× discrepancy root cause

### Context (do not reverify — these numbers are established)

- `ccusage` output for `CLAUDE_CONFIG_DIR=/home/ff235/.claude-claude_rate_limit`:
  ```
  2026-04-14:  51,803 output tokens,  12,290,847 cache_read,  $7.891817
  2026-04-19:  33,520 output tokens,   1,297,832 cache_read,  $2.328146
  2026-04-20:  71,216 output tokens,   3,571,307 cache_read,  $5.474787
  ```
- Our raw scan (same dir, all assistant events):
  ```
  2026-04-14: 111,546 output (2.15×), 22,294,583 cache_read (1.81×)
  2026-04-19:  71,418 output (2.13×),  2,595,557 cache_read (2.00×)
  2026-04-20: 175,310 output (2.46×),  8,608,363 cache_read (2.41×)
  ```
- Excluding `isSidechain=True` did not close the gap.
- Files live under `/home/ff235/.claude-claude_rate_limit/projects/**/*.jsonl`.
- 586/589 assistant events in that dir have a `requestId` field.
- ccusage source: `/home/ff235/.npm-global/lib/node_modules/ccusage/dist/data-loader-9ESMosno.js`

### Primary hypothesis

ccusage deduplicates by `requestId` — when Claude Code logs the same
assistant response twice (e.g., streaming partial + final, or retry +
success), ccusage counts it once.

### Step-by-step probe

**Step A1 — Find ccusage's dedup logic directly.**
```bash
grep -n "requestId\|dedup\|Set\|Map\|unique" \
  /home/ff235/.npm-global/lib/node_modules/ccusage/dist/data-loader-9ESMosno.js \
  | head -40
```
Then read ~200 chars of context around any match that also references
`usage`, `inputTokens`, `outputTokens`, `push`, or `aggregate`. Goal:
identify the exact dedup key.

**Step A2 — Quantify duplicate requestIds in the real data.**
Run this Python (do not modify the dir or the files):
```python
import json, glob, os
from collections import Counter, defaultdict

CONFIG_DIR = '/home/ff235/.claude-claude_rate_limit'
dates = {'2026-04-14', '2026-04-19', '2026-04-20'}

# Count (requestId, date) occurrences and sum output tokens per group
groups = defaultdict(lambda: {'count': 0, 'output_sum': 0, 'cache_read_sum': 0})
no_req_id = defaultdict(int)

for path in glob.glob(os.path.join(CONFIG_DIR, 'projects/**/*.jsonl'), recursive=True):
    with open(path) as f:
        for line in f:
            try: ev = json.loads(line)
            except: continue
            if ev.get('type') != 'assistant': continue
            ts = ev.get('timestamp','')[:10]
            if ts not in dates: continue
            msg = ev.get('message') or {}
            u = msg.get('usage') or {}
            if 'input_tokens' not in u: continue
            rid = ev.get('requestId')
            if not rid:
                no_req_id[ts] += 1
                continue
            key = (rid, ts)
            groups[key]['count'] += 1
            groups[key]['output_sum'] += u.get('output_tokens', 0)
            groups[key]['cache_read_sum'] += u.get('cache_read_input_tokens', 0)

# For each date, compute: unique requestId count, dup rate, and
# what total we'd get if we kept only ONE entry per requestId
by_date = defaultdict(lambda: {'unique_reqs': 0, 'dup_reqs': 0, 'dedup_output': 0, 'dedup_cache_read': 0})
for (rid, date), g in groups.items():
    by_date[date]['unique_reqs'] += 1
    if g['count'] > 1: by_date[date]['dup_reqs'] += 1
    # If dedup policy is "sum across dups", we keep g['output_sum']
    # If policy is "first only", we'd divide by count. Test both below.
    by_date[date]['dedup_output']     += g['output_sum'] // g['count']  # first-only
    by_date[date]['dedup_cache_read'] += g['cache_read_sum'] // g['count']

for d in sorted(dates):
    print(f"{d}: unique_reqs={by_date[d]['unique_reqs']}  "
          f"dup_reqs={by_date[d]['dup_reqs']}  "
          f"dedup_output={by_date[d]['dedup_output']}  "
          f"dedup_cache_read={by_date[d]['dedup_cache_read']}  "
          f"no_reqId={no_req_id[d]}")
```

Compare the `dedup_output` column to ccusage's reference (51,803 / 33,520 /
71,216). Close match (within 5%) ⟹ dedup-by-requestId confirmed.

**Step A3 — If A2 doesn't match, try these alternatives in order.**
Do not skip ahead; test one at a time.
1. Dedup by `message.id` (Anthropic message UUID) instead of `requestId`.
2. Dedup by `uuid` (event uuid).
3. Filter by `ev.get('type') == 'assistant' AND msg.get('role') == 'assistant'`
   only (drop any synthetic role).
4. Filter out events where `msg.get('stop_reason')` is `None` or `'tool_use'`
   (count only `end_turn` / `stop_sequence`).

For each, rerun the comparison above. Record which (if any) matches.

### Decision matrix

| Outcome | Plan action |
|---|---|
| A2 matches ccusage within 5% | Adopt `requestId` dedup. Add `SEEN_REQUEST_IDS` dedup to parser. Add as a `TurnDedup` invariant. Done. |
| A2 off but one A3 alternative matches | Adopt that dedup rule. Document rationale in `docs/SCHEMA.md`. |
| No rule matches within 5% | Record as `INCONCLUSIVE`. Open a GitHub issue on ccusage asking for dedup logic. Flag in plan: tokenol ships with its own totals + explicit `--compare-with-ccusage` diagnostic, and pricing table is validated against Anthropic docs rather than ccusage. |

### Deliverable

One paragraph in `VALIDATION.md` §"Post-probe findings" stating:
- Which dedup rule (if any) closes the 2× gap
- Final residual diff vs ccusage after applying it
- Whether the rule generalizes (test one more date: 2026-04-02 — we have
  data in `~/.claude/projects/**/*.jsonl`, scope that test separately)

---

## Probe B — Tokenizer / calibration factors from Claude Code source leak

### Context

- 2026-03-31 npm leak: `@anthropic-ai/claude-code@2.1.88` shipped with a
  `.map` sourcemap. Bundle is ~59.8 MB, ~1,900 TypeScript files,
  ~512k lines.
- Our tiktoken `p50k_base` proxy is ~58% off from Anthropic's billing on
  visible content. `cl100k_base` is even worse (~64%).
- Goal: determine if Claude Code ships an internal tokenizer, calibration
  table, or character-per-token constants we can use locally.

### Step-by-step probe

**Step B1 — Check if the leaked bundle is present locally.**
```bash
find / -name "claude-code" -type d 2>/dev/null | head -20
find ~ -name "*.map" -path "*claude*" 2>/dev/null | head -5
ls -la /home/ff235/.npm-global/lib/node_modules/@anthropic-ai/ 2>/dev/null
ls /home/ff235/.bun/install/cache/@anthropic-ai/ 2>/dev/null
```
If the current installed `@anthropic-ai/claude-code` is v2.1.88, we can
inspect directly. Otherwise, note the installed version.

**Step B2 — If v2.1.88 is not installed, fetch it cleanly.**
```bash
mkdir -p /tmp/cc-leak-probe && cd /tmp/cc-leak-probe
npm pack @anthropic-ai/claude-code@2.1.88 2>&1 | tail -5
tar -xzf anthropic-ai-claude-code-2.1.88.tgz
ls package/
```
If version is unavailable (yanked), try the closest available version
(`npm view @anthropic-ai/claude-code versions --json | tail -30`) and note
which one was actually fetched. Different versions likely have same
tokenizer code — we mainly want it, not specifically the leak.

**Step B3 — Search for tokenizer artifacts.**
Do these searches inside the unpacked or installed bundle (use the
appropriate root path):
```bash
BUNDLE=/tmp/cc-leak-probe/package  # or the installed path from B1
# Tokenizer imports
grep -rEl "tiktoken|tokenizer|countTokens|encode.*token|BPE" "$BUNDLE" 2>/dev/null | head -10
# Calibration tables / per-block factors
grep -rnE "charsPerToken|tokensPerChar|calibrat|estimat.*token" "$BUNDLE" 2>/dev/null | head -20
# Look for the count_tokens API call (would be "messages/count_tokens")
grep -rn "count_tokens\|count-tokens" "$BUNDLE" 2>/dev/null | head -10
# Embedded vocab files
find "$BUNDLE" \( -name "*.bpe" -o -name "*.tiktoken" -o -name "vocab.json" -o -name "tokenizer.json" \) 2>/dev/null
```

**Step B4 — If the bundle is minified/obfuscated, check for `.map` next to it.**
```bash
find "$BUNDLE" -name "*.map" -size +1M 2>/dev/null
```
A large .map file = sourcemap present. If found, use `sourcemap-visualizer`
is overkill — just grep the .map for readable identifiers:
```bash
grep -oE '"[a-zA-Z][a-zA-Z0-9_]{5,40}Token[a-zA-Z0-9_]*"' "$MAP_FILE" | sort -u | head -30
```
Looking for names like `countTokens`, `estimateTokens`, `tokenizeText`.

**Step B5 — Test candidates.**
For each tokenizer-like artifact found, extract it and run the same
500-sample accuracy test from `VALIDATION.md` §Check 4. The working probe
script is already in the session history; reuse it, swapping the tokenizer.

### Decision matrix

| Outcome | Plan action |
|---|---|
| Found a usable local tokenizer/calibration (median error <15%) | Adopt it. Update §5 to default to this tokenizer; `--calibrate` demotes to "exact mode" only. |
| Found calibration factors (chars-per-token per block type) but not a full tokenizer | Adopt the factors in `tokenize/local.py` as a stratified estimator. Expect ~15–25% error. `--calibrate` still recommended for exact thinking metrics. |
| Found nothing useful (minified beyond recovery, or no tokenizer code in bundle) | Keep current plan: `--calibrate` required for reliable thinking %; default mode shows thinking ON/OFF + output-token uplift only. Document error explicitly. |

### Deliverable

Same `VALIDATION.md` §"Post-probe findings" paragraph. Include:
- Which version was inspected (2.1.88 or substitute)
- What was found (tokenizer? factors? nothing?)
- Measured accuracy if a candidate was tested
- Final recommendation for §5 of `REPO_PLAN.md`

---

## Out of scope for this handoff

Do **not**:
- Rewrite `REPO_PLAN.md`. That is a separate step after this report lands.
- Implement any tokenol code. Probes only.
- Investigate issues outside A and B, even if you spot them. Record them
  at the bottom of `VALIDATION.md` under "Findings to revisit later" and
  move on.
- Install packages other than those strictly needed (`npm pack` is fine;
  adding new pip deps is not).

## Environment snapshot

- Python: `python3` (miniconda3), `tiktoken==0.12.0` already installed
- Node: v18.0.11, ccusage installed globally
- JSONL data: `~/.claude*/projects/**/*.jsonl` (1,235 files total)
- `CLAUDE_CONFIG_DIR=/home/ff235/.claude-claude_rate_limit` currently set
- Reference files: `REPO_PLAN.md`, `VALIDATION.md` in this repo root
- The ccusage bundle is at
  `/home/ff235/.npm-global/lib/node_modules/ccusage/dist/`

## Success criteria for this handoff

Exactly one new section appended to `VALIDATION.md`, titled
"Post-probe findings", containing:

1. One paragraph for Probe A with the decision-matrix outcome
2. One paragraph for Probe B with the decision-matrix outcome
3. A 3–5 bullet list titled "Blocking plan edits (revised)" that
   supersedes the list at the end of `VALIDATION.md`

No other files modified.
