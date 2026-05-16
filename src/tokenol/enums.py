from enum import Enum


class BlockType(str, Enum):
    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    REDACTED_THINKING = "redacted_thinking"
    OTHER = "other"


class BlowUpVerdict(str, Enum):
    OK = "OK"
    CONTEXT_CREEP = "CONTEXT_CREEP"
    SIDECHAIN_HEAVY = "SIDECHAIN_HEAVY"
    TOOL_ERROR_STORM = "TOOL_ERROR_STORM"
    RUNAWAY_WINDOW = "RUNAWAY_WINDOW"


class AssumptionTag(str, Enum):
    WINDOW_BOUNDARY_HEURISTIC = "WINDOW_BOUNDARY_HEURISTIC"
    UNKNOWN_MODEL_FALLBACK = "UNKNOWN_MODEL_FALLBACK"
    DEDUP_PASSTHROUGH = "DEDUP_PASSTHROUGH"
    INTERRUPTED_TURN_SKIPPED = "INTERRUPTED_TURN_SKIPPED"
    GEMINI_UNPRICED = "GEMINI_UNPRICED"


class AttributionMode(str, Enum):
    PRORATA = "prorata"
    EXCL_CACHE_READ = "excl_cache_read"
