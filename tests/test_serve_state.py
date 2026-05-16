"""Unit tests for serve/state.py: ParseCache and build_snapshot_full."""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import tokenol.serve.state as _state_mod
from tokenol.metrics.cost import cost_for_turn
from tokenol.model.events import RawEvent, Session, ToolCost, Turn, Usage
from tokenol.persistence.store import HistoryStore
from tokenol.serve.state import (
    ParseCache,
    SnapshotResult,
    _recompute_excl_cache_read,
    build_project_detail,
    build_snapshot_full,
    build_tool_detail,
    derive_delta_turns,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_P5_TOP_LEVEL_KEYS = {
    "generated_at", "config", "thresholds", "period",
    "topbar_summary", "tiles", "anomaly",
    "hourly_today", "daily", "models", "recent_activity",
    "assumptions_summary",
}


def _write_session(proj_dir: Path, sid: str, cwd: str, model: str, ts_iso: str, uid: str) -> None:
    import json
    sys_ev = json.dumps({
        "type": "system", "timestamp": ts_iso, "sessionId": sid,
        "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
    })
    asst_ev = json.dumps({
        "type": "assistant", "timestamp": ts_iso, "sessionId": sid,
        "requestId": f"req-{uid}", "uuid": f"evt-{uid}", "isSidechain": False,
        "model": model,
        "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}},
    })
    (proj_dir / f"{sid}.jsonl").write_text(sys_ev + "\n" + asst_ev + "\n")


@contextmanager
def _mock_dirs(tmp_path: Path):
    """Patch state.py's get_config_dirs to return tmp_path."""
    original_gcd = _state_mod.get_config_dirs
    original_fjf = _state_mod.find_jsonl_files

    def mock_gcd(all_projects=False):
        return [tmp_path]

    _state_mod.get_config_dirs = mock_gcd
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original_gcd
        _state_mod.find_jsonl_files = original_fjf


# ---- ParseCache tests --------------------------------------------------


def test_parse_cache_hit(tmp_path: Path) -> None:
    """Same (size, mtime_ns) → no re-parse (same list object returned)."""
    dst = tmp_path / "basic.jsonl"
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    key1, events1 = cache.get_or_parse(dst)
    key2, events2 = cache.get_or_parse(dst)

    assert key1 == key2
    assert events1 is events2
    assert cache.size == 1


def test_parse_cache_miss_on_change(tmp_path: Path) -> None:
    """Changed mtime_ns/size → re-parses and returns new list."""
    dst = tmp_path / "basic.jsonl"
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    key1, events1 = cache.get_or_parse(dst)

    extra = b'\n{"type":"system","timestamp":"2026-04-14T10:10:00Z","sessionId":"sess-001","cwd":"/tmp"}\n'
    dst.write_bytes(dst.read_bytes() + extra)

    key2, events2 = cache.get_or_parse(dst)
    assert key1 != key2
    assert events1 is not events2
    assert cache.size == 2


def test_parse_cache_purge(tmp_path: Path) -> None:
    dst = tmp_path / "basic.jsonl"
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    cache.get_or_parse(dst)
    assert cache.size == 1

    cache.purge(set())
    assert cache.size == 0


def test_parse_cache_get_derived_memoizes(tmp_path: Path) -> None:
    """get_derived skips the builder on a key-set hit and rebuilds on miss.

    This is the core idle-CPU optimization: identical (path, size, mtime_ns) sets
    must reuse the cached (turns, sessions, fired) tuple without re-iterating
    events. A new key in the set forces a rebuild.
    """
    dst = tmp_path / "basic.jsonl"
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    key, _events = cache.get_or_parse(dst)

    call_count = 0

    def builder(events):
        nonlocal call_count
        call_count += 1
        from tokenol.serve.state import _build_turns_and_sessions
        return _build_turns_and_sessions(events)

    keys = frozenset({key})
    r1 = cache.get_derived(keys, builder)
    r2 = cache.get_derived(keys, builder)
    assert call_count == 1, "second call with same key set must hit the memo"
    assert r1 is r2, "memo must return the same triple object"

    # A different (simulated) key set forces rebuild.
    other_key = ("/tmp/other.jsonl", 999, 999)
    cache.get_derived(frozenset({key, other_key}), builder)
    assert call_count == 2


def test_parse_cache_invalidates_derived_on_new_file(tmp_path: Path) -> None:
    """A new get_or_parse miss must invalidate the derived memo.

    Otherwise, after a JSONL file changes mtime, build_snapshot_full would happily
    return stale (turns, sessions) computed before the change.
    """
    dst = tmp_path / "basic.jsonl"
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    cache = ParseCache()
    key1, _ = cache.get_or_parse(dst)

    call_count = 0

    def builder(events):
        nonlocal call_count
        call_count += 1
        from tokenol.serve.state import _build_turns_and_sessions
        return _build_turns_and_sessions(events)

    cache.get_derived(frozenset({key1}), builder)
    assert call_count == 1

    # Mutate file → new key, recorded via a fresh get_or_parse miss → memo invalidated.
    dst.write_bytes(dst.read_bytes() + b'\n{"type":"system","timestamp":"2026-04-14T10:10:00Z","sessionId":"x","cwd":"/tmp"}\n')
    key2, _ = cache.get_or_parse(dst)
    assert key1 != key2

    cache.get_derived(frozenset({key2}), builder)
    assert call_count == 2, "memo must be invalidated when a new file revision is parsed"


# ---- build_snapshot_full tests -----------------------------------------


def test_snapshot_shape(tmp_path: Path) -> None:
    """Snapshot contains the Phase 5 top-level keys."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache(), all_projects=False)

    assert isinstance(result, SnapshotResult)
    assert set(result.payload.keys()) == _P5_TOP_LEVEL_KEYS

    p = result.payload
    assert p["config"]["reference_usd"] == 50.0
    assert p["config"]["tick_seconds"] == 5
    assert p["period"] == "today"
    assert isinstance(p["thresholds"], dict)


def test_snapshot_daily_active_projects_alphabetical(tmp_path: Path) -> None:
    """daily.active_projects is sorted alphabetically by label (basename), case-insensitive."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    base_ts = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    # Insertion order is z, a, M — correct sort is a, M, z (case-insensitive).
    _write_session(proj, "sess-z", "/home/u/zeta",  "claude-opus-4-7",   base_ts, "z")
    _write_session(proj, "sess-a", "/home/u/alpha", "claude-opus-4-7",   base_ts, "a")
    _write_session(proj, "sess-m", "/home/u/Middle", "claude-sonnet-4-6", base_ts, "m")

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache(), all_projects=False)

    active = result.payload["daily"]["active_projects"]
    labels = [p["label"] for p in active]
    assert labels == ["alpha", "Middle", "zeta"]

    models = [m["label"] for m in result.payload["daily"]["active_models"]]
    assert models == sorted(models, key=str.lower)


