"""Unit tests for `core/interest.py` (module/core-interest, PLAN.md §13.2).

Two conventions this file holds to, both non-negotiable:

* **No tolerance anywhere.** Integer arithmetic is exact, so every assertion is `==`
  (CLAUDE.md §4.6). `pytest.approx` and `assertAlmostEqual` do not appear.
* **No clock reads.** Every date is an explicit literal or Hypothesis-generated
  (CLAUDE.md §4.4). Nothing here calls `.today()`.

`core.periods.CalendarMonthResolver` is a stub on a sibling Phase-1 branch, so the
resolver used here is a local fake satisfying the `PeriodResolver` protocol. That is
the documented Phase-1 pattern (PLAN.md §11): sign against the protocol, do not wait
on the implementation.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from core.interest import build_statement_cycles, interest_for_cycle
from core.types import AccountKind, AppError, ErrorCode, PeriodId

# The actual/365 denominator, spelled out rather than imported, so the test would
# catch the module quietly changing it.
DENOMINATOR = 10_000 * 365
ONE_DAY = dt.timedelta(days=1)


# --------------------------------------------------------------------------- fakes


class FakeMonthResolver:
    """Half-open calendar months, standing in for `CalendarMonthResolver`.

    Satisfies `core.periods.PeriodResolver` structurally. It is deliberately the
    dumbest possible correct implementation: these tests are about `core/interest.py`,
    and a clever resolver here would test the wrong module.
    """

    def period_for(self, d: dt.date) -> PeriodId:
        return f"{d.year:04d}-{d.month:02d}"

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        year = int(period_id[:4])
        month = int(period_id[5:7])
        start = dt.date(year, month, 1)
        if month == 12:
            return start, dt.date(year + 1, 1, 1)
        return start, dt.date(year, month + 1, 1)

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        if end < start:
            return ()
        out: list[PeriodId] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            out.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return tuple(out)


@dataclass(frozen=True)
class FakeAccount:
    """The three fields `build_statement_cycles` reads (`StatementCycleAccountLike`)."""

    entity_id: str
    kind: AccountKind
    statement_close_day: int | None


RESOLVER = FakeMonthResolver()

CARD = FakeAccount("acct-card", AccountKind.CREDIT_CARD, 15)
SAVINGS = FakeAccount("acct-sav", AccountKind.SAVINGS, None)


def card(close_day: int) -> FakeAccount:
    return FakeAccount("acct-card", AccountKind.CREDIT_CARD, close_day)


# ================================================================ interest_for_cycle
# PLAN.md §7.2 worked examples, transcribed literally. CLAUDE.md §5.3: if a property
# test and a worked example disagree, the documentation is the specification.


def test_worked_example_card_2199_bps_31_days() -> None:
    """PLAN.md §7.2, first block, every intermediate line included.

    Card, apr_bps = 2199 (21.99% APR), statement-close balance 120_000 ($1,200.00),
    31-day cycle, previous statement not paid in full.
    """
    assert 120_000 * 2199 == 263_880_000
    assert 263_880_000 * 31 == 8_180_280_000
    assert 10_000 * 365 == 3_650_000
    assert 8_180_280_000 // 3_650_000 == 2241

    assert interest_for_cycle(120_000, 2199, 31) == 2241


def test_worked_example_card_quotient_is_genuinely_floored() -> None:
    """2241.17..., floored. The example is a rounding anchor, not a clean division."""
    assert 8_180_280_000 % 3_650_000 != 0
    assert 2241 * 3_650_000 < 8_180_280_000
    assert 8_180_280_000 < 2242 * 3_650_000


def test_worked_example_savings_450_bps_30_days() -> None:
    """PLAN.md §7.2, second block.

    Savings, apr_bps = 450 (4.50%), balance 500_000 ($5,000.00), 30-day period.
    """
    assert 500_000 * 450 * 30 // 3_650_000 == 1849

    assert interest_for_cycle(500_000, 450, 30) == 1849


def test_zero_outstanding_earns_nothing() -> None:
    assert interest_for_cycle(0, 2199, 31) == 0
    assert interest_for_cycle(0, 0, 1) == 0
    assert interest_for_cycle(0, 999_999, 3650) == 0


def test_zero_apr_earns_nothing() -> None:
    assert interest_for_cycle(120_000, 0, 31) == 0
    assert interest_for_cycle(10**15, 0, 3650) == 0


def test_result_is_int_not_float() -> None:
    """Postcondition: `result is int; no float anywhere in the computation`."""
    result = interest_for_cycle(120_000, 2199, 31)
    assert type(result) is int


def test_amount_below_one_minor_unit_of_interest_floors_to_zero() -> None:
    """A balance too small to earn a single cent earns nothing, not a fraction."""
    # 1 * 2199 * 31 == 68_169, far below the 3_650_000 denominator.
    assert interest_for_cycle(1, 2199, 31) == 0
    # One minor unit at 100% APR for one day: 1 * 10_000 * 1 // 3_650_000 == 0.
    assert interest_for_cycle(1, 10_000, 1) == 0


def test_floor_never_rounds_up() -> None:
    """The exact quotient here is 1.9999...; a rounding implementation returns 2."""
    # 7_299_999 // 3_650_000 == 1 (not 2).
    outstanding, apr, days = 7_299_999, 1, 1
    assert outstanding * apr * days == 7_299_999
    assert interest_for_cycle(outstanding, apr, days) == 1


def test_multiplication_happens_before_division() -> None:
    """Dividing the rate down first discards precision (CLAUDE.md §2.1).

    apr_bps = 450 is 0 after `// 10_000`, so an implementation that scales the rate
    before multiplying returns 0 for every balance. This is the exact failure the
    multiply-before-divide rule exists to prevent.
    """
    assert 450 // 10_000 == 0
    assert interest_for_cycle(500_000, 450, 30) != 0


def test_negative_outstanding_raises_and_is_never_clamped() -> None:
    """A credit earns no interest — but silently treating it as zero would hide a
    caller that passed a liability's signed `balance_minor` (PLAN.md §7.1)."""
    for bad in (-1, -120_000, -(10**12)):
        with pytest.raises(AppError) as excinfo:
            interest_for_cycle(bad, 2199, 31)
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
        assert excinfo.value.details["outstanding_minor"] == bad


