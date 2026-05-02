"""Tests for tokenol.persistence.forget_handoff."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from tokenol.persistence.forget_handoff import (
    ForgetRequest,
    clear_pidfile,
    pidfile_path,
    read_live_pid,
    request_path,
    submit_forget_request,
    take_forget_request,
    write_pidfile,
)


def test_pidfile_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    write_pidfile()
    assert pidfile_path().exists()
    assert read_live_pid() == os.getpid()
    clear_pidfile()
    assert not pidfile_path().exists()


def test_read_live_pid_returns_none_when_pid_dead(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    # Write a PID that almost certainly isn't running.
    pidfile_path().parent.mkdir(parents=True, exist_ok=True)
    pidfile_path().write_text("9999999")
    assert read_live_pid() is None


def test_submit_and_take_forget_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    req = ForgetRequest(
        kind="session", value="sess-abc",
        submitted_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    submit_forget_request(req)
    assert request_path().exists()

    taken = take_forget_request()
    assert taken == req
    assert not request_path().exists()  # consumed


def test_take_forget_request_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    assert take_forget_request() is None


def test_submit_is_atomic_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TOKENOL_HISTORY_DIR", str(tmp_path))
    submit_forget_request(ForgetRequest(
        kind="all", value=None,
        submitted_at=datetime.now(tz=timezone.utc),
    ))
    # Verify no leftover .tmp file.
    leftovers = list(tmp_path.glob("pending-forget*.tmp"))
    assert leftovers == []
