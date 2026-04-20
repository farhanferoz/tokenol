"""AssumptionTag recorder — collects fired heuristics for report footer."""

from __future__ import annotations

from collections import Counter

from tokenol.enums import AssumptionTag


class AssumptionRecorder:
    """Thread-unsafe (single-threaded CLI use only) assumption counter."""

    def __init__(self) -> None:
        self._counts: Counter[AssumptionTag] = Counter()

    def record(self, tags: list[AssumptionTag]) -> None:
        for t in tags:
            self._counts[t] += 1

    def fired(self) -> dict[AssumptionTag, int]:
        return dict(self._counts)

    def summary_lines(self) -> list[str]:
        if not self._counts:
            return []
        lines = ["Assumptions fired:"]
        for tag, count in sorted(self._counts.items(), key=lambda x: x[0].value):
            lines.append(f"  {tag.value}: {count:,} event(s)")
        return lines

    def reset(self) -> None:
        self._counts.clear()


_recorder = AssumptionRecorder()


def record(tags: list[AssumptionTag]) -> None:
    _recorder.record(tags)


def fired() -> dict[AssumptionTag, int]:
    return _recorder.fired()


def summary_lines() -> list[str]:
    return _recorder.summary_lines()


def reset() -> None:
    _recorder.reset()