def test_negative_outstanding_does_not_return_a_sign_mirrored_charge() -> None:
    """Explicitly NOT the abs-then-reapply-sign discipline of `split_bps`."""
    with pytest.raises(AppError):
        interest_for_cycle(-120_000, 2199, 31)


def test_negative_apr_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        interest_for_cycle(120_000, -1, 31)
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_non_positive_cycle_days_raises() -> None:
    for bad_days in (0, -1, -31):
        with pytest.raises(AppError) as excinfo:
            interest_for_cycle(120_000, 2199, bad_days)
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_a_full_year_at_a_round_rate_is_the_rate() -> None:
    """365 days at 10.00% on $1,000.00 is $100.00, exactly."""
    assert interest_for_cycle(100_000, 1000, 365) == 10_000


# ------------------------------------------------- interest_for_cycle, module-local
# Hypothesis strategies are defined here, not in tests/properties/strategies.py —
# that file belongs to module/properties (PLAN.md §13.3) and Phase-1 agents must not
# create it. These are module-local properties, not the §5.1 invariant suite.

outstandings = st.integers(min_value=0, max_value=10**14)
aprs = st.integers(min_value=0, max_value=100_000)
day_counts = st.integers(min_value=1, max_value=3660)


@given(outstandings, aprs, day_counts)
def test_property_result_is_a_non_negative_int(
    outstanding: int, apr: int, days: int
) -> None:
    result = interest_for_cycle(outstanding, apr, days)
    assert type(result) is int
    assert result >= 0


@given(outstandings, aprs, day_counts)
def test_property_result_is_the_exact_floor(
    outstanding: int, apr: int, days: int
) -> None:
    """`result` brackets the true quotient from below, with no slack."""
    numerator = outstanding * apr * days
    result = interest_for_cycle(outstanding, apr, days)
    assert result * DENOMINATOR <= numerator
    assert numerator < (result + 1) * DENOMINATOR


@given(outstandings, aprs, day_counts)
def test_property_zero_factor_means_zero_interest(
    outstanding: int, apr: int, days: int
) -> None:
    assert interest_for_cycle(0, apr, days) == 0
    assert interest_for_cycle(outstanding, 0, days) == 0


