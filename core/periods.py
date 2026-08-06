"""Period resolution and period algebra.

Owned by `module/core-periods` (PLAN.md §13.2).

Nothing outside this module may assume months (PLAN.md §4.1). That restriction is what
keeps a future paycheck-driven resolver a cheap swap rather than a rewrite.

Period boundaries are pure `dt.date` comparisons. Business dates carry no time
component at all, so there is no midnight ambiguity and no DST edge case anywhere in
this file.

Decisions taken where CONTRACTS.md §8.2 is silent, each documented on the member that
implements it:

* A malformed `PeriodId` raises `AppError(VALIDATION_FAILED)`. `PeriodId` is a bare
  `str` alias (CONTRACTS.md §2), so nothing upstream of `bounds` can reject "2026-13"
  on its behalf.
* `periods_between(start, end)` with `start > end` returns an empty sequence rather
  than raising. It is an enumeration helper that callers fold over; an empty range is
  the total, composable answer, and it makes the function's own postcondition
  ("ascending, no gaps") vacuously true instead of undefined.
* `bounds("9999-12")` raises `AppError(VALIDATION_FAILED)`: the half-open end of the
  final representable month is 10000-01-01, which is not a `dt.date`. Every other
  period is unaffected. See `_MAX_YEAR` below.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from collections.abc import Sequence
from typing import Protocol

from core.types import AppError, ErrorCode, PeriodId

# "YYYY-MM", anchored, ASCII digits only. `re.ASCII` matters: without it `\d` also
# matches non-ASCII decimal digits, so a string of Arabic-Indic digits would parse to a
# period id that no `period_for` call could ever produce and no round trip preserve.
_PERIOD_ID_PATTERN = re.compile(r"\A(\d{4})-(\d{2})\Z", re.ASCII)

_MONTHS_PER_YEAR = 12

# `dt.date` stops at 9999-12-31. Nothing here constructs a date beyond it: the month
# arithmetic runs on integers, and the single place where a real date would be built
# past the end of the range -- the exclusive bound of 9999-12 -- is rejected explicitly.
_MIN_YEAR = dt.date.min.year
_MAX_YEAR = dt.date.max.year


def _month_index(year: int, month: int) -> int:
    """Months since year zero, as a single ordinal. Ascending in calendar order."""
    return year * _MONTHS_PER_YEAR + (month - 1)


def _period_id_at(index: int) -> PeriodId:
    """Inverse of `_month_index`, formatted as "YYYY-MM"."""
    return f"{index // _MONTHS_PER_YEAR:04d}-{index % _MONTHS_PER_YEAR + 1:02d}"


def _next_month(year: int, month: int) -> tuple[int, int]:
    """The (year, month) after (year, month).

    Pure integer arithmetic, and deliberately allowed to run off the end of the
    `dt.date` range -- the caller decides whether the result has to become a date.
    """
    if month == _MONTHS_PER_YEAR:
        return (year + 1, 1)
    return (year, month + 1)


def _days_in_month(year: int, month: int) -> int:
    """Length of the month, leap years included."""
    return calendar.monthrange(year, month)[1]


def _reject(message: str, **details: object) -> AppError:
    return AppError(ErrorCode.VALIDATION_FAILED, message, dict(details))


def _parse_period_id(period_id: PeriodId) -> tuple[int, int]:
    """Parse "YYYY-MM" into (year, month), validating strictly.

    `PeriodId` is a `str` alias rather than a `NewType`, so a caller can hand this
    function any string at all. Everything that is not exactly the form `period_for`
    emits is rejected: wrong shape, wrong separator, surrounding whitespace, month 00
    or 13, year 0000, non-ASCII digits.

    Raises:
        AppError(VALIDATION_FAILED)
    """
    match = _PERIOD_ID_PATTERN.fullmatch(period_id)
    if match is None:
        raise _reject('malformed period id; expected "YYYY-MM"', period_id=period_id)
    year = int(match.group(1))
    month = int(match.group(2))
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        raise _reject(
            f"period id year out of range [{_MIN_YEAR}, {_MAX_YEAR}]",
            period_id=period_id,
        )
    if not 1 <= month <= _MONTHS_PER_YEAR:
        raise _reject(
            f"period id month out of range [1, {_MONTHS_PER_YEAR}]",
            period_id=period_id,
        )
    return (year, month)


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
        period_for is total over dt.date -- every date maps to exactly one period
        bounds(period_for(d)) always contains d
        periods_between is ascending with no gaps

    Stateless, and therefore trivially shareable: two instances are interchangeable.
    """

    def period_for(self, d: dt.date) -> PeriodId:
        """The period containing `d`.

        Total over `dt.date`: every date has a year and a month, so every date maps to
        exactly one period and nothing here can fail.

        Postconditions:
            result matches "YYYY-MM", zero-padded on both fields
            bounds(result)[0] <= d < bounds(result)[1]   (for every d except those in
                9999-12, whose exclusive bound is not representable -- see `bounds`)
        """
        return f"{d.year:04d}-{d.month:02d}"

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        """Returns (start inclusive, end exclusive).

        Postconditions:
            start == dt.date(year, month, 1)
            end   == the first day of the following month
            start < end, and (end - start).days is the length of the month

        Raises:
            AppError(VALIDATION_FAILED) if `period_id` is not a well-formed "YYYY-MM"
            (see `_parse_period_id`), or if it is "9999-12", whose exclusive end --
            10000-01-01 -- is not a representable `dt.date`. Returning `dt.date.max`
            there would quietly make the half-open interval exclude 9999-12-31, which
            is worse than refusing.
        """
        year, month = _parse_period_id(period_id)
        next_year, next_month = _next_month(year, month)
        if next_year > _MAX_YEAR:
            raise _reject(
                "period has no representable exclusive end: the first day of the "
                f"month after {period_id} falls outside dt.date",
                period_id=period_id,
            )
        return (dt.date(year, month, 1), dt.date(next_year, next_month, 1))

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        """Ascending, inclusive of the periods containing both endpoints.

        Postconditions:
            result[0] == period_for(start) and result[-1] == period_for(end),
                whenever start <= end
            strictly ascending, contiguous, no gaps and no repeats:
                bounds(result[i])[1] == bounds(result[i + 1])[0]
            len(result) == 1 when start and end fall in the same period
            result is empty iff start > end

        An inverted range yields an empty sequence rather than an error. CONTRACTS.md
        §8.2 does not say, and this is an enumeration helper: callers fold over the
        result, so "no periods" composes where a raise would force a guard at every
        call site. `EFFECTIVE_RANGE_INVALID` exists for inverted *definition version*
        ranges, which are persisted input; a date pair handed to a pure enumerator is
        not that.

        Emptiness is decided on the DATES, not on their periods. `start` after `end`
        inside a single month is still an inverted range and still yields nothing --
        if it collapsed to that one shared month, the answer would depend on period
        granularity, and a future paycheck-driven resolver would answer differently
        for the same two dates.
        """
        if start > end:
            return ()
        first = _month_index(start.year, start.month)
        last = _month_index(end.year, end.month)
        return tuple(_period_id_at(index) for index in range(first, last + 1))


