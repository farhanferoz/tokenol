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

## New tags added in Phase 2

`WINDOW_BOUNDARY_HEURISTIC` was defined in Phase 1 but first fired in Phase 2 when `align_windows()` is called. It fires once per `align_windows()` call, not once per turn.

---

## CLI Flags

- `--strict` — refuse any assumption fallback; error out with non-zero exit code.
- `--show-assumptions` — force the footer to print even if no assumptions fired. Useful for CI output.
- `--log-level debug` — set Python's root logging level to DEBUG; enables verbose per-decision output.
