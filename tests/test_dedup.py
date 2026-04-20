"""Dedup regression: message.id:requestId compound key."""

from pathlib import Path

from tokenol.ingest.builder import build_turns

FIXTURES = Path(__file__).parent / "fixtures"


def test_dedup_basic_no_duplicates():
    """basic.jsonl has 2 distinct events — should yield 2 turns."""
    turns = build_turns([FIXTURES / "basic.jsonl"])
    assert len(turns) == 2


def test_dedup_collapses_duplicate():
    """dedup.jsonl has 2 events with same key — should yield 1 turn."""
    turns = build_turns([FIXTURES / "dedup.jsonl"])
    assert len(turns) == 1


def test_dedup_unique_keys():
    turns = build_turns([FIXTURES / "basic.jsonl"])
    keys = [t.dedup_key for t in turns]
    assert len(keys) == len(set(keys))


def test_interrupted_turn_zero_cost():
    turns = build_turns([FIXTURES / "interrupted.jsonl"])
    assert len(turns) == 1
    assert turns[0].cost_usd == 0.0
    assert turns[0].usage.output_tokens == 0


def test_cost_tags_reach_recorder():
    """Regression: tags produced by cost_for_turn must reach the recorder footer."""
    from tokenol import assumptions as rec
    from tokenol.enums import AssumptionTag

    rec.reset()
    build_turns([FIXTURES / "gemini.jsonl"])
    assert rec.fired().get(AssumptionTag.GEMINI_UNPRICED, 0) == 1
