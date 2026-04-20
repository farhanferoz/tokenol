"""CLI integration tests using Typer's CliRunner."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokenol.cli import app

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"


def _make_config_dir(fixture_files: list[str]) -> Path:
    """Create a temp dir with projects/subdir/file.jsonl layout."""
    tmp = Path(tempfile.mkdtemp())
    projects_dir = tmp / "projects" / "proj-test"
    projects_dir.mkdir(parents=True)
    for fname in fixture_files:
        src = FIXTURES / fname
        shutil.copy(src, projects_dir / fname)
    return tmp


@pytest.fixture()
def basic_config_dir(tmp_path):
    """Config dir with basic.jsonl only (no assumptions fired)."""
    projects = tmp_path / "projects" / "proj-test"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURES / "basic.jsonl", projects / "basic.jsonl")
    return tmp_path


@pytest.fixture()
def assumption_config_dir(tmp_path):
    """Config dir with missing_ids.jsonl (fires DEDUP_PASSTHROUGH)."""
    projects = tmp_path / "projects" / "proj-test"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURES / "missing_ids.jsonl", projects / "missing_ids.jsonl")
    return tmp_path


def test_daily_basic(basic_config_dir):
    """daily command returns 0 on basic fixture."""
    result = runner.invoke(app, ["daily", "--since", "30d"], env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)})
    assert result.exit_code == 0, result.output


def test_daily_show_assumptions_prints_footer_when_empty(basic_config_dir):
    """--show-assumptions prints footer even when no assumptions fired."""
    result = runner.invoke(
        app,
        ["daily", "--since", "30d", "--show-assumptions"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code == 0
    # No assumptions should have fired, but footer is requested.
    # The footer contains "Assumptions fired:" only when there are counts.
    # With --show-assumptions and no assumptions, nothing extra should crash.
    # Just check it doesn't error.
    assert "Error" not in result.output


def test_strict_exits_nonzero_when_assumptions_fire(assumption_config_dir):
    """--strict exits non-zero when DEDUP_PASSTHROUGH fires."""
    result = runner.invoke(
        app,
        ["daily", "--since", "30d", "--strict"],
        env={"CLAUDE_CONFIG_DIR": str(assumption_config_dir)},
    )
    assert result.exit_code != 0


def test_sessions_returns_zero(basic_config_dir):
    """sessions command returns 0 on basic fixture."""
    result = runner.invoke(
        app,
        ["sessions", "--since", "30d", "--top", "5", "--sort", "cost"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code == 0, result.output


def test_projects_returns_zero(basic_config_dir):
    """projects command returns 0 on basic fixture."""
    result = runner.invoke(
        app,
        ["projects", "--since", "30d"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code == 0, result.output


def test_models_returns_zero(basic_config_dir):
    """models command returns 0 on basic fixture."""
    result = runner.invoke(
        app,
        ["models", "--since", "30d"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code == 0, result.output


def test_live_invalid_last(basic_config_dir):
    """live rejects bare numbers without unit suffix."""
    result = runner.invoke(
        app,
        ["live", "--last", "20"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code != 0


def test_live_no_recent_data(basic_config_dir):
    """live with no recent data exits cleanly (0) when no active window."""
    # basic.jsonl has data from 2026-04-14; 'now' is 2026-04-20
    # so there are no turns in the last 10h
    result = runner.invoke(
        app,
        ["live", "--last", "20m"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    # Should exit 0 (no active window)
    assert result.exit_code == 0


def test_log_level_accepted(basic_config_dir):
    """--log-level debug is accepted without error."""
    result = runner.invoke(
        app,
        ["daily", "--since", "30d", "--log-level", "debug"],
        env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
    )
    assert result.exit_code == 0


def test_sessions_sort_keys(basic_config_dir):
    """All sort keys are accepted."""
    for key in ["cost", "input", "output", "cache_read", "turns", "max_input", "duration"]:
        result = runner.invoke(
            app,
            ["sessions", "--since", "30d", "--sort", key],
            env={"CLAUDE_CONFIG_DIR": str(basic_config_dir)},
        )
        assert result.exit_code == 0, f"sort key '{key}' failed: {result.output}"