@given(outstandings, outstandings, aprs, day_counts)
def test_property_monotone_in_outstanding(
    a: int, b: int, apr: int, days: int
) -> None:
    assume(a <= b)
    assert interest_for_cycle(a, apr, days) <= interest_for_cycle(b, apr, days)


@given(outstandings, aprs, aprs, day_counts)
def test_property_monotone_in_apr(outstanding: int, a: int, b: int, days: int) -> None:
    assume(a <= b)
    assert interest_for_cycle(outstanding, a, days) <= interest_for_cycle(
        outstanding, b, days
    )


@given(outstandings, aprs, day_counts, day_counts)
def test_property_monotone_in_days(
    outstanding: int, apr: int, a: int, b: int
) -> None:
    assume(a <= b)
    assert interest_for_cycle(outstanding, apr, a) <= interest_for_cycle(
        outstanding, apr, b
    )


@given(outstandings, aprs, day_counts, day_counts)
def test_property_no_intra_cycle_compounding(
    outstanding: int, apr: int, d1: int, d2: int
) -> None:
    """Splitting a cycle in two at a constant balance never *increases* interest.

    With no compounding the day count is linear, so the only difference between one
    long cycle and two short ones is where the floor lands: two floors discard at
    most as much as, and generally more than, one.
    """
    split = interest_for_cycle(outstanding, apr, d1) + interest_for_cycle(
        outstanding, apr, d2
    )
    whole = interest_for_cycle(outstanding, apr, d1 + d2)
    assert split <= whole


@given(outstandings, aprs, day_counts)
def test_property_scale_invariance_of_the_formula(
    outstanding: int, apr: int, days: int
) -> None:
    """Ten times the balance is at least ten times the interest, never more than
    ten times plus the floor slack. Catches an implementation that divides early."""
    single = interest_for_cycle(outstanding, apr, days)
    tenfold = interest_for_cycle(outstanding * 10, apr, days)
    assert 10 * single <= tenfold
    assert tenfold <= 10 * single + 9


# ============================================================ build_statement_cycles


def days_in(cycle: tuple[str, dt.date, dt.date]) -> int:
    _, start, end_exclusive = cycle
    return (end_exclusive - start).days


def test_asset_account_cycles_are_the_periods() -> None:
    """No statement_close_day, so cycles are period-aligned (PLAN.md §7.1)."""
    cycles = build_statement_cycles(
        SAVINGS, dt.date(2026, 1, 1), dt.date(2026, 3, 31), RESOLVER
    )
    assert list(cycles) == [
        ("acct-sav:2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1)),
        ("acct-sav:2026-02", dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
        ("acct-sav:2026-03", dt.date(2026, 3, 1), dt.date(2026, 4, 1)),
    ]


def test_asset_account_first_cycle_starts_at_genesis_and_last_ends_at_as_of() -> None:
    cycles = build_statement_cycles(
        SAVINGS, dt.date(2026, 1, 10), dt.date(2026, 2, 20), RESOLVER
    )
    assert list(cycles) == [
        ("acct-sav:2026-01", dt.date(2026, 1, 10), dt.date(2026, 2, 1)),
        ("acct-sav:2026-02", dt.date(2026, 2, 1), dt.date(2026, 2, 21)),
    ]


def test_genesis_equal_to_as_of_date_yields_one_one_day_cycle() -> None:
    """The degenerate window. `as_of_date` is inclusive, so it is one day, not zero —
    which also keeps `interest_for_cycle`'s `cycle_days > 0` precondition satisfiable.
    """
    day = dt.date(2026, 5, 17)
    cycles = build_statement_cycles(SAVINGS, day, day, RESOLVER)
    assert list(cycles) == [("acct-sav:2026-05", day, dt.date(2026, 5, 18))]
    assert days_in(cycles[0]) == 1


def test_card_cycles_close_on_the_statement_close_day() -> None:
    """close_day 15: the close date is the last day *in* its cycle, so the cycle ends
    exclusive on the 16th."""
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 1), dt.date(2026, 3, 20), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 1, 16)),
        ("acct-card:2026-02", dt.date(2026, 1, 16), dt.date(2026, 2, 16)),
        ("acct-card:2026-03", dt.date(2026, 2, 16), dt.date(2026, 3, 16)),
        ("acct-card:2026-04", dt.date(2026, 3, 16), dt.date(2026, 3, 21)),
    ]


