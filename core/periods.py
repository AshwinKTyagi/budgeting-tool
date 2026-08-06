"""Period resolution and period algebra.

Owned by `module/core-periods` (PLAN.md §13.2).

Nothing outside this module may assume months (PLAN.md §4.1). That restriction is what
keeps a future paycheck-driven resolver a cheap swap rather than a rewrite.

Period boundaries are pure `dt.date` comparisons. Business dates carry no time
component at all, so there is no midnight ambiguity and no DST edge case anywhere in
this file.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol

from core.types import PeriodId


class PeriodResolver(Protocol):
    """Maps dates to periods. CalendarMonthResolver is the only implementation
    built; paycheck-driven is future work. Nothing outside this module may assume
    months (PLAN.md §4.1)."""

    def period_for(self, d: dt.date) -> PeriodId: ...

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        """Returns (start inclusive, end exclusive)."""
        ...

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        """Ascending, inclusive of the periods containing both endpoints."""
        ...


class CalendarMonthResolver:
    """Half-open calendar months: [first day, first day of next month).
    PeriodId is "YYYY-MM".

    Postconditions:
        period_for is total over dt.date — every date maps to exactly one period
        bounds(period_for(d)) always contains d
        periods_between is ascending with no gaps
    """

    def period_for(self, d: dt.date) -> PeriodId:
        raise NotImplementedError

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        raise NotImplementedError

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        raise NotImplementedError


def clamp_day_to_month(year: int, month: int, day: int) -> dt.date:
    """Clamp `day` to the last valid day of the month.

    Preconditions:  1 <= day <= 31
    Postconditions: clamp_day_to_month(2026, 2, 31) == dt.date(2026, 2, 28)
                    result is always a valid date in (year, month)
    """
    raise NotImplementedError
