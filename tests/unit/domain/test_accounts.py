"""Unit tests for `domain/accounts.py` (CONTRACTS.md §5.2, §8.6; PLAN.md §6.2, §7).

Owned by `module/domain-accounts` (PLAN.md §13.2).

Three conventions, stated once:

* No tolerance anywhere. Every assertion is exact equality (CLAUDE.md §4.6), and the
  two worked examples from PLAN.md §7.2 are transcribed as literal integers rather
  than recomputed by the test — a test that recomputes the formula it is checking only
  proves the code agrees with itself.
* No clock read. Every date and instant is an explicit literal (CLAUDE.md §4.4), so
  these tests answer the same way in 2026 as in 2036.
* The Hypothesis strategies here are deliberately module-local. `tests/properties/`
  and its shared `strategies.py` belong to `module/properties` in Phase 4
  (PLAN.md §13.3).

The properties this module owns, from CLAUDE.md §5.1, are covered below:

  10  actuals are barriers    -> `test_property_pinned_cycle_ignores_backdated_events`
  11  grace period            -> `test_property_paying_every_statement_in_full_...`
  12  interest mode-invariant -> `test_property_interest_is_mode_invariant`
  (balances)                  -> `test_property_outstanding_is_abs_of_a_liability_...`
                                 `test_property_balances_are_order_independent`
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.types import (
    AccountKind,
    BudgetTiming,
    CycleId,
    Minor,
    ObligationStatus,
)
from domain.accounts import (
    AccountBalance,
    StatementCycleSummary,
    derive_obligation_status,
    fold_account_balances,
    fold_statement_cycles,
)
from domain.definitions import Account
from domain.events import (
    AccountOpeningBalance,
    Event,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
)

# --------------------------------------------------------------------------- helpers

RECORDED_AT: Final = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)

CHECKING: Final = "checking"
SAVINGS: Final = "savings"
CARD: Final = "card"
LOAN: Final = "loan"


def _uuid(n: int) -> UUID:
    return UUID(int=n)


def _account(
    entity_id: str,
    kind: AccountKind,
    *,
    name: str = "An account",
    apr_bps: int = 0,
    statement_close_day: int | None = None,
    payment_due_day: int | None = None,
    effective_from: dt.date = dt.date(2020, 1, 1),
    effective_to: dt.date | None = None,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    version: int = 1,
) -> Account:
    return Account(
        version_id=_uuid(900_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name=name,
        kind=kind,
        apr_bps=apr_bps,
        statement_close_day=statement_close_day,
        payment_due_day=payment_due_day,
        budget_timing=budget_timing,
    )


def _card(
    *,
    apr_bps: int = 2199,
    payment_due_day: int | None = 15,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    effective_from: dt.date = dt.date(2020, 1, 1),
    effective_to: dt.date | None = None,
    version: int = 1,
) -> Account:
    return _account(
        CARD,
        AccountKind.CREDIT_CARD,
        name="Visa",
        apr_bps=apr_bps,
        statement_close_day=31,
        payment_due_day=payment_due_day,
        budget_timing=budget_timing,
        effective_from=effective_from,
        effective_to=effective_to,
        version=version,
    )


def _income(n: int, date: dt.date, account_id: str, amount_minor: Minor) -> IncomeReceived:
    return IncomeReceived(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"income:{n}",
        amount_minor=amount_minor,
        source="employer",
        account_id=account_id,
    )


def _gift(n: int, date: dt.date, account_id: str, amount_minor: Minor) -> GiftReceived:
    return GiftReceived(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"gift:{n}",
        amount_minor=amount_minor,
        source="aunt",
        account_id=account_id,
    )


def _expense(
    n: int, date: dt.date, account_id: str, amount_minor: Minor
) -> ExpenseRecorded:
    return ExpenseRecorded(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"expense:{n}",
        amount_minor=amount_minor,
        category="groceries",
        account_id=account_id,
    )


def _opening(
    n: int, date: dt.date, account_id: str, amount_minor: Minor
) -> AccountOpeningBalance:
    return AccountOpeningBalance(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"opening:{n}",
        account_id=account_id,
        amount_minor=amount_minor,
    )


def _transfer(
    n: int,
    date: dt.date,
    from_account_id: str,
    to_account_id: str,
    amount_minor: Minor,
) -> TransferMade:
    return TransferMade(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"transfer:{n}",
        amount_minor=amount_minor,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
    )


def _payment(
    n: int, date: dt.date, account_id: str, amount_minor: Minor
) -> PaymentMade:
    return PaymentMade(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"payment:{n}",
        amount_minor=amount_minor,
        obligation_id="rent:2026-01",
        account_id=account_id,
    )


def _charged(
    n: int, date: dt.date, account_id: str, cycle_id: CycleId, amount_minor: Minor
) -> InterestCharged:
    return InterestCharged(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"charged:{n}",
        account_id=account_id,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def _earned(
    n: int, date: dt.date, account_id: str, cycle_id: CycleId, amount_minor: Minor
) -> InterestEarned:
    return InterestEarned(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"earned:{n}",
        account_id=account_id,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def _drawn(n: int, date: dt.date, amount_minor: Minor) -> SavingsDrawn:
    return SavingsDrawn(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"drawn:{n}",
        amount_minor=amount_minor,
        reason="car repair",
    )


def _ledger(*events: Event) -> tuple[Event, ...]:
    """Sort into the canonical ledger order, `(date, recorded_at, event_id)`."""
    return tuple(sorted(events, key=lambda e: (e.date, e.recorded_at, str(e.event_id))))


def _by_id(balances: Sequence[AccountBalance]) -> dict[str, AccountBalance]:
    return {balance.account_id: balance for balance in balances}


def _first_of_next_month(date: dt.date) -> dt.date:
    if date.month == 12:
        return dt.date(date.year + 1, 1, 1)
    return dt.date(date.year, date.month + 1, 1)


def _month_cycles(
    account_id: str, first_month_start: dt.date, count: int
) -> tuple[tuple[CycleId, dt.date, dt.date], ...]:
    """`count` calendar-month cycles, ascending, contiguous — the shape a card closing
    on the 31st produces (`core/interest.py::build_statement_cycles`)."""
    starts: list[dt.date] = [first_month_start]
    while len(starts) < count:
        starts.append(_first_of_next_month(starts[-1]))
    return tuple(
        (f"{account_id}:{start.year:04d}-{start.month:02d}", start, _first_of_next_month(start))
        for start in starts
    )


# ------------------------------------------------------------- derive_obligation_status


@pytest.mark.parametrize(
    ("amount_minor", "paid_minor", "expected"),
    [
        (10_000, 0, ObligationStatus.UNPAID),
        (10_000, 1, ObligationStatus.PARTIALLY_PAID),
        (10_000, 9_999, ObligationStatus.PARTIALLY_PAID),
        (10_000, 10_000, ObligationStatus.PAID),
        (10_000, 10_001, ObligationStatus.OVERPAID),
        (1, 1, ObligationStatus.PAID),
        # The contract's four rules do not name these; the docstring does.
        (0, 0, ObligationStatus.UNPAID),
        (0, 1, ObligationStatus.OVERPAID),
        (10_000, -1, ObligationStatus.UNPAID),
    ],
)
def test_obligation_status_table(
    amount_minor: Minor, paid_minor: Minor, expected: ObligationStatus
) -> None:
    assert derive_obligation_status(amount_minor, paid_minor) == expected


@given(
    amount_minor=st.integers(min_value=-10**6, max_value=10**6),
    paid_minor=st.integers(min_value=-10**6, max_value=10**6),
)
def test_property_obligation_status_is_total(
    amount_minor: Minor, paid_minor: Minor
) -> None:
    """Every pair of ints classifies, and OVERPAID means exactly what it says."""
    status = derive_obligation_status(amount_minor, paid_minor)
    assert status in set(ObligationStatus)
    if status is ObligationStatus.OVERPAID:
        assert paid_minor > amount_minor
        assert paid_minor > 0
    if status is ObligationStatus.PAID:
        assert paid_minor == amount_minor


@given(
    amount_minor=st.integers(min_value=1, max_value=10**9),
    paid_minor=st.one_of(
        st.integers(min_value=-10, max_value=10**9),
        st.sampled_from([0, 1, 99, 100_001, 999_999]),
    ),
)
def test_property_obligation_status_matches_the_documented_rules(
    amount_minor: Minor, paid_minor: Minor
) -> None:
    status = derive_obligation_status(amount_minor, paid_minor)
    if paid_minor <= 0:
        assert status is ObligationStatus.UNPAID
    elif paid_minor < amount_minor:
        assert status is ObligationStatus.PARTIALLY_PAID
    elif paid_minor == amount_minor:
        assert status is ObligationStatus.PAID
    else:
        assert status is ObligationStatus.OVERPAID


# --------------------------------------------------------------- fold_account_balances


_ACCOUNTS: Final = (
    _account(CHECKING, AccountKind.CHECKING, name="Everyday", version=1),
    _account(SAVINGS, AccountKind.SAVINGS, name="Rainy day", apr_bps=450, version=2),
    _card(version=3),
    _account(LOAN, AccountKind.LOAN, name="Car loan", apr_bps=799, version=4),
)


def test_balances_fold_every_account_touching_event() -> None:
    events = _ledger(
        _opening(1, dt.date(2026, 1, 1), CHECKING, 500_000),
        _income(2, dt.date(2026, 1, 5), CHECKING, 200_000),
        _gift(3, dt.date(2026, 1, 6), CHECKING, 7_500),
        _expense(4, dt.date(2026, 1, 7), CHECKING, 12_345),
        _expense(5, dt.date(2026, 1, 8), CARD, 30_000),
        _payment(6, dt.date(2026, 1, 9), CHECKING, 150_000),
        _transfer(7, dt.date(2026, 2, 2), CHECKING, CARD, 30_000),
        _charged(8, dt.date(2026, 2, 1), CARD, "card:2026-01", 1_234),
        _earned(9, dt.date(2026, 2, 1), SAVINGS, "savings:2026-01", 611),
        _opening(10, dt.date(2026, 1, 1), LOAN, -1_800_000),
    )
    implied = ((dt.date(2026, 1, 31), CHECKING, SAVINGS, 100_000),)

    balances = _by_id(
        fold_account_balances(events, _ACCOUNTS, implied, dt.date(2026, 2, 28))
    )

    assert balances[CHECKING].balance_minor == (
        500_000 + 200_000 + 7_500 - 12_345 - 150_000 - 30_000 - 100_000
    )
    assert balances[SAVINGS].balance_minor == 100_000 + 611
    # Purchase, then the statement payment: a transfer, budget-neutral, and it leaves
    # only the interest owed (PLAN.md §1).
    assert balances[CARD].balance_minor == -30_000 + 30_000 - 1_234
    assert balances[LOAN].balance_minor == -1_800_000

    assert balances[CARD].outstanding_minor == 1_234
    assert balances[LOAN].outstanding_minor == 1_800_000
    assert balances[CHECKING].outstanding_minor is None
    assert balances[SAVINGS].outstanding_minor is None

    assert balances[CARD].cumulative_interest_minor == 1_234
    assert balances[SAVINGS].cumulative_interest_minor == 611
    assert balances[CHECKING].cumulative_interest_minor == 0

    assert balances[CARD].name == "Visa"
    assert balances[CARD].apr_bps == 2199
    assert balances[SAVINGS].kind is AccountKind.SAVINGS


def test_balances_come_back_one_per_account_sorted_by_id() -> None:
    balances = fold_account_balances((), _ACCOUNTS, (), dt.date(2026, 2, 28))
    assert tuple(b.account_id for b in balances) == (CARD, CHECKING, LOAN, SAVINGS)
    assert all(b.balance_minor == 0 for b in balances)
    assert _by_id(balances)[CARD].outstanding_minor == 0


def test_events_and_transfers_after_as_of_date_are_excluded() -> None:
    events = _ledger(
        _income(1, dt.date(2026, 1, 5), CHECKING, 200_000),
        _income(2, dt.date(2026, 2, 5), CHECKING, 300_000),
    )
    implied = (
        (dt.date(2026, 1, 31), CHECKING, SAVINGS, 100_000),
        (dt.date(2026, 2, 28), CHECKING, SAVINGS, 100_000),
    )

    balances = _by_id(
        fold_account_balances(events, _ACCOUNTS, implied, dt.date(2026, 1, 31))
    )
    assert balances[CHECKING].balance_minor == 200_000 - 100_000
    assert balances[SAVINGS].balance_minor == 100_000


def test_a_negative_implied_transfer_drains_savings_back_to_checking() -> None:
    """A shortfall reverses the implied transfer's direction (PLAN.md §6.2)."""
    implied = ((dt.date(2026, 1, 31), CHECKING, SAVINGS, -40_000),)
    balances = _by_id(
        fold_account_balances((), _ACCOUNTS, implied, dt.date(2026, 1, 31))
    )
    assert balances[CHECKING].balance_minor == 40_000
    assert balances[SAVINGS].balance_minor == -40_000


