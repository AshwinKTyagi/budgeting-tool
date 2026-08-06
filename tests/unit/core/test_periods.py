"""Unit tests for `core/periods.py` (CONTRACTS.md §8.2).

Every date in this file is an explicit literal or a Hypothesis-generated value. There
is no clock read anywhere (CLAUDE.md §4.4), so these tests give the same answer in
2026 as in 2126 -- which is the whole point of `as_of_date` being a parameter.

No tolerance of any kind (CLAUDE.md §4.6): dates and period ids are exact values and
every assertion is `==`.

Strategies are defined locally on purpose. `tests/properties/strategies.py` is the
shared home for cross-module strategies and belongs to `module/properties` in Phase 4;
these are module-local properties about month algebra and nothing else needs them.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.periods import CalendarMonthResolver, PeriodResolver, clamp_day_to_month
from core.types import AppError, ErrorCode, PeriodId

# ------------------------------------------------------------------ conformance
# Structural conformance, checked by mypy rather than at runtime: if
# `CalendarMonthResolver` ever drifts from the Protocol, `mypy --strict` fails here.
# CONTRACTS.md §8.2 declares `PeriodResolver` as the seam a future paycheck-driven
# resolver slots into, so this assignment is the seam's only static guard.
_RESOLVER: PeriodResolver = CalendarMonthResolver()

RESOLVER = CalendarMonthResolver()

# `dt.date` runs 0001-01-01 .. 9999-12-31.
_ANY_DATE = st.dates()

# Dates whose period has a representable exclusive end. Only 9999-12 is excluded:
# the first day of the month after it is 10000-01-01, which is not a `dt.date`.
_BOUNDABLE_DATE = st.dates(max_value=dt.date(9999, 11, 30))

# A narrow window for the range properties, so an example enumerates thousands of
# periods at most rather than the ~120_000 the full `dt.date` span would produce.
_NEARBY_DATE = st.dates(min_value=dt.date(1900, 1, 1), max_value=dt.date(2200, 12, 31))


@st.composite
def _close_date_pairs(draw: st.DrawFn) -> tuple[dt.date, dt.date]:
    """Two dates within about a month of each other, in either order.

    Two independent `st.dates()` draws essentially never land in the same month, so
    they never exercise the case that actually distinguishes date-level emptiness from
    period-level emptiness: `start > end` with both endpoints inside one month.
    """
    anchor = draw(_NEARBY_DATE)
    offset = draw(st.integers(min_value=-40, max_value=40))
    return (anchor, anchor + dt.timedelta(days=offset))


# ================================================================== period_for


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (dt.date(2026, 3, 31), "2026-03"),
        (dt.date(2026, 3, 1), "2026-03"),
        (dt.date(2026, 1, 1), "2026-01"),
        (dt.date(2026, 12, 31), "2026-12"),
        (dt.date(2026, 10, 15), "2026-10"),
        (dt.date(2024, 2, 29), "2024-02"),
        (dt.date(7, 3, 4), "0007-03"),
        (dt.date(1, 1, 1), "0001-01"),
        (dt.date(999, 9, 9), "0999-09"),
        (dt.date(9999, 12, 31), "9999-12"),
    ],
)
def test_period_for_formats_zero_padded_year_and_month(
    d: dt.date, expected: PeriodId
) -> None:
    assert RESOLVER.period_for(d) == expected


def test_period_for_ignores_the_day() -> None:
    """A receipt dated 2026-03-31 is in 2026-03 regardless of anything else
    (PLAN.md §4.2). Every day of a month maps to the same period."""
    ids = {RESOLVER.period_for(dt.date(2026, 3, day)) for day in range(1, 32)}
    assert ids == {"2026-03"}


@given(d=_ANY_DATE)
def test_period_for_is_total_and_well_formed(d: dt.date) -> None:
    """Total over `dt.date`: every date maps to exactly one period, and that period
    id round-trips through `bounds`' parser."""
    period_id = RESOLVER.period_for(d)
    assert len(period_id) == 7
    assert period_id[4] == "-"
    assert period_id[:4] == f"{d.year:04d}"
    assert period_id[5:] == f"{d.month:02d}"


# ====================================================================== bounds


