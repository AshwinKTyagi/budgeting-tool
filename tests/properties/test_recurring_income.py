"""Properties of the recurring-income forecast view (PLAN.md §8.2, §8.5).

Expanding a `RecurringIncome` is the first cadence-aware schedule in the codebase, and
schedules are exactly where hand-picked examples miss: a person choosing examples picks
the 1st of the month, not the 31st of February or a semimonthly anchor on the 30th.

Every assertion is `==` or a set relation. No tolerance anywhere (CLAUDE.md §4.6).
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from domain.definitions import RecurringIncome, expand_recurring_incomes
from tests.properties.strategies import (
    ARITHMETIC_SETTINGS,
    business_dates,
    recurring_income,
    recurring_incomes,
)


@given(income=recurring_incomes(), start=business_dates(), end=business_dates())
@ARITHMETIC_SETTINGS
def test_expansion_is_deterministic(
    income: RecurringIncome, start: dt.date, end: dt.date
) -> None:
    """Same inputs, same output, always — the projection-level guarantee (CLAUDE.md
    §5.1 property 5) applied to the expansion that feeds it."""
    first = expand_recurring_incomes((income,), start, end)
    second = expand_recurring_incomes((income,), start, end)

    assert first == second


@given(
    incomes=st.lists(recurring_incomes(), min_size=0, max_size=4, unique_by=id),
    start=business_dates(),
    end=business_dates(),
)
@ARITHMETIC_SETTINGS
def test_expansion_is_independent_of_input_order(
    incomes: list[RecurringIncome], start: dt.date, end: dt.date
) -> None:
    """Shuffling the version bundle cannot change the answer. This is what keeps the
    result independent of the order a repository happened to return rows in."""
    distinct = tuple(
        recurring_income(
            entity_id=f"e{index}",
            version=index,
            cadence=income.cadence,
            anchor_day=income.anchor_day,
            amount_minor=income.amount_minor,
            effective_from=income.effective_from,
        )
        for index, income in enumerate(incomes)
    )

    forward = expand_recurring_incomes(distinct, start, end)
    backward = expand_recurring_incomes(tuple(reversed(distinct)), start, end)

    assert forward == backward


@given(income=recurring_incomes(), start=business_dates(), end=business_dates())
@ARITHMETIC_SETTINGS
def test_every_occurrence_lands_inside_both_windows(
    income: RecurringIncome, start: dt.date, end: dt.date
) -> None:
    """No occurrence may precede its own `effective_from`, reach its `effective_to`, or
    escape the requested window. The middle one is half-open, like every other
    definition range in the codebase."""
    rows = expand_recurring_incomes((income,), start, end)

    for row in rows:
        assert start <= row.date <= end
        assert row.date >= income.effective_from
        assert (
            income.effective_to is None
            or row.date < income.effective_to
        )


@given(income=recurring_incomes(), start=business_dates(), end=business_dates())
@ARITHMETIC_SETTINGS
def test_occurrences_are_unique_and_sorted(
    income: RecurringIncome, start: dt.date, end: dt.date
) -> None:
    """`income_id` is the dedupe key of the event a confirmation appends, so two rows
    sharing one would offer the same paycheck twice and let it be entered twice.

    A semimonthly job anchored on the 30th genuinely collapses both of February's dates
    onto the 28th; that must yield one occurrence, not a duplicate.
    """
    rows = expand_recurring_incomes((income,), start, end)

    ids = [row.income_id for row in rows]
    assert len(set(ids)) == len(ids)
    assert list(rows) == sorted(rows, key=lambda row: (row.date, row.income_id))


@given(income=recurring_incomes(), start=business_dates(), end=business_dates())
@ARITHMETIC_SETTINGS
def test_the_id_encodes_the_entity_and_the_date(
    income: RecurringIncome, start: dt.date, end: dt.date
) -> None:
    """The id is parsed back apart by `api/suggestions.py`, so its shape is a contract
    and not a formatting choice."""
    rows = expand_recurring_incomes((income,), start, end)

    for row in rows:
        assert row.income_id == f"expected:income:{row.entity_id}:{row.date.isoformat()}"


@given(income=recurring_incomes(), start=business_dates(), end=business_dates())
@ARITHMETIC_SETTINGS
def test_narrowing_the_window_only_removes_occurrences(
    income: RecurringIncome, start: dt.date, end: dt.date
) -> None:
    """The window filters; it never generates. A suggestion list for a shorter period
    must be a subset of the longer one, or the schedule would depend on when you
    looked."""
    wide = expand_recurring_incomes((income,), start, end)
    narrow = expand_recurring_incomes(
        (income,), start, min(end, start + dt.timedelta(days=20))
    )

    assert {row.income_id for row in narrow} <= {row.income_id for row in wide}
