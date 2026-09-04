"""A snapshot tick must not re-scan the filesystem the SSE gate just scanned.

The broadcaster decides whether to build by globbing every JSONL and stat'ing it
(`compute_active_keys`). `build_snapshot_full` then globbed the same ~3,900 files
again, and `select_edge_paths` stat'ed each of them a third time — all to recover
information the gate already held. Profiling a live server put `find_jsonl_files`
at roughly a fifth of remaining CPU, almost entirely inside `glob._iterdir`.

Passing the gate's key set through removes the second glob and the third stat:
the key is (path, size, mtime_ns), so the mtimes edge selection needs are in it.
"""

from __future__ import annotations

from pathlib import Path

from tokenol.serve import state as state_mod
from tokenol.serve.state import ParseCache, build_snapshot_full, compute_active_keys

FIXTURES = Path(__file__).parent / "fixtures"


def _corpus(tmp_path: Path) -> Path:
    proj = tmp_path / "projects"
    proj.mkdir(parents=True)
    (proj / "sess-001.jsonl").write_bytes((FIXTURES / "basic.jsonl").read_bytes())
    return tmp_path


def test_passing_active_keys_skips_the_second_glob(tmp_path, monkeypatch) -> None:
    root = _corpus(tmp_path)
    monkeypatch.setattr(state_mod, "get_config_dirs", lambda all_projects=False: [root])

    calls = 0
    real = state_mod.find_jsonl_files

    def counting(dirs=None):
        nonlocal calls
        calls += 1
        return real(dirs)

    monkeypatch.setattr(state_mod, "find_jsonl_files", counting)

    keys = compute_active_keys(all_projects=False)
    calls = 0  # the gate's own scan is expected; count only what the build adds
    build_snapshot_full(ParseCache(), all_projects=False, active_keys=keys)
    assert calls == 0, f"build re-globbed the corpus {calls} time(s) despite being handed the keys"


def test_result_is_identical_with_and_without_precomputed_keys(tmp_path, monkeypatch) -> None:
    """The optimisation must be invisible in the output."""
    root = _corpus(tmp_path)
    monkeypatch.setattr(state_mod, "get_config_dirs", lambda all_projects=False: [root])

    keys = compute_active_keys(all_projects=False)
    with_keys = build_snapshot_full(ParseCache(), all_projects=False, active_keys=keys)
    without = build_snapshot_full(ParseCache(), all_projects=False)

    assert len(with_keys.turns) == len(without.turns)
    assert [t.dedup_key for t in with_keys.turns] == [t.dedup_key for t in without.turns]
    assert {s.session_id for s in with_keys.sessions} == {s.session_id for s in without.sessions}


def test_new_file_is_still_picked_up_on_the_next_tick(tmp_path, monkeypatch) -> None:
    """Reusing the gate's keys must not make the build blind to new work."""
    root = _corpus(tmp_path)
    monkeypatch.setattr(state_mod, "get_config_dirs", lambda all_projects=False: [root])

    cache = ParseCache()
    first = build_snapshot_full(cache, all_projects=False, active_keys=compute_active_keys(all_projects=False))

    # Distinct ids: a byte copy would dedup against the first file and prove nothing.
    second_text = (
        (FIXTURES / "basic.jsonl")
        .read_text()
        .replace("-aaa", "-zz1")
        .replace("-bbb", "-zz2")
        .replace("evt-0", "evt-9")
        .replace("sess-001", "sess-002")
    )
    (root / "projects" / "sess-002.jsonl").write_text(second_text)
    second = build_snapshot_full(cache, all_projects=False, active_keys=compute_active_keys(all_projects=False))

    assert len(second.turns) > len(first.turns), "a newly added file was not picked up"