def test_card_in_progress_cycle_is_truncated_at_as_of_date() -> None:
    """The trailing cycle stops at as_of_date rather than running to its close date.

    Charging interest on days that have not happened would overstate the estimate
    (PLAN.md §7.3); it is labelled with the period it will close in.
    """
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 1), dt.date(2026, 3, 20), RESOLVER
    )
    cycle_id, start, end_exclusive = cycles[-1]
    assert cycle_id == "acct-card:2026-04"
    assert start == dt.date(2026, 3, 16)
    assert end_exclusive == dt.date(2026, 3, 21)
    assert days_in(cycles[-1]) == 5


def test_card_full_cycle_between_two_closes_is_a_calendar_month_of_days() -> None:
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 1), dt.date(2026, 3, 20), RESOLVER
    )
    # 2026-01-16 .. 2026-02-16 is 31 days; 2026-02-16 .. 2026-03-16 is 28.
    assert days_in(cycles[1]) == 31
    assert days_in(cycles[2]) == 28


def test_close_day_31_clamps_into_february() -> None:
    """statement_close_day 31 closes on 28 February in a common year."""
    cycles = build_statement_cycles(
        card(31), dt.date(2026, 1, 1), dt.date(2026, 4, 30), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1)),
        ("acct-card:2026-02", dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
        ("acct-card:2026-03", dt.date(2026, 3, 1), dt.date(2026, 4, 1)),
        ("acct-card:2026-04", dt.date(2026, 4, 1), dt.date(2026, 5, 1)),
    ]
    # February's cycle is 28 days; April's close day 31 clamps to the 30th.
    assert days_in(cycles[1]) == 28
    assert days_in(cycles[3]) == 30


def test_close_day_31_clamps_to_29_in_a_leap_february() -> None:
    cycles = build_statement_cycles(
        card(31), dt.date(2028, 2, 1), dt.date(2028, 2, 29), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2028-02", dt.date(2028, 2, 1), dt.date(2028, 3, 1)),
    ]
    assert days_in(cycles[0]) == 29


def test_close_day_30_clamps_into_february_but_not_into_march() -> None:
    """February has no 30th, so the statement closes on the 28th and its cycle ends
    exclusive on 1 March. March *does* have a 30th, so it closes on the 30th and its
    cycle ends on the 31st — leaving 31 March as the first day of April's statement.

    The asymmetry is the point: clamping is a property of the month, not a blanket
    "close day near the end means end of month".
    """
    cycles = build_statement_cycles(
        card(30), dt.date(2026, 2, 1), dt.date(2026, 3, 31), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-02", dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
        ("acct-card:2026-03", dt.date(2026, 3, 1), dt.date(2026, 3, 31)),
        ("acct-card:2026-04", dt.date(2026, 3, 31), dt.date(2026, 4, 1)),
    ]
    assert days_in(cycles[0]) == 28
    assert days_in(cycles[1]) == 30
    assert days_in(cycles[2]) == 1


def test_close_day_1_is_valid_and_closes_on_the_first() -> None:
    cycles = build_statement_cycles(
        card(1), dt.date(2026, 1, 1), dt.date(2026, 2, 28), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 1, 2)),
        ("acct-card:2026-02", dt.date(2026, 1, 2), dt.date(2026, 2, 2)),
        ("acct-card:2026-03", dt.date(2026, 2, 2), dt.date(2026, 3, 1)),
    ]


def test_period_whose_close_precedes_genesis_contributes_no_cycle() -> None:
    """Opened on the 20th with a close day of the 15th: January's statement already
    closed, so the first cycle runs from genesis to February's close."""
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 20), dt.date(2026, 2, 28), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-02", dt.date(2026, 1, 20), dt.date(2026, 2, 16)),
        ("acct-card:2026-03", dt.date(2026, 2, 16), dt.date(2026, 3, 1)),
    ]


def test_genesis_on_the_close_day_itself_gets_a_one_day_first_cycle() -> None:
    """The close day belongs to the cycle it closes, so genesis == close is one day."""
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 15), dt.date(2026, 1, 31), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-01", dt.date(2026, 1, 15), dt.date(2026, 1, 16)),
        ("acct-card:2026-02", dt.date(2026, 1, 16), dt.date(2026, 2, 1)),
    ]


