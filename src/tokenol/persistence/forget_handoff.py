"""Pidfile + request-file handshake between `tokenol forget` and a live serve.

The CLI uses `read_live_pid()` to detect a running serve; if found it writes
the request via `submit_forget_request(...)` and exits. The serve broadcaster
calls `take_forget_request()` once per tick to consume any pending requests.
Both the pidfile and the request file live under `~/.tokenol/` (or the path
indicated by the `TOKENOL_HISTORY_DIR` env var, used by tests).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


def base_dir() -> Path:
    env = os.environ.get("TOKENOL_HISTORY_DIR")
    if env:
        return Path(env)
    return Path.home() / ".tokenol"


def pidfile_path() -> Path:
    return base_dir() / "serve.pid"


def request_path() -> Path:
    return base_dir() / "pending-forget.json"


@dataclass(frozen=True)
class ForgetRequest:
    kind: Literal["session", "project", "older_than", "all"]
    value: str | None  # session_id / cwd / ISO duration string / None for "all"
    submitted_at: datetime


# ---- pidfile -----------------------------------------------------------------

def write_pidfile() -> None:
    """Write current PID to the pidfile, creating the directory if needed."""
    p = pidfile_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(str(os.getpid()))


def clear_pidfile() -> None:
    with contextlib.suppress(FileNotFoundError):
        pidfile_path().unlink()


def read_live_pid() -> int | None:
    """Return PID from the pidfile if it points to a live process; else None.

    A stale pidfile (e.g. from a crashed serve) is treated as no-live-serve.
    """
    p = pidfile_path()
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


# ---- request file ------------------------------------------------------------

def submit_forget_request(req: ForgetRequest) -> None:
    """Atomically write the request via tempfile + rename."""
    p = request_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {**asdict(req), "submitted_at": req.submitted_at.isoformat()}
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="pending-forget", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        Path(tmp).replace(p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def take_forget_request() -> ForgetRequest | None:
    """Read + delete the pending-forget file; returns None if absent or unparsable."""
    p = request_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        req = ForgetRequest(
            kind=data["kind"],
            value=data.get("value"),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
        )
    except (OSError, ValueError, KeyError):
        # Malformed request — drop it so a misformed file doesn't wedge the loop.
        with contextlib.suppress(OSError):
            p.unlink()
        return None
    with contextlib.suppress(OSError):
        p.unlink()
    return req