def test_snapshot_daily_active_projects_disambiguates_collisions(tmp_path: Path) -> None:
    """When two cwds share a basename, labels extend to the shortest unique suffix."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    base_ts = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    # Two cwds share basename 'task_a', one lives under mercor/, the other plain.
    _write_session(proj, "sess-1", "/home/u/dev/mercor/task_a", "claude-opus-4-7", base_ts, "1")
    _write_session(proj, "sess-2", "/home/u/dev/task_a",        "claude-opus-4-7", base_ts, "2")
    _write_session(proj, "sess-3", "/home/u/dev/unique",        "claude-opus-4-7", base_ts, "3")

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache(), all_projects=False)

    by_value = {p["value"]: p["label"] for p in result.payload["daily"]["active_projects"]}
    assert by_value["/home/u/dev/mercor/task_a"] == "mercor/task_a"
    assert by_value["/home/u/dev/task_a"]        == "dev/task_a"
    # Non-colliding cwd keeps its plain basename.
    assert by_value["/home/u/dev/unique"] == "unique"


def test_snapshot_daily_active_projects_excludes_old(tmp_path: Path) -> None:
    """Sessions older than the daily default 30-day window don't appear in daily.active_projects."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    fresh_ts = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts   = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    _write_session(proj, "sess-fresh", "/home/u/fresh", "claude-opus-4-7", fresh_ts, "f")
    _write_session(proj, "sess-old",   "/home/u/old",   "claude-opus-4-7", old_ts,   "o")

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache(), all_projects=False)

    values = [it["value"] for it in result.payload["daily"]["active_projects"]]
    assert values == ["/home/u/fresh"]


def test_snapshot_topbar_summary_shape(tmp_path: Path) -> None:
    """topbar_summary has expected fields."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    tb = result.payload["topbar_summary"]
    for key in ("today_cost", "sessions_count", "output_tokens", "input_tokens", "model_mix", "last_active"):
        assert key in tb, f"topbar_summary missing: {key}"
    assert tb["today_cost"] >= 0
    assert tb["sessions_count"] >= 0


def test_snapshot_tiles_shape(tmp_path: Path) -> None:
    """tiles has the four Phase 5 metrics with expected sub-keys."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    tiles = result.payload["tiles"]
    assert set(tiles.keys()) == {"hit_pct", "cost_per_kw", "ctx_ratio", "cache_reuse"}
    for name, tile in tiles.items():
        assert "value" in tile, f"tiles.{name} missing 'value'"
        assert "delta_ratio" in tile, f"tiles.{name} missing 'delta_ratio'"
        assert "baseline_label" in tile, f"tiles.{name} missing 'baseline_label'"
        assert "goal" in tile, f"tiles.{name} missing 'goal'"


