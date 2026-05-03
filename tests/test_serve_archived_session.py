"""End-to-end: JSONL deletion preserves dashboard data via the persistent store."""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

import json
from contextlib import contextmanager
from pathlib import Path

import tokenol.ingest.discovery as _disc_mod
import tokenol.serve.state as _state_mod
from tokenol.persistence.store import HistoryStore
from tokenol.serve.state import ParseCache, build_snapshot_full


def _write_session(proj_dir: Path, sid: str, cwd: str, model: str, ts_iso: str, uid: str) -> None:
    sys_ev = json.dumps({
        "type": "system", "timestamp": ts_iso, "sessionId": sid,
        "uuid": f"sys-{uid}", "isSidechain": False, "cwd": cwd,
    })
    asst_ev = json.dumps({
        "type": "assistant", "timestamp": ts_iso, "sessionId": sid,
        "requestId": f"req-{uid}", "uuid": f"evt-{uid}", "isSidechain": False,
        "model": model,
        "message": {
            "id": f"msg-{uid}", "role": "assistant", "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
            },
        },
    })
    (proj_dir / f"{sid}.jsonl").write_text(sys_ev + "\n" + asst_ev + "\n")


@contextmanager
def _mock_dirs(claude_root: Path):
    """Patch get_config_dirs in both state and discovery modules to return *claude_root*."""
    original_state = _state_mod.get_config_dirs
    original_disc = _disc_mod.get_config_dirs
    _state_mod.get_config_dirs = lambda all_projects=False: [claude_root]
    _disc_mod.get_config_dirs = lambda all_projects=False: [claude_root]
    try:
        yield
    finally:
        _state_mod.get_config_dirs = original_state
        _disc_mod.get_config_dirs = original_disc


def test_jsonl_deletion_preserves_snapshot(tmp_path: Path) -> None:
    """End-to-end: delete a JSONL between two snapshots, dashboard stays the same.

    Verifies the headline claim of PR 1: once turns are persisted, deleting their
    source JSONL files is invisible to the quantitative dashboard payload.
    """
    claude_root = tmp_path / "claude"
    proj = claude_root / "projects" / "p1"
    proj.mkdir(parents=True)
    _write_session(proj, "sid-A", "/proj/a", "claude-sonnet-4-6", "2026-05-01T12:00:00Z", "1")
    _write_session(proj, "sid-B", "/proj/b", "claude-opus-4-7",   "2026-05-01T13:00:00Z", "2")

    store = HistoryStore(tmp_path / "h.duckdb")
    # Wide hot window so both turns hydrate into memory after deletion.
    store._hot_window_days = 365

    try:
        with _mock_dirs(claude_root):
            cache = ParseCache()
            r1 = build_snapshot_full(cache, history_store=store)
            # Force-flush whatever the broadcaster would normally batch.
            store.flush(
                turns=cache._hot_turns,
                sessions=list(cache._hot_sessions_by_id.values()),
            )

        # Delete one JSONL — sid-A should become archived; sid-B stays live.
        (proj / "sid-A.jsonl").unlink()

        with _mock_dirs(claude_root):
            cache2 = ParseCache()
            r2 = build_snapshot_full(cache2, history_store=store)

        # Quantitative payload sections must match exactly.
        for k in ("topbar_summary", "tiles", "models", "recent_activity"):
            assert r1.payload[k] == r2.payload[k], f"divergence in {k}"

        # sid-A is archived (no live JSONL); sid-B is not.
        sids = {s.session_id: s for s in r2.sessions}
        assert sids["sid-A"].archived is True, "sid-A should be archived after JSONL deletion"
        assert sids["sid-B"].archived is False, "sid-B has live JSONL — must not be archived"
    finally:
        store.close()
