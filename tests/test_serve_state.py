"""Unit tests for serve/state.py: ParseCache and build_snapshot_full."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import tokenol.serve.state as _state_mod
from tokenol.serve.state import (
    ParseCache,
    SnapshotResult,
    build_project_detail,
    build_snapshot_full,
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


# ---- build_tool_detail tests ------------------------------------------

from collections import Counter


def test_build_tool_detail_returns_payload():
    from datetime import datetime, timezone
    from tokenol.serve.state import build_tool_detail
    from tokenol.model.events import Session, Turn, Usage

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
        _turn("sC", t2, "claude-sonnet-4-6",  {"Grep": 1}),  # no Read
    ]
    sessions = [
        Session(session_id="sA", source_file="a.jsonl", is_sidechain=False, cwd="/p/projA", turns=[turns[0]]),
        Session(session_id="sB", source_file="b.jsonl", is_sidechain=False, cwd="/p/projB", turns=[turns[1]]),
        Session(session_id="sC", source_file="c.jsonl", is_sidechain=False, cwd="/p/projA", turns=[turns[2]]),
    ]

    detail = build_tool_detail("Read", turns, sessions)
    assert detail["name"] == "Read"
    assert detail["total_invocations"] == 3  # 2 in sA + 1 in sB

    # Per-project breakdown: projA (2 invocations in sA) and projB (1 in sB). projA's
    # last_active is sA's turn since sC doesn't use Read.
    projects = {p["cwd"]: p for p in detail["projects_using_tool"]}
    assert set(projects.keys()) == {"/p/projA", "/p/projB"}
    assert projects["/p/projA"]["count"] == 2
    assert projects["/p/projB"]["count"] == 1
    assert projects["/p/projA"]["cwd_b64"]  # non-empty base64
    assert projects["/p/projA"]["last_active"] == t0.isoformat()
    # Projects sorted by count desc.
    assert detail["projects_using_tool"][0]["count"] >= detail["projects_using_tool"][1]["count"]

    # Per-model breakdown: only opus (sonnet didn't call Read).
    models = {m["model"]: m for m in detail["models_using_tool"]}
    assert set(models.keys()) == {"claude-opus-4-7"}
    assert models["claude-opus-4-7"]["count"] == 3


def test_build_tool_detail_unknown_returns_none():
    from tokenol.serve.state import build_tool_detail
    assert build_tool_detail("NoSuchTool", [], []) is None


def test_build_tool_detail_excludes_interrupted():
    """Interrupted turns (no usage billed) still might have tool_use content,
    but we exclude them from counts to match /api/breakdown/tools."""
    from datetime import datetime, timezone
    from tokenol.serve.state import build_tool_detail
    from tokenol.model.events import Session, Turn, Usage

    ts = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    interrupted = Turn(
        dedup_key="k", timestamp=ts, session_id="s1", model="claude-opus-4-7",
        usage=Usage(), is_sidechain=False, stop_reason=None,
        is_interrupted=True, tool_use_count=1, tool_names=Counter({"Read": 1}),
    )
    sessions = [Session(session_id="s1", source_file="s.jsonl", is_sidechain=False, cwd="/p", turns=[interrupted])]
    assert build_tool_detail("Read", [interrupted], sessions) is None
