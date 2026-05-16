"""Core data model: Event → Turn → Session → Project."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from tokenol.enums import AssumptionTag

# Shared read-only sentinels for the "no content" case on RawEvent / Turn.
# Non-assistant events (user messages, system events) never produce tool_use
# blocks, and most Turns carry no fallback assumptions; without sharing, the
# parser would create 283 K empty Counter instances + matching empty dicts +
# 87 K empty lists, each carrying 56–80 B of per-instance overhead. Sharing
# saves ~40 MiB on a typical corpus.
#
# SAFETY: these singletons must never be mutated. The audit at
# `src/tokenol/` does not write to .tool_names / .tool_costs / .assumptions
# anywhere — only reads. The parser and `_build_turns_and_sessions` assign
# fresh Counter / dict / list when content exists, and these sentinels only
# in the empty case. If a future caller mutates a Turn's field, it must
# replace it with a fresh container first; never mutate in place.
EMPTY_TOOL_NAMES: Counter[str] = Counter()
EMPTY_TOOL_COSTS: dict[str, ToolCost] = {}
EMPTY_ASSUMPTIONS: list[AssumptionTag] = []


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(slots=True)
class ToolCost:
    """Attributed slice of a turn's cost for one tool.

    cost_usd combines the per-tool shares of all four pricing components
    (input_usd + output_usd + cache_read_usd + cache_creation_usd).
    """

    tool_name: str
    input_tokens: float = 0.0        # fractional after share split
    output_tokens: float = 0.0
    cost_usd: float = 0.0


@dataclass(slots=True)
class RawEvent:
    """One parsed line from a JSONL file, after type filtering."""

    # Provenance
    source_file: str
    line_number: int

    # Identity
    event_type: str           # "assistant", "user", "system", …
    session_id: str
    request_id: str | None
    message_id: str | None    # message.id (Anthropic UUID)
    uuid: str | None          # event-level uuid

    # Timing
    timestamp: datetime

    # Token accounting (None = interrupted / no billing data)
    usage: Usage | None

    # Model
    model: str | None

    # Structural flags
    is_sidechain: bool
    stop_reason: str | None

    # Tool counts (parsed from message.content)
    tool_use_count: int = 0
    tool_error_count: int = 0

    # Tool names (parsed from message.content tool_use blocks)
    tool_names: Counter[str] = field(default_factory=Counter)

    # Working directory (from system events)
    cwd: str | None = None

    # Per-tool cost attribution. Token counts are float (fractional after share split).
    tool_costs: dict[str, ToolCost] = field(default_factory=dict)
    unattributed_input_tokens: float = 0.0
    unattributed_output_tokens: float = 0.0
    unattributed_cost_usd: float = 0.0


@dataclass(slots=True)
class Turn:
    """One deduplicated assistant response."""

    dedup_key: str            # message_id:request_id (or passthrough)
    timestamp: datetime
    session_id: str
    model: str | None
    usage: Usage
    is_sidechain: bool
    stop_reason: str | None
    assumptions: list[AssumptionTag] = field(default_factory=list)
    cost_usd: float = 0.0
    is_interrupted: bool = False
    tool_use_count: int = 0
    tool_error_count: int = 0
    tool_names: Counter[str] = field(default_factory=Counter)

    # Per-tool cost attribution. Token counts are float (fractional after share split).
    tool_costs: dict[str, ToolCost] = field(default_factory=dict)
    unattributed_input_tokens: float = 0.0
    unattributed_output_tokens: float = 0.0
    unattributed_cost_usd: float = 0.0


@dataclass(slots=True)
class Session:
    """All turns from one JSONL file (one sessionId)."""

    session_id: str
    source_file: str
    is_sidechain: bool
    cwd: str | None = None
    turns: list[Turn] = field(default_factory=list)
    archived: bool = False

    @property
    def total_cost(self) -> float:
        return sum(t.cost_usd for t in self.turns)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.usage.output_tokens for t in self.turns)

    @property
    def total_input_tokens(self) -> int:
        return sum(t.usage.input_tokens for t in self.turns)

    @property
    def total_cache_read(self) -> int:
        return sum(t.usage.cache_read_input_tokens for t in self.turns)

    @property
    def total_cache_creation(self) -> int:
        return sum(t.usage.cache_creation_input_tokens for t in self.turns)


@dataclass(slots=True)
class Project:
    """Aggregation of sessions under one config directory."""

    config_dir: str
    sessions: list[Session] = field(default_factory=list)

    @property
    def all_turns(self) -> list[Turn]:
        return [t for s in self.sessions for t in s.turns]