def test_savings_drawn_does_not_move_an_account_balance() -> None:
    """`SavingsDrawn` names no account: it is a budgetary top-up, and the cash side is
    a `TransferMade` when money actually moves. Folding it here would debit savings
    twice for one movement."""
    events = _ledger(
        _opening(1, dt.date(2026, 1, 1), SAVINGS, 500_000),
        _drawn(2, dt.date(2026, 1, 10), 50_000),
    )
    balances = _by_id(
        fold_account_balances(events, _ACCOUNTS, (), dt.date(2026, 1, 31))
    )
    assert balances[SAVINGS].balance_minor == 500_000
    assert balances[CHECKING].balance_minor == 0


def test_a_refund_credits_the_account_it_was_charged_to() -> None:
    events = _ledger(
        _expense(1, dt.date(2026, 1, 8), CARD, 30_000),
        _expense(2, dt.date(2026, 1, 9), CARD, -12_000),
    )
    balances = _by_id(
        fold_account_balances(events, _ACCOUNTS, (), dt.date(2026, 1, 31))
    )
    assert balances[CARD].balance_minor == -18_000
    assert balances[CARD].outstanding_minor == 18_000


def test_events_naming_an_unknown_account_are_ignored() -> None:
    events = _ledger(_income(1, dt.date(2026, 1, 5), "ghost", 200_000))
    balances = fold_account_balances(events, _ACCOUNTS, (), dt.date(2026, 1, 31))
    assert "ghost" not in _by_id(balances)
    assert all(b.balance_minor == 0 for b in balances)