def clamp_day_to_month(year: int, month: int, day: int) -> dt.date:
    """Clamp `day` to the last valid day of the month.

    Preconditions:  1 <= day <= 31
    Postconditions: clamp_day_to_month(2026, 2, 31) == dt.date(2026, 2, 28)
                    result is always a valid date in (year, month)

    This is what lets a "due on the 31st" `FixedCost` land in February. Only the day is
    clamped -- the year and the month come back untouched, so the result is always
    inside the requested month.

    Raises:
        AppError(VALIDATION_FAILED) if the preconditions do not hold: `day` outside
        [1, 31], `month` outside [1, 12], or `year` outside the `dt.date` range. A day
        of 0 or 32 is not a number any clamp could rescue -- it is malformed input,
        which is exactly what an error is for (CONTRACTS.md §7).
    """
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        raise _reject(f"year out of range [{_MIN_YEAR}, {_MAX_YEAR}]", year=year)
    if not 1 <= month <= _MONTHS_PER_YEAR:
        raise _reject(f"month out of range [1, {_MONTHS_PER_YEAR}]", month=month)
    if not 1 <= day <= 31:
        raise _reject("day out of range [1, 31]", day=day)
    last_day = _days_in_month(year, month)
    return dt.date(year, month, day if day < last_day else last_day)
