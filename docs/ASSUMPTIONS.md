# tokenol Assumptions Catalog

Every heuristic or fallback that tokenol applies is recorded as an `AssumptionTag`.
These surface in three places:
- Report footer summary (printed automatically when any fire)
- `--show-assumptions` flag forces the footer to print even when empty
- `--log-level debug` emits stderr JSON lines per decision
- `--strict` mode treats any fired assumption as a fatal error

---

## Assumption Tags

| Tag | Heuristic | Why needed | Error mode |
|---|---|---|---|
| `WINDOW_BOUNDARY_HEURISTIC` | 5h window starts at first billable event; runs 5h wall-clock; next event after expiry starts a new window | Anthropic's exact server-side rule isn't published; matches community reverse-engineering | Drifts if Anthropic uses fixed-UTC or overlapping-rolling windows |
| `UNKNOWN_MODEL_FALLBACK` | Unknown Claude model inherits pricing + context from nearest-known family sibling | No machine-readable pricing feed; new models appear before we update | Warned per model; slight mispricing until registry updated |
| `DEDUP_PASSTHROUGH` | Events with `null` `message.id` or `null` `requestId` pass through dedup (matches ccusage behavior) | Cannot form the compound hash key | Rare; logged per event |
| `INTERRUPTED_TURN_SKIPPED` | Assistant messages with no `usage` fields (stop_reason=NONE) excluded from cost | Request never completed; no billing data | None — correctly excludes |
| `GEMINI_UNPRICED` | Non-Claude models (gemini-*) parsed but not priced | Multi-provider pricing is post-v1 | Cost rows show `—` for these models |

---

## Firing Semantics

`WINDOW_BOUNDARY_HEURISTIC` fires once per `align_windows()` call, not once per turn.

---

## CLI Flags

- `--strict` — refuse any assumption fallback; error out with non-zero exit code.
- `--show-assumptions` — force the footer to print even if no assumptions fired. Useful for CI output.
- `--log-level debug` — set Python's root logging level to DEBUG; enables verbose per-decision output.

---

## Per-Tool Cost Attribution Heuristics (0.6.0+)

The per-tool cost attribution model splits each billable turn's `cost_usd` across the tools it invoked. The split does **not** change the turn's billable cost — it only decomposes already-computed cost for the dashboard — so the heuristics below are **not** recorded as `AssumptionTag`s and don't surface in the assumption footer or `--strict` mode. They are documented here for completeness.

| Heuristic | Choice | Why | Failure mode |
|---|---|---|---|
| **Output-side byte share** | A turn's `output_usd` is split across `tool_use` blocks emitted on the same turn by their JSON byte size (`json.dumps(block, separators=(",", ":"))`). `text`, `thinking`, sentinel-named, and non-`tool_use` blocks go to `__unattributed__`. | Block byte size is the only signal directly proportional to model output work that survives the JSONL round-trip. | A tool whose call is short but triggers extensive downstream work is under-attributed relative to a tool whose output dominates the message. |
| **Input-side byte share** | `input_usd + cache_read_usd + cache_creation_usd` are combined into one input cost pool. The pool is split across `tool_use` / `tool_use_result` blocks accumulated in per-session byte tallies from prior turns, by accumulated byte size. Text / thinking content from prior turns contributes to a non-tool bucket that reduces the per-tool denominator. | `cache_read_usd` dominates input-side spend on Claude Code workloads; treating the three input components as one pool keeps the split coherent regardless of which component the model billed against on a given turn. | Sessions where cache-read cost dominates look "tool-heavy" because most context lives in earlier tool calls. The 0.6.1 `excl_cache_read` attribution mode (Tool Mix panel only) offers an alternative lens. |
| **Monotonic byte tally** | The per-session input tallies grow monotonically — they never subtract a `tool_result` that has been dropped from the model's visible window. Only compaction resets them. | Claude Code performs full-conversation compaction (auto at 95 % context, or user-triggered) as its primary eviction mechanism; partial mid-session pruning is not assumed. | Partial pruning between compactions would leave evicted tool bytes still absorbing input share until the next compaction reset. |
| **Compaction reset threshold** | When an assistant turn's input token pool drops below **20 %** of the session's running peak (`COMPACTION_DROP_RATIO = 0.2` in `src/tokenol/ingest/parser.py`), tokenol clears its per-session byte tallies and resets the peak to the new pool size. | Real compactions drop visible context by an order of magnitude; the 20 % cutoff catches every observed compaction while staying clear of natural drift. The peak reset prevents a session that genuinely stabilises at low context from re-triggering on every turn (which would otherwise dump all attribution into `__unattributed__`). | A graceful prune that drops to 25–40 % of peak would not trigger reset, so old tool bytes would keep absorbing input share until they fall out naturally. A single anomalously-small turn could false-positive and dump that one turn's input cost to `__unattributed__`. |
| **Unmatched `tool_use_id` bucketing** | `tool_result` blocks whose `tool_use_id` doesn't match any prior `tool_use` declaration (cross-file boundaries, parse gaps, missing declarations) attribute to the `UNKNOWN_TOOL` sentinel — surfaced as `__unattributed__`. | tokenol cannot guess which tool produced an unmatched result without speculation. | Cross-file jumps temporarily inflate `__unattributed__` until the matching declarations re-enter the window. |

Switching the Tool Mix panel to the 0.6.1 `excl_cache_read` mode changes the input cost pool to `input_usd + cache_creation_usd` only; `cache_read_usd` flows entirely into the residual instead of being split across tools. See [`docs/METRICS.md`](METRICS.md) for the formulas.