def test_descriptive_fields_come_from_the_version_effective_at_as_of_date() -> None:
    versions = (
        _card(apr_bps=2199, effective_to=dt.date(2026, 2, 1), version=1),
        _card(apr_bps=1099, effective_from=dt.date(2026, 2, 1), version=2),
    )
    january = _by_id(fold_account_balances((), versions, (), dt.date(2026, 1, 15)))
    february = _by_id(fold_account_balances((), versions, (), dt.date(2026, 2, 15)))
    assert january[CARD].apr_bps == 2199
    assert february[CARD].apr_bps == 1099


def test_an_account_with_no_effective_version_still_reports_its_last_one() -> None:
    """Closing every version out must not make money vanish from the answer."""
    versions = (
        _card(apr_bps=2199, effective_to=dt.date(2026, 2, 1), version=1),
        _card(
            apr_bps=1099,
            effective_from=dt.date(2026, 2, 1),
            effective_to=dt.date(2026, 3, 1),
            version=2,
        ),
    )
    events = _ledger(_expense(1, dt.date(2026, 1, 8), CARD, 30_000))
    balances = _by_id(
        fold_account_balances(events, versions, (), dt.date(2026, 6, 1))
    )
    assert balances[CARD].apr_bps == 1099
    assert balances[CARD].balance_minor == -30_000


