"""SSE streaming generator with idle back-off and shallow-diff payloads."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable

from tokenol.serve.state import ParseCache, SnapshotResult, build_snapshot_full

log = logging.getLogger(__name__)


def _shallow_diff(prev: dict, curr: dict) -> dict:
    """Return only top-level keys whose values changed."""
    return {k: v for k, v in curr.items() if prev.get(k) != v}


async def snapshot_stream(
    parse_cache: ParseCache,
    all_projects: bool,
    reference_usd: float,
    get_tick_seconds: Callable[[], int],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings.

    First message: full snapshot.
    Subsequent messages: shallow diff of changed top-level keys.
    Idle back-off: after 30 s with no changes, tick × 3 (min 15 s). Resets on change.
    """
    prev_payload: dict | None = None
    last_change_ts = time.monotonic()
    IDLE_THRESHOLD = 30.0

    while True:
        tick = int(get_tick_seconds())
        idle_seconds = time.monotonic() - last_change_ts

        effective_tick = max(tick * 3, 15) if idle_seconds >= IDLE_THRESHOLD else tick

        try:
            result: SnapshotResult = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda t=tick: build_snapshot_full(parse_cache, all_projects, reference_usd, t),
            )
        except Exception:
            log.exception("snapshot build failed — skipping tick")
            await asyncio.sleep(effective_tick)
            continue
        curr = result.payload

        data = curr if prev_payload is None else _shallow_diff(prev_payload, curr)

        if data:
            last_change_ts = time.monotonic()
            yield f"data: {json.dumps(data)}\n\n"

        prev_payload = curr
        await asyncio.sleep(effective_tick)
