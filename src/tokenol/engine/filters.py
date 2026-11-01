"""The one filter object every engine query takes, and the closed sets it uses.

Searched before writing: ``grep -rn "class .*Filter" src`` and ``grep -rn "since" src/tokenol/serve``
found per-endpoint ``since`` / project / model parameters re-checked by hand in each handler, and
no shared object, so this is new. The WHERE text is rendered here once, against the fixed table
aliases below, so no query repeats a predicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

# Aliases every query in ``queries.py`` uses for these tables; the WHERE text depends on them.
TURNS_ALIAS = "t"
SESSIONS_ALIAS = "s"
MODELS_ALIAS = "m"

# What a null model or cwd is called when it becomes a group label (as the Python code does).
UNKNOWN_LABEL = "(unknown)"


class InterruptedPolicy(str, Enum):
    """Whether interrupted turns are part of an aggregation (a per-consumer choice, INV 6b.7)."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class GroupKey(str, Enum):
    """What an aggregation is split by."""

    PROJECT = "project"
    MODEL = "model"


class SearchField(str, Enum):
    """Which column of turns a search lists distinct values of."""

    MODEL = "model"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class TurnFilter:
    """Which turns a query covers. Dates are inclusive UTC days."""

    since: date | None = None
    until: date | None = None
    projects: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    session_id: str | None = None
    interrupted: InterruptedPolicy = InterruptedPolicy.INCLUDE

    def where(self) -> tuple[str, list]:
        """A WHERE body (always non-empty) over the aliases above, with its parameters.

        Timestamps are compared as a half-open range rather than through a date cast so the
        column's min/max statistics can skip row groups.
        """
        t, s, m = TURNS_ALIAS, SESSIONS_ALIAS, MODELS_ALIAS
        clauses: list[str] = ["true"]
        params: list = []
        if self.since is not None:
            clauses.append(f"{t}.ts >= ?")
            params.append(datetime.combine(self.since, time.min))
        if self.until is not None:
            clauses.append(f"{t}.ts < ?")
            params.append(datetime.combine(self.until + timedelta(days=1), time.min))
        if self.projects:
            clauses.append(f"{s}.project_cwd IN ({_marks(self.projects)})")
            params.extend(self.projects)
        if self.models:
            clauses.append(f"{m}.model IN ({_marks(self.models)})")
            params.extend(self.models)
        if self.session_id is not None:
            clauses.append(f"{s}.session_id = ?")
            params.append(self.session_id)
        if self.interrupted is InterruptedPolicy.EXCLUDE:
            clauses.append(f"NOT {t}.is_interrupted")
        return " AND ".join(clauses), params


def _marks(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)
