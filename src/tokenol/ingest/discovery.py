"""Discover JSONL files from ~/.claude* config directories."""

from __future__ import annotations

import glob
import os
from pathlib import Path


def get_config_dirs() -> list[Path]:
    """Return config directories to scan.

    If CLAUDE_CONFIG_DIR is set, use only that directory (matching ccusage
    behavior). Otherwise, scan all ~/.claude* directories.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return [Path(env)]

    home = Path.home()
    return sorted(p for p in home.glob(".claude*") if p.is_dir())


def find_jsonl_files(dirs: list[Path] | None = None) -> list[Path]:
    """Return all JSONL files under the given (or auto-discovered) dirs."""
    if dirs is None:
        dirs = get_config_dirs()

    files: list[Path] = []
    for d in dirs:
        pattern = str(d / "projects" / "**" / "*.jsonl")
        files.extend(Path(p) for p in glob.glob(pattern, recursive=True))
    return sorted(files)