def test_a_transfer_between_own_accounts_is_balance_neutral_overall() -> None:
    """PLAN.md §1: both sides move, and the total does not."""
    before = fold_account_balances(
        _ledger(_opening(1, dt.date(2026, 1, 1), CHECKING, 500_000)),
        _ACCOUNTS,
        (),
        dt.date(2026, 1, 31),
    )
    after = fold_account_balances(
        _ledger(
            _opening(1, dt.date(2026, 1, 1), CHECKING, 500_000),
            _transfer(2, dt.date(2026, 1, 20), CHECKING, SAVINGS, 123_457),
        ),
        _ACCOUNTS,
        (),
        dt.date(2026, 1, 31),
    )
    assert sum(b.balance_minor for b in before) == sum(b.balance_minor for b in after)


# ------------------------------------------------------- balance strategies + properties

_AWKWARD_MINOR: Final = st.sampled_from(
    [0, 1, -1, 3, -3, 7, 99, 100_001, -100_001, 999_999, -999_999]
)


def _minor_amounts() -> st.SearchStrategy[Minor]:
    """Signed, spanning zero, biased toward amounts that do not divide evenly."""
    return st.one_of(_AWKWARD_MINOR, st.integers(min_value=-10**9, max_value=10**9))


def _positive_minor() -> st.SearchStrategy[Minor]:
    return st.one_of(
        st.sampled_from([1, 3, 7, 99, 100_001, 999_999]),
        st.integers(min_value=1, max_value=10**7),
    )


_LEDGER_ACCOUNTS: Final = (CHECKING, SAVINGS, CARD, LOAN)
_BUSINESS_DATES: Final = st.dates(
    min_value=dt.date(2026, 1, 1), max_value=dt.date(2026, 3, 31)
)


@st.composite
def _ledgers(draw: st.DrawFn) -> tuple[Event, ...]:
    """A coherent ledger over the four known accounts, in canonical order."""
    count = draw(st.integers(min_value=0, max_value=8))
    events: list[Event] = []
    for index in range(count):
        shape = draw(
            st.sampled_from(
                ("income", "gift", "expense", "opening", "transfer", "payment",
                 "charged", "earned", "drawn")
            )
        )
        date = draw(_BUSINESS_DATES)
        account_id = draw(st.sampled_from(_LEDGER_ACCOUNTS))
        if shape == "income":
            events.append(_income(index, date, account_id, draw(_positive_minor())))
        elif shape == "gift":
            events.append(_gift(index, date, account_id, draw(_positive_minor())))
        elif shape == "expense":
            events.append(_expense(index, date, account_id, draw(_minor_amounts())))
        elif shape == "opening":
            events.append(_opening(index, date, account_id, draw(_minor_amounts())))
        elif shape == "transfer":
            other = draw(
                st.sampled_from(
                    tuple(a for a in _LEDGER_ACCOUNTS if a != account_id)
                )
            )
            events.append(
                _transfer(index, date, account_id, other, draw(_positive_minor()))
            )
        elif shape == "payment":
            events.append(_payment(index, date, account_id, draw(_positive_minor())))
        elif shape == "charged":
            events.append(
                _charged(index, date, account_id, "c:2026-01", draw(_positive_minor()))
            )
        elif shape == "earned":
            events.append(
                _earned(index, date, account_id, "c:2026-01", draw(_positive_minor()))
            )
        else:
            events.append(_drawn(index, date, draw(_positive_minor())))
    return _ledger(*events)


@given(events=_ledgers())
@settings(max_examples=200)
def test_property_outstanding_is_abs_of_a_liability_balance_and_none_otherwise(
    events: tuple[Event, ...],
) -> None:
    balances = fold_account_balances(events, _ACCOUNTS, (), dt.date(2026, 3, 31))
    for balance in balances:
        if balance.kind in (AccountKind.CREDIT_CARD, AccountKind.LOAN):
            assert balance.outstanding_minor == abs(balance.balance_minor)
            assert balance.outstanding_minor is not None
            assert balance.outstanding_minor >= 0
        else:
            assert balance.outstanding_minor is None


@given(events=_ledgers(), seed=st.integers(min_value=0, max_value=10**6))
@settings(max_examples=200)
def test_property_balances_are_order_independent(
    events: tuple[Event, ...], seed: int
) -> None:
    """The same event set in any arrival order folds to the same balances. Rotating is
    enough to disturb the order without needing a shuffled copy of the set."""
    if not events:
        return
    offset = seed % len(events)
    rotated = events[offset:] + events[:offset]
    as_of = dt.date(2026, 3, 31)
    assert tuple(fold_account_balances(events, _ACCOUNTS, (), as_of)) == tuple(
        fold_account_balances(rotated, _ACCOUNTS, (), as_of)
    )