def test_snapshot_models_shape(tmp_path: Path) -> None:
    """models section has range, rows, aggregate."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    models = result.payload["models"]
    assert "range" in models
    assert "rows" in models
    assert "aggregate" in models
    agg = models["aggregate"]
    assert "active_count" in agg
    assert "dominant" in agg
    assert "cost_split" in agg


def test_assumptions_summary_has_all_tags(tmp_path: Path) -> None:
    """assumptions_summary contains all 5 AssumptionTag keys."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())
    summary = result.payload["assumptions_summary"]
    expected = {
        "window_boundary_heuristic", "unknown_model_fallback",
        "dedup_passthrough", "interrupted_turn_skipped", "gemini_unpriced",
    }
    assert set(summary.keys()) == expected


def test_snapshot_empty_state(tmp_path: Path) -> None:
    """Empty directory → zero-cost snapshot, no sessions."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    p = result.payload
    assert p["anomaly"] is None
    assert p["topbar_summary"]["today_cost"] == 0.0
    assert p["topbar_summary"]["sessions_count"] == 0
    assert p["recent_activity"]["rows"] == []
    assert p["models"]["rows"] == []
    assert result.sessions == []


def test_snapshot_recent_activity_shape(tmp_path: Path) -> None:
    """recent_activity has window, aggregate, rows fields."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    ra = result.payload["recent_activity"]
    assert "window" in ra
    assert "aggregate" in ra
    assert "rows" in ra
    agg = ra["aggregate"]
    for key in ("projects", "turns", "output", "cost", "model_mix", "hit_pct", "cost_per_kw"):
        assert key in agg, f"recent_activity.aggregate missing: {key}"


