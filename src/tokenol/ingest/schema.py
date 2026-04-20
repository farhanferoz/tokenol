"""Schema version pass-through — version surfaced as metadata, not dispatch."""

from __future__ import annotations


def extract_schema_version(event: dict) -> str | None:
    """Return the Claude Code version string from a system event, if present."""
    if event.get("type") == "system":
        return event.get("version")
    return None
