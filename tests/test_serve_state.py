"""Unit tests for serve/state.py: ParseCache and build_snapshot_full."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import tokenol.serve.state as _state_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures"

from tokenol.serve.state import ParseCache, SnapshotResult, build_snapshot_full


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
    """Snapshot contains all required top-level keys."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache(), all_projects=False)

    assert isinstance(result, SnapshotResult)
    p = result.payload
    for key in [
        "generated_at", "config", "active_window", "today",
        "daily_90d", "sessions", "projects", "models",
        "heatmap_14d", "recent_turns", "assumptions_fired",
    ]:
        assert key in p, f"Missing key: {key}"

    for range_key in ("24h", "7d", "14d"):
        assert range_key in p["sessions"]
        assert range_key in p["projects"]
        assert range_key in p["models"]

    assert p["config"]["reference_usd"] == 50.0
    assert p["config"]["tick_seconds"] == 5


def test_snapshot_empty_state(tmp_path: Path) -> None:
    """Empty directory → zero-cost snapshot, no sessions, no active window."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    p = result.payload
    assert p["active_window"] is None
    assert p["today"]["cost_usd"] == 0.0
    assert p["today"]["turns"] == 0
    assert p["sessions"]["14d"] == []
    assert p["projects"]["14d"] == []
    assert p["models"]["14d"] == []
    assert p["recent_turns"] == []
    assert result.sessions == []


def test_heatmap_shape(tmp_path: Path) -> None:
    """Heatmap is exactly 14 rows × 24 cols."""
    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    hm = result.payload["heatmap_14d"]
    assert len(hm["dates"]) == 14
    assert hm["hours"] == list(range(24))
    assert len(hm["cells"]) == 14
    assert all(len(row) == 24 for row in hm["cells"])


def test_heatmap_totals_match_daily(tmp_path: Path) -> None:
    """Sum of heatmap cells ≈ sum of daily_90d costs over the same 14-day window."""
    dst = tmp_path / "projects" / "sess-001.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((FIXTURES_DIR / "basic.jsonl").read_bytes())

    with _mock_dirs(tmp_path):
        result = build_snapshot_full(ParseCache())

    hm = result.payload["heatmap_14d"]
    heatmap_total = sum(v for row in hm["cells"] for v in row)

    hm_dates = set(hm["dates"])
    daily_total = sum(
        d["cost_usd"]
        for d in result.payload["daily_90d"]
        if d["date"] in hm_dates
    )

    # Heatmap cells are rounded to 4 decimal places; allow small floating error
    assert abs(heatmap_total - daily_total) < 0.01