@given(events=_ledgers(), amount_minor=_positive_minor())
@settings(max_examples=200)
def test_property_a_transfer_never_changes_the_total_of_all_balances(
    events: tuple[Event, ...], amount_minor: Minor
) -> None:
    as_of = dt.date(2026, 3, 31)
    with_transfer = _ledger(
        *events, _transfer(9_999, dt.date(2026, 2, 14), CHECKING, CARD, amount_minor)
    )
    assert sum(
        b.balance_minor
        for b in fold_account_balances(events, _ACCOUNTS, (), as_of)
    ) == sum(
        b.balance_minor
        for b in fold_account_balances(with_transfer, _ACCOUNTS, (), as_of)
    )


# --------------------------------------------------------------- fold_statement_cycles


def _summaries_by_id(
    summaries: Sequence[StatementCycleSummary],
) -> dict[CycleId, StatementCycleSummary]:
    return {summary.cycle_id: summary for summary in summaries}


# A January cycle plus a deliberately 31-day February one, so the second cycle
# reproduces PLAN.md §7.2 exactly: 120_000 at 2199 bps over 31 days.
_JAN: Final = ("card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1))
_FEB_31_DAYS: Final = ("card:2026-02", dt.date(2026, 2, 1), dt.date(2026, 3, 4))


def test_worked_example_card_interest_from_plan_7_2() -> None:
    """120_000 at 21.99% over 31 days is 2241 (PLAN.md §7.2), transcribed literally."""
    card = _card()
    events = _ledger(_expense(1, dt.date(2026, 1, 5), CARD, 120_000))

    summaries = fold_statement_cycles(card, (card,), events, (_JAN, _FEB_31_DAYS))
    january, february = summaries

    assert january.close_balance_minor == -120_000
    assert january.interest_minor == 0  # no previous statement to have left unpaid
    assert january.grace_applied is True
    assert january.paid_in_full_by_due_date is False

    assert february.close_balance_minor == -120_000
    assert february.interest_minor == 2241
    assert february.is_estimate is True
    assert february.grace_applied is False


def test_worked_example_savings_interest_from_plan_7_2() -> None:
    """500_000 at 4.50% over a 30-day period is 1849 (PLAN.md §7.2)."""
    savings = _account(SAVINGS, AccountKind.SAVINGS, name="Rainy day", apr_bps=450)
    events = _ledger(_opening(1, dt.date(2026, 4, 1), SAVINGS, 500_000))
    april = ("savings:2026-04", dt.date(2026, 4, 1), dt.date(2026, 5, 1))

    (summary,) = fold_statement_cycles(savings, (savings,), events, (april,))

    assert (april[2] - april[1]).days == 30
    assert summary.close_balance_minor == 500_000
    assert summary.interest_minor == 1849
    assert summary.is_estimate is True
    # Grace is a statement rule; an asset accrues every period.
    assert summary.grace_applied is False
    assert summary.paid_in_full_by_due_date is False


def test_the_first_cycle_is_graced_for_a_card_and_never_for_an_asset() -> None:
    """PLAN.md §7.1 draws the line between the kinds: grace is a *credit-card* rule
    ("interest is zero when the previous statement was paid in full by its due date"),
    while an asset simply accrues on its balance at period close and has no grace
    concept at all. So the vacuously-true seed the fold starts from — there was no
    previous statement, hence no unpaid one — must reach a card's first cycle and must
    not reach an asset's, which would otherwise earn nothing in its opening period."""
    card = _card(apr_bps=450)
    savings = _account(SAVINGS, AccountKind.SAVINGS, apr_bps=450)
    january_savings = ("savings:2026-01", dt.date(2026, 1, 1), dt.date(2026, 2, 1))

    (card_first,) = fold_statement_cycles(
        card,
        (card,),
        _ledger(_opening(1, dt.date(2026, 1, 1), CARD, -500_000)),
        (_JAN,),
    )
    (savings_first,) = fold_statement_cycles(
        savings,
        (savings,),
        _ledger(_opening(2, dt.date(2026, 1, 1), SAVINGS, 500_000)),
        (january_savings,),
    )

    assert card_first.close_balance_minor == -500_000
    assert card_first.grace_applied is True
    assert card_first.interest_minor == 0

    assert savings_first.close_balance_minor == 500_000
    assert savings_first.grace_applied is False
    # Same rate, same 31 days, same magnitude: only the grace rule differs.
    assert savings_first.interest_minor == 1910


def test_an_overdrawn_asset_account_accrues_no_interest() -> None:
    checking = _account(CHECKING, AccountKind.CHECKING, apr_bps=450)
    events = _ledger(_opening(1, dt.date(2026, 4, 1), CHECKING, -100_000))
    april = ("checking:2026-04", dt.date(2026, 4, 1), dt.date(2026, 5, 1))

    (summary,) = fold_statement_cycles(checking, (checking,), events, (april,))
    assert summary.close_balance_minor == -100_000
    assert summary.interest_minor == 0


def test_a_card_in_credit_accrues_no_interest() -> None:
    card = _card()
    events = _ledger(_transfer(1, dt.date(2026, 1, 5), CHECKING, CARD, 50_000))
    summaries = fold_statement_cycles(card, (card,), events, (_JAN, _FEB_31_DAYS))
    assert summaries[1].close_balance_minor == 50_000
    assert summaries[1].interest_minor == 0


def test_paying_the_statement_in_full_by_the_due_date_grants_grace() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        # Due 2026-02-15: the close day (31 Jan) has passed the due day (15), so the
        # due date is the 15th of the following month.
        _transfer(2, dt.date(2026, 2, 10), CHECKING, CARD, 120_000),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is True
    assert february.grace_applied is True
    assert february.interest_minor == 0
    assert february.close_balance_minor == 0


def test_payment_on_the_due_date_itself_counts() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 15), CHECKING, CARD, 120_000),
    )
    january, _february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is True


