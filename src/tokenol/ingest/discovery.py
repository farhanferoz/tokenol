"""Discover JSONL files from ~/.claude* config directories."""

from __future__ import annotations

import glob
import os
from pathlib import Path


def get_config_dirs(all_projects: bool = False) -> list[Path]:
    """Return config directories to scan.

    - If *all_projects* is True, scan every ~/.claude* directory (ignoring
      CLAUDE_CONFIG_DIR). Useful when workspace isolation points the env var
      at a single project but you want a cross-project view.
    - Otherwise, if CLAUDE_CONFIG_DIR is set, honour it (single path, or
      colon- or comma-separated list of paths). Matches ccusage behaviour.
    - Otherwise, scan all ~/.claude* directories.
    """
    home = Path.home()
    all_dirs = sorted(p for p in home.glob(".claude*") if p.is_dir())

    if all_projects:
        return all_dirs

    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        parts = [p for chunk in env.split(",") for p in chunk.split(":") if p]
        return [Path(p) for p in parts]

    return all_dirs


def find_jsonl_files(dirs: list[Path] | None = None) -> list[Path]:
    """Return all JSONL files under the given (or auto-discovered) dirs."""
    if dirs is None:
        dirs = get_config_dirs()

    files: list[Path] = []
    for d in dirs:
        pattern = str(d / "projects" / "**" / "*.jsonl")
        files.extend(Path(p) for p in glob.glob(pattern, recursive=True))
    return sorted(files)


def select_edge_paths(
    paths: list[Path],
    last_mtime_ns_by_path: dict[Path, int],
) -> list[Path]:
    """Return the subset of *paths* worth re-parsing this tick.

    A path is kept when:
    - The marks dict is empty (no warm tier — caller falls back to "all"), or
    - The path has no mark (newly discovered file), or
    - The file's `st_mtime_ns` differs from the persisted mark.

    Paths whose stat() fails are dropped silently.
    """
    if not last_mtime_ns_by_path:
        return list(paths)

    kept: list[Path] = []
    for p in paths:
        mark = last_mtime_ns_by_path.get(p)
        if mark is None:
            kept.append(p)
            continue
        try:
            mtime_ns = p.stat().st_mtime_ns
        except OSError:
            continue
        if mtime_ns != mark:
            kept.append(p)
    return kept
