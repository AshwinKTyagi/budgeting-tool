"""Balance invariants: CLAUDE.md §5.1 properties 13-15.

Owned by `module/properties` (PLAN.md §13.2).

  13  savings reconciliation -> `test_property_13_savings_reconciles_exactly`
  14  no double-counting     -> `test_property_14_a_settled_card_is_recognized_exactly_once`
  15  transfers are neutral  -> `test_property_15_a_transfer_leaves_discretionary_alone`

Property 14 is the one that catches the recognition-principle bug (CLAUDE.md §5.1), so
it is checked twice, from two directions: once against a card whose whole story is in
the ledger, and once by *attribution* — folding the same card story into a background
that cannot mention the card, and asserting the difference the card made is exactly what
was charged to it.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from core.types import AccountKind, BudgetTiming, Minor
from domain.events import (
    AccountOpeningBalance,
    Event,
    InterestEarned,
    SavingsDrawn,
    TransferMade,
)
from domain.projection import project

from tests.properties.strategies import (
    AS_OF,
    CARD,
    CHECKING,
    DEFAULT_DEFINITIONS,
    LEDGER_SETTINGS,
    LOAN,
    PaidInFullLedger,
    SAVINGS,
    SettledCardLedger,
    account_balance,
    business_dates,
    card_interest_total,
    definitions_with_card,
    discretionary_remaining_by_period,
    discretionary_spent_total,
    ledgers,
    paid_in_full_ledgers,
    positive_minor_amounts,
    settled_card_ledgers,
    transfer,
    without_voided,
)

#: Every account a transfer may run between, for property 15. All four kinds, both
#: directions, including the two liabilities.
_TRANSFERABLE = (CHECKING, SAVINGS, CARD, LOAN)


def _savings_delta(event: Event) -> Minor:
    """The effect of one *explicit* event on the savings account, in the contract's terms.

    Only the three terms `SavingsSummary`'s invariant names as reaching savings from the
    ledger: an opening balance, recorded interest earned, and an explicit transfer
    (CONTRACTS.md §5.2). `ledgers()` never lands income on savings or spends from it,
    which is what makes this list exhaustive for a generated ledger rather than merely
    typical of one.
    """
    if isinstance(event, (AccountOpeningBalance, InterestEarned)):
        return event.amount_minor if event.account_id == SAVINGS else 0
    if isinstance(event, TransferMade):
        if event.from_account_id == SAVINGS:
            return -event.amount_minor
        if event.to_account_id == SAVINGS:
            return event.amount_minor
    return 0


# ----------------------------------------------------------------------- property 13


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_13_savings_reconciles_exactly(events: tuple[Event, ...]) -> None:
    """Property 13. Savings balance equals `opening + Σallocations − Σdraws + Σinterest
    ± explicit transfers`, exactly, for any generated event sequence (CLAUDE.md §5.1).

    Each term on the right is recomputed from the surviving ledger rather than read back
    off `State`, so this is a reconciliation and not a restatement of the same sum. The
    two `cumulative_*` fields are checked against independently derived totals first,
    for the same reason: if the projection miscounted a draw, the formula would still
    balance while both sides were wrong.

    Voided events are dropped before summing (CONTRACTS.md §5.1 step 1) — they are
    invisible to the fold, so counting them here would compare two different ledgers.
    """
    state = project(events, DEFAULT_DEFINITIONS, AS_OF)
    live = without_voided(events)

    opening_minor = sum(
        event.amount_minor
        for event in live
        if isinstance(event, AccountOpeningBalance) and event.account_id == SAVINGS
    )
    interest_minor = sum(
        event.amount_minor
        for event in live
        if isinstance(event, InterestEarned) and event.account_id == SAVINGS
    )
    transfers_minor = sum(
        _savings_delta(event) for event in live if isinstance(event, TransferMade)
    )
    drawn_minor = sum(
        event.amount_minor for event in live if isinstance(event, SavingsDrawn)
    )

    assert state.savings.cumulative_drawn_minor == drawn_minor
    assert state.savings.cumulative_interest_minor == interest_minor
    assert state.savings.balance_minor == (
        opening_minor
        + state.savings.cumulative_allocated_minor
        - drawn_minor
        + interest_minor
        + transfers_minor
    )


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_13a_the_savings_account_and_the_savings_summary_agree(
    events: tuple[Event, ...],
) -> None:
    """Property 13, across the two views that must not drift apart.

    `AccountBalance` folds what actually moved; `SavingsSummary` is the budget-side view
    and additionally subtracts deliberate draws, which move no cash of their own — when
    cash really moves the ledger carries a `TransferMade` for it, and counting the draw
    in both places would debit savings twice for one movement (`domain/accounts.py`).

    So the two views differ by exactly `cumulative_drawn_minor`, and nothing else. That
    is a single sentence in the implementation's docstring and an untested claim until
    something asserts it.
    """
    state = project(events, DEFAULT_DEFINITIONS, AS_OF)

    assert account_balance(state, SAVINGS) == (
        state.savings.balance_minor + state.savings.cumulative_drawn_minor
    )


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_13b_the_pending_allocation_is_the_open_periods_share(
    events: tuple[Event, ...],
) -> None:
    """Property 13's boundary: what has posted, and what has not.

    The implied savings transfer posts on the period's last day, in one movement
    (PLAN.md §6.2), so the in-progress period's allocation is deliberately *not* in the
    balance and is reported separately. `cumulative_allocated` must therefore be exactly
    the closed periods' shares and `pending_allocation` exactly the open ones — an
    off-by-one period here would silently move a month's savings into or out of the
    balance.
    """
    state = project(events, DEFAULT_DEFINITIONS, AS_OF)

    assert state.savings.cumulative_allocated_minor == sum(
        period.savings_allocated_minor for period in state.periods if period.is_closed
    )
    assert state.savings.pending_allocation_minor == sum(
        period.savings_allocated_minor
        for period in state.periods
        if not period.is_closed
    )


# ----------------------------------------------------------------------- property 14


@given(sample=settled_card_ledgers())
@LEDGER_SETTINGS
def test_property_14_a_settled_card_is_recognized_exactly_once(
    sample: SettledCardLedger,
) -> None:
    """Property 14. For any ledger, the total discretionary reduction attributable to a
    credit card equals what was charged to it, once fully paid, under **both** timing
    modes (CLAUDE.md §5.1). This is the property that catches the recognition-principle
    bug.

    The card is charged, optionally charged an *actual* interest amount, then settled
    with one transfer for the exact outstanding total. The interest is recorded rather
    than estimated so that the settling amount is computable: an estimate depends on the
    very balance the payment is about to change (PLAN.md §7.3).

    What must come off discretionary is `charged + interest`, exactly once:

    * under `AT_PURCHASE` it arrives as the purchases, plus the cycle's interest — which
      was never recognized anywhere else, so it is separately budget-relevant;
    * under `AT_STATEMENT_PAYMENT` it arrives as the single settling transfer, whose
      amount already contains the interest. Recognizing the interest again on top would
      be the double-count PLAN.md §6.4 names as the easiest bug this flag produces.

    The failure this catches is the obvious implementation: recognizing the purchase at
    the till *and* the statement payment as an outflow. That would report `2 × charged`
    under `AT_PURCHASE` and leave this assertion two-to-one out.
    """
    at_purchase = project(
        sample.events,
        definitions_with_card(budget_timing=BudgetTiming.AT_PURCHASE),
        AS_OF,
    )
    at_payment = project(
        sample.events,
        definitions_with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT),
        AS_OF,
    )

    # "Once fully paid" is a precondition of the property, so it is asserted, not
    # assumed: a card still carrying a balance would make the equality below a claim
    # about an unfinished story.
    assert account_balance(at_purchase, CARD) == 0
    assert account_balance(at_payment, CARD) == 0

    assert card_interest_total(at_purchase) == sample.interest_minor
    assert discretionary_spent_total(at_purchase) == sample.total_recognized_minor
    assert discretionary_spent_total(at_payment) == sample.total_recognized_minor


@given(background=ledgers(include_card=False), sample=paid_in_full_ledgers())
@LEDGER_SETTINGS
def test_property_14a_the_card_costs_the_background_exactly_what_it_charged(
    background: tuple[Event, ...], sample: PaidInFullLedger
) -> None:
    """Property 14, by attribution.

    The version above reads the total off a ledger that contains nothing but the card.
    This one folds the same card story into a *background* ledger generated with
    `include_card=False` — income, gifts, obligations, payments, savings draws,
    transfers between the asset accounts, none of which can touch the card — and
    measures what the card added by difference.

    The difference is what "attributable to a credit card" means, and it is the form
    that would catch a leak in the other direction: an implementation that recognized a
    card payment as an ordinary outflow would raise the background's own total, not just
    the card's.

    Every statement here is settled inside its grace window, so the interest is zero and
    the attributable reduction is exactly what was charged, under either mode.
    """
    card_story = tuple(event for event in sample.events if event.date >= dt.date(2026, 1, 1))
    at_purchase = definitions_with_card(budget_timing=BudgetTiming.AT_PURCHASE)
    at_payment = definitions_with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT)

    for definitions in (at_purchase, at_payment):
        without = project(background, definitions, AS_OF)
        with_card = project((*background, *card_story), definitions, AS_OF)

        assert card_interest_total(with_card) == 0
        assert (
            discretionary_spent_total(with_card) - discretionary_spent_total(without)
            == sample.total_charged_minor
        )


@given(sample=settled_card_ledgers())
@LEDGER_SETTINGS
def test_property_14b_paying_the_card_bill_is_not_itself_an_expense(
    sample: SettledCardLedger,
) -> None:
    """Property 14's mechanism, isolated: under `AT_PURCHASE` the settling transfer
    recognizes nothing at all.

    Paying a credit card bill is a transfer, not an expense, because the purchase was
    already recognized (PLAN.md §1). Dropping the settlement from the ledger must
    therefore leave every period's discretionary spend untouched. If the two differed,
    the payment would be being recognized somewhere, which is precisely the
    double-count.

    The card is **zero-APR here**, and that is load-bearing rather than convenient.
    Against an interest-bearing card, removing the settlement leaves a balance to carry,
    which accrues interest in every later cycle, which correctly reduces discretionary —
    a real future expense the payment prevented, not a recognition of the payment
    itself. (Confirmed on the way in: with the default 21.99% card, dropping a
    settlement of `100` moved `2026-02` through `2026-04` by one minor unit each,
    exactly the carried-balance interest.) Zeroing the rate removes that coupling so
    this test says only what it means to say; the coupling itself is what
    `test_property_9a_...` asserts is live.
    """
    definitions = definitions_with_card(
        budget_timing=BudgetTiming.AT_PURCHASE, card_apr_bps=0
    )
    unsettled = tuple(
        event
        for event in sample.events
        if not (isinstance(event, TransferMade) and event.to_account_id == CARD)
    )

    settled_state = project(sample.events, definitions, AS_OF)
    unsettled_state = project(unsettled, definitions, AS_OF)

    assert discretionary_remaining_by_period(
        settled_state
    ) == discretionary_remaining_by_period(unsettled_state)
    assert account_balance(settled_state, CARD) == 0
    assert account_balance(unsettled_state, CARD) == -sample.total_recognized_minor


# ----------------------------------------------------------------------- property 15


@given(
    events=ledgers(),
    date=business_dates(),
    amount=positive_minor_amounts(),
    pair=st.lists(
        st.sampled_from(_TRANSFERABLE), min_size=2, max_size=2, unique=True
    ),
)
@LEDGER_SETTINGS
def test_property_15_a_transfer_leaves_discretionary_alone(
    events: tuple[Event, ...],
    date: dt.date,
    amount: Minor,
    pair: list[str],
) -> None:
    """Property 15. Inserting any `TransferMade` between own accounts leaves every
    period's `discretionary_remaining` unchanged (CLAUDE.md §5.1).

    Moving money between your own accounts never touches discretionary — that is the
    recognition principle's first consequence (PLAN.md §1), and `TransferMade` exists to
    make the card-payment side non-budgetary.

    Two readings of "any transfer" are excluded here, and both exclusions are the
    contract's rather than this test's convenience:

    * **The card is zero-APR in this world.** Paying a card down genuinely reduces the
      interest it will accrue, and card interest genuinely reduces discretionary under
      `AT_PURCHASE`. That is not a double-count, it is the transfer changing a *future*
      expense; with the rate at zero there is no such coupling and the claim is about
      the transfer alone.
    * **The card is `AT_PURCHASE`.** Under `AT_STATEMENT_PAYMENT` a transfer into the
      card is *defined* to be the recognition point (PLAN.md §6.4), so budget-neutrality
      is false there by design. That mode is covered by property 14 instead.

    The transfer's date is drawn from inside the window, so it cannot move genesis and
    every period is present on both sides — which makes this an equality over the whole
    mapping rather than over whichever periods happened to survive.
    """
    definitions = definitions_with_card(card_apr_bps=0)
    inserted = transfer(
        960_001, date, amount, from_account_id=pair[0], to_account_id=pair[1]
    )

    before = project(events, definitions, AS_OF)
    after = project((*events, inserted), definitions, AS_OF)

    assert discretionary_remaining_by_period(
        before
    ) == discretionary_remaining_by_period(after)


@given(
    events=ledgers(),
    date=business_dates(),
    amount=positive_minor_amounts(),
    pair=st.lists(
        st.sampled_from(_TRANSFERABLE), min_size=2, max_size=2, unique=True
    ),
)
@LEDGER_SETTINGS
def test_property_15a_a_transfer_moves_both_sides_and_nothing_else(
    events: tuple[Event, ...],
    date: dt.date,
    amount: Minor,
    pair: list[str],
) -> None:
    """Property 15's premise: budget-neutral must not mean inert.

    A transfer that changed no balance at all would satisfy property 15 trivially. The
    two named accounts move by `∓amount` and every other account is untouched — which is
    also what makes the *balance* side of the recognition table (PLAN.md §1, "both sides
    move") true rather than merely intended.

    Balances are signed, so a transfer into a liability reduces what is owed and the
    same `+amount` covers both cases with no branch on kind.
    """
    definitions = definitions_with_card(card_apr_bps=0)
    inserted = transfer(
        960_002, date, amount, from_account_id=pair[0], to_account_id=pair[1]
    )

    before = project(events, definitions, AS_OF)
    after = project((*events, inserted), definitions, AS_OF)

    for balance in before.accounts:
        expected = balance.balance_minor
        if balance.account_id == pair[0]:
            expected -= amount
        elif balance.account_id == pair[1]:
            expected += amount
        assert account_balance(after, balance.account_id) == expected


@given(events=ledgers(), date=business_dates(), amount=positive_minor_amounts())
@LEDGER_SETTINGS
def test_property_15b_a_transfer_into_savings_does_not_become_income(
    events: tuple[Event, ...], date: dt.date, amount: Minor
) -> None:
    """Property 15, at the place the mistake would be cheapest to make.

    Moving money into savings is not income and must not be allocated: allocating it
    would split it by the policy and hand a share of it straight back to discretionary,
    which is the opposite of what the user asked for. Allocatable income counts only
    actual `IncomeReceived` and `GiftReceived` (PLAN.md §8.2).
    """
    definitions = definitions_with_card(card_apr_bps=0)
    inserted = transfer(
        960_003, date, amount, from_account_id=CHECKING, to_account_id=SAVINGS
    )

    before = project(events, definitions, AS_OF)
    after = project((*events, inserted), definitions, AS_OF)

    assert tuple(
        (p.period_id, p.allocatable_income_minor, p.savings_allocated_minor)
        for p in before.periods
    ) == tuple(
        (p.period_id, p.allocatable_income_minor, p.savings_allocated_minor)
        for p in after.periods
    )
    assert account_balance(after, SAVINGS) == account_balance(before, SAVINGS) + amount


@given(events=ledgers())
@LEDGER_SETTINGS
def test_a_liability_reports_its_outstanding_as_the_absolute_balance(
    events: tuple[Event, ...],
) -> None:
    """CONTRACTS.md §5.2's sign convention, which every balance property above assumes.

    `balance_minor` is signed and negative means liability; `outstanding_minor` is the
    non-negative face of the same figure, and is `None` for an asset. Getting this
    backwards is what `core/interest.py` refuses a negative input to prevent, so it is
    worth one property of its own.
    """
    state = project(events, DEFAULT_DEFINITIONS, AS_OF)

    for balance in state.accounts:
        if balance.kind in (AccountKind.CREDIT_CARD, AccountKind.LOAN):
            assert balance.outstanding_minor == abs(balance.balance_minor)
        else:
            assert balance.outstanding_minor is None
