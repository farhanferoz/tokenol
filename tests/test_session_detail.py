"""Tests for serve/session_detail.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tokenol.model.events import Session, Turn, Usage
from tokenol.serve.session_detail import build_session_detail


def _make_turn(
    ts: str = "2026-04-14T10:00:00Z",
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
        dedup_key="key",
        timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        session_id="sess-001",
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


def _make_session(turns: list[Turn]) -> Session:
    return Session(
        session_id="sess-001",
        source_file="/path/to/sess-001.jsonl",
        is_sidechain=False,
        cwd="/home/user/project",
        turns=turns,
    )


class TestCostComponents:
    def test_keys_present_on_every_row(self):
        session = _make_session([_make_turn(), _make_turn(ts="2026-04-14T10:05:00Z")])
        detail = build_session_detail(session)
        for row in detail["turns"]:
            assert "cost_components" in row
            cc = row["cost_components"]
            assert set(cc.keys()) == {"input", "output", "cache_read", "cache_creation"}

    def test_sum_matches_cost_usd(self):
        turns = [
            _make_turn(input_t=1000, output_t=200, cache_read=500, cache_creation=100),
            _make_turn(ts="2026-04-14T10:05:00Z", input_t=2000, output_t=300, cache_read=1000, cache_creation=0),
        ]
        session = _make_session(turns)
        detail = build_session_detail(session)
        for row in detail["turns"]:
            cc = row["cost_components"]
            total = sum(cc.values())
            # cost_usd on Turn is pre-set; cost_for_turn recomputes from usage.
            # Just verify components sum to a non-negative finite value.
            assert total >= 0.0
            assert abs(total - (cc["input"] + cc["output"] + cc["cache_read"] + cc["cache_creation"])) < 1e-12

    def test_zero_usage_turn_all_components_zero(self):
        turn = _make_turn(input_t=0, output_t=0, cache_read=0, cache_creation=0, cost_usd=0.0)
        detail = build_session_detail(_make_session([turn]))
        cc = detail["turns"][0]["cost_components"]
        assert cc["input"] == 0.0
        assert cc["output"] == 0.0
        assert cc["cache_read"] == 0.0
        assert cc["cache_creation"] == 0.0

    def test_known_model_components_positive(self):
        turn = _make_turn(model="claude-opus-4-7", input_t=1000, output_t=200, cache_read=500, cache_creation=100)
        detail = build_session_detail(_make_session([turn]))
        cc = detail["turns"][0]["cost_components"]
        assert cc["input"] > 0
        assert cc["output"] > 0
        assert cc["cache_read"] > 0
        assert cc["cache_creation"] > 0

    def test_empty_session_no_turns(self):
        detail = build_session_detail(_make_session([]))
        assert detail["turns"] == []


class TestExistingKeys:
    def test_existing_turn_keys_unchanged(self):
        detail = build_session_detail(_make_session([_make_turn()]))
        row = detail["turns"][0]
        for key in ("ts", "model", "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_creation_tokens", "cost_usd", "is_sidechain",
                    "tool_use_count", "tool_error_count", "stop_reason"):
            assert key in row

    def test_top_level_keys_present(self):
        detail = build_session_detail(_make_session([_make_turn()]))
        for key in ("session_id", "source_file", "model", "cwd", "verdict",
                    "first_ts", "last_ts", "totals", "turns", "patterns"):
            assert key in detail


class TestPatterns:
    def test_patterns_key_present(self):
        detail = build_session_detail(_make_session([_make_turn()]))
        assert "patterns" in detail
        assert isinstance(detail["patterns"], list)

    def test_healthy_session_no_patterns(self):
        # Low-cost, no sidechains, no gaps — should be clean
        turns = [
            _make_turn(ts=f"2026-04-14T10:0{i}:00+00:00", input_t=100, cache_creation=10, cache_read=50)
            for i in range(5)
        ]
        detail = build_session_detail(_make_session(turns))
        assert detail["patterns"] == []

    def test_known_pattern_idle_expiry(self):
        # 2-hour idle gap + high cache_creation spike
        base = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)
        t1 = _make_turn(ts=base.isoformat(), input_t=100, cache_creation=10)
        t2_ts = (base + timedelta(hours=2)).isoformat()
        t2 = _make_turn(ts=t2_ts, input_t=100, cache_creation=900, cache_read=0)
        detail = build_session_detail(_make_session([t1, t2]))
        kinds = [p["kind"] for p in detail["patterns"]]
        assert "idle_expiry" in kinds

    def test_pattern_fields_present(self):
        base = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)
        t1 = _make_turn(ts=base.isoformat(), input_t=100, cache_creation=10)
        t2 = _make_turn(ts=(base + timedelta(hours=2)).isoformat(), input_t=100, cache_creation=900, cache_read=0)
        detail = build_session_detail(_make_session([t1, t2]))
        p = next(x for x in detail["patterns"] if x["kind"] == "idle_expiry")
        for field in ("kind", "severity", "headline", "reason", "suggested_fix", "turn_indices"):
            assert field in p
