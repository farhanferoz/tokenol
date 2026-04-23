"""Unit tests for serve/prefs.py."""

from __future__ import annotations

from pathlib import Path

from tokenol.metrics.thresholds import DEFAULTS
from tokenol.serve.prefs import Preferences


def test_roundtrip(tmp_path: Path) -> None:
    """Load → mutate → save → reload gives same values."""
    p = tmp_path / "prefs.json"
    prefs = Preferences(tick_seconds=10, reference_usd=25.0)
    prefs.thresholds["hit_rate_good_pct"] = 99
    prefs.save(p)

    loaded = Preferences.load(p)
    assert loaded.tick_seconds == 10
    assert loaded.reference_usd == 25.0
    assert loaded.thresholds["hit_rate_good_pct"] == 99


def test_defaults_on_missing_file(tmp_path: Path) -> None:
    """Missing file → default Preferences, no exception."""
    prefs = Preferences.load(tmp_path / "nonexistent.json")
    assert prefs.tick_seconds == 5
    assert prefs.reference_usd == 50.0
    assert prefs.thresholds == dict(DEFAULTS)


def test_defaults_on_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON → default Preferences, no exception."""
    p = tmp_path / "prefs.json"
    p.write_text("not json {{{")
    prefs = Preferences.load(p)
    assert prefs.tick_seconds == 5
    assert prefs.thresholds == dict(DEFAULTS)


def test_to_dict_shape() -> None:
    """to_dict includes all three top-level keys."""
    d = Preferences().to_dict()
    assert set(d.keys()) == {"tick_seconds", "reference_usd", "thresholds"}


def test_load_merges_new_defaults(tmp_path: Path) -> None:
    """Unknown threshold keys in saved file are ignored; missing defaults are filled in."""
    p = tmp_path / "prefs.json"
    p.write_text('{"thresholds": {"hit_rate_good_pct": 99, "future_key": 123}}')
    prefs = Preferences.load(p)
    assert prefs.thresholds["hit_rate_good_pct"] == 99
    assert "future_key" not in prefs.thresholds
    # All DEFAULTS keys are present.
    for key in DEFAULTS:
        assert key in prefs.thresholds


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    """save() creates parent directories if they don't exist."""
    p = tmp_path / "a" / "b" / "prefs.json"
    Preferences().save(p)
    assert p.exists()