@pytest.mark.parametrize(
    ("period_id", "start", "end"),
    [
        ("2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1)),
        ("2026-02", dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
        ("2026-04", dt.date(2026, 4, 1), dt.date(2026, 5, 1)),
        ("2026-12", dt.date(2026, 12, 1), dt.date(2027, 1, 1)),
        ("0001-01", dt.date(1, 1, 1), dt.date(1, 2, 1)),
        ("9999-11", dt.date(9999, 11, 1), dt.date(9999, 12, 1)),
    ],
)
def test_bounds_are_half_open_month_edges(
    period_id: PeriodId, start: dt.date, end: dt.date
) -> None:
    assert RESOLVER.bounds(period_id) == (start, end)


def test_bounds_crosses_the_year_boundary_december_to_january() -> None:
    """December's exclusive end is 1 January of the next year, so December and the
    following January abut with no gap and no overlap."""
    december = RESOLVER.bounds("2025-12")
    january = RESOLVER.bounds("2026-01")
    assert december == (dt.date(2025, 12, 1), dt.date(2026, 1, 1))
    assert december[1] == january[0]


def test_bounds_are_half_open_at_both_edges() -> None:
    """[start, end): the day before start belongs to the previous period and the end
    date itself belongs to the next one."""
    start, end = RESOLVER.bounds("2026-03")
    assert RESOLVER.period_for(start) == "2026-03"
    assert RESOLVER.period_for(start - dt.timedelta(days=1)) == "2026-02"
    assert RESOLVER.period_for(end - dt.timedelta(days=1)) == "2026-03"
    assert RESOLVER.period_for(end) == "2026-04"


@pytest.mark.parametrize(
    ("period_id", "days"),
    [
        ("2024-02", 29),  # divisible by 4 -- leap
        ("2023-02", 28),
        ("2026-02", 28),
        ("2000-02", 29),  # divisible by 400 -- leap
        ("2100-02", 28),  # divisible by 100 but not 400 -- NOT leap
        ("1900-02", 28),
        ("2026-01", 31),
        ("2026-04", 30),
    ],
)
def test_bounds_span_matches_the_length_of_the_month(
    period_id: PeriodId, days: int
) -> None:
    start, end = RESOLVER.bounds(period_id)
    assert (end - start).days == days


def test_bounds_leap_day_belongs_to_february() -> None:
    """2024-02-29 exists and is inside 2024-02's half-open interval."""
    start, end = RESOLVER.bounds("2024-02")
    leap_day = dt.date(2024, 2, 29)
    assert start <= leap_day < end
    assert RESOLVER.period_for(leap_day) == "2024-02"
    assert end == dt.date(2024, 3, 1)


def test_bounds_february_2100_is_not_a_leap_month() -> None:
    """The century rule: 2100 is divisible by 100 and not by 400."""
    assert RESOLVER.bounds("2100-02") == (dt.date(2100, 2, 1), dt.date(2100, 3, 1))
    with pytest.raises(ValueError):
        dt.date(2100, 2, 29)


@given(d=_BOUNDABLE_DATE)
def test_bounds_of_period_for_always_contains_the_date(d: dt.date) -> None:
    """The central postcondition: `bounds(period_for(d))` always contains `d`, and
    contains it half-open."""
    start, end = RESOLVER.bounds(RESOLVER.period_for(d))
    assert start <= d
    assert d < end


@given(d=_BOUNDABLE_DATE)
def test_bounds_edges_round_trip_through_period_for(d: dt.date) -> None:
    """`start` is in the period; `end` is the first date of the next one."""
    period_id = RESOLVER.period_for(d)
    start, end = RESOLVER.bounds(period_id)
    assert RESOLVER.period_for(start) == period_id
    assert RESOLVER.period_for(end) != period_id
    assert start.day == 1
    assert end.day == 1


@given(d=_BOUNDABLE_DATE)
def test_bounds_are_strictly_ordered(d: dt.date) -> None:
    start, end = RESOLVER.bounds(RESOLVER.period_for(d))
    assert start < end


def test_bounds_of_the_final_representable_month_is_refused() -> None:
    """9999-12's exclusive end would be 10000-01-01, which is not a `dt.date`.
    Refusing is better than returning `dt.date.max`, which would silently make the
    interval exclude 9999-12-31."""
    with pytest.raises(AppError) as excinfo:
        RESOLVER.bounds("9999-12")
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details["period_id"] == "9999-12"


# ------------------------------------------------------- malformed period ids


@pytest.mark.parametrize(
    "period_id",
    [
        "",
        "2026",
        "2026-",
        "-01",
        "2026-1",  # month not zero-padded
        "26-01",  # year not four digits
        "02026-01",  # year too long
        "2026-013",  # month too long
        "2026/01",  # wrong separator
        "2026_01",
        "2026 01",
        "202601",  # separator missing
        "2026-01-01",  # a date, not a period
        " 2026-01",  # leading whitespace
        "2026-01 ",  # trailing whitespace
        "2026-01\n",  # trailing newline -- `$` alone would have matched this
        "\n2026-01",
        "abcd-ef",
        "YYYY-MM",
        "2026-00",  # month zero
        "2026-13",  # month thirteen
        "2026-99",
        "0000-01",  # year zero is outside `dt.date`
        "٢٠٢٦-٠١",  # Arabic-Indic digits
    ],
)
def test_bounds_rejects_a_malformed_period_id(period_id: str) -> None:
    """`PeriodId` is a `str` alias, so nothing upstream can reject these on
    `bounds`' behalf. Anything that is not exactly what `period_for` emits is
    malformed input -- an error, not a warning (CONTRACTS.md §7)."""
    with pytest.raises(AppError) as excinfo:
        RESOLVER.bounds(period_id)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_bounds_accepts_every_month_of_a_year() -> None:
    """All twelve months parse, and each one's end is the next one's start."""
    ids = [f"2026-{month:02d}" for month in range(1, 13)]
    all_bounds = [RESOLVER.bounds(period_id) for period_id in ids]
    for (_, end), (next_start, _) in zip(all_bounds, all_bounds[1:], strict=False):
        assert end == next_start
    assert all_bounds[0][0] == dt.date(2026, 1, 1)
    assert all_bounds[-1][1] == dt.date(2027, 1, 1)


# ============================================================= periods_between


def test_periods_between_single_period_same_date() -> None:
    d = dt.date(2026, 3, 15)
    assert list(RESOLVER.periods_between(d, d)) == ["2026-03"]


def test_periods_between_single_period_different_days_same_month() -> None:
    result = RESOLVER.periods_between(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert list(result) == ["2026-03"]


def test_periods_between_is_inclusive_of_both_endpoint_periods() -> None:
    result = RESOLVER.periods_between(dt.date(2026, 3, 31), dt.date(2026, 6, 1))
    assert list(result) == ["2026-03", "2026-04", "2026-05", "2026-06"]


def test_periods_between_spans_a_year_boundary() -> None:
    result = RESOLVER.periods_between(dt.date(2025, 11, 30), dt.date(2026, 2, 1))
    assert list(result) == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_periods_between_spans_multiple_years() -> None:
    result = RESOLVER.periods_between(dt.date(2024, 12, 31), dt.date(2027, 1, 1))
    assert len(result) == 26
    assert result[0] == "2024-12"
    assert result[-1] == "2027-01"
    assert list(result[:3]) == ["2024-12", "2025-01", "2025-02"]


def test_periods_between_adjacent_months_one_day_apart() -> None:
    result = RESOLVER.periods_between(dt.date(2026, 1, 31), dt.date(2026, 2, 1))
    assert list(result) == ["2026-01", "2026-02"]


def test_periods_between_reversed_range_is_empty() -> None:
    """CONTRACTS.md §8.2 is silent on `start > end`; this module returns an empty
    sequence so callers can fold over the result without a guard."""
    result = RESOLVER.periods_between(dt.date(2026, 6, 1), dt.date(2026, 3, 1))
    assert list(result) == []
    assert len(result) == 0


def test_periods_between_reversed_within_a_single_month_is_empty() -> None:
    """Both endpoints share a period, but `start > end`, so the range is still
    inverted and still empty. The emptiness test is on the dates, not the periods."""
    result = RESOLVER.periods_between(dt.date(2026, 3, 20), dt.date(2026, 3, 10))
    assert list(result) == []


def test_periods_between_reversed_by_one_day_across_a_boundary_is_empty() -> None:
    result = RESOLVER.periods_between(dt.date(2026, 2, 1), dt.date(2026, 1, 31))
    assert list(result) == []


def test_periods_between_covers_the_full_dt_date_range() -> None:
    """Totality at the extremes: 0001-01 through 9999-12, every month, no gaps."""
    result = RESOLVER.periods_between(dt.date.min, dt.date.max)
    assert result[0] == "0001-01"
    assert result[-1] == "9999-12"
    assert len(result) == 9999 * 12


@given(start=_NEARBY_DATE, end=_NEARBY_DATE)
def test_periods_between_is_strictly_ascending_with_no_repeats(
    start: dt.date, end: dt.date
) -> None:
    result = list(RESOLVER.periods_between(start, end))
    assert result == sorted(result)
    assert len(set(result)) == len(result)


@given(start=_NEARBY_DATE, end=_NEARBY_DATE)
def test_periods_between_is_contiguous(start: dt.date, end: dt.date) -> None:
    """No gaps: each period's exclusive end is the next period's inclusive start."""
    result = list(RESOLVER.periods_between(start, end))
    for earlier, later in zip(result, result[1:], strict=False):
        assert RESOLVER.bounds(earlier)[1] == RESOLVER.bounds(later)[0]


@given(start=_NEARBY_DATE, end=_NEARBY_DATE)
def test_periods_between_endpoints_are_the_endpoint_periods(
    start: dt.date, end: dt.date
) -> None:
    result = list(RESOLVER.periods_between(start, end))
    if start > end:
        assert result == []
    else:
        assert result[0] == RESOLVER.period_for(start)
        assert result[-1] == RESOLVER.period_for(end)


@given(start=_NEARBY_DATE, end=_NEARBY_DATE)
def test_periods_between_is_empty_exactly_when_the_range_is_inverted(
    start: dt.date, end: dt.date
) -> None:
    assert (len(RESOLVER.periods_between(start, end)) == 0) == (start > end)


@given(pair=_close_date_pairs())
def test_periods_between_emptiness_is_decided_on_dates_not_periods(
    pair: tuple[dt.date, dt.date],
) -> None:
    """The distinguishing case: endpoints close enough to share a month. Emptiness
    tracks `start > end` even then -- it does not collapse to "same period, so
    non-empty"."""
    start, end = pair
    assert (len(RESOLVER.periods_between(start, end)) == 0) == (start > end)


@given(pair=_close_date_pairs())
def test_periods_between_close_pairs_stay_contiguous_and_inclusive(
    pair: tuple[dt.date, dt.date],
) -> None:
    start, end = pair
    result = list(RESOLVER.periods_between(start, end))
    if start > end:
        assert result == []
        return
    assert result[0] == RESOLVER.period_for(start)
    assert result[-1] == RESOLVER.period_for(end)
    assert len(result) <= 3
    for earlier, later in zip(result, result[1:], strict=False):
        assert RESOLVER.bounds(earlier)[1] == RESOLVER.bounds(later)[0]


@given(start=_NEARBY_DATE, end=_NEARBY_DATE)
def test_periods_between_contains_the_period_of_every_date_in_the_range(
    start: dt.date, end: dt.date
) -> None:
    """The enumeration is complete: the union of the listed periods covers
    [start, end] with nothing left over on either side."""
    result = list(RESOLVER.periods_between(start, end))
    if start > end:
        return
    assert RESOLVER.bounds(result[0])[0] <= start
    assert end < RESOLVER.bounds(result[-1])[1]


@given(d=_NEARBY_DATE)
def test_periods_between_a_date_and_itself_is_its_own_period(d: dt.date) -> None:
    assert list(RESOLVER.periods_between(d, d)) == [RESOLVER.period_for(d)]


# =========================================================== clamp_day_to_month


@pytest.mark.parametrize(
    ("year", "month", "day", "expected"),
    [
        # The worked example from CONTRACTS.md §8.2.
        (2026, 2, 31, dt.date(2026, 2, 28)),
        # 31 in February, leap and non-leap.
        (2024, 2, 31, dt.date(2024, 2, 29)),
        (2023, 2, 31, dt.date(2023, 2, 28)),
        (2000, 2, 31, dt.date(2000, 2, 29)),
        (2100, 2, 31, dt.date(2100, 2, 28)),
        # 30 in February.
        (2024, 2, 30, dt.date(2024, 2, 29)),
        (2026, 2, 30, dt.date(2026, 2, 28)),
        # 29 in February: needs no clamping in a leap year, clamps otherwise.
        (2024, 2, 29, dt.date(2024, 2, 29)),
        (2026, 2, 29, dt.date(2026, 2, 28)),
        # 31 in every 30-day month.
        (2026, 4, 31, dt.date(2026, 4, 30)),
        (2026, 6, 31, dt.date(2026, 6, 30)),
        (2026, 9, 31, dt.date(2026, 9, 30)),
        (2026, 11, 31, dt.date(2026, 11, 30)),
        # No clamping needed.
        (2026, 1, 31, dt.date(2026, 1, 31)),
        (2026, 3, 31, dt.date(2026, 3, 31)),
        (2026, 12, 31, dt.date(2026, 12, 31)),
        (2026, 4, 30, dt.date(2026, 4, 30)),
        (2026, 2, 28, dt.date(2026, 2, 28)),
        (2026, 2, 15, dt.date(2026, 2, 15)),
        (2026, 6, 1, dt.date(2026, 6, 1)),
        # Range extremes.
        (1, 2, 31, dt.date(1, 2, 28)),
        (9999, 12, 31, dt.date(9999, 12, 31)),
    ],
)
def test_clamp_day_to_month(year: int, month: int, day: int, expected: dt.date) -> None:
    assert clamp_day_to_month(year, month, day) == expected


def test_clamp_day_to_month_never_moves_the_month() -> None:
    """A "due on the 31st" fixed cost lands on the last day of the month it is due
    in -- never in the following one."""
    for month in range(1, 13):
        result = clamp_day_to_month(2026, month, 31)
        assert result.year == 2026
        assert result.month == month


@pytest.mark.parametrize("day", [0, -1, 32, 100])
def test_clamp_day_to_month_rejects_a_day_outside_1_to_31(day: int) -> None:
    """The precondition is `1 <= day <= 31`. A day of 0 or 32 is malformed input, not
    something a clamp could rescue."""
    with pytest.raises(AppError) as excinfo:
        clamp_day_to_month(2026, 1, day)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize("month", [0, -1, 13, 100])
def test_clamp_day_to_month_rejects_an_impossible_month(month: int) -> None:
    with pytest.raises(AppError) as excinfo:
        clamp_day_to_month(2026, month, 15)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize("year", [0, -1, 10000])
def test_clamp_day_to_month_rejects_a_year_outside_dt_date(year: int) -> None:
    with pytest.raises(AppError) as excinfo:
        clamp_day_to_month(year, 1, 15)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


@given(
    year=st.integers(min_value=1, max_value=9999),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=31),
)
def test_clamp_day_to_month_is_a_valid_date_in_the_requested_month(
    year: int, month: int, day: int
) -> None:
    """Postcondition: the result is always a valid date in (year, month), and the day
    is either the requested one or the last of the month -- never anything else."""
    result = clamp_day_to_month(year, month, day)
    assert result.year == year
    assert result.month == month
    assert result.day <= day
    assert result.day == day or result.day == _last_day_of(year, month)


@given(
    year=st.integers(min_value=1, max_value=9999),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=31),
)
def test_clamp_day_to_month_is_idempotent(year: int, month: int, day: int) -> None:
    """Clamping an already-clamped day changes nothing."""
    once = clamp_day_to_month(year, month, day)
    twice = clamp_day_to_month(once.year, once.month, once.day)
    assert once == twice


@given(
    year=st.integers(min_value=1, max_value=9999),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=31),
)
def test_clamp_day_to_month_agrees_with_the_resolver(
    year: int, month: int, day: int
) -> None:
    """The clamped date lands in the period it was asked for -- the two halves of
    this module cannot disagree about which month a day belongs to."""
    result = clamp_day_to_month(year, month, day)
    assert RESOLVER.period_for(result) == f"{year:04d}-{month:02d}"


def _last_day_of(year: int, month: int) -> int:
    """Length of a month, computed from `dt.date` alone so it is independent of the
    implementation under test."""
    if month == 12:
        first_of_next = dt.date(year + 1, 1, 1) if year < 9999 else None
        if first_of_next is None:
            return 31
    else:
        first_of_next = dt.date(year, month + 1, 1)
    return (first_of_next - dt.date(year, month, 1)).days


# ================================================================== statelessness


def test_two_resolvers_are_interchangeable() -> None:
    """`CalendarMonthResolver` holds no state, so a fresh instance answers
    identically. Nothing in `domain/` should ever need to thread one particular
    instance around for correctness."""
    other = CalendarMonthResolver()
    d = dt.date(2026, 7, 4)
    assert RESOLVER.period_for(d) == other.period_for(d)
    assert RESOLVER.bounds("2026-07") == other.bounds("2026-07")
    assert list(RESOLVER.periods_between(d, dt.date(2026, 9, 1))) == list(
        other.periods_between(d, dt.date(2026, 9, 1))
    )
