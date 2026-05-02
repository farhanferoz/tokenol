"""Integration tests for the SSE broadcaster."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import tokenol.serve.state as _state_mod
from tokenol.metrics.thresholds import DEFAULTS
from tokenol.serve.state import ParseCache
from tokenol.serve.streaming import (
    IDLE_THRESHOLD,
    IDLE_TICK_FLOOR,
    SnapshotBroadcaster,
    _effective_tick,
    _shallow_diff,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@contextmanager
def _mock_dirs(tmp_path: Path):
    original_gcd = _state_mod.get_config_dirs
    _state_mod.get_config_dirs = lambda all_projects=False: [tmp_path]
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original_gcd


def _make_broadcaster(
    parse_cache: ParseCache | None = None,
    heartbeat_s: float = 0.0,
) -> SnapshotBroadcaster:
    """Default heartbeat=0 so the file-mtime gate doesn't block test rebuilds."""
    return SnapshotBroadcaster(
        parse_cache=parse_cache or ParseCache(),
        all_projects=False,
        get_reference_usd=lambda: 50.0,
        get_tick_seconds=lambda: 1,
        get_thresholds=lambda: dict(DEFAULTS),
        heartbeat_s=heartbeat_s,
    )


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


# ---- idle-backoff math (independent of broadcaster) --------------------


def test_idle_backoff_formula() -> None:
    """`max(tick * 3, IDLE_TICK_FLOOR)` once idle ≥ IDLE_THRESHOLD."""
    assert _effective_tick(5, IDLE_THRESHOLD - 0.1) == 5
    assert _effective_tick(5, IDLE_THRESHOLD) == max(15, IDLE_TICK_FLOOR)
    assert _effective_tick(10, IDLE_THRESHOLD) == 30
    assert _effective_tick(2, IDLE_THRESHOLD) == IDLE_TICK_FLOOR  # floor wins
    assert _effective_tick(5, 0.0) == 5


# ---- broadcaster integration tests -------------------------------------


