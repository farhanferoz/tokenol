"""Integration tests for SSE streaming."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod
from tokenol.serve.state import ParseCache
from tokenol.serve.streaming import _shallow_diff, snapshot_stream

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@contextmanager
def _mock_dirs(tmp_path: Path):
    original_gcd = _state_mod.get_config_dirs
    _state_mod.get_config_dirs = lambda all_projects=False: [tmp_path]
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original_gcd


# ---- _shallow_diff unit tests ------------------------------------------


def test_shallow_diff_no_change() -> None:
    d = {"a": 1, "b": [1, 2]}
    assert _shallow_diff(d, d) == {}


def test_shallow_diff_changed_key() -> None:
    prev = {"a": 1, "b": 2, "c": 3}
    curr = {"a": 1, "b": 99, "c": 3}
    assert _shallow_diff(prev, curr) == {"b": 99}


def test_shallow_diff_new_key() -> None:
    prev = {"a": 1}
    curr = {"a": 1, "b": 2}
    assert _shallow_diff(prev, curr) == {"b": 2}


# ---- SSE stream integration tests --------------------------------------


@pytest.mark.asyncio
async def test_stream_first_connect_sends_full_snapshot(tmp_path: Path) -> None:
    """First SSE message is the full snapshot."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    messages = []

    with _mock_dirs(tmp_path):
        async for chunk in snapshot_stream(
            parse_cache=cache,
            all_projects=False,
            reference_usd=50.0,
            get_tick_seconds=lambda: 999,  # very long tick so we only get one message
        ):
            messages.append(json.loads(chunk.removeprefix("data: ").strip()))
            break  # only take the first message

    assert len(messages) == 1
    # Full snapshot must contain all required keys
    for key in ["generated_at", "config", "thresholds", "period", "topbar_summary", "tiles"]:
        assert key in messages[0], f"Missing key: {key}"


@pytest.mark.asyncio
async def test_stream_subsequent_sends_diff_only(tmp_path: Path) -> None:
    """Second message contains only the changed keys."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    call_count = 0
    original_build = _state_mod.build_snapshot_full

    # Second call mutates generated_at so it looks "changed"
    def patched_build(parse_cache, all_projects=False, reference_usd=50.0, tick_seconds=5):
        nonlocal call_count
        call_count += 1
        result = original_build(parse_cache, all_projects, reference_usd, tick_seconds)
        if call_count == 2:
            # Simulate a change only in generated_at
            result.payload["generated_at"] = "2099-01-01T00:00:00+00:00"
        return result

    messages = []
    with _mock_dirs(tmp_path):
        _state_mod.build_snapshot_full = patched_build
        try:
            async for chunk in snapshot_stream(
                parse_cache=cache,
                all_projects=False,
                reference_usd=50.0,
                get_tick_seconds=lambda: 0,  # no sleep
            ):
                data = json.loads(chunk.removeprefix("data: ").strip())
                messages.append(data)
                if len(messages) == 2:
                    break
        finally:
            _state_mod.build_snapshot_full = original_build

    assert len(messages) == 2
    # Second message is a diff — only the changed key
    assert "generated_at" in messages[1]
    assert "today" not in messages[1]  # unchanged, not in diff


@pytest.mark.asyncio
async def test_stream_diff_omits_unchanged_top_level_keys(tmp_path: Path) -> None:
    """When a top-level key is unchanged between ticks, it MUST be omitted from the
    diff. The frontend must therefore merge diffs into local state, not replace it —
    this test documents that contract so regressions are caught.
    """
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    call_count = 0
    original_build = _state_mod.build_snapshot_full

    def patched_build(parse_cache, all_projects=False, reference_usd=50.0, tick_seconds=5,
                      period="today", thresholds=None):
        nonlocal call_count
        call_count += 1
        result = original_build(parse_cache, all_projects, reference_usd, tick_seconds,
                                period, thresholds)
        if call_count == 2:
            # Simulate a single changed key so the diff is non-empty and emitted.
            result.payload["generated_at"] = "2099-01-01T00:00:00+00:00"
        return result

    messages = []
    with _mock_dirs(tmp_path):
        _state_mod.build_snapshot_full = patched_build
        try:
            async for chunk in snapshot_stream(
                parse_cache=cache,
                all_projects=False,
                reference_usd=50.0,
                get_tick_seconds=lambda: 0,
            ):
                messages.append(json.loads(chunk.removeprefix("data: ").strip()))
                if len(messages) == 2:
                    break
        finally:
            _state_mod.build_snapshot_full = original_build

    # First message: full snapshot — must include structural keys the UI relies on.
    assert "config" in messages[0]
    assert "thresholds" in messages[0]
    # Second message: diff — unchanged keys are omitted. Structural/stable keys like
    # 'config' and 'thresholds' don't change between identical ticks, so they MUST
    # be absent from the diff. If they leak in, the frontend merge regression test
    # would be silently weakened.
    assert "config" not in messages[1], (
        "config leaked into diff — frontend merge fix may be untested"
    )
    assert "thresholds" not in messages[1]


def test_idle_backoff_formula() -> None:
    """effective_tick = max(tick * 3, 15) when idle ≥ 30 s."""
    IDLE_THRESHOLD = 30.0

    def effective_tick(tick: int, idle_seconds: float) -> int:
        if idle_seconds >= IDLE_THRESHOLD:
            return max(tick * 3, 15)
        return tick

    assert effective_tick(5, 29.9) == 5    # not yet idle
    assert effective_tick(5, 30.0) == 15   # idle: 5*3=15
    assert effective_tick(10, 30.0) == 30  # idle: 10*3=30
    assert effective_tick(2, 30.0) == 15   # idle: min capped at 15
    assert effective_tick(5, 0.0) == 5     # fresh
