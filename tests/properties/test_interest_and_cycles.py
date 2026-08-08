"""Interest and statement cycles: CLAUDE.md §5.1 properties 9-12.

Owned by `module/properties` (PLAN.md §13.2).

   9  cascade determinism  -> `test_property_9_a_backdated_event_lands_where_it_belongs`
  10  actuals are barriers -> `test_property_10_a_pinned_cycle_ignores_backdated_events`
  11  grace period         -> `test_property_11_a_card_paid_in_full_accrues_no_interest`
  12  mode invariance      -> `test_property_12_interest_and_balances_are_mode_invariant`

Cycles chain. Each carries forward its closing balance and whether the statement was
paid in full by its due date, so no cycle can be computed in isolation (PLAN.md §7.4).
That chaining is what makes properties 9 and 10 worth stating separately: the cascade is
real — one backdated receipt does move interest you already looked at — and a recorded
actual is what truncates it.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from core.types import BudgetTiming, Minor
from domain.accounts import StatementCycleSummary
from domain.events import Event, ExpenseRecorded, TransferMade
from domain.projection import PeriodSummary, project

from tests.properties.strategies import (
    AS_OF,
    CARD,
    CARD_CLOSE_DAY,
    LEDGER_SETTINGS,
    BackdatedLedger,
    PaidInFullLedger,
    anchor_event,
    backdated_ledgers,
    business_dates,
    card_interest_total,
    cycle_interest,
    definitions_with_card,
    expense,
    interest_charged,
    ledgers,
    paid_in_full_ledgers,
    positive_minor_amounts,
)

#: The cycle every barrier test pins. With the card closing on the 28th and genesis at
#: 2026-01-01, `card:2026-02` runs [2026-01-29, 2026-03-01) — a full 31-day cycle with a
#: cycle before it and three after, so both the incoming and the outgoing halves of the
#: chain are exercised.
_PINNED_CYCLE = "card:2026-02"

#: What the barrier tests record as the actual. A value no estimate would land on, so an
#: assertion that reads it back cannot be satisfied by the computation it supersedes.
_RECORDED_INTEREST: Minor = 4_242


def _cycle(
    cycles: tuple[StatementCycleSummary, ...], cycle_id: str
) -> StatementCycleSummary:
    """The named cycle. Raises rather than returning `None` — a property about a cycle
    that was never built must fail, not pass vacuously."""
    for cycle in cycles:
        if cycle.cycle_id == cycle_id:
            return cycle
    raise KeyError(cycle_id)


# ------------------------------------------------------------------------ property 9


@given(sample=backdated_ledgers(), position=st.integers(min_value=0, max_value=32))
@LEDGER_SETTINGS
def test_property_9_a_backdated_event_lands_where_it_belongs(
    sample: BackdatedLedger, position: int
) -> None:
    """Property 9. Inserting a backdated event and recomputing yields the same `State`
    as if that event had been present from the start (CLAUDE.md §5.1).

    History-independence, not immunity: backdating *does* change past interest, and it
    should. What must not happen is the answer depending on *when* the event turned up.
    Since every read recomputes from genesis and nothing is ever cached (PLAN.md §3),
    the two are the same fold over the same set — and this property is what would catch
    a cache added later that did not invalidate correctly.

    `position` splices the late event at an arbitrary index rather than only at the
    ends, so the claim covers every arrival point rather than just first and last.
    """
    ledger, late = sample
    at_index = position % (len(ledger) + 1)
    spliced = (*ledger[:at_index], late, *ledger[at_index:])
    definitions = definitions_with_card()

    assert project(spliced, definitions, AS_OF) == project(
        (late, *ledger), definitions, AS_OF
    )


@given(backdated_amount=st.integers(min_value=1_000, max_value=900_000))
@LEDGER_SETTINGS
def test_property_9a_a_backdated_charge_really_does_move_a_past_cycles_interest(
    backdated_amount: Minor,
) -> None:
    """Property 9's premise, asserted rather than assumed.

    Property 9 is an equality, and an equality is satisfied trivially if backdating
    changes nothing at all. This is the companion that shows the cascade is live: a card
    charge backdated into January raises the *February* statement's interest, because
    January's unpaid close balance is what February opens on (PLAN.md §7.4).

    Nothing here is ever paid, so the grace chain never re-engages after the vacuously
    graced first cycle and the whole balance accrues.
    """
    base: tuple[Event, ...] = (
        anchor_event(),
        expense(930_001, dt.date(2026, 1, 10), 500_000, account_id=CARD),
    )
    late = expense(930_002, dt.date(2026, 1, 20), backdated_amount, account_id=CARD)
    definitions = definitions_with_card()

    before = project(base, definitions, AS_OF)
    after = project((*base, late), definitions, AS_OF)

    assert cycle_interest(after, _PINNED_CYCLE) > cycle_interest(before, _PINNED_CYCLE)


# ----------------------------------------------------------------------- property 10


@given(
    background=ledgers(include_card=False),
    charge=positive_minor_amounts(),
    backdated=positive_minor_amounts(),
    backdated_date=business_dates(
        min_value=dt.date(2026, 1, 29), max_value=dt.date(2026, 2, 28)
    ),
)
@LEDGER_SETTINGS
def test_property_10_a_pinned_cycle_ignores_backdated_events(
    background: tuple[Event, ...],
    charge: Minor,
    backdated: Minor,
    backdated_date: dt.date,
) -> None:
    """Property 10. A cycle with a recorded `InterestCharged` produces the same figure
    regardless of any backdated event within that cycle (CLAUDE.md §5.1).

    Entering an actual pins the cycle and truncates the cascade there, which is the main
    practical reason the estimate is not authoritative (PLAN.md §7.4): when the tool and
    the bank disagree, the bank is right.

    The background is generated with `include_card=False` so it cannot contain a second
    `InterestCharged` for this cycle. Two actuals for one cycle are a correction entered
    twice and the later one legitimately wins (CONTRACTS.md §8.6) — a background free to
    produce one would make this test's subject ambiguous rather than make the code
    wrong.

    `backdated_date` is drawn from inside the pinned cycle's own window,
    `[2026-01-29, 2026-03-01)`, which is exactly where the figure must not move.
    """
    definitions = definitions_with_card()
    card_story: tuple[Event, ...] = (
        expense(940_001, dt.date(2026, 1, 10), charge, account_id=CARD),
        interest_charged(
            940_002,
            dt.date(2026, 2, CARD_CLOSE_DAY),
            _PINNED_CYCLE,
            _RECORDED_INTEREST,
        ),
    )
    late = expense(940_003, backdated_date, backdated, account_id=CARD)

    without = project((*background, *card_story), definitions, AS_OF)
    with_late = project((*background, *card_story, late), definitions, AS_OF)

    assert cycle_interest(without, _PINNED_CYCLE) == _RECORDED_INTEREST
    assert cycle_interest(with_late, _PINNED_CYCLE) == _RECORDED_INTEREST
    assert _cycle(with_late.statement_cycles, _PINNED_CYCLE).is_estimate is False


# ----------------------------------------------------------------------- property 11


@given(sample=paid_in_full_ledgers())
@LEDGER_SETTINGS
def test_property_11_a_card_paid_in_full_accrues_no_interest(
    sample: PaidInFullLedger,
) -> None:
    """Property 11. A card whose every statement is paid in full by its due date accrues
    zero interest across any generated ledger (CLAUDE.md §5.1).

    The generator settles each month's statement on the 10th of the following month,
    inside a grace window that runs from the close on the 28th to the due date on the
    15th. Every cycle therefore opens at zero, and each one's grace comes from the
    previous statement having been settled — the chain exercised end to end rather than
    only at its first, vacuously graced link.
    """
    state = project(sample.events, definitions_with_card(), AS_OF)
    card_cycles = tuple(
        cycle for cycle in state.statement_cycles if cycle.account_id == CARD
    )

    assert card_cycles  # the card must actually have been folded
    assert card_interest_total(state) == 0
    for cycle in card_cycles:
        assert cycle.interest_minor == 0


@given(sample=paid_in_full_ledgers())
@LEDGER_SETTINGS
def test_property_11a_a_card_paid_in_full_ends_at_a_zero_balance(
    sample: PaidInFullLedger,
) -> None:
    """Property 11's premise. "Paid in full" has to mean the money actually moved, or
    the zero-interest assertion above is about a card nobody ever used.

    March's statement is settled on 10 April and `as_of` is 30 April, so every charge
    the generator made has been paid by the time the projection is taken.
    """
    state = project(sample.events, definitions_with_card(), AS_OF)
    card = next(balance for balance in state.accounts if balance.account_id == CARD)

    assert card.balance_minor == 0
    assert card.outstanding_minor == 0


# ----------------------------------------------------------------------- property 12

_DISCRETIONARY_FIELDS = frozenset(
    {"discretionary_spent_minor", "discretionary_remaining_minor"}
)


def _without_discretionary(period: PeriodSummary) -> dict[str, object]:
    """A `PeriodSummary` as a mapping, minus the two fields a timing mode may move.

    `discretionary_allocated_minor` is deliberately *not* removed. Allocation is a
    function of income, fixed costs and the policy, none of which `budget_timing`
    touches, so it has to match too.
    """
    return {
        name: value
        for name, value in period.model_dump().items()
        if name not in _DISCRETIONARY_FIELDS
    }


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_12_interest_and_balances_are_mode_invariant(
    events: tuple[Event, ...],
) -> None:
    """Property 12. For the same ledger, computed interest and card outstanding balance
    are identical under `AT_PURCHASE` and `AT_STATEMENT_PAYMENT`; only `discretionary`
    differs (CLAUDE.md §5.1).

    The assertion is deliberately wider than the contract's words: every statement
    cycle, every account balance, every obligation row, the savings summary and the
    warning list must match, and so must every field of every `PeriodSummary` except the
    two that carry discretionary. Narrowing it to "interest and outstanding" would let
    the mode leak into, say, `fixed_due` unnoticed.

    `budget_timing` affects only whether interest additionally reduces discretionary. It
    never affects whether interest is computed, whether it can be overridden, or what
    the card owes (PLAN.md §6.4).
    """
    at_purchase = project(
        events, definitions_with_card(budget_timing=BudgetTiming.AT_PURCHASE), AS_OF
    )
    at_payment = project(
        events,
        definitions_with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT),
        AS_OF,
    )

    assert at_purchase.statement_cycles == at_payment.statement_cycles
    assert at_purchase.accounts == at_payment.accounts
    assert at_purchase.obligations == at_payment.obligations
    assert at_purchase.savings == at_payment.savings
    assert at_purchase.warnings == at_payment.warnings

    for purchase_period, payment_period in zip(
        at_purchase.periods, at_payment.periods, strict=True
    ):
        assert _without_discretionary(purchase_period) == _without_discretionary(
            payment_period
        )


@given(events=ledgers(include_voids=False), charge=positive_minor_amounts())
@LEDGER_SETTINGS
def test_property_12a_the_two_modes_recognize_a_card_at_different_times(
    events: tuple[Event, ...], charge: Minor
) -> None:
    """Property 12, from the other side: the modes must not be *identical* either.

    A charge that has not yet been settled is recognized under `AT_PURCHASE` and not
    under `AT_STATEMENT_PAYMENT`, so the totals differ by exactly what was charged to
    the card, plus the interest `AT_PURCHASE` additionally recognizes, less whatever
    statement payments the ledger happened to make. Pinning the *difference* is what
    stops property 12 from being satisfiable by a `budget_timing` flag nothing reads.

    Voids are excluded from the generated ledger here because the right-hand side is
    computed from the raw events; a voided card charge is invisible to the projection
    and would make the two sides count different sets.
    """
    ledger = (
        *events,
        expense(970_001, dt.date(2026, 1, 15), charge, account_id=CARD),
    )
    at_purchase = project(
        ledger, definitions_with_card(budget_timing=BudgetTiming.AT_PURCHASE), AS_OF
    )
    at_payment = project(
        ledger,
        definitions_with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT),
        AS_OF,
    )

    purchase_spend = sum(p.discretionary_spent_minor for p in at_purchase.periods)
    payment_spend = sum(p.discretionary_spent_minor for p in at_payment.periods)
    card_expenses = sum(
        event.amount_minor
        for event in ledger
        if isinstance(event, ExpenseRecorded) and event.account_id == CARD
    )
    statement_payments = sum(
        event.amount_minor
        for event in ledger
        if isinstance(event, TransferMade) and event.to_account_id == CARD
    )

    assert purchase_spend - payment_spend == (
        card_expenses + card_interest_total(at_purchase) - statement_payments
    )


@given(sample=paid_in_full_ledgers())
@LEDGER_SETTINGS
def test_property_12b_a_settled_card_costs_the_same_under_either_mode(
    sample: PaidInFullLedger,
) -> None:
    """Property 12 meeting property 14: once a card is settled and graced, even the
    discretionary *totals* agree between the two modes.

    Under `AT_PURCHASE` the purchases are recognized, and the interest with them — zero
    here, because every statement was paid inside its grace window. Under
    `AT_STATEMENT_PAYMENT` the settling transfers are recognized instead, and they add
    up to the same number. That is the recognition principle stated as an equality
    between two accounting policies rather than as a rule about one of them.
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

    purchase_spend = sum(p.discretionary_spent_minor for p in at_purchase.periods)
    payment_spend = sum(p.discretionary_spent_minor for p in at_payment.periods)

    assert purchase_spend == payment_spend == sample.total_charged_minor
