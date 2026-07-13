"""Shared test fixtures.

All `tests/fixtures/*.jsonl` files carry a hardcoded absolute event timestamp
(currently: most at 2026-04-14, `per_tool_basic.jsonl` at 2026-05-15,
`skills.jsonl` at 2026-06-10) — they do NOT move with wall-clock time.

This has caused two separate CI breaks (2026-06-05, 2026-07-13): a test hit a
windowed endpoint/range (default range, `7d`/`14d`/`30d`/`90d`/`today`, or any
`since`-cutoff computation from `date.today()`) against one of these fixtures,
passed for weeks, then started failing/emptying out once the real calendar
date moved past the window relative to the fixture's fixed date.

When writing a NEW test that pairs a static fixture with range/window logic:
  - If the test only cares about panel *shape* (not that data is present),
    a stale fixture is harmless.
  - If the test asserts data IS present, either pass `range=all` (or the
    endpoint's all-time equivalent) so the fixed date can't age out, or build
    the event(s) with a timestamp relative to `date.today()` (see
    `test_daily_insufficient_history` / `test_project_detail_default_range_14d`
    for the pattern) instead of reusing a fixture file.
  - If the test asserts data is ABSENT/empty for being outside the window
    (e.g. `test_breakdown_tools_empty_window`), a stale fixture is safe by
    construction — it only gets more clearly outside the window over time.
"""

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
