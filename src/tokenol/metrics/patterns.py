"""Automated pattern detectors for session drill-down diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

from tokenol.metrics.context import context_tokens
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.model.events import Turn
from tokenol.model.pricing import context_window


@dataclass
class PatternHit:
    kind:           str        # "idle_expiry" | "compaction_reinflation" | "context_ceiling_plateau" | "sidechain_explosion" | "tool_error_storm"
    severity:       str        # "red" | "amber" | "info"
    headline:       str
    reason:         str
    suggested_fix:  str
    turn_indices:   list[int]  # 0-indexed positions in session.turns that triggered


def detect_patterns(turns: list[Turn], thresholds: dict | None = None) -> list[PatternHit]:
    """Run all five detectors and return matching PatternHits."""
    t = {**DEFAULTS, **(thresholds or {})}
    hits: list[PatternHit] = []
    hits.extend(_idle_expiry(turns, t))
    hits.extend(_compaction_reinflation(turns, t))
    hits.extend(_context_ceiling_plateau(turns, t))
    hits.extend(_sidechain_explosion(turns, t))
    hits.extend(_tool_error_storm(turns, t))
    return hits


def _idle_expiry(turns: list[Turn], t: dict) -> list[PatternHit]:
    gap_thresh   = t["idle_expiry_gap_seconds"]
    creat_thresh = t["idle_expiry_creation_ratio"]
    thresh_h     = round(gap_thresh / 3600, 1)

    hits: list[PatternHit] = []
    for i in range(len(turns) - 1):
        nxt = turns[i + 1]
        gap = (nxt.timestamp - turns[i].timestamp).total_seconds()
        if gap < gap_thresh:
            continue
        denom = max(1, nxt.usage.input_tokens)
        creation_ratio = nxt.usage.cache_creation_input_tokens / denom
        if creation_ratio <= creat_thresh:
            continue
        h = round(gap / 3600, 1)
        p = round(creation_ratio * 100)
        hits.append(PatternHit(
            kind="idle_expiry",
            severity="amber",
            headline=f"Prompt cache expired during a {h}h idle gap before turn {i + 1}.",
            reason=f"Next turn was {p}% cache_creation.",
            suggested_fix=(
                f"Start a fresh session after breaks longer than {thresh_h}h — "
                "the 5-minute default prompt-cache TTL has long expired."
            ),
            turn_indices=[i + 1],
        ))

    # Escalate to red when 3+ hits in the session
    if len(hits) >= 3:
        for h in hits:
            h.severity = "red"

    return hits


def _compaction_reinflation(turns: list[Turn], t: dict) -> list[PatternHit]:
    if not turns:
        return []
    drop_ratio      = t["compaction_drop_ratio"]
    reinflate_ratio = t["compaction_reinflate_ratio"]
    red_cycles      = int(t["compaction_red_cycles"])
    W = 5

    visible = [context_tokens(tr) for tr in turns]

    # Find cycles: peak → drop ≤ (1-drop_ratio)*peak within W → rise ≥ reinflate_ratio*peak within W
    cycle_indices: list[int] = []
    n = len(visible)
    i = 0
    while i < n:
        peak = visible[i]
        if peak == 0:
            i += 1
            continue
        # Look for a drop
        drop_at = None
        for j in range(i + 1, min(i + 1 + W, n)):
            if visible[j] <= (1 - drop_ratio) * peak:
                drop_at = j
                break
        if drop_at is None:
            i += 1
            continue
        # Look for a reinflation
        for k in range(drop_at + 1, min(drop_at + 1 + W, n)):
            if visible[k] >= reinflate_ratio * peak:
                cycle_indices.extend([i, drop_at, k])
                i = k + 1
                break
        else:
            i += 1

    if not cycle_indices:
        return []

    cycles = len(cycle_indices) // 3
    severity = "red" if cycles >= red_cycles else "amber"
    p = round(reinflate_ratio * 100)
    return [PatternHit(
        kind="compaction_reinflation",
        severity=severity,
        headline=f"Context was compacted {cycles} time(s) and re-inflated to the ceiling.",
        reason=f"Each compaction was followed by tokens climbing back to ≥{p}% of the previous peak.",
        suggested_fix=(
            "If the sub-tasks are unrelated, /clear + fresh session avoids "
            "paying to re-summarise the old context."
        ),
        turn_indices=sorted(set(cycle_indices)),
    )]


def _context_ceiling_plateau(turns: list[Turn], t: dict) -> list[PatternHit]:
    if not turns:
        return []
    plateau_fraction  = t["context_plateau_fraction"]
    plateau_min_turns = int(t["context_plateau_min_turns"])

    _cw_cache: dict[str | None, int] = {}
    def _cw(model: str | None) -> int:
        if model not in _cw_cache:
            cw = context_window(model) if model else None
            _cw_cache[model] = cw if cw is not None else 200_000
        return _cw_cache[model]

    # A turn is "at the ceiling" if its visible tokens >= fraction * its model's context window
    at_ceiling = [
        context_tokens(tr) >= plateau_fraction * _cw(tr.model)
        for tr in turns
    ]

    # Find the longest run of consecutive turns at or above the threshold
    best_start, best_len = 0, 0
    run_start, run_len   = 0, 0
    for i, at in enumerate(at_ceiling):
        if at:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_len  = run_len
                best_start = run_start
        else:
            run_len = 0

    if best_len < plateau_min_turns:
        return []

    p        = round(plateau_fraction * 100)
    severity = "red" if best_len >= plateau_min_turns * 2 else "amber"
    return [PatternHit(
        kind="context_ceiling_plateau",
        severity=severity,
        headline=f"Session ran at ≥{p}% of the model's context window for {best_len} turns.",
        reason="Each of those turns paid near-full-context input rates.",
        suggested_fix=(
            "Split the work across sessions; there's no way to economise "
            "once you're pinned to the ceiling."
        ),
        turn_indices=list(range(best_start, best_start + best_len)),
    )]


def _sidechain_explosion(turns: list[Turn], t: dict) -> list[PatternHit]:
    if not turns:
        return []
    cost_share_thresh = t["sidechain_explosion_cost_share"]

    total_cost = sum(tr.cost_usd for tr in turns)
    if total_cost == 0:
        return []

    sidechain_indices = [i for i, tr in enumerate(turns) if tr.is_sidechain]
    side_cost = sum(turns[i].cost_usd for i in sidechain_indices)
    share = side_cost / total_cost

    if share <= cost_share_thresh:
        return []

    p        = round(share * 100)
    severity = "red" if share >= 0.6 else "amber"
    a        = f"{side_cost:.2f}"
    b        = f"{total_cost:.2f}"
    return [PatternHit(
        kind="sidechain_explosion",
        severity=severity,
        headline=f"Sidechain/task-agent work accounted for {p}% of this session's cost.",
        reason=f"${a} of ${b} billed was sidechain.",
        suggested_fix=(
            "Review the task-agent prompts — they did most of the spending. "
            "Consider scoping them tighter."
        ),
        turn_indices=sidechain_indices,
    )]


def _tool_error_storm(turns: list[Turn], t: dict) -> list[PatternHit]:
    if not turns:
        return []
    W          = int(t["tool_error_storm_window"])
    rate_thresh = t["tool_error_storm_rate"]

    hits: list[PatternHit] = []
    i = 0
    while i + W <= len(turns):
        window = turns[i:i + W]
        use = sum(tr.tool_use_count for tr in window)
        err = sum(tr.tool_error_count for tr in window)
        if use > 0 and err / use > rate_thresh:
            rate = round(err / use * 100)
            j    = i + W - 1
            hits.append(PatternHit(
                kind="tool_error_storm",
                severity="red" if err / use > 0.5 else "amber",
                headline=f"Tool errors spiked in turns {i}–{j} ({rate}% error rate).",
                reason=f"{err} errors across {use} tool uses.",
                suggested_fix=(
                    "Check which tool is failing — a single bad tool in a loop "
                    "can dominate cost."
                ),
                turn_indices=list(range(i, i + W)),
            ))
            i += W  # non-overlapping
        else:
            i += 1

    return hits