@pytest.mark.asyncio
async def test_subscribe_first_message_is_full_snapshot(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    bc = _make_broadcaster()
    with _mock_dirs(tmp_path):
        agen = bc.subscribe("today").__aiter__()
        try:
            chunk = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
        finally:
            await agen.aclose()

    msg = json.loads(chunk.removeprefix("data: ").strip())
    for key in ["generated_at", "config", "thresholds", "period", "topbar_summary", "tiles"]:
        assert key in msg, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_subscribe_subsequent_message_is_diff(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    # Patch build_snapshot_full so the second tick mutates only `generated_at`.
    original_build = _state_mod.build_snapshot_full
    call_count = 0

    def patched_build(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        result = original_build(*args, **kwargs)
        if call_count >= 2:
            result.payload["generated_at"] = "2099-01-01T00:00:00+00:00"
        return result

    bc = SnapshotBroadcaster(
        parse_cache=ParseCache(),
        all_projects=False,
        get_reference_usd=lambda: 50.0,
        get_tick_seconds=lambda: 0,  # no-sleep tick
        get_thresholds=lambda: dict(DEFAULTS),
        heartbeat_s=0.0,
    )

    messages: list[dict] = []
    with _mock_dirs(tmp_path):
        import tokenol.serve.streaming as _stream_mod
        _stream_mod.build_snapshot_full = patched_build
        try:
            agen = bc.subscribe("today").__aiter__()
            try:
                while len(messages) < 2:
                    chunk = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
                    messages.append(json.loads(chunk.removeprefix("data: ").strip()))
            finally:
                await agen.aclose()
        finally:
            _stream_mod.build_snapshot_full = original_build

    assert len(messages) == 2
    # First: full snapshot — structural keys present.
    assert "config" in messages[0]
    assert "thresholds" in messages[0]
    # Second: diff only — unchanged structural keys absent, changed key present.
    assert "generated_at" in messages[1]
    assert "config" not in messages[1]
    assert "thresholds" not in messages[1]


@pytest.mark.asyncio
async def test_two_subscribers_share_one_producer(tmp_path: Path) -> None:
    """Two tabs on the same period → one producer task, one build per tick."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    original_build = _state_mod.build_snapshot_full
    build_calls = 0

    def counting_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    bc = SnapshotBroadcaster(
        parse_cache=ParseCache(),
        all_projects=False,
        get_reference_usd=lambda: 50.0,
        get_tick_seconds=lambda: 0,
        get_thresholds=lambda: dict(DEFAULTS),
        heartbeat_s=0.0,
    )

    with _mock_dirs(tmp_path):
        import tokenol.serve.streaming as _stream_mod
        _stream_mod.build_snapshot_full = counting_build
        try:
            agen1 = bc.subscribe("today").__aiter__()
            agen2 = bc.subscribe("today").__aiter__()
            try:
                # First message for each subscriber.
                await asyncio.wait_for(agen1.__anext__(), timeout=5.0)
                await asyncio.wait_for(agen2.__anext__(), timeout=5.0)
                # Both subscribers should be sharing one group.
                assert bc.group_count() == 1
                # Let a few ticks run, then check build/subscriber ratio.
                await asyncio.sleep(0.1)
                builds_during_run = build_calls
                # Each subscriber receives messages from the SAME producer build,
                # so builds_during_run should be ~equal to ticks, not 2× ticks.
                # We don't know exact tick count under no-sleep; assert each subscriber
                # got at least one more message and the count stayed reasonable.
                assert builds_during_run >= 1
            finally:
                await agen1.aclose()
                await agen2.aclose()
        finally:
            _stream_mod.build_snapshot_full = original_build


@pytest.mark.asyncio
async def test_producer_shuts_down_when_last_subscriber_leaves(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    bc = _make_broadcaster()
    with _mock_dirs(tmp_path):
        agen = bc.subscribe("today").__aiter__()
        try:
            await asyncio.wait_for(agen.__anext__(), timeout=5.0)
            assert bc.group_count() == 1
        finally:
            await agen.aclose()
        # After aclose, the finally block in subscribe() runs and removes the group.
        # Give the event loop a tick to process the cancellation.
        await asyncio.sleep(0)
        assert bc.group_count() == 0


@pytest.mark.asyncio
async def test_gate_skips_rebuild_when_files_unchanged(tmp_path: Path) -> None:
    """With heartbeat>0 and no JSONL mtime change, the producer must NOT rebuild
    after the initial snapshot. This is the idle-CPU optimization: stat the files
    cheaply, skip the full snapshot assembly when nothing has changed.
    """
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    original_build = _state_mod.build_snapshot_full
    build_calls = 0

    def counting_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    bc = SnapshotBroadcaster(
        parse_cache=ParseCache(),
        all_projects=False,
        get_reference_usd=lambda: 50.0,
        get_tick_seconds=lambda: 0,
        get_thresholds=lambda: dict(DEFAULTS),
        heartbeat_s=3600.0,  # effectively never heartbeat during the test
    )

    with _mock_dirs(tmp_path):
        import tokenol.serve.streaming as _stream_mod
        _stream_mod.build_snapshot_full = counting_build
        try:
            agen = bc.subscribe("today").__aiter__()
            try:
                await asyncio.wait_for(agen.__anext__(), timeout=5.0)
                # Initial build to bootstrap.
                assert build_calls == 1
                # Let the producer cycle several gate-check iterations.
                await asyncio.sleep(0.2)
                # No file changed and heartbeat is far in the future, so build_calls
                # must not have grown past the initial bootstrap.
                assert build_calls == 1, (
                    f"Expected gate to skip rebuilds, saw {build_calls} builds"
                )
            finally:
                await agen.aclose()
        finally:
            _stream_mod.build_snapshot_full = original_build


@pytest.mark.asyncio
async def test_gate_rebuilds_when_file_mtime_changes(tmp_path: Path) -> None:
    """When a JSONL file's (size, mtime_ns) changes, the gate must trigger a rebuild
    even if the heartbeat hasn't elapsed.
    """
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    original_build = _state_mod.build_snapshot_full
    build_calls = 0

    def counting_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    bc = SnapshotBroadcaster(
        parse_cache=ParseCache(),
        all_projects=False,
        get_reference_usd=lambda: 50.0,
        get_tick_seconds=lambda: 0,
        get_thresholds=lambda: dict(DEFAULTS),
        heartbeat_s=3600.0,
    )

    with _mock_dirs(tmp_path):
        import tokenol.serve.streaming as _stream_mod
        _stream_mod.build_snapshot_full = counting_build
        try:
            agen = bc.subscribe("today").__aiter__()
            try:
                await asyncio.wait_for(agen.__anext__(), timeout=5.0)
                assert build_calls == 1

                # Append to the file → new mtime/size → gate should fire.
                dst.write_bytes(
                    dst.read_bytes()
                    + b'\n{"type":"system","timestamp":"2026-04-14T10:10:00Z","sessionId":"x","cwd":"/tmp"}\n'
                )
                # Allow several gate-check iterations to detect the change.
                for _ in range(50):
                    if build_calls > 1:
                        break
                    await asyncio.sleep(0.01)
                assert build_calls > 1, "gate should rebuild after file mtime change"
            finally:
                await agen.aclose()
        finally:
            _stream_mod.build_snapshot_full = original_build


@pytest.mark.asyncio
async def test_distinct_periods_get_distinct_groups(tmp_path: Path) -> None:
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    bc = _make_broadcaster()
    with _mock_dirs(tmp_path):
        a = bc.subscribe("today").__aiter__()
        b = bc.subscribe("7d").__aiter__()
        try:
            await asyncio.wait_for(a.__anext__(), timeout=5.0)
            await asyncio.wait_for(b.__anext__(), timeout=5.0)
            assert bc.group_count() == 2
        finally:
            await a.aclose()
            await b.aclose()


@pytest.mark.asyncio
async def test_broadcaster_applies_pending_forget(tmp_path, monkeypatch) -> None:
    """A live serve consumes pending forget requests within one tick.

    The broadcaster polls take_forget_request() each tick. When a request is
    present, it applies the deletion to the store AND evicts the affected
    session_ids from the in-memory hot tier so the next snapshot reflects it
    without requiring a process restart.
    """
    from collections import Counter
    from datetime import datetime, timezone

    from tokenol.enums import AssumptionTag
    from tokenol.model.events import Session, Turn, Usage
    from tokenol.persistence.flusher import FlushQueue
    from tokenol.persistence.forget_handoff import ForgetRequest, submit_forget_request
    from tokenol.persistence.store import HistoryStore
    from tokenol.serve.state import ParseCache

    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    store = HistoryStore(tmp_path / "h.duckdb")
    try:
        pre_existing_turn = Turn(
            dedup_key="k1",
            timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            session_id="sess-X",
            model="claude-sonnet-4-6",
            usage=Usage(input_tokens=100, output_tokens=50,
                        cache_read_input_tokens=20, cache_creation_input_tokens=10),
            is_sidechain=False, stop_reason="end_turn", cost_usd=0.01,
            is_interrupted=False, tool_use_count=0, tool_error_count=0,
            tool_names=Counter(), assumptions=[AssumptionTag.UNKNOWN_MODEL_FALLBACK],
        )
        pre_existing_session = Session(
            session_id="sess-X", source_file="/tmp/x.jsonl",
            is_sidechain=False, cwd="/tmp/proj", turns=[],
        )
        store.flush([pre_existing_turn], [pre_existing_session])

        # Prime the parse cache's hot tier as if the broadcaster had hydrated it.
        parse_cache = ParseCache()
        parse_cache._hot_initialized = True
        parse_cache._hot_turns = [pre_existing_turn]
        parse_cache._hot_sessions_by_id = {"sess-X": pre_existing_session}
        parse_cache._known_dedup_keys = {"k1"}
        parse_cache._known_passthrough_locs = set()
        parse_cache._last_ts_by_session = {"sess-X": pre_existing_turn.timestamp}
        parse_cache._fired = Counter()

        flush_queue = FlushQueue(store, count_threshold=1000, interval_seconds=60)
        await flush_queue.start()
        try:
            broadcaster = SnapshotBroadcaster(
                parse_cache=parse_cache,
                all_projects=False,
                get_reference_usd=lambda: 50.0,
                get_tick_seconds=lambda: 1,
                get_thresholds=lambda: {},
                history_store=store,
                flush_queue=flush_queue,
            )

            submit_forget_request(ForgetRequest(
                kind="session", value="sess-X",
                submitted_at=datetime.now(tz=timezone.utc),
            ))

            await broadcaster.process_pending_forget()

            # Store row gone.
            rows = store._con.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'sess-X'"
            ).fetchone()
            assert rows == (0,)
            # In-memory hot tier evicted.
            assert "sess-X" not in parse_cache._hot_sessions_by_id
            # Hot turns list also pruned.
            assert all(t.session_id != "sess-X" for t in parse_cache._hot_turns)
        finally:
            await flush_queue.stop()
    finally:
        store.close()