def test_cycle_id_is_entity_id_colon_period_id() -> None:
    account = FakeAccount("visa-9821", AccountKind.CREDIT_CARD, 15)
    cycles = build_statement_cycles(
        account, dt.date(2026, 6, 1), dt.date(2026, 7, 31), RESOLVER
    )
    for cycle_id, _start, _end in cycles:
        entity_id, separator, period_id = cycle_id.partition(":")
        assert separator == ":"
        assert entity_id == "visa-9821"
        # The period component is a real period, not a formatting accident.
        period_start, period_end_exclusive = RESOLVER.bounds(period_id)
        assert period_start < period_end_exclusive
        assert RESOLVER.period_for(period_start) == period_id
    assert [c[0] for c in cycles] == [
        "visa-9821:2026-06",
        "visa-9821:2026-07",
        "visa-9821:2026-08",
    ]


def test_cycles_span_a_year_end() -> None:
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 12, 1), dt.date(2027, 1, 31), RESOLVER
    )
    assert list(cycles) == [
        ("acct-card:2026-12", dt.date(2026, 12, 1), dt.date(2026, 12, 16)),
        ("acct-card:2027-01", dt.date(2026, 12, 16), dt.date(2027, 1, 16)),
        ("acct-card:2027-02", dt.date(2027, 1, 16), dt.date(2027, 2, 1)),
    ]


