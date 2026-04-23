"""Tests for metrics/patterns.py — pattern detectors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tokenol.metrics.patterns import detect_patterns
from tokenol.model.events import Turn, Usage

_BASE_TS = datetime(2026, 4, 14, 10, 0, 0, tzinfo=timezone.utc)


def _turn(
    *,
    offset_h: float = 0,
    model: str = "claude-opus-4-7",
    input_t: int = 1000,
    output_t: int = 200,
    cache_read: int = 500,
    cache_creation: int = 100,
    cost_usd: float = 0.01,
    is_sidechain: bool = False,
    tool_use_count: int = 0,
    tool_error_count: int = 0,
) -> Turn:
    return Turn(
        dedup_key="k",
        timestamp=_BASE_TS + timedelta(hours=offset_h),
        session_id="s",
        model=model,
        usage=Usage(
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        ),
        is_sidechain=is_sidechain,
        stop_reason="end_turn",
        cost_usd=cost_usd,
        tool_use_count=tool_use_count,
        tool_error_count=tool_error_count,
    )


# ── idle_expiry ────────────────────────────────────────────────────────────────

class TestIdleExpiry:
    def test_positive_single_gap(self):
        turns = [
            _turn(offset_h=0),
            # 2-hour gap + high cache_creation ratio
            _turn(offset_h=2, input_t=100, cache_creation=9000, cache_read=0),
        ]
        hits = detect_patterns(turns)
        kinds = [h.kind for h in hits]
        assert "idle_expiry" in kinds

    def test_negative_short_gap(self):
        turns = [
            _turn(offset_h=0),
            _turn(offset_h=0.1, input_t=1000, cache_creation=900, cache_read=0),
        ]
        hits = detect_patterns(turns)
        assert not any(h.kind == "idle_expiry" for h in hits)

    def test_negative_low_creation_ratio(self):
        turns = [
            _turn(offset_h=0),
            # 2h gap but low cache_creation ratio
            _turn(offset_h=2, input_t=1000, cache_creation=50, cache_read=500),
        ]
        hits = detect_patterns(turns)
        assert not any(h.kind == "idle_expiry" for h in hits)

    def test_severity_escalates_to_red_at_three(self):
        # 4 turns with 3 big gaps → 3 hits → red
        turns = [
            _turn(offset_h=0),
            _turn(offset_h=2,  input_t=100, cache_creation=9000, cache_read=0),
            _turn(offset_h=4,  input_t=100, cache_creation=9000, cache_read=0),
            _turn(offset_h=6,  input_t=100, cache_creation=9000, cache_read=0),
        ]
        hits = [h for h in detect_patterns(turns) if h.kind == "idle_expiry"]
        assert all(h.severity == "red" for h in hits)


# ── compaction_reinflation ────────────────────────────────────────────────────

class TestCompactionReinflation:
    def _build_cycle_turns(self, n_cycles: int = 1) -> list[Turn]:
        """Create n_cycles of peak → drop → rise in visible tokens."""
        turns = []
        peak = 50_000
        for _ in range(n_cycles):
            turns.append(_turn(input_t=peak, cache_read=0, cache_creation=0, offset_h=len(turns) * 0.1))
            # Drop to ~10%
            turns.append(_turn(input_t=int(peak * 0.1), cache_read=0, cache_creation=0, offset_h=len(turns) * 0.1))
            # Rise back to ~85%
            turns.append(_turn(input_t=int(peak * 0.85), cache_read=0, cache_creation=0, offset_h=len(turns) * 0.1))
        return turns

    def test_positive_one_cycle(self):
        turns = self._build_cycle_turns(1)
        hits = detect_patterns(turns)
        assert any(h.kind == "compaction_reinflation" for h in hits)

    def test_severity_red_at_threshold(self):
        turns = self._build_cycle_turns(3)
        hits = [h for h in detect_patterns(turns) if h.kind == "compaction_reinflation"]
        assert hits
        assert hits[0].severity == "red"

    def test_negative_drop_no_reinflation(self):
        # Drop but never re-inflate
        turns = [
            _turn(input_t=50_000, offset_h=0),
            _turn(input_t=5_000,  offset_h=0.1),
            _turn(input_t=5_000,  offset_h=0.2),
            _turn(input_t=5_000,  offset_h=0.3),
        ]
        hits = detect_patterns(turns)
        assert not any(h.kind == "compaction_reinflation" for h in hits)


# ── context_ceiling_plateau ───────────────────────────────────────────────────

class TestContextCeilingPlateau:
    def test_positive_long_plateau(self):
        # 25 turns near the haiku 200k ceiling
        cw = 200_000
        turns = [_turn(model="claude-haiku-4-5", input_t=int(cw * 0.95), cache_read=0, cache_creation=0, offset_h=i * 0.1)
                 for i in range(25)]
        hits = detect_patterns(turns, {"context_plateau_min_turns": 20})
        assert any(h.kind == "context_ceiling_plateau" for h in hits)

    def test_negative_short_plateau(self):
        cw = 200_000
        turns = [_turn(model="claude-haiku-4-5", input_t=int(cw * 0.95), cache_read=0, cache_creation=0, offset_h=i * 0.1)
                 for i in range(10)]
        hits = detect_patterns(turns, {"context_plateau_min_turns": 20})
        assert not any(h.kind == "context_ceiling_plateau" for h in hits)


# ── sidechain_explosion ───────────────────────────────────────────────────────

class TestSidechainExplosion:
    def test_positive_high_sidechain_share(self):
        turns = [
            _turn(cost_usd=0.10, is_sidechain=False),
            _turn(cost_usd=0.10, is_sidechain=True),
            _turn(cost_usd=0.10, is_sidechain=True),
            _turn(cost_usd=0.10, is_sidechain=True),
        ]  # sidechain = 0.30/0.40 = 75%
        hits = detect_patterns(turns)
        assert any(h.kind == "sidechain_explosion" for h in hits)

    def test_negative_low_sidechain_share(self):
        turns = [
            _turn(cost_usd=0.90, is_sidechain=False),
            _turn(cost_usd=0.10, is_sidechain=True),
        ]  # 10% sidechain
        hits = detect_patterns(turns)
        assert not any(h.kind == "sidechain_explosion" for h in hits)

    def test_severity_red_above_sixty_pct(self):
        turns = [
            _turn(cost_usd=0.05, is_sidechain=False),
            _turn(cost_usd=0.95, is_sidechain=True),
        ]  # 95%
        hits = [h for h in detect_patterns(turns) if h.kind == "sidechain_explosion"]
        assert hits and hits[0].severity == "red"


# ── tool_error_storm ──────────────────────────────────────────────────────────

class TestToolErrorStorm:
    def test_positive_high_error_rate(self):
        turns = [
            _turn(tool_use_count=2, tool_error_count=1, offset_h=i * 0.1)
            for i in range(10)
        ]  # 50% error rate over 10-turn window
        hits = detect_patterns(turns)
        assert any(h.kind == "tool_error_storm" for h in hits)

    def test_negative_low_error_rate(self):
        turns = [
            _turn(tool_use_count=10, tool_error_count=1, offset_h=i * 0.1)
            for i in range(10)
        ]  # 10% → below default 20% threshold
        hits = detect_patterns(turns)
        assert not any(h.kind == "tool_error_storm" for h in hits)

    def test_negative_no_tool_uses(self):
        turns = [_turn(tool_use_count=0, tool_error_count=0, offset_h=i * 0.1) for i in range(10)]
        hits = detect_patterns(turns)
        assert not any(h.kind == "tool_error_storm" for h in hits)


# ── edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_turns_returns_empty(self):
        assert detect_patterns([]) == []

    def test_single_turn_no_hits(self):
        hits = detect_patterns([_turn()])
        assert not any(h.kind in ("idle_expiry", "compaction_reinflation") for h in hits)


# ── integration: all 5 patterns in one synthetic session ──────────────────────

class TestAllPatternsIntegration:
    def test_all_five_patterns_triggered(self):
        """Synthetic 200-turn session that triggers every detector."""
        turns: list[Turn] = []

        # 1. idle_expiry: 3 large idle gaps (each 2h) with high cache_creation
        for gap in range(3):
            turns.append(_turn(offset_h=gap * 3.0))
            turns.append(_turn(
                offset_h=gap * 3.0 + 2.0,
                input_t=100, cache_creation=9000, cache_read=0,
            ))

        # 2. compaction_reinflation: 3 cycles (triggers red)
        base = len(turns) * 0.1
        peak = 50_000
        for _ in range(3):
            turns.append(_turn(input_t=peak, cache_read=0, cache_creation=0, offset_h=base + len(turns) * 0.05))
            turns.append(_turn(input_t=int(peak * 0.05), cache_read=0, cache_creation=0, offset_h=base + len(turns) * 0.05))
            turns.append(_turn(input_t=int(peak * 0.85), cache_read=0, cache_creation=0, offset_h=base + len(turns) * 0.05))

        # 3. context_ceiling_plateau: 25 turns near 200k ceiling (haiku model)
        cw = 200_000
        for i in range(25):
            turns.append(_turn(model="claude-haiku-4-5", input_t=int(cw * 0.95), cache_read=0, cache_creation=0, offset_h=20 + i * 0.1))

        # 4. sidechain_explosion: lots of expensive sidechain turns
        for i in range(5):
            turns.append(_turn(cost_usd=0.01, is_sidechain=False, offset_h=50 + i * 0.1))
        for i in range(15):
            turns.append(_turn(cost_usd=0.10, is_sidechain=True, offset_h=51 + i * 0.1))

        # 5. tool_error_storm: 10 turns with 50% tool error rate
        for i in range(10):
            turns.append(_turn(tool_use_count=2, tool_error_count=1, offset_h=60 + i * 0.1))

        hits = detect_patterns(turns, {"context_plateau_min_turns": 20})
        kinds_found = {h.kind for h in hits}
        expected = {
            "idle_expiry", "compaction_reinflation", "context_ceiling_plateau",
            "sidechain_explosion", "tool_error_storm",
        }
        assert expected <= kinds_found, f"Missing patterns: {expected - kinds_found}"
        assert len(hits) >= 5
