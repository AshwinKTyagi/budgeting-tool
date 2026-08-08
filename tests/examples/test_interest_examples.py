"""PLAN.md §7.2, transcribed literally. Owned by `module/properties` (PLAN.md §13.2).

CLAUDE.md §5.3: these are regression anchors on the *documented* behavior. **If a
property test and a worked example disagree, the documentation is the specification and
the code is wrong.** One assertion in this file is currently on the wrong side of that
sentence and is marked `xfail(strict=True)`; see
`test_a_savings_account_is_credited_the_interest_it_earned`.

Every intermediate product §7.2 prints is asserted as a literal rather than recomputed.
`assert interest_for_cycle(120_000, 2199, 31) == 120_000 * 2199 * 31 // 3_650_000` would
be satisfied by any implementation that agreed with itself, including one that divided
before multiplying — which is the specific mistake CLAUDE.md §2.1 forbids and which loses
precision integer arithmetic cannot recover. `2241` is not satisfiable that way.

No tolerance (CLAUDE.md §4.6). No clock read: every date is a literal (§4.4).
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import pytest

from core.interest import interest_for_cycle
from core.types import AccountKind, BudgetTiming, Minor
from domain.accounts import StatementCycleSummary
from domain.definitions import Account
from domain.projection import State, project

from tests.properties.strategies import (
    CHECKING,
    SAVINGS,
    account,
    allocation_policy,
    card_account,
    definitions_bundle,
    expense,
    interest_earned,
    opening_balance,
)

# --------------------------------------------------------------- the examples, verbatim
#
#   Card, `apr_bps = 2199` (21.99% APR), statement-close balance `120_000` ($1,200.00),
#   31-day cycle, previous statement not paid in full:
#
#       120000 * 2199              = 263_880_000
#       263_880_000 * 31           = 8_180_280_000
#       10000 * 365                = 3_650_000
#       8_180_280_000 // 3_650_000 = 2241        (exact quotient 2241.17..., floored)
#
#       => interest 2241  ($22.41)
#
#   Savings, `apr_bps = 450` (4.50%), balance `500_000` ($5,000.00), 30-day period:
#
#       500000 * 450 * 30 // 3_650_000 = 1849    ($18.49)
#
#       savings balance      500_000 -> 501_849
#       allocatable_income   unchanged
#       discretionary        unchanged

# -- the card ---------------------------------------------------------------------
_CARD_BALANCE_MINOR: Final[Minor] = 120_000
_CARD_APR_BPS: Final = 2_199
_CARD_CYCLE_DAYS: Final = 31
_CARD_STEP_ONE: Final = 263_880_000  # 120000 * 2199
_CARD_STEP_TWO: Final = 8_180_280_000  # 263_880_000 * 31
_CARD_INTEREST_MINOR: Final[Minor] = 2_241

# -- the savings account ----------------------------------------------------------
_SAVINGS_BALANCE_MINOR: Final[Minor] = 500_000
_SAVINGS_APR_BPS: Final = 450
_SAVINGS_CYCLE_DAYS: Final = 30
_SAVINGS_INTEREST_MINOR: Final[Minor] = 1_849
_SAVINGS_BALANCE_AFTER_MINOR: Final[Minor] = 501_849

#: The actual/365 denominator, as §7.2 prints it.
_DENOMINATOR: Final = 3_650_000

#: The cycle the card example lands in. With the card closing on the 28th and genesis at
#: the 2026-01-10 charge, `card:2026-02` runs [2026-01-29, 2026-03-01) — exactly the
#: 31-day cycle §7.2 describes, and its predecessor closed unpaid.
_CARD_CYCLE_ID: Final = "card:2026-02"

#: The cycle the savings example lands in. Asset accounts are period-aligned (PLAN.md
#: §7.1 accrues on the balance at period close), and April 2026 has 30 days.
_SAVINGS_CYCLE_ID: Final = "savings:2026-04"

_MARCH_15: Final = dt.date(2026, 3, 15)
_APRIL_30: Final = dt.date(2026, 4, 30)


def _cycle(state: State, cycle_id: str) -> StatementCycleSummary:
    """The named cycle. Raises rather than returning `None` — an example that read zero
    for a cycle nobody built would pass while proving nothing."""
    for cycle in state.statement_cycles:
        if cycle.cycle_id == cycle_id:
            return cycle
    raise KeyError(cycle_id)


def _savings_only() -> tuple[Account, ...]:
    """One savings account bearing 4.50%. No checking account, so nothing derives an
    implied transfer and the balance moves only for the reasons §7.2 names."""
    return (
        account(
            SAVINGS,
            AccountKind.SAVINGS,
            name="Savings",
            apr_bps=_SAVINGS_APR_BPS,
            version=2,
        ),
    )


# ------------------------------------------------------- the card, step by printed step


def test_the_card_products_are_what_plan_7_2_prints() -> None:
    """§7.2's first three lines, as literals.

    Multiplication happens before division, so the full product is formed at exact
    precision and only the final quotient is floored (CLAUDE.md §2.1). Dividing the rate
    down first — `120000 * (2199 // 10000)` — would give zero.
    """
    assert _CARD_BALANCE_MINOR * _CARD_APR_BPS == _CARD_STEP_ONE
    assert _CARD_STEP_ONE * _CARD_CYCLE_DAYS == _CARD_STEP_TWO
    assert 10_000 * 365 == _DENOMINATOR


def test_the_card_quotient_is_floored_to_2241() -> None:
    """§7.2's fourth line: `8_180_280_000 // 3_650_000 = 2241`, the exact quotient being
    `2241.17...`.

    Floored, never rounded. `2242` is what a `round()` would give from a float division,
    and `round` is a name the purity gate rejects outright in `core/` and `domain/`
    (CLAUDE.md §2.3, `D003`).
    """
    assert _CARD_STEP_TWO // _DENOMINATOR == _CARD_INTEREST_MINOR
    assert _CARD_STEP_TWO % _DENOMINATOR == 630_000  # the discarded 0.17...


def test_interest_for_cycle_reproduces_the_card_example() -> None:
    """§7.2's conclusion, and the postcondition CONTRACTS.md §8.3 states verbatim:
    `(120_000, 2199, 31) -> 2241`.

    `outstanding_minor` is an absolute amount, never a signed balance — a card carrying
    $1,200 has `balance_minor == -120_000` and `outstanding_minor == 120_000`, and it is
    the latter that comes in here (PLAN.md §7.1).
    """
    assert (
        interest_for_cycle(_CARD_BALANCE_MINOR, _CARD_APR_BPS, _CARD_CYCLE_DAYS)
        == _CARD_INTEREST_MINOR
    )


def test_the_card_example_appears_in_state_as_a_31_day_cycle_charging_2241() -> None:
    """§7.2's card, as the projection reports it.

    $1,200 charged on 10 January and never paid. The January statement closes on the 28th
    unpaid, so February's cycle — [2026-01-29, 2026-03-01), 31 days — is not graced and
    accrues on its close balance of $1,200. Every figure §7.2 names is checked: the day
    count, the signed close balance, and the charge.

    `is_estimate` is `True` because no `InterestCharged` has been entered. The estimate is
    always flagged as such and a recorded actual supersedes it (PLAN.md §7.3).
    """
    definitions = definitions_bundle(
        accounts=(
            account(CHECKING, AccountKind.CHECKING),
            card_account(apr_bps=_CARD_APR_BPS),
        ),
        policies=(allocation_policy(),),
    )
    events = (
        expense(1, dt.date(2026, 1, 10), _CARD_BALANCE_MINOR, account_id="card"),
    )

    state = project(events, definitions, _MARCH_15)
    cycle = _cycle(state, _CARD_CYCLE_ID)

    assert cycle.start_date == dt.date(2026, 1, 29)
    assert cycle.end_date_exclusive == dt.date(2026, 3, 1)
    assert (cycle.end_date_exclusive - cycle.start_date).days == _CARD_CYCLE_DAYS
    assert cycle.close_balance_minor == -_CARD_BALANCE_MINOR
    assert cycle.grace_applied is False
    assert cycle.paid_in_full_by_due_date is False
    assert cycle.interest_minor == _CARD_INTEREST_MINOR
    assert cycle.is_estimate is True


def test_card_interest_reduces_discretionary_once_in_the_period_it_closes() -> None:
    """PLAN.md §1's table, for the §7.2 figure: "Card interest charged | expense".

    Under `AT_PURCHASE` the interest was never recognized anywhere else, so it is
    separately budget-relevant — and it is recognized exactly once, in the period
    containing the cycle's close date. February's `2241` is the §7.2 number arriving in
    the budget.

    The purchase itself is recognized in January, at purchase, for its full `120_000`.
    Asserting both together is what makes this an anchor on the recognition principle
    rather than on interest alone: `120_000` in January and `2_241` in February, never
    `122_241` in either.
    """
    definitions = definitions_bundle(
        accounts=(
            account(CHECKING, AccountKind.CHECKING),
            card_account(apr_bps=_CARD_APR_BPS, budget_timing=BudgetTiming.AT_PURCHASE),
        ),
        policies=(allocation_policy(),),
    )
    events = (
        expense(1, dt.date(2026, 1, 10), _CARD_BALANCE_MINOR, account_id="card"),
    )

    state = project(events, definitions, _MARCH_15)
    spent = {
        period.period_id: period.discretionary_spent_minor for period in state.periods
    }

    assert spent["2026-01"] == _CARD_BALANCE_MINOR
    assert spent["2026-02"] == _CARD_INTEREST_MINOR


# ---------------------------------------------------- the savings account, step by step


def test_interest_for_cycle_reproduces_the_savings_example() -> None:
    """§7.2's second calculation: `500000 * 450 * 30 // 3_650_000 = 1849`.

    Also CONTRACTS.md §8.3's second stated postcondition, `(500_000, 450, 30) -> 1849`.
    For an asset account the amount passed in is the balance itself, which is
    non-negative in the normal case; an overdrawn account accrues nothing and the caller
    passes zero rather than a negative (PLAN.md §7.1).
    """
    assert _SAVINGS_BALANCE_MINOR * _SAVINGS_APR_BPS * _SAVINGS_CYCLE_DAYS == (
        6_750_000_000
    )
    assert 6_750_000_000 // _DENOMINATOR == _SAVINGS_INTEREST_MINOR
    assert (
        interest_for_cycle(
            _SAVINGS_BALANCE_MINOR, _SAVINGS_APR_BPS, _SAVINGS_CYCLE_DAYS
        )
        == _SAVINGS_INTEREST_MINOR
    )


def test_the_savings_example_appears_in_state_as_a_30_day_period_earning_1849() -> None:
    """§7.2's savings account, as the projection computes it.

    $5,000 held from 1 April, `as_of` 30 April. Asset accounts accrue on the balance at
    *period* close, so the cycle is the period: [2026-04-01, 2026-05-01), 30 days. The
    figure is `1849`.

    Grace is a statement rule and never reaches an asset account — a savings account
    earns on its very first period like any other (`domain/accounts.py`). If it were
    graced the way a card's first cycle is, this would report zero.
    """
    definitions = definitions_bundle(
        accounts=_savings_only(), policies=(allocation_policy(),)
    )
    events = (
        opening_balance(
            1, dt.date(2026, 4, 1), _SAVINGS_BALANCE_MINOR, account_id=SAVINGS
        ),
    )

    state = project(events, definitions, _APRIL_30)
    cycle = _cycle(state, _SAVINGS_CYCLE_ID)

    assert cycle.start_date == dt.date(2026, 4, 1)
    assert cycle.end_date_exclusive == dt.date(2026, 5, 1)
    assert (cycle.end_date_exclusive - cycle.start_date).days == _SAVINGS_CYCLE_DAYS
    assert cycle.close_balance_minor == _SAVINGS_BALANCE_MINOR
    assert cycle.interest_minor == _SAVINGS_INTEREST_MINOR
    assert cycle.is_estimate is True


def test_earned_interest_is_not_allocatable_income_and_does_not_touch_discretionary() -> (
    None
):
    """§7.2's last two lines: "allocatable_income unchanged", "discretionary unchanged".

    Interest earned on savings is **not** allocatable income (PLAN.md §7.1). Allocating
    it would split it by the policy and leak half of it back to discretionary, which is
    the rejected alternative in PLAN.md §12's decision log — "otherwise half your savings
    interest leaks back to discretionary".

    Asserted for both the estimated and the recorded form, because the recognition rule
    is about what the money *is*, not about how the figure was arrived at.
    """
    definitions = definitions_bundle(
        accounts=_savings_only(), policies=(allocation_policy(),)
    )
    opening = opening_balance(
        1, dt.date(2026, 4, 1), _SAVINGS_BALANCE_MINOR, account_id=SAVINGS
    )
    recorded = interest_earned(
        2, dt.date(2026, 4, 30), _SAVINGS_CYCLE_ID, _SAVINGS_INTEREST_MINOR
    )

    estimated_state = project((opening,), definitions, _APRIL_30)
    recorded_state = project((opening, recorded), definitions, _APRIL_30)

    for state in (estimated_state, recorded_state):
        for period in state.periods:
            assert period.allocatable_income_minor == 0
            assert period.income_minor == 0
            assert period.gifts_minor == 0
            assert period.discretionary_allocated_minor == 0
            assert period.discretionary_spent_minor == 0
            assert period.discretionary_remaining_minor == 0


def test_a_recorded_actual_supersedes_the_estimate_and_credits_the_account() -> None:
    """§7.2's "savings balance 500_000 -> 501_849", by the path that reaches it today.

    A user-entered `InterestEarned` for the cycle supersedes the estimate (PLAN.md §7.3),
    pins the cycle — `is_estimate` goes `False` — and, being a real ledger event, moves
    the account balance to `501_849`.

    This is the same arithmetic as the estimate above; the difference is only whether the
    figure came from the projection or from the statement. The estimated path does *not*
    reach this balance, which is the defect the next test records.
    """
    definitions = definitions_bundle(
        accounts=_savings_only(), policies=(allocation_policy(),)
    )
    events = (
        opening_balance(
            1, dt.date(2026, 4, 1), _SAVINGS_BALANCE_MINOR, account_id=SAVINGS
        ),
        interest_earned(
            2, dt.date(2026, 4, 30), _SAVINGS_CYCLE_ID, _SAVINGS_INTEREST_MINOR
        ),
    )

    state = project(events, definitions, _APRIL_30)
    savings = next(
        balance for balance in state.accounts if balance.account_id == SAVINGS
    )

    assert _cycle(state, _SAVINGS_CYCLE_ID).interest_minor == _SAVINGS_INTEREST_MINOR
    assert _cycle(state, _SAVINGS_CYCLE_ID).is_estimate is False
    assert savings.balance_minor == _SAVINGS_BALANCE_AFTER_MINOR
    assert savings.cumulative_interest_minor == _SAVINGS_INTEREST_MINOR
    assert state.savings.cumulative_interest_minor == _SAVINGS_INTEREST_MINOR
    assert state.savings.balance_minor == _SAVINGS_BALANCE_AFTER_MINOR


@pytest.mark.xfail(
    strict=True,
    reason=(
        "computed interest never reaches AccountBalance.balance_minor, so PLAN.md §7.2's "
        "'savings balance 500_000 -> 501_849' reports 500_000 — see report"
    ),
)
def test_a_savings_account_is_credited_the_interest_it_earned() -> None:
    """§7.2, the disputed line: **"savings balance `500_000` -> `501_849`"**.

    PLAN.md §7.1 says an asset account's interest "is credited to that same account", and
    §7.2 prints the resulting balance. The projection computes `1849` for this cycle —
    `test_the_savings_example_appears_in_state_as_a_30_day_period_earning_1849` asserts
    exactly that — but `State.accounts` still reports `500_000`, and
    `SavingsSummary.cumulative_interest_minor` still reports `0`.

    The cause is that `fold_account_balances` counts recorded `InterestEarned` /
    `InterestCharged` events only, and its frozen signature (CONTRACTS.md §8.6) takes no
    statement cycles, so estimates structurally cannot reach it. `fold_statement_cycles`,
    meanwhile, *does* carry its own estimates forward into later cycles' close balances —
    so the two views of the same account disagree, which is the sharper form of the
    problem. On the card example above, `card:2026-03` opens on a close balance of
    `-122_241` while `State.accounts` reports the card at `-120_000`.

    This is left `xfail(strict=True)` rather than fixed: `domain/` is not this branch's
    to write (PLAN.md §13.2), and choosing between "estimates credit the balance" and
    "§7.2's line describes the actual only" is a contract decision, not a local one
    (CLAUDE.md §6). Raised in the report.

    Minimal deterministic repro — no Hypothesis, no seed:

        Account(entity_id="savings", kind=SAVINGS, apr_bps=450, effective_from=2020-01-01)
        AccountOpeningBalance(account_id="savings", amount_minor=500_000, date=2026-04-01)
        project(events, definitions, as_of_date=2026-04-30)

        State.statement_cycles["savings:2026-04"].interest_minor == 1849   # computed
        State.accounts["savings"].balance_minor                  == 500_000
                                                        expected == 501_849
    """
    definitions = definitions_bundle(
        accounts=_savings_only(), policies=(allocation_policy(),)
    )
    events = (
        opening_balance(
            1, dt.date(2026, 4, 1), _SAVINGS_BALANCE_MINOR, account_id=SAVINGS
        ),
    )

    state = project(events, definitions, _APRIL_30)
    savings = next(
        balance for balance in state.accounts if balance.account_id == SAVINGS
    )

    # The estimate is there, and it is the §7.2 figure.
    assert _cycle(state, _SAVINGS_CYCLE_ID).interest_minor == _SAVINGS_INTEREST_MINOR

    # PLAN.md §7.2: the balance it is credited to.
    assert savings.balance_minor == _SAVINGS_BALANCE_AFTER_MINOR