def test_genesis_after_as_of_date_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        build_statement_cycles(
            SAVINGS, dt.date(2026, 3, 2), dt.date(2026, 3, 1), RESOLVER
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_credit_card_without_a_close_day_raises() -> None:
    account = FakeAccount("acct-card", AccountKind.CREDIT_CARD, None)
    with pytest.raises(AppError) as excinfo:
        build_statement_cycles(
            account, dt.date(2026, 1, 1), dt.date(2026, 3, 1), RESOLVER
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_close_day_outside_1_to_31_raises() -> None:
    for bad_day in (0, -1, 32, 99):
        with pytest.raises(AppError) as excinfo:
            build_statement_cycles(
                card(bad_day), dt.date(2026, 1, 1), dt.date(2026, 3, 1), RESOLVER
            )
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_loan_without_a_close_day_is_period_aligned_not_an_error() -> None:
    """The CREDIT_CARD precondition is about cards only; a loan accrues per period."""
    loan = FakeAccount("acct-loan", AccountKind.LOAN, None)
    cycles = build_statement_cycles(
        loan, dt.date(2026, 1, 1), dt.date(2026, 2, 15), RESOLVER
    )
    assert list(cycles) == [
        ("acct-loan:2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1)),
        ("acct-loan:2026-02", dt.date(2026, 2, 1), dt.date(2026, 2, 16)),
    ]


def test_cycles_feed_interest_for_cycle_without_violating_its_precondition() -> None:
    """The two halves of this module compose: every emitted cycle is >= 1 day.

    A card opened on 20 January with a close day of the 15th, viewed on 3 April:
    January's statement has already closed, so the cycles are 27, 28 and 19 days.
    The expected charges are derived longhand below rather than read back off the
    implementation — a test that asserts `interest_for_cycle(...) ==
    interest_for_cycle(...)` proves nothing.

        120_000 * 2199   == 263_880_000        10_000 * 365 == 3_650_000
        263_880_000 * 27 ==   7_124_760_000    // 3_650_000 == 1951  rem 3_610_000
        263_880_000 * 28 ==   7_388_640_000    // 3_650_000 == 2024  rem 1_040_000
        263_880_000 * 19 ==   5_013_720_000    // 3_650_000 == 1373  rem 2_270_000
    """
    cycles = build_statement_cycles(
        CARD, dt.date(2026, 1, 20), dt.date(2026, 4, 3), RESOLVER
    )
    assert [days_in(c) for c in cycles] == [27, 28, 19]
    assert min(days_in(c) for c in cycles) >= 1

    assert 120_000 * 2199 == 263_880_000
    assert 263_880_000 * 27 // 3_650_000 == 1951
    assert 263_880_000 * 28 // 3_650_000 == 2024
    assert 263_880_000 * 19 // 3_650_000 == 1373

    charges = [interest_for_cycle(120_000, 2199, days_in(c)) for c in cycles]
    assert charges == [1951, 2024, 1373]


# ------------------------------------------- build_statement_cycles, module-local

window_dates = st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2050, 12, 31))
close_days = st.one_of(st.none(), st.integers(min_value=1, max_value=31))


@given(window_dates, window_dates, close_days)
def test_property_cycles_are_ascending_contiguous_and_non_overlapping(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    assume(genesis <= as_of_date)
    account = FakeAccount("acct", AccountKind.CREDIT_CARD, close_day)
    if close_day is None:
        account = FakeAccount("acct", AccountKind.SAVINGS, None)

    cycles = build_statement_cycles(account, genesis, as_of_date, RESOLVER)

    assert cycles
    for _, start, end_exclusive in cycles:
        assert start < end_exclusive
    for earlier, later in zip(cycles, cycles[1:], strict=False):
        assert earlier[2] == later[1]


@given(window_dates, window_dates, close_days)
def test_property_no_cycle_starts_after_as_of_date(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    assume(genesis <= as_of_date)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("acct", kind, close_day)

    for _, start, _end in build_statement_cycles(
        account, genesis, as_of_date, RESOLVER
    ):
        assert start <= as_of_date


@given(window_dates, window_dates, close_days)
def test_property_cycles_tile_the_window_exactly(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    """No gaps and no overhang: the cycles cover [genesis, as_of_date] and nothing
    else, so the day counts sum to the inclusive window length."""
    assume(genesis <= as_of_date)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("acct", kind, close_day)

    cycles = build_statement_cycles(account, genesis, as_of_date, RESOLVER)

    assert cycles[0][1] == genesis
    assert cycles[-1][2] == as_of_date + ONE_DAY
    assert sum(days_in(c) for c in cycles) == (as_of_date - genesis).days + 1


@given(window_dates, window_dates, close_days)
def test_property_cycle_ids_are_unique_and_correctly_shaped(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    assume(genesis <= as_of_date)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("wallet-7", kind, close_day)

    ids = [cycle_id for cycle_id, _, _ in build_statement_cycles(
        account, genesis, as_of_date, RESOLVER
    )]

    assert len(ids) == len(set(ids))
    for cycle_id in ids:
        entity_id, _, period_id = cycle_id.partition(":")
        assert entity_id == "wallet-7"
        start, end_exclusive = RESOLVER.bounds(period_id)
        assert start < end_exclusive


@given(window_dates, window_dates, close_days)
def test_property_cycle_ids_are_ascending_by_period(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    """PeriodId is "YYYY-MM", so lexicographic order is chronological order."""
    assume(genesis <= as_of_date)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("acct", kind, close_day)

    period_ids = [
        cycle_id.partition(":")[2]
        for cycle_id in (
            c[0] for c in build_statement_cycles(account, genesis, as_of_date, RESOLVER)
        )
    ]
    assert period_ids == sorted(period_ids)


@given(window_dates, window_dates, close_days)
def test_property_deterministic(
    genesis: dt.date, as_of_date: dt.date, close_day: int | None
) -> None:
    assume(genesis <= as_of_date)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("acct", kind, close_day)

    first = build_statement_cycles(account, genesis, as_of_date, RESOLVER)
    second = build_statement_cycles(account, genesis, as_of_date, RESOLVER)
    assert list(first) == list(second)


@given(window_dates, st.integers(min_value=0, max_value=400), close_days)
def test_property_extending_as_of_date_only_appends(
    genesis: dt.date, span: int, close_day: int | None
) -> None:
    """Cycles are history-independent: asking later never rewrites a closed cycle.

    Every cycle that ended on or before the earlier as_of_date is bit-identical in
    the longer enumeration. This is what lets `fold_statement_cycles` treat a closed
    cycle as settled (PLAN.md §7.4).
    """
    earlier = genesis + dt.timedelta(days=span)
    later = earlier + dt.timedelta(days=31)
    assume(later.year <= 2050)
    kind = AccountKind.SAVINGS if close_day is None else AccountKind.CREDIT_CARD
    account = FakeAccount("acct", kind, close_day)

    short = build_statement_cycles(account, genesis, earlier, RESOLVER)
    long = build_statement_cycles(account, genesis, later, RESOLVER)

    settled = [c for c in short if c[2] <= earlier]
    assert settled == list(long)[: len(settled)]
