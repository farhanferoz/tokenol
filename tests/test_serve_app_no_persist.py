"""Default-mode (`persist=False`) regression tests for tokenol serve.

These tests guarantee that `tokenol serve` without `--persist` reproduces
v0.3.2 behavior: no `import duckdb`, no `~/.tokenol/` directory created,
`app.state.history_store is None`, no flusher task. Spec:
docs/superpowers/specs/2026-05-03-opt-in-persistence-design.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_default_create_app_does_not_import_duckdb(tmp_path):
    """A subprocess that constructs the default app must not load duckdb.

    Subprocess isolation is required because the parent test process likely
    has duckdb in sys.modules already from earlier persistence tests.
    """
    snippet = (
        "import os, sys, pathlib;"
        f"os.environ['HOME']={str(tmp_path)!r};"
        "from tokenol.serve.app import create_app, ServerConfig;"
        "app = create_app(ServerConfig());"
        "assert app.state.history_store is None, 'history_store leaked';"
        "assert app.state.flush_queue is None, 'flush_queue leaked';"
        "assert 'duckdb' not in sys.modules, 'duckdb was imported';"
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_default_app_state_has_no_store(tmp_path, monkeypatch):
    """In-process check that ServerConfig() yields None store/queue."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from tokenol.serve.app import ServerConfig, create_app

    app = create_app(ServerConfig())
    assert app.state.history_store is None
    assert app.state.flush_queue is None


def test_persist_true_constructs_store(tmp_path, monkeypatch):
    """With persist=True, the store + queue are wired up and the DB file appears."""
    pytest.importorskip("duckdb")
    monkeypatch.setenv("HOME", str(tmp_path))
    from tokenol.serve.app import ServerConfig, create_app

    app = create_app(ServerConfig(persist=True))
    assert app.state.history_store is not None
    assert app.state.flush_queue is not None
    db_path = Path(tmp_path) / ".tokenol" / "history.duckdb"
    assert db_path.exists(), f"expected {db_path} to exist after create_app"


def test_default_reports_an_unreadable_store_without_failing_startup(tmp_path, monkeypatch, capsys):
    """A corrupt store must be reported, not raised — the warm tier is optional.

    Default mode opens any existing store read-only so persisted history still
    shows in the dashboard. When the file is not a DuckDB database at all,
    startup has to continue with no warm tier rather than die.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = Path(tmp_path) / ".tokenol"
    store_dir.mkdir()
    (store_dir / "history.duckdb").write_bytes(b"x" * 1024)
    from tokenol.serve.app import ServerConfig, create_app

    app = create_app(ServerConfig())
    captured = capsys.readouterr()
    assert "could not be read" in captured.err.replace("\n", "")
    assert app.state.warm_store is None
    assert app.state.history_store is None


def test_default_honours_TOKENOL_HISTORY_PATH_for_the_warm_tier(tmp_path, monkeypatch, capsys):
    """The store is located via TOKENOL_HISTORY_PATH, not just ~/.tokenol/."""
    custom_db = tmp_path / "custom" / "my_history.duckdb"
    custom_db.parent.mkdir()
    custom_db.write_bytes(b"x" * 2048)
    monkeypatch.setenv("TOKENOL_HISTORY_PATH", str(custom_db))
    # Set HOME to a clean dir so the default path definitely doesn't exist.
    monkeypatch.setenv("HOME", str(tmp_path / "clean_home"))
    from tokenol.serve.app import ServerConfig, create_app

    create_app(ServerConfig())
    captured = capsys.readouterr()
    # Rich may word-wrap long paths; collapse newlines before asserting.
    err_flat = captured.err.replace("\n", "")
    assert str(custom_db) in err_flat