def test_payment_after_the_due_date_does_not_count() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 16), CHECKING, CARD, 120_000),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is False
    assert february.grace_applied is False


def test_a_partial_payment_is_not_paid_in_full() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 10), CHECKING, CARD, 119_999),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is False
    assert february.grace_applied is False
    # One cent left owed over 31 days rounds down to nothing, and that is exact.
    assert february.close_balance_minor == -1
    assert february.interest_minor == 0


def test_purchases_made_in_the_grace_window_do_not_count_as_payment() -> None:
    """A new purchase belongs to the next statement, not to settling this one."""
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _expense(2, dt.date(2026, 2, 10), CARD, -500),  # a refund credits
        _expense(3, dt.date(2026, 2, 11), CARD, 400_000),  # a purchase does not
    )
    january, _february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is False


def test_a_card_with_no_payment_due_day_is_never_paid_in_full() -> None:
    card = _card(payment_due_day=None)
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 2), CHECKING, CARD, 120_000),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is False
    assert february.grace_applied is False


def test_a_due_day_after_the_close_day_settles_in_the_same_month() -> None:
    """Closing on the 10th and due on the 25th: the due date is that same month's."""
    card = _account(
        CARD,
        AccountKind.CREDIT_CARD,
        name="Visa",
        apr_bps=2199,
        statement_close_day=10,
        payment_due_day=25,
    )
    cycles = (
        ("card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 1, 11)),
        ("card:2026-02", dt.date(2026, 1, 11), dt.date(2026, 2, 11)),
    )
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 1, 25), CHECKING, CARD, 120_000),
    )
    first, second = fold_statement_cycles(card, (card,), events, cycles)
    assert first.paid_in_full_by_due_date is True
    assert second.grace_applied is True


def test_a_due_day_past_the_end_of_a_short_month_is_clamped() -> None:
    """Closing 31 January, due on the 31st: February has no 31st, so 28 February."""
    card = _card(payment_due_day=31)
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 28), CHECKING, CARD, 120_000),
    )
    january, _february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is True


def test_a_due_day_equal_to_the_close_day_is_due_the_following_month() -> None:
    """The due date is strictly after the close date, so a card closing on the 10th and
    due on the 10th has a month to pay rather than no window at all."""
    card = _account(
        CARD,
        AccountKind.CREDIT_CARD,
        name="Visa",
        apr_bps=2199,
        statement_close_day=10,
        payment_due_day=10,
    )
    cycles = (
        ("card:2026-01", dt.date(2026, 1, 1), dt.date(2026, 1, 11)),
        ("card:2026-02", dt.date(2026, 1, 11), dt.date(2026, 2, 11)),
    )
    on_time = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 10), CHECKING, CARD, 120_000),
    )
    late = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 11), CHECKING, CARD, 120_000),
    )
    assert fold_statement_cycles(card, (card,), on_time, cycles)[
        0
    ].paid_in_full_by_due_date is True
    assert fold_statement_cycles(card, (card,), late, cycles)[
        0
    ].paid_in_full_by_due_date is False


def test_a_recorded_interest_charge_pins_its_cycle() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _charged(2, dt.date(2026, 3, 3), CARD, "card:2026-02", 2_500),
    )
    _january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert february.interest_minor == 2_500
    assert february.is_estimate is False
    assert february.grace_applied is False


def test_the_last_recorded_charge_for_a_cycle_wins() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _charged(2, dt.date(2026, 3, 1), CARD, "card:2026-02", 2_500),
        _charged(3, dt.date(2026, 3, 2), CARD, "card:2026-02", 2_241),
    )
    _january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert february.interest_minor == 2_241


def test_a_pinned_charge_outranks_grace() -> None:
    """When the bank charged despite a full payment, the bank is right (PLAN.md §7.3),
    and `grace_applied` reports the truth rather than a waiver that did not happen."""
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 10), CHECKING, CARD, 120_000),
        _charged(3, dt.date(2026, 3, 3), CARD, "card:2026-02", 900),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.paid_in_full_by_due_date is True
    assert february.interest_minor == 900
    assert february.is_estimate is False
    assert february.grace_applied is False


def test_a_pinned_charge_of_zero_beside_grace_still_reads_as_a_waiver() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _transfer(2, dt.date(2026, 2, 10), CHECKING, CARD, 120_000),
        _charged(3, dt.date(2026, 3, 3), CARD, "card:2026-02", 0),
    )
    _january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert february.interest_minor == 0
    assert february.is_estimate is False
    assert february.grace_applied is True


