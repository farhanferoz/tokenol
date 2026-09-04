"""A growing session file must not accumulate one cache entry per append.

ParseCache keys entries on (path, size, mtime_ns), so every append to a live
session file mints a new key. Nothing removed the old one: `purge()` is the only
eviction and it runs solely on the non-store derivation path, which store-backed
servers never take. The retained value is the file's entire parsed event list, so
the leak grows with session activity rather than with corpus size.

Measured 2026-09-04: a plain `tokenol serve` on a real corpus grew from 1.71 GB
at 3.6 minutes to 2.2 GB at 42 minutes on identical load.
"""

from __future__ import annotations

from pathlib import Path

from tokenol.serve.state import ParseCache

FIXTURE = Path(__file__).parent / "fixtures" / "basic.jsonl"


def _grow(tmp_path: Path) -> tuple[Path, str]:
    """A JSONL file plus one line that can be appended to it repeatedly."""
    src = FIXTURE.read_text().splitlines()
    target = tmp_path / "sess-grow.jsonl"
    target.write_text("\n".join(src) + "\n")
    return target, src[0] + "\n"


def test_appending_does_not_accumulate_cache_entries(tmp_path: Path) -> None:
    target, line = _grow(tmp_path)
    cache = ParseCache()

    for _ in range(5):
        cache.get_or_parse(target)
        with target.open("a") as fh:
            fh.write(line)

    assert cache.size == 1, f"one file left {cache.size} cache entries after 5 appends"


def test_eviction_keeps_the_current_version_readable(tmp_path: Path) -> None:
    """Evicting stale versions must not disturb the entry the caller just got."""
    target, line = _grow(tmp_path)
    cache = ParseCache()

    _key, first = cache.get_or_parse(target)
    first_count = len(first)
    with target.open("a") as fh:
        fh.write(line)

    key, second = cache.get_or_parse(target)
    assert cache.size == 1
    assert len(second) > first_count, "appended line was not parsed"
    # The returned key must still resolve inside the cache — get_derived looks
    # entries up by key and silently drops any it cannot find.
    assert cache._store.get(key) is second


def test_distinct_paths_are_kept_independently(tmp_path: Path) -> None:
    """Eviction is per-path; a second file must not be collateral damage."""
    a, line = _grow(tmp_path)
    b = tmp_path / "sess-other.jsonl"
    b.write_text(a.read_text())

    cache = ParseCache()
    cache.get_or_parse(a)
    cache.get_or_parse(b)
    with a.open("a") as fh:
        fh.write(line)
    cache.get_or_parse(a)

    assert cache.size == 2, "evicting one path's stale entry dropped another path"
