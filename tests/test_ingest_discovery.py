"""Tests for tokenol.ingest.discovery.select_edge_paths."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenol.ingest.discovery import select_edge_paths


def _touch(p: Path, *, seconds_ago: float) -> None:
    p.write_text("")
    ts = (datetime.now(tz=timezone.utc).timestamp()) - seconds_ago
    os.utime(p, (ts, ts))


def test_returns_all_paths_when_marks_empty(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    _touch(a, seconds_ago=10)
    b = tmp_path / "b.jsonl"
    _touch(b, seconds_ago=10)
    assert sorted(select_edge_paths([a, b], {})) == sorted([a, b])


def test_keeps_paths_with_no_persisted_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    _touch(a, seconds_ago=10)
    new = tmp_path / "new.jsonl"
    _touch(new, seconds_ago=10)
    marks = {"a": datetime.now(tz=timezone.utc)}
    # 'new' has no mark → kept; 'a' is older than its mark → dropped.
    assert select_edge_paths([a, new], marks) == [new]


def test_keeps_paths_newer_than_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    _touch(a, seconds_ago=10)
    marks = {"a": datetime.now(tz=timezone.utc) - timedelta(hours=1)}
    assert select_edge_paths([a], marks) == [a]


def test_drops_paths_older_than_mark(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    _touch(a, seconds_ago=3600)  # 1-hour-old file
    marks = {"a": datetime.now(tz=timezone.utc)}  # mark = now
    assert select_edge_paths([a], marks) == []


def test_session_id_is_filename_stem(tmp_path: Path) -> None:
    p = tmp_path / "abc-123.jsonl"
    _touch(p, seconds_ago=10)
    # Mark keyed by 'abc-123' (the stem) drops the file.
    marks = {"abc-123": datetime.now(tz=timezone.utc)}
    assert select_edge_paths([p], marks) == []