def test_estimated_interest_opens_the_next_cycle_and_a_pin_does_not_double_count() -> None:
    """An estimate exists nowhere but this fold, so it must roll forward itself. A
    recorded charge is already in the ledger and moves the balance on its own date."""
    card = _card()
    march = ("card:2026-03", dt.date(2026, 3, 4), dt.date(2026, 4, 4))
    estimated = fold_statement_cycles(
        card,
        (card,),
        _ledger(_expense(1, dt.date(2026, 1, 5), CARD, 120_000)),
        (_JAN, _FEB_31_DAYS, march),
    )
    assert estimated[1].interest_minor == 2241
    assert estimated[2].close_balance_minor == -120_000 - 2241

    pinned = fold_statement_cycles(
        card,
        (card,),
        _ledger(
            _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
            _charged(2, dt.date(2026, 3, 3), CARD, "card:2026-02", 2241),
        ),
        (_JAN, _FEB_31_DAYS, march),
    )
    assert pinned[1].interest_minor == 2241
    assert pinned[1].is_estimate is False
    # Same opening balance for March either way: counted once, never twice.
    assert pinned[2].close_balance_minor == -120_000 - 2241


def test_apr_is_resolved_at_the_cycle_start_not_mid_cycle() -> None:
    """A rate change effective mid-cycle applies from the FOLLOWING cycle, so a past
    cycle's interest never moves because of a rate edit (PLAN.md §7.4)."""
    old = _card(apr_bps=2199, effective_to=dt.date(2026, 2, 15), version=1)
    new = _card(apr_bps=1000, effective_from=dt.date(2026, 2, 15), version=2)
    march = ("card:2026-03", dt.date(2026, 3, 4), dt.date(2026, 4, 4))
    events = _ledger(_expense(1, dt.date(2026, 1, 5), CARD, 120_000))

    summaries = fold_statement_cycles(
        new, (old, new), events, (_JAN, _FEB_31_DAYS, march)
    )
    # February started on the 1st, before the rate change on the 15th: still 21.99%.
    assert summaries[1].interest_minor == 2241
    # March opens on 120_000 + 2241 owed, at the new 10.00% over 31 days.
    assert summaries[2].close_balance_minor == -122_241
    assert summaries[2].interest_minor == 1038


def test_version_resolution_falls_back_to_the_account_argument() -> None:
    """A cycle no version governs uses `account` itself, which is total and is what a
    caller passing a single version expects."""
    card = _card(apr_bps=2199, effective_from=dt.date(2026, 2, 20))
    events = _ledger(_expense(1, dt.date(2026, 1, 5), CARD, 120_000))
    _january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert february.interest_minor == 2241


def test_no_cycles_folds_to_nothing() -> None:
    card = _card()
    assert fold_statement_cycles(card, (card,), (), ()) == ()


def test_events_before_the_first_cycle_still_land_in_it() -> None:
    card = _card()
    events = _ledger(_opening(1, dt.date(2025, 12, 20), CARD, -120_000))
    january, _february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.close_balance_minor == -120_000


def test_another_accounts_events_do_not_move_this_cycle() -> None:
    card = _card()
    events = _ledger(
        _expense(1, dt.date(2026, 1, 5), CARD, 120_000),
        _expense(2, dt.date(2026, 1, 6), CHECKING, 999_999),
        _charged(3, dt.date(2026, 2, 1), LOAN, "card:2026-02", 50_000),
    )
    january, february = fold_statement_cycles(
        card, (card,), events, (_JAN, _FEB_31_DAYS)
    )
    assert january.close_balance_minor == -120_000
    assert february.interest_minor == 2241  # the loan's charge pinned nothing here


def test_interest_is_identical_under_both_budget_timing_modes() -> None:
    events = _ledger(_expense(1, dt.date(2026, 1, 5), CARD, 120_000))
    at_purchase = _card(budget_timing=BudgetTiming.AT_PURCHASE)
    at_payment = _card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT)
    assert tuple(
        fold_statement_cycles(at_purchase, (at_purchase,), events, (_JAN, _FEB_31_DAYS))
    ) == tuple(
        fold_statement_cycles(at_payment, (at_payment,), events, (_JAN, _FEB_31_DAYS))
    )


# ---------------------------------------------------- statement-cycle strategies + properties


@st.composite
def _card_ledgers(draw: st.DrawFn) -> tuple[int, tuple[Minor, ...], int]:
    """`(cycle_count, one purchase per cycle, apr)` for a card closing month-end.

    Purchases are biased toward amounts that do not divide evenly, and the APR range
    spans 0 (a promotional card, which must accrue nothing) through 35.99%.
    """
    count = draw(st.integers(min_value=1, max_value=4))
    purchases = draw(
        st.lists(
            st.one_of(
                st.sampled_from([0, 1, 3, 99, 100_001, 999_999]),
                st.integers(min_value=0, max_value=5_000_000),
            ),
            min_size=count,
            max_size=count,
        )
    )
    apr_bps = draw(
        st.one_of(
            st.sampled_from([0, 1, 2199, 3599]),
            st.integers(min_value=0, max_value=3599),
        )
    )
    return count, tuple(purchases), apr_bps


def _purchase_events(purchases: Sequence[Minor]) -> tuple[Event, ...]:
    """One purchase on the 10th of each successive month from January 2026."""
    return tuple(
        _expense(index, dt.date(2026, 1 + index, 10), CARD, amount)
        for index, amount in enumerate(purchases)
    )


