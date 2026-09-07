"""Per-file parse marks: `{absolute JSONL path: st_mtime_ns}` as a JSON sidecar.

`select_edge_paths` skips a file whose mtime matches its mark. The marks used
to live only in memory, so every cold start re-parsed the whole corpus to
rediscover what the store already held — 5,120 files and over five minutes on
the live server, for a few thousand genuinely new turns. Persisting them turns
that into a stat() sweep.

Safety rule, enforced by the caller (`state._store_backed_derivation`): a mark
is saved only when the flush queue reports every enqueued turn WRITTEN. Then a
crash at any moment leaves either no mark (the file is re-parsed on the next
start, exactly as before) or a mark whose turns are already in the store. And
marks are used only under --persist: plain `serve` has no writer, so skipping a
file there would drop its turns entirely.

Lives next to the pidfile (`~/.tokenol/`, or TOKENOL_HISTORY_DIR).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from tokenol.persistence.forget_handoff import base_dir

MARKS_FILENAME = "parse-marks.json"


def marks_path() -> Path:
    return base_dir() / MARKS_FILENAME


def load_marks() -> dict[Path, int]:
    """Marks from disk; `{}` when absent, unreadable or malformed.

    An empty result means "parse everything", which is the pre-existing
    behaviour — so a corrupt sidecar costs a slow start, never wrong data.
    """
    p = marks_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[Path, int] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, int):
            out[Path(k)] = v
    return out


def save_marks(marks: dict[Path, int]) -> None:
    """Atomic write via tempfile + rename, same pattern as submit_forget_request."""
    p = marks_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="parse-marks", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({str(k): v for k, v in marks.items()}, f, separators=(",", ":"))
        Path(tmp).replace(p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clear_marks() -> None:
    with contextlib.suppress(FileNotFoundError):
        marks_path().unlink()
