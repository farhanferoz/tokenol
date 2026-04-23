"""User preferences: tick cadence, reference cost, and metric thresholds."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tokenol.metrics.thresholds import DEFAULTS

log = logging.getLogger(__name__)


def default_path() -> Path:
    """Return $XDG_CONFIG_HOME/tokenol/prefs.json (fallback ~/.config/...)."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(xdg) / "tokenol" / "prefs.json"


@dataclass
class Preferences:
    tick_seconds: int = 5
    reference_usd: float = 50.0
    thresholds: dict = field(default_factory=lambda: dict(DEFAULTS))

    @classmethod
    def load(cls, path: Path) -> Preferences:
        """Return defaults if path is missing or contents are malformed."""
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            prefs = cls()
            if "tick_seconds" in data:
                prefs.tick_seconds = int(data["tick_seconds"])
            if "reference_usd" in data:
                prefs.reference_usd = float(data["reference_usd"])
            if "thresholds" in data and isinstance(data["thresholds"], dict):
                prefs.thresholds = {k: data["thresholds"].get(k, v) for k, v in DEFAULTS.items()}
            return prefs
        except Exception:
            log.warning("Malformed prefs at %s — using defaults", path)
            return cls()

    def save(self, path: Path) -> None:
        """Atomic write via tempfile + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tick_seconds": self.tick_seconds,
            "reference_usd": self.reference_usd,
            "thresholds": self.thresholds,
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            Path(tmp).replace(path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def to_dict(self) -> dict:
        return asdict(self)