def test_snapshot_daily_shape(tmp_path: Path) -> None:
    """daily section has range, earliest_available, series, moving_avg_7d."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    daily = result.payload["daily"]
    assert "range" in daily
    assert "earliest_available" in daily
    assert "series" in daily
    assert "moving_avg_7d" in daily
    assert isinstance(daily["series"], list)
    assert isinstance(daily["moving_avg_7d"], list)


def test_snapshot_period_param(tmp_path: Path) -> None:
    """period param is reflected in payload and affects models.range."""
    with _mock_dirs(tmp_path):
        r7d = build_snapshot_full(ParseCache(), period="7d")

    assert r7d.payload["period"] == "7d"
    assert r7d.payload["models"]["range"] == "7d"
    assert r7d.payload["daily"]["range"] == "7d"


# ---- build_project_detail tests ------------------------------------------

def _build_project_sessions(tmp_path: Path, cwd: str, entries: list[tuple[str, str, str]]):
    """Build a list of Session objects for project tests.

    entries: list of (session_id_suffix, timestamp_iso, uid_suffix)
    """
    import json

    from tokenol.ingest.builder import build_sessions

    proj_dir = tmp_path / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    all_paths = []
    for suffix, ts_iso, uid in entries:
        sid = f"sess-{suffix}"
        path = proj_dir / f"{sid}.jsonl"
        user_ev = json.dumps({
            "type": "user", "timestamp": ts_iso, "sessionId": sid, "cwd": cwd,
            "message": {"role": "user", "content": "hi"},
        })
        asst_ev = json.dumps({
            "type": "assistant", "timestamp": ts_iso, "sessionId": sid,
            "requestId": f"req-{uid}", "uuid": f"evt-{uid}", "isSidechain": False,
            "model": "claude-opus-4-7",
            "message": {"id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1000, "output_tokens": 200,
                                  "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100}},
        })
        path.write_text(user_ev + "\n" + asst_ev + "\n")
        all_paths.append(path)

    from tokenol.ingest.builder import build_turns
    turns = build_turns(all_paths)
    sessions = build_sessions(turns, paths=all_paths)
    return sessions


def test_project_detail_default_range_14d(tmp_path: Path) -> None:
    """Default range=14d includes sessions within last 14 days."""
    from datetime import date, timedelta
    today = date.today()
    recent = (today - timedelta(days=5)).isoformat() + "T10:00:00Z"
    old = (today - timedelta(days=20)).isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [
        ("recent", recent, "r1"),
        ("old", old, "o1"),
    ])
    result = build_project_detail(cwd, sessions, range_key="14d")
    assert result is not None
    assert result["session_count"] == 1
    assert result["range_key"] == "14d"


def test_project_detail_range_all_includes_old_sessions(tmp_path: Path) -> None:
    """range=all includes sessions regardless of age."""
    from datetime import date, timedelta
    today = date.today()
    recent = (today - timedelta(days=5)).isoformat() + "T10:00:00Z"
    old = (today - timedelta(days=20)).isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [
        ("recent", recent, "r1"),
        ("old", old, "o1"),
    ])
    result = build_project_detail(cwd, sessions, range_key="all")
    assert result is not None
    assert result["session_count"] == 2


def test_project_detail_range_1d_scopes_to_today(tmp_path: Path) -> None:
    """range=1d only includes sessions with last_ts today."""
    from datetime import date, timedelta
    today = date.today()
    today_ts = today.isoformat() + "T10:00:00Z"
    yesterday_ts = (today - timedelta(days=1)).isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [
        ("today", today_ts, "t1"),
        ("yest", yesterday_ts, "y1"),
    ])
    result = build_project_detail(cwd, sessions, range_key="1d")
    assert result is not None
    assert result["session_count"] == 1


def test_project_detail_range_no_activity_returns_none(tmp_path: Path) -> None:
    """When no sessions fall in the selected range, returns None."""
    from datetime import date, timedelta
    old = (date.today() - timedelta(days=60)).isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [("old", old, "o1")])
    result = build_project_detail(cwd, sessions, range_key="14d")
    assert result is None


def test_project_detail_invalid_range_raises(tmp_path: Path) -> None:
    """Unknown range key raises ValueError."""
    import pytest
    cwd = "/repo/myproject"
    with pytest.raises(ValueError, match="Unknown range"):
        build_project_detail(cwd, [], range_key="bogus")


def test_project_detail_top_turns_have_efficiency_metrics(tmp_path: Path) -> None:
    """top_turns_by_cost entries include hit_rate, cost_per_kw, ctx_ratio."""
    from datetime import date
    ts = date.today().isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [("s1", ts, "u1")])
    result = build_project_detail(cwd, sessions, range_key="14d")
    assert result is not None
    assert result["top_turns_by_cost"], "expected at least one turn"
    turn = result["top_turns_by_cost"][0]
    assert "hit_rate" in turn
    assert "cost_per_kw" in turn
    assert "ctx_ratio" in turn
    # hit_rate = 800/(800+100+1000) = 800/1900 ≈ 0.421 (fraction, 0–1 scale)
    assert turn["hit_rate"] is not None
    assert 0.40 < turn["hit_rate"] < 0.45


def test_project_detail_sessions_have_cost_per_kw_and_ctx(tmp_path: Path) -> None:
    """sessions dicts include cost_per_kw and ctx_ratio."""
    from datetime import date
    ts = date.today().isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [("s1", ts, "u1")])
    result = build_project_detail(cwd, sessions, range_key="14d")
    assert result is not None
    sess = result["sessions"][0]
    assert "cost_per_kw" in sess
    assert "ctx_ratio" in sess


def test_project_detail_cache_trend_key(tmp_path: Path) -> None:
    """Payload uses 'cache_trend' (not 'cache_trend_14d')."""
    from datetime import date
    ts = date.today().isoformat() + "T10:00:00Z"
    cwd = "/repo/myproject"
    sessions = _build_project_sessions(tmp_path, cwd, [("s1", ts, "u1")])
    result = build_project_detail(cwd, sessions, range_key="14d")
    assert result is not None
    assert "cache_trend" in result
    assert "cache_trend_14d" not in result


def test_grouped_cwd_rolls_child_under_parent(tmp_path: Path) -> None:
    """Sessions in a nested cwd get remapped to the shortest active ancestor."""
    from tokenol.model.events import Session
    from tokenol.serve.state import _grouped_cwd_by_sid

    sessions = [
        Session(session_id="a", source_file="", is_sidechain=False, cwd="/dev/StratSense"),
        Session(session_id="b", source_file="", is_sidechain=False, cwd="/dev/StratSense/StratSense-Backend"),
        Session(session_id="c", source_file="", is_sidechain=False, cwd="/dev/StratSense/StratSense-Backend/deep/dir"),
        Session(session_id="d", source_file="", is_sidechain=False, cwd="/dev/OtherProj"),
        Session(session_id="e", source_file="", is_sidechain=False, cwd=None),
    ]
    m = _grouped_cwd_by_sid(sessions)
    assert m["a"] == "/dev/StratSense"
    assert m["b"] == "/dev/StratSense"
    assert m["c"] == "/dev/StratSense"
    assert m["d"] == "/dev/OtherProj"
    assert m["e"] == "(unknown)"


def test_grouped_cwd_siblings_stay_separate(tmp_path: Path) -> None:
    """When the parent dir has no activity, sibling children stay separate."""
    from tokenol.model.events import Session
    from tokenol.serve.state import _grouped_cwd_by_sid

    sessions = [
        Session(session_id="a", source_file="", is_sidechain=False, cwd="/dev/StratSense/Backend"),
        Session(session_id="b", source_file="", is_sidechain=False, cwd="/dev/StratSense/Frontend"),
    ]
    m = _grouped_cwd_by_sid(sessions)
    assert m["a"] == "/dev/StratSense/Backend"
    assert m["b"] == "/dev/StratSense/Frontend"


def test_multi_session_file_sets_source_file_per_session(tmp_path: Path) -> None:
    """A single JSONL file holding N sessions must set source_file on each.

    Regression guard: earlier code keyed session_source by path.stem, which
    broke for multi-session files (stem != sessionId for all but one).
    """
    from tokenol.ingest.parser import parse_file
    from tokenol.serve.state import _build_turns_and_sessions

    dst = tmp_path / "multi.jsonl"
    dst.write_bytes((FIXTURES_DIR / "multi.jsonl").read_bytes())

    events = parse_file(dst)
    _, sessions, _fired = _build_turns_and_sessions(events)

    assert {s.session_id for s in sessions} == {"sess-a", "sess-b", "sess-c"}
    for s in sessions:
        assert s.source_file == str(dst), f"{s.session_id} has source_file={s.source_file!r}"


# ---- build_tool_detail tests ------------------------------------------


def test_build_tool_detail_returns_payload():
    def _turn(sid, ts, model, tools, cost=0.0, err_count=0):
        return Turn(
            dedup_key=f"k-{ts.isoformat()}", timestamp=ts, session_id=sid,
            model=model, usage=Usage(input_tokens=1, output_tokens=1),
            is_sidechain=False, stop_reason="tool_use",
            cost_usd=cost, tool_use_count=sum(tools.values()),
            tool_error_count=err_count, tool_names=Counter(tools),
        )

    t0 = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)

    turns = [
        _turn("sA", t0, "claude-opus-4-7",    {"Read": 2, "Edit": 1}),
        _turn("sB", t1, "claude-opus-4-7",    {"Read": 1, "Bash": 3}, err_count=1),
        _turn("sC", t2, "claude-sonnet-4-6",  {"Grep": 1}),
    ]
    sessions = [
        Session(session_id="sA", source_file="a.jsonl", is_sidechain=False, cwd="/p/projA", turns=[turns[0]]),
        Session(session_id="sB", source_file="b.jsonl", is_sidechain=False, cwd="/p/projB", turns=[turns[1]]),
        Session(session_id="sC", source_file="c.jsonl", is_sidechain=False, cwd="/p/projA", turns=[turns[2]]),
    ]

    detail = build_tool_detail("Read", turns, sessions)
    assert detail["name"] == "Read"
    assert detail["total_invocations"] == 3

    by_project = {p["project_label"]: p for p in detail["by_project"]}
    assert set(by_project.keys()) == {"projA", "projB"}
    assert by_project["projA"]["invocations"] == 2
    assert by_project["projB"]["invocations"] == 1
    assert by_project["projA"]["cwd_b64"]
    assert by_project["projA"]["last_active"] == t0.isoformat()
    invs = [p["invocations"] for p in detail["by_project"]]
    assert invs == sorted(invs, reverse=True) or invs == sorted([p["cost_usd"] for p in detail["by_project"]], reverse=True)  # cost-sorted is the canonical order

    by_model = {m["name"]: m for m in detail["by_model"]}
    assert set(by_model.keys()) == {"claude-opus-4-7"}
    assert by_model["claude-opus-4-7"]["invocations"] == 3

    sc = detail["scorecards"]
    assert sc["invocations"] == 3
    assert "cost_usd" in sc and "output_tokens" in sc and "top_project" in sc


def test_build_tool_detail_unknown_returns_none():
    assert build_tool_detail("NoSuchTool", [], []) is None


def test_build_tool_detail_excludes_interrupted():
    """Interrupted turns (no usage billed) still might have tool_use content,
    but we exclude them from counts to match /api/breakdown/tools."""
    ts = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    interrupted = Turn(
        dedup_key="k", timestamp=ts, session_id="s1", model="claude-opus-4-7",
        usage=Usage(), is_sidechain=False, stop_reason=None,
        is_interrupted=True, tool_use_count=1, tool_names=Counter({"Read": 1}),
    )
    sessions = [Session(session_id="s1", source_file="s.jsonl", is_sidechain=False, cwd="/p", turns=[interrupted])]
    assert build_tool_detail("Read", [interrupted], sessions) is None


def test_build_tool_detail_includes_linger_only_turns():
    """A tool whose presence on later turns is purely linger-only (cost without
    a fresh invocation) must still contribute to total_cost and by_project.
    Regression test for the by_tool/tool_detail reconciliation gap where
    `tool_turns` previously filtered to tool_names-only turns and dropped
    linger-only attribution."""
    from tokenol.model.events import ToolCost
    t0 = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 14, 11, 0, tzinfo=timezone.utc)

    # Turn 0: invokes Read (cost + names).
    invoke = Turn(
        dedup_key="k0", timestamp=t0, session_id="s1", model="claude-opus-4-7",
        usage=Usage(input_tokens=10, output_tokens=10), is_sidechain=False,
        stop_reason="tool_use", cost_usd=0.05, tool_use_count=1,
        tool_names=Counter({"Read": 1}),
        tool_costs={"Read": ToolCost(tool_name="Read", output_tokens=10, cost_usd=0.05)},
    )
    # Turn 1: no fresh invocation, but Read result lingers in input bytes →
    # cost attributed to Read with no entry in tool_names.
    linger = Turn(
        dedup_key="k1", timestamp=t1, session_id="s1", model="claude-opus-4-7",
        usage=Usage(input_tokens=50, output_tokens=5), is_sidechain=False,
        stop_reason="end_turn", cost_usd=0.04, tool_use_count=0,
        tool_names=Counter(),
        tool_costs={"Read": ToolCost(tool_name="Read", input_tokens=40, cost_usd=0.03)},
    )
    sessions = [Session(session_id="s1", source_file="s.jsonl", is_sidechain=False, cwd="/p/a", turns=[invoke, linger])]

    detail = build_tool_detail("Read", [invoke, linger], sessions)
    assert detail is not None
    # Total cost reflects both the invocation slice and the linger slice.
    assert detail["scorecards"]["cost_usd"] == 0.05 + 0.03
    # Invocations counted from tool_names only; linger turn doesn't bump it.
    assert detail["total_invocations"] == 1


def test_build_tool_detail_rejects_sentinels():
    """The cost-attribution sentinels are not real tools; tool detail must 404
    rather than render a bogus page (which would have happened before the
    sentinel rejection was added at the entry point)."""
    from tokenol.serve.state import build_tool_detail as _build
    assert _build("__unattributed__", [], []) is None
    assert _build("__unknown__", [], []) is None


def test_accumulate_tool_costs_union_includes_linger_only():
    """_accumulate_tool_costs must surface tools that appear in tool_costs even
    when they're absent from tool_names — without this, project/model by_tool
    rollups silently drop linger-only attribution."""
    from tokenol.model.events import ToolCost
    from tokenol.serve.state import _accumulate_tool_costs
    ts = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    linger = Turn(
        dedup_key="k", timestamp=ts, session_id="s1", model="claude-opus-4-7",
        usage=Usage(input_tokens=50, output_tokens=5), is_sidechain=False,
        stop_reason="end_turn", cost_usd=0.04,
        tool_names=Counter(),
        tool_costs={"Read": ToolCost(tool_name="Read", input_tokens=40, cost_usd=0.03)},
    )
    cost, invs, last = _accumulate_tool_costs([linger])
    assert cost["Read"] == 0.03
    # Invocations 0 (no tool_names entry) but the tool is still represented.
    assert invs.get("Read", 0) == 0
    # last_active populated from the tool_costs pass so callers can iterate the
    # union without a KeyError.
    assert "Read" in last


def test_accumulate_tool_costs_folds_unknown_into_unattributed():
    """The __unknown__ sentinel (unmatched tool_result bytes) must not surface
    as a real tool — it gets folded into __unattributed__ so callers' filter
    drops it from project/model by_tool rollups."""
    from tokenol.model.events import ToolCost
    from tokenol.serve.state import _accumulate_tool_costs
    ts = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    turn = Turn(
        dedup_key="k", timestamp=ts, session_id="s1", model="claude-opus-4-7",
        usage=Usage(input_tokens=50, output_tokens=5), is_sidechain=False,
        stop_reason="end_turn", cost_usd=0.02,
        tool_names=Counter(),
        tool_costs={"__unknown__": ToolCost(tool_name="__unknown__", input_tokens=20, cost_usd=0.02)},
    )
    cost, _invs, _last = _accumulate_tool_costs([turn])
    assert "__unknown__" not in cost
    assert cost["__unattributed__"] == 0.02


# ---- derive_delta_turns tests ------------------------------------------


def _ev(
    *,
    sid: str,
    msg_id: str | None,
    req_id: str | None,
    ts: datetime,
    source: str = "/tmp/x.jsonl",
    line: int = 1,
    cwd: str = "/proj",
    model: str = "claude-sonnet-4-6",
) -> RawEvent:
    return RawEvent(
        source_file=source,
        line_number=line,
        event_type="assistant",
        session_id=sid,
        request_id=req_id,
        message_id=msg_id,
        uuid=f"u-{line}",
        timestamp=ts,
        usage=Usage(input_tokens=10, output_tokens=5),
        model=model,
        is_sidechain=False,
        stop_reason="end_turn",
        cwd=cwd,
    )


def test_derive_delta_turns_skips_known_dedup_keys() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev(sid="s", msg_id="m1", req_id="r1", ts=ts),
        _ev(sid="s", msg_id="m2", req_id="r2", ts=ts, line=2),
    ]
    turns, _, _, _ = derive_delta_turns(
        events,
        existing_dedup_keys={"m1:r1"},
        existing_passthrough_locations=set(),
    )
    assert {t.dedup_key for t in turns} == {"m2:r2"}


def test_derive_delta_turns_skips_known_passthroughs() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=4),
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=7),
    ]
    turns, _, _, _ = derive_delta_turns(
        events,
        existing_dedup_keys=set(),
        existing_passthrough_locations={("/x.jsonl", 4)},
    )
    # Line 4 known → skipped; line 7 emitted as a new passthrough turn.
    assert len(turns) == 1
    assert turns[0].dedup_key  # passthroughs use uuid or id() as fallback


def test_derive_delta_turns_emits_session_metadata_for_new_sids() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [_ev(sid="brand-new", msg_id="m1", req_id="r1", ts=ts, cwd="/proj/new")]
    _, sessions, _, _ = derive_delta_turns(
        events,
        existing_dedup_keys=set(),
        existing_passthrough_locations=set(),
    )
    assert len(sessions) == 1
    assert sessions[0].session_id == "brand-new"
    assert sessions[0].cwd == "/proj/new"


def test_derive_delta_turns_returns_accepted_passthrough_locations() -> None:
    """The 4th return value is the set of (source_file, line_number) for emitted passthroughs."""
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = [
        # Accepted passthrough (no dedup key, not synthetic, not in known set).
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=10),
        # Synthetic — must NOT appear in accepted set.
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=20, model="<synthetic>"),
        # Skipped (already known) — must NOT appear in accepted set.
        _ev(sid="s", msg_id=None, req_id=None, ts=ts, source="/x.jsonl", line=30),
    ]
    _, _, _, accepted = derive_delta_turns(
        events,
        existing_dedup_keys=set(),
        existing_passthrough_locations={("/x.jsonl", 30)},
    )
    assert accepted == {("/x.jsonl", 10)}


def test_snapshot_equivalence_via_store(tmp_path: Path) -> None:
    """Snapshot from JSONLs == snapshot from store-only after JSONL deletion.

    Verifies that once turns are persisted to the store, deleting their source
    JSONL files does not change the dashboard's quantitative payload.
    """
    proj = tmp_path / "claude" / "projects" / "p1"
    proj.mkdir(parents=True)
    _write_session(proj, "sid-A", "/proj/a", "claude-sonnet-4-6", "2026-05-01T12:00:00Z", "1")
    _write_session(proj, "sid-B", "/proj/b", "claude-opus-4-7",   "2026-05-01T13:00:00Z", "2")

    store = HistoryStore(tmp_path / "h.duckdb")
    # Use a wide hot window so both turns hydrate into memory in run 2.
    store._hot_window_days = 365

    try:
        with _mock_dirs(tmp_path / "claude"):
            cache = ParseCache()
            r1 = build_snapshot_full(cache, history_store=store)
            # Force-flush whatever the broadcaster would normally batch.
            store.flush(
                turns=cache._hot_turns,
                sessions=list(cache._hot_sessions_by_id.values()),
            )

        # Delete the live JSONLs.
        for f in proj.glob("*.jsonl"):
            f.unlink()

        with _mock_dirs(tmp_path / "claude"):
            cache2 = ParseCache()
            r2 = build_snapshot_full(cache2, history_store=store)

        # Quantitative payload sections must match across the deletion.
        for k in ("topbar_summary", "tiles", "models", "recent_activity"):
            assert r1.payload[k] == r2.payload[k], f"divergence in {k}"
        # Sessions are preserved across deletion.
        assert {s.session_id for s in r1.sessions} == {s.session_id for s in r2.sessions}
    finally:
        store.close()


# ---- _recompute_excl_cache_read tests ---------------------------------


def _turn_with_costs(usage: Usage, model: str, tool_costs: dict[str, ToolCost],
                     *, unattr_input=0.0, unattr_output=0.0, unattr_cost=0.0,
                     ts: datetime | None = None) -> Turn:
    ts = ts or datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)
    return Turn(
        dedup_key=f"k-{ts.isoformat()}",
        timestamp=ts,
        session_id="s1",
        model=model,
        usage=usage,
        is_sidechain=False,
        stop_reason="tool_use",
        tool_costs=tool_costs,
        unattributed_input_tokens=unattr_input,
        unattributed_output_tokens=unattr_output,
        unattributed_cost_usd=unattr_cost,
    )


def test_recompute_excl_cache_read_drops_cache_read_from_input_pool():
    """A turn with 60% tool byte-share on input and 40% on output should see
    its cache_read_usd flow entirely to unattributed; tool cost becomes
    in_share * (input_usd + cache_creation_usd) + out_share * output_usd."""
    usage = Usage(
        input_tokens=1_000,
        output_tokens=10_000,
        cache_read_input_tokens=900_000,
        cache_creation_input_tokens=99_000,
    )
    # Pool = 1_000_000; 60% tool input share => 600_000 input_tokens stored.
    # 40% tool output share => 4_000 output_tokens stored.
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=600_000.0,
                         output_tokens=4_000.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    expected_read = (
        0.6 * (turn_cost.input_usd + turn_cost.cache_creation_usd)
        + 0.4 * turn_cost.output_usd
    )
    assert result.keys() == {"Read"}
    assert result["Read"] == pytest.approx(expected_read, rel=1e-9)


def test_recompute_excl_cache_read_handles_zero_input_pool():
    """input_token_pool == 0 should not raise; in_share is 0."""
    usage = Usage(input_tokens=0, output_tokens=100,
                  cache_read_input_tokens=0, cache_creation_input_tokens=0)
    tool_costs = {
        "Edit": ToolCost(tool_name="Edit", input_tokens=0.0,
                         output_tokens=80.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    # Only output side contributes; out_share = 80/100 = 0.8.
    assert result["Edit"] == pytest.approx(0.8 * turn_cost.output_usd, rel=1e-9)


def test_recompute_excl_cache_read_handles_zero_output_tokens():
    """output_tokens == 0 (rare but possible) should not raise; out_share is 0."""
    usage = Usage(input_tokens=1_000, output_tokens=0,
                  cache_read_input_tokens=9_000, cache_creation_input_tokens=0)
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=5_000.0,
                         output_tokens=0.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)
    # in_share = 5000/10000 = 0.5; out_share = 0.
    expected = 0.5 * (turn_cost.input_usd + turn_cost.cache_creation_usd)
    assert result["Read"] == pytest.approx(expected, rel=1e-9)


def test_recompute_excl_cache_read_empty_tool_costs():
    usage = Usage(input_tokens=1_000, output_tokens=1_000,
                  cache_read_input_tokens=5_000, cache_creation_input_tokens=0)
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs={})
    assert _recompute_excl_cache_read(turn) == {}


def test_recompute_excl_cache_read_linger_only_tool():
    """Tool with positive input share but zero output share (lingered from
    a prior turn) gets a non-zero cost on the input side alone."""
    usage = Usage(input_tokens=0, output_tokens=100,
                  cache_read_input_tokens=7_000, cache_creation_input_tokens=3_000)
    tool_costs = {
        "Read": ToolCost(tool_name="Read", input_tokens=3_000.0,
                         output_tokens=0.0, cost_usd=0.0),
    }
    turn = _turn_with_costs(usage, "claude-opus-4-7", tool_costs)
    turn_cost = cost_for_turn("claude-opus-4-7", usage)

    result = _recompute_excl_cache_read(turn)

    # input_token_pool = 0 + 7000 + 3000 = 10_000
    # in_share = 3000 / 10000 = 0.3; out_share = 0
    # input_pool_excl = input_usd + cache_creation_usd (cache_read_usd dropped)
    # cost = 0.3 * (input_usd + cache_creation_usd) + 0 * output_usd
    expected = 0.3 * (turn_cost.input_usd + turn_cost.cache_creation_usd)
    assert result["Read"] == pytest.approx(expected, rel=1e-9)
    assert result["Read"] > 0.0   # non-zero — the whole point of this case