@given(ledger=_card_ledgers())
@settings(max_examples=200)
def test_property_paying_every_statement_in_full_accrues_zero_interest(
    ledger: tuple[int, tuple[Minor, ...], int],
) -> None:
    """CLAUDE.md §5.1 property 11. Each statement is settled on the 5th of the
    following month, inside the window that ends on the 15th."""
    count, purchases, apr_bps = ledger
    card = _card(apr_bps=apr_bps)
    payments = tuple(
        _transfer(100 + index, dt.date(2026, 2 + index, 5), CHECKING, CARD, amount)
        for index, amount in enumerate(purchases)
        if amount > 0
    )
    events = _ledger(*_purchase_events(purchases), *payments)
    summaries = fold_statement_cycles(
        card, (card,), events, _month_cycles(CARD, dt.date(2026, 1, 1), count)
    )

    assert len(summaries) == count
    for index, summary in enumerate(summaries):
        assert summary.interest_minor == 0
        assert summary.grace_applied is True
        assert summary.paid_in_full_by_due_date is True
        assert summary.close_balance_minor == -purchases[index]


@given(ledger=_card_ledgers())
@settings(max_examples=200)
def test_property_interest_is_mode_invariant(
    ledger: tuple[int, tuple[Minor, ...], int],
) -> None:
    """CLAUDE.md §5.1 property 12: timing changes budget recognition, never a figure."""
    count, purchases, apr_bps = ledger
    events = _ledger(*_purchase_events(purchases))
    cycles = _month_cycles(CARD, dt.date(2026, 1, 1), count)
    at_purchase = _card(apr_bps=apr_bps, budget_timing=BudgetTiming.AT_PURCHASE)
    at_payment = _card(
        apr_bps=apr_bps, budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT
    )
    assert tuple(
        fold_statement_cycles(at_purchase, (at_purchase,), events, cycles)
    ) == tuple(fold_statement_cycles(at_payment, (at_payment,), events, cycles))


@given(
    ledger=_card_ledgers(),
    pinned_minor=st.one_of(
        st.sampled_from([0, 1, 4_999]), st.integers(min_value=0, max_value=100_000)
    ),
    backdated_minor=_positive_minor(),
)
@settings(max_examples=200)
def test_property_pinned_cycle_ignores_backdated_events(
    ledger: tuple[int, tuple[Minor, ...], int],
    pinned_minor: Minor,
    backdated_minor: Minor,
) -> None:
    """CLAUDE.md §5.1 property 10: a cycle with a recorded `InterestCharged` produces
    the same figure regardless of any backdated event within that cycle."""
    count, purchases, apr_bps = ledger
    card = _card(apr_bps=apr_bps)
    cycles = _month_cycles(CARD, dt.date(2026, 1, 1), count)
    pinned_cycle_id, start_date, _end_exclusive = cycles[-1]

    base = _ledger(
        *_purchase_events(purchases),
        _charged(500, dt.date(2026, 1 + count, 1), CARD, pinned_cycle_id, pinned_minor),
    )
    backdated = _ledger(
        *base, _expense(501, start_date, CARD, backdated_minor)
    )

    pinned_before = _summaries_by_id(
        fold_statement_cycles(card, (card,), base, cycles)
    )[pinned_cycle_id]
    pinned_after = _summaries_by_id(
        fold_statement_cycles(card, (card,), backdated, cycles)
    )[pinned_cycle_id]

    assert pinned_before.interest_minor == pinned_minor
    assert pinned_after.interest_minor == pinned_minor
    assert pinned_before.is_estimate is False
    assert pinned_after.is_estimate is False


@given(ledger=_card_ledgers())
@settings(max_examples=200)
def test_property_estimated_interest_is_never_negative_and_grace_means_zero(
    ledger: tuple[int, tuple[Minor, ...], int],
) -> None:
    count, purchases, apr_bps = ledger
    card = _card(apr_bps=apr_bps)
    events = _ledger(*_purchase_events(purchases))
    summaries = fold_statement_cycles(
        card, (card,), events, _month_cycles(CARD, dt.date(2026, 1, 1), count)
    )
    for summary in summaries:
        assert summary.interest_minor >= 0
        if summary.grace_applied:
            assert summary.interest_minor == 0
        if summary.interest_minor > 0:
            assert summary.close_balance_minor < 0


@given(ledger=_card_ledgers())
@settings(max_examples=200)
def test_property_folding_the_same_ledger_twice_is_identical(
    ledger: tuple[int, tuple[Minor, ...], int],
) -> None:
    """Determinism: no hidden state, no cache, no clock."""
    count, purchases, apr_bps = ledger
    card = _card(apr_bps=apr_bps)
    events = _ledger(*_purchase_events(purchases))
    cycles = _month_cycles(CARD, dt.date(2026, 1, 1), count)
    assert tuple(fold_statement_cycles(card, (card,), events, cycles)) == tuple(
        fold_statement_cycles(card, (card,), events, cycles)
    )


@given(ledger=_card_ledgers())
@settings(max_examples=100)
def test_property_folding_does_not_mutate_its_inputs(
    ledger: tuple[int, tuple[Minor, ...], int],
) -> None:
    count, purchases, apr_bps = ledger
    card = _card(apr_bps=apr_bps)
    events = _ledger(*_purchase_events(purchases))
    cycles = _month_cycles(CARD, dt.date(2026, 1, 1), count)
    events_before = tuple(event.model_copy(deep=True) for event in events)

    fold_statement_cycles(card, (card,), events, cycles)
    fold_account_balances(events, _ACCOUNTS, (), dt.date(2026, 12, 31))

    assert tuple(events) == events_before
