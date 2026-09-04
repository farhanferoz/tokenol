"""A second tokenol must fail with a sentence, not a DuckDB traceback.

DuckDB takes an exclusive, cross-process lock on the store file, so a second
`tokenol serve --persist` cannot corrupt or duplicate anything -- it dies at
connect. The problem this guards is ergonomic: without translation the user
gets a 40-line traceback ending in `IOException: IO Error: Could not set lock
on file ...`, which does not say "tokenol is already running" and does not
tell them what to do about it.

The lock is only taken across processes -- two connections inside one process
are allowed -- so every test here holds the lock from a real subprocess.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

duckdb = pytest.importorskip("duckdb")

from tokenol.persistence.store import HistoryStore, StoreLockedError  # noqa: E402


def _hold_lock(path, ready_token="LOCKED"):
    """Spawn a process that opens `path` read-write and holds it until killed."""
    code = textwrap.dedent(f"""
        import duckdb, sys, time
        con = duckdb.connect({str(path)!r})
        con.execute("CREATE TABLE IF NOT EXISTS t(x INT)")
        print({ready_token!r}, flush=True)
        time.sleep(120)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if ready_token in line:
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"lock holder died: {proc.stderr.read()}")
    proc.kill()
    raise RuntimeError("lock holder never became ready")


@pytest.fixture
def locked_store(tmp_path):
    path = tmp_path / "history.duckdb"
    proc = _hold_lock(path)
    try:
        yield path, proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_opening_a_locked_store_raises_store_locked_error(locked_store):
    path, _ = locked_store
    with pytest.raises(StoreLockedError):
        HistoryStore(path=path)


def test_store_locked_error_names_the_holding_pid(locked_store):
    path, holder_pid = locked_store
    with pytest.raises(StoreLockedError) as exc:
        HistoryStore(path=path)
    assert exc.value.pid == holder_pid


def test_store_locked_error_message_is_actionable(locked_store):
    path, holder_pid = locked_store
    with pytest.raises(StoreLockedError) as exc:
        HistoryStore(path=path)
    msg = str(exc.value)
    assert str(holder_pid) in msg
    assert str(path) in msg
    # It must say what to do, not merely what failed.
    assert "already running" in msg.lower()


def test_read_only_open_of_a_write_locked_store_also_raises(locked_store):
    """DuckDB refuses even a reader while a writer holds the lock."""
    path, _ = locked_store
    with pytest.raises(StoreLockedError):
        HistoryStore(path=path, read_only=True)


def test_non_lock_io_errors_are_not_swallowed(tmp_path, monkeypatch):
    """Only lock conflicts translate; other IO errors must surface unchanged."""
    from tokenol.persistence import store as store_mod

    def _boom(*a, **k):
        raise duckdb.IOException("IO Error: disk is on fire")

    monkeypatch.setattr(store_mod.duckdb, "connect", _boom)
    with pytest.raises(duckdb.IOException) as exc:
        HistoryStore(path=tmp_path / "h.duckdb")
    assert "disk is on fire" in str(exc.value)
    assert not isinstance(exc.value, StoreLockedError)


# ---- CLI surface -------------------------------------------------------------
# The store-level error is only half the fix: `tokenol serve --persist` must
# turn it into a message and a non-zero exit, never let it escape as a traceback.
#
# NOTE these assertions are deliberately strict about HOW it exits. CliRunner
# reports exit_code 1 for ANY unhandled exception and never prints "Traceback"
# into result.output, so asserting only on those two passes even with no
# handling at all. The real contract is: the exception must not escape the
# command, and the explanation must reach the user's terminal.


def test_serve_persist_refuses_cleanly_when_store_is_locked(locked_store, monkeypatch):
    from typer.testing import CliRunner

    from tokenol.cli import app as cli_app

    path, holder_pid = locked_store
    monkeypatch.setenv("TOKENOL_HISTORY_PATH", str(path))
    result = CliRunner().invoke(cli_app, ["serve", "--persist"])

    assert result.exit_code == 1, result.output
    # Must be handled, not propagated: anything but a clean SystemExit means
    # the user saw a traceback.
    assert not isinstance(result.exception, StoreLockedError), "error escaped the CLI"
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # And the explanation must actually be printed.
    assert "already running" in result.output.lower(), result.output
    assert str(holder_pid) in result.output


# Deliberately NOT tested here: the same-port case. uvicorn binds the socket,
# catches EADDRINUSE itself and raises SystemExit(1), so tokenol never sees an
# OSError to translate. A test that monkeypatches uvicorn.run into raising one
# proves only that the mock was called -- it passed against a guard that could
# never fire in production. uvicorn's own "address already in use" line is the
# real user-facing behaviour.
