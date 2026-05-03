"""Tests for tokenol.ingest.discovery.select_edge_paths."""

from __future__ import annotations

import os
from pathlib import Path

from tokenol.ingest.discovery import select_edge_paths


def test_returns_all_paths_when_marks_empty(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text("")
    b = tmp_path / "b.jsonl"
    b.write_text("")
    assert sorted(select_edge_paths([a, b], {})) == sorted([a, b])


def test_keeps_paths_with_no_persisted_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text("")
    new = tmp_path / "new.jsonl"
    new.write_text("")
    # 'a' has a mark equal to its current mtime → dropped; 'new' has no mark → kept.
    marks = {a: a.stat().st_mtime_ns}
    assert select_edge_paths([a, new], marks) == [new]


def test_keeps_paths_whose_mtime_ns_changed(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text("first")
    old_mtime_ns = a.stat().st_mtime_ns
    # Force mtime change via utime
    new_mtime_ns = old_mtime_ns + 1_000_000  # +1 ms in nanoseconds
    os.utime(a, ns=(new_mtime_ns, new_mtime_ns))
    marks = {a: old_mtime_ns}
    assert select_edge_paths([a], marks) == [a]


def test_drops_paths_whose_mtime_ns_is_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text("")
    mtime_ns = a.stat().st_mtime_ns
    marks = {a: mtime_ns}
    assert select_edge_paths([a], marks) == []


def test_drops_missing_paths_silently(tmp_path: Path) -> None:
    a = tmp_path / "gone.jsonl"
    # File does not exist; provide a mark so the OSError branch is exercised.
    marks = {a: 12345}
    assert select_edge_paths([a], marks) == []
