"""Unit tests for `domain/projection.py` (CONTRACTS.md §5, §8.7; PLAN.md §1, §5-§8).

Owned by `module/domain-projection` (PLAN.md §13.2).

Four conventions, stated once:

* No tolerance anywhere. Every assertion is exact equality (CLAUDE.md §4.6), and the
  worked example from PLAN.md §5.2 is transcribed as literal integers rather than
  recomputed — a test that recomputes the formula it is checking only proves the code
  agrees with itself.
* No clock read. Every date and instant is an explicit literal (CLAUDE.md §4.4), so
  these tests answer the same way in 2026 as in 2036.
* The Hypothesis strategies here are deliberately module-local. `tests/properties/` and
  its shared `strategies.py` belong to `module/properties` in Phase 4 (PLAN.md §13.3).
* Anomalies are asserted to be *data*. Every pathological ledger below is projected and
  its `State` inspected; none of them is expected to raise (CONTRACTS.md §7).

The properties from CLAUDE.md §5.1 that this module owns:

   3  the top-level invariant   -> `test_property_the_invariant_holds_for_any_ledger`
   4  policy change is inert    -> `test_property_a_later_policy_leaves_a_closed_...`
   5  determinism               -> `test_property_project_is_deterministic`
   6  ingestion-order           -> `test_property_shuffling_arrival_order_changes_...`
   8  void equivalence          -> `test_property_void_equivalence`
   9  cascade determinism       -> `test_property_a_backdated_event_lands_where_it_...`
  10  actuals are barriers      -> `test_property_a_pinned_cycle_ignores_backdated_...`
  11  grace period              -> `test_property_a_card_paid_in_full_accrues_nothing`
  12  interest mode-invariance  -> `test_property_interest_and_outstanding_are_mode_...`
  13  savings reconciliation    -> `test_property_savings_reconciles_exactly`
  14  no double-counting        -> `test_property_no_double_counting_under_either_mode`
  15  transfers are neutral     -> `test_property_inserting_a_transfer_changes_nothing`
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Final
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from core.periods import CalendarMonthResolver
from core.types import (
    AccountKind,
    BudgetTiming,
    Cadence,
    CycleId,
    Minor,
    ObligationSource,
    ObligationStatus,
    PeriodId,
    WarningCode,
)
from domain.definitions import (
    Account,
    AllocationPolicy,
    Definitions,
    FixedCost,
    RecurringIncome,
)
from domain.events import (
    AccountOpeningBalance,
    Event,
    EventVoided,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
)
from domain.projection import PeriodSummary, State, project

# --------------------------------------------------------------------------- helpers

RECORDED_AT: Final = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
EPOCH: Final = dt.date(2020, 1, 1)

CHECKING: Final = "checking"
SAVINGS: Final = "savings"
CARD: Final = "card"

RESOLVER: Final = CalendarMonthResolver()


def _uuid(n: int) -> UUID:
    return UUID(int=n)


# ------------------------------------------------------------------- definitions


def _account(
    entity_id: str,
    kind: AccountKind,
    *,
    name: str = "An account",
    apr_bps: int = 0,
    statement_close_day: int | None = None,
    payment_due_day: int | None = None,
    effective_from: dt.date = EPOCH,
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
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    statement_close_day: int = 28,
    payment_due_day: int = 15,
    version: int = 5,
) -> Account:
    return _account(
        CARD,
        AccountKind.CREDIT_CARD,
        name="Visa",
        apr_bps=apr_bps,
        statement_close_day=statement_close_day,
        payment_due_day=payment_due_day,
        budget_timing=budget_timing,
        version=version,
    )


def _policy(
    savings_bps: int = 5_000,
    *,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
    entity_id: str = "policy",
    version: int = 1,
) -> AllocationPolicy:
    return AllocationPolicy(
        version_id=_uuid(800_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        savings_bps=savings_bps,
        discretionary_bps=10_000 - savings_bps,
    )


def _fixed_cost(
    entity_id: str = "rent",
    *,
    amount_minor: Minor = 120_000,
    due_day: int = 1,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
    payee: str = "Landlord",
    version: int = 1,
) -> FixedCost:
    return FixedCost(
        version_id=_uuid(700_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name=entity_id,
        amount_minor=amount_minor,
        cadence=Cadence.MONTHLY,
        due_day=due_day,
        payee=payee,
        category="housing",
    )


def _definitions(
    *,
    accounts: Sequence[Account] | None = None,
    policies: Sequence[AllocationPolicy] | None = None,
    fixed_costs: Sequence[FixedCost] = (),
    recurring_incomes: Sequence[RecurringIncome] = (),
) -> Definitions:
    """The seeded shape from CONTRACTS.md §4: one CHECKING, one SAVINGS, one 50/50."""
    if accounts is None:
        accounts = (
            _account(CHECKING, AccountKind.CHECKING, name="Checking", version=1),
            _account(SAVINGS, AccountKind.SAVINGS, name="Savings", version=2),
        )
    if policies is None:
        policies = (_policy(),)
    return Definitions(
        recurring_incomes=tuple(recurring_incomes),
        fixed_costs=tuple(fixed_costs),
        allocation_policies=tuple(policies),
        accounts=tuple(accounts),
    )


DEFS: Final = _definitions()


def _with_card(
    *,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    apr_bps: int = 2199,
    fixed_costs: Sequence[FixedCost] = (),
) -> Definitions:
    return _definitions(
        accounts=(
            _account(CHECKING, AccountKind.CHECKING, name="Checking", version=1),
            _account(SAVINGS, AccountKind.SAVINGS, name="Savings", version=2),
            _card(apr_bps=apr_bps, budget_timing=budget_timing),
        ),
        fixed_costs=fixed_costs,
    )


# ------------------------------------------------------------------------ events


def _income(
    n: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING
) -> IncomeReceived:
    return IncomeReceived(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"income:{n}",
        amount_minor=amount_minor,
        source="employer",
        account_id=account_id,
    )


def _gift(
    n: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING
) -> GiftReceived:
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
    n: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING
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


def _transfer(
    n: int,
    date: dt.date,
    amount_minor: Minor,
    *,
    from_account_id: str = CHECKING,
    to_account_id: str = SAVINGS,
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


def _opening(
    n: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING
) -> AccountOpeningBalance:
    return AccountOpeningBalance(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"opening:{n}",
        account_id=account_id,
        amount_minor=amount_minor,
    )


def _draw(n: int, date: dt.date, amount_minor: Minor) -> SavingsDrawn:
    return SavingsDrawn(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"draw:{n}",
        amount_minor=amount_minor,
        reason="car repair",
    )


def _raised(
    n: int,
    date: dt.date,
    due_date: dt.date,
    amount_minor: Minor,
    *,
    obligation_id: str = "bill:1",
    recurring_id: str | None = None,
) -> ObligationRaised:
    return ObligationRaised(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"raised:{n}",
        obligation_id=obligation_id,
        due_date=due_date,
        amount_minor=amount_minor,
        payee="Landlord",
        category="housing",
        recurring_id=recurring_id,
    )


def _payment(
    n: int,
    date: dt.date,
    amount_minor: Minor,
    *,
    obligation_id: str = "bill:1",
    account_id: str = CHECKING,
) -> PaymentMade:
    return PaymentMade(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"payment:{n}",
        amount_minor=amount_minor,
        obligation_id=obligation_id,
        account_id=account_id,
    )


def _charged(
    n: int, date: dt.date, cycle_id: CycleId, amount_minor: Minor
) -> InterestCharged:
    return InterestCharged(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"charged:{n}",
        account_id=CARD,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def _earned(
    n: int, date: dt.date, cycle_id: CycleId, amount_minor: Minor
) -> InterestEarned:
    return InterestEarned(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"earned:{n}",
        account_id=SAVINGS,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def _void(n: int, date: dt.date, target: Event) -> EventVoided:
    return EventVoided(
        event_id=_uuid(n),
        date=date,
        recorded_at=RECORDED_AT,
        dedupe_key=f"void:{n}",
        target_event_id=target.event_id,
        reason="entered twice",
    )


# ------------------------------------------------------------------- assertions


def _period(state: State, period_id: PeriodId) -> PeriodSummary:
    """The one summary for `period_id`. Fails loudly rather than returning None."""
    matches = [p for p in state.periods if p.period_id == period_id]
    assert len(matches) == 1, f"{period_id} appears {len(matches)} times"
    return matches[0]


def _codes(state: State) -> tuple[WarningCode, ...]:
    return tuple(warning.code for warning in state.warnings)


def _assert_invariant(state: State) -> None:
    """PLAN.md §5.3, on every period. Exact, never approximate."""
    for period in state.periods:
        assert (
            period.fixed_due_minor
            + period.savings_allocated_minor
            + period.discretionary_allocated_minor
            == period.allocatable_income_minor
        )


# =========================================================================== shape


def test_an_empty_ledger_reports_exactly_the_current_period() -> None:
    """Genesis clamps to `as_of_date`, so the answer is one period of zeros.

    Not an error and not an empty `periods`: a user who has entered nothing still has a
    current period, and every consumer of `State` may assume `current_period_id` names
    one of the rows in `periods`.
    """
    state = project((), DEFS, dt.date(2026, 3, 15))

    assert state.current_period_id == "2026-03"
    assert tuple(p.period_id for p in state.periods) == ("2026-03",)
    period = _period(state, "2026-03")
    assert period.allocatable_income_minor == 0
    assert period.savings_allocated_minor == 0
    assert period.discretionary_allocated_minor == 0
    assert period.discretionary_remaining_minor == 0
    assert period.is_closed is False
    assert state.obligations == ()
    assert state.savings.balance_minor == 0
    assert state.warnings == ()


def test_periods_run_from_genesis_through_as_of_ascending_and_contiguous() -> None:
    events = (_income(1, dt.date(2026, 1, 10), 500_000),)

    state = project(events, DEFS, dt.date(2026, 4, 5))

    assert tuple(p.period_id for p in state.periods) == (
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
    )
    for earlier, later in zip(state.periods, state.periods[1:], strict=False):
        assert earlier.end_date_exclusive == later.start_date


def test_a_backdated_obligation_due_date_decides_genesis() -> None:
    """`ObligationRaised` is the one event whose period comes from `due_date`.

    A bill entered in March and due in January is a January obligation, so January must
    be in `periods` — otherwise its `fixed_due` would be recognized nowhere.
    """
    events = (
        _raised(1, dt.date(2026, 3, 2), dt.date(2026, 1, 20), 90_000),
    )

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert state.periods[0].period_id == "2026-01"
    assert _period(state, "2026-01").fixed_due_minor == 90_000
    assert _period(state, "2026-03").fixed_due_minor == 0


def test_a_ledger_entirely_in_the_future_reports_only_the_current_period() -> None:
    """Genesis is capped at `as_of_date`, so the range can never invert."""
    events = (_income(1, dt.date(2027, 6, 1), 500_000),)

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert tuple(p.period_id for p in state.periods) == ("2026-03",)
    assert _period(state, "2026-03").income_minor == 0


def test_the_current_period_is_closed_only_once_it_has_ended() -> None:
    events = (_income(1, dt.date(2026, 1, 10), 500_000),)

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert _period(state, "2026-01").is_closed is True
    assert _period(state, "2026-02").is_closed is True
    assert _period(state, "2026-03").is_closed is False

    at_boundary = project(events, DEFS, dt.date(2026, 3, 1))
    assert _period(at_boundary, "2026-02").is_closed is True
    assert _period(at_boundary, "2026-03").is_closed is False


def test_as_of_truncates_knowledge_not_merely_the_report() -> None:
    """An event dated after `as_of_date` is invisible to every figure in `State`.

    This is what makes a time-travel query honest: the same call answers the same way
    whether it is made today or after three more months of receipts arrive.
    """
    events = (
        _income(1, dt.date(2026, 1, 10), 500_000),
        _expense(2, dt.date(2026, 2, 14), 20_000),
    )

    before = project(events, DEFS, dt.date(2026, 1, 31))
    after = project(events, DEFS, dt.date(2026, 2, 28))

    assert _period(before, "2026-01").discretionary_spent_minor == 0
    assert "2026-02" not in tuple(p.period_id for p in before.periods)
    assert _period(after, "2026-02").discretionary_spent_minor == 20_000

    # And the truncated answer is exactly the answer given a truncated ledger.
    assert before == project(events[:1], DEFS, dt.date(2026, 1, 31))


def test_state_and_everything_reachable_from_it_is_frozen() -> None:
    state = project((_income(1, dt.date(2026, 3, 2), 100_000),), DEFS, dt.date(2026, 3, 15))

    with pytest.raises(ValidationError):
        state.as_of_date = dt.date(2026, 4, 1)
    with pytest.raises(ValidationError):
        state.periods[0].income_minor = 1
    with pytest.raises(ValidationError):
        state.savings.balance_minor = 1
    assert isinstance(state.periods, tuple)
    assert isinstance(state.warnings, tuple)


# ======================================================================= invariant


def test_the_worked_example_from_plan_5_2() -> None:
    """`100_001` under 50/50 resolves to `50001/50000` — savings wins the tie.

    Transcribed literally from PLAN.md §5.2. The documentation is the specification;
    if this and a property test ever disagree, the code is wrong.
    """
    events = (_income(1, dt.date(2026, 3, 4), 100_001),)

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.allocatable_income_minor == 100_001
    assert period.savings_allocated_minor == 50_001
    assert period.discretionary_allocated_minor == 50_000
    assert period.savings_allocated_minor + period.discretionary_allocated_minor == 100_001


def test_the_negative_worked_example_from_plan_5_2() -> None:
    """A remainder of `-100_001` splits to `-50001/-50000`: magnitudes, sign reapplied.

    Nothing is clamped. A period whose fixed costs exceed its income reports negative
    savings *and* negative discretionary (PLAN.md §6.1).
    """
    events = (_raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 100_001),)

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.allocatable_income_minor == 0
    assert period.fixed_due_minor == 100_001
    assert period.savings_allocated_minor == -50_001
    assert period.discretionary_allocated_minor == -50_000


def test_gifts_fold_into_allocatable_income_identically_to_income() -> None:
    events = (
        _income(1, dt.date(2026, 3, 4), 300_000),
        _gift(2, dt.date(2026, 3, 20), 50_000),
    )

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.income_minor == 300_000
    assert period.gifts_minor == 50_000
    assert period.allocatable_income_minor == 350_000
    assert period.savings_allocated_minor == 175_000


def test_fixed_costs_exceeding_income_warn_without_clamping() -> None:
    events = (_income(1, dt.date(2026, 3, 4), 100_000),)
    defs = _definitions(fixed_costs=(_fixed_cost(amount_minor=400_000),))

    state = project(events, defs, dt.date(2026, 3, 31))
    period = _period(state, "2026-03")

    assert period.fixed_due_minor == 400_000
    assert period.savings_allocated_minor == -150_000
    assert period.discretionary_allocated_minor == -150_000
    assert WarningCode.NEGATIVE_ALLOCATION in _codes(state)
    _assert_invariant(state)


def test_recurring_income_is_forecast_only_and_never_allocates() -> None:
    """PLAN.md §8.2. The asymmetry with `FixedCost` is the point: an unpaid bill is
    still owed, an unreceived paycheck cannot be spent."""
    recurring = RecurringIncome(
        version_id=_uuid(600_001),
        entity_id="salary",
        effective_from=EPOCH,
        effective_to=None,
        recorded_at=RECORDED_AT,
        name="Salary",
        amount_minor=500_000,
        cadence=Cadence.MONTHLY,
        anchor_day=1,
        account_id=CHECKING,
    )
    defs = _definitions(recurring_incomes=(recurring,))

    state = project((_income(1, dt.date(2026, 3, 4), 100_000),), defs, dt.date(2026, 3, 31))

    assert _period(state, "2026-03").income_minor == 100_000


# ======================================================================= the policy


def test_a_policy_is_resolved_at_the_period_start() -> None:
    defs = _definitions(
        policies=(
            _policy(5_000, effective_to=dt.date(2026, 3, 1), version=1),
            _policy(8_000, effective_from=dt.date(2026, 3, 1), version=2),
        )
    )
    events = (
        _income(1, dt.date(2026, 2, 10), 100_000),
        _income(2, dt.date(2026, 3, 10), 100_000),
    )

    state = project(events, defs, dt.date(2026, 3, 31))

    assert _period(state, "2026-02").savings_bps == 5_000
    assert _period(state, "2026-02").savings_allocated_minor == 50_000
    assert _period(state, "2026-03").savings_bps == 8_000
    assert _period(state, "2026-03").savings_allocated_minor == 80_000


def test_a_policy_effective_mid_period_applies_from_the_next_period() -> None:
    """PLAN.md §8.3 — this is what makes a closed period immune to a policy change."""
    defs = _definitions(
        policies=(
            _policy(5_000, effective_to=dt.date(2026, 3, 15), version=1),
            _policy(9_000, effective_from=dt.date(2026, 3, 15), version=2),
        )
    )
    events = (
        _income(1, dt.date(2026, 3, 10), 100_000),
        _income(2, dt.date(2026, 4, 10), 100_000),
    )

    state = project(events, defs, dt.date(2026, 4, 30))

    assert _period(state, "2026-03").savings_bps == 5_000
    assert _period(state, "2026-04").savings_bps == 9_000


def test_a_period_no_policy_governs_falls_back_to_the_seeded_default() -> None:
    """A backdated event landing before the first policy still gets a 50/50 answer,
    marked synthetic by `UUID(int=0)`. Raising would contradict the postcondition that
    anomalies are warnings, and there is no `WarningCode` for a missing policy."""
    defs = _definitions(policies=(_policy(8_000, effective_from=dt.date(2026, 3, 1)),))
    events = (_income(1, dt.date(2026, 2, 4), 100_000),)

    state = project(events, defs, dt.date(2026, 3, 31))

    february = _period(state, "2026-02")
    assert february.policy_version_id == UUID(int=0)
    assert february.savings_bps == 5_000
    assert february.savings_allocated_minor == 50_000
    assert _period(state, "2026-03").savings_bps == 8_000


# ============================================================ determinism and order


def test_project_is_deterministic() -> None:
    events = (
        _income(1, dt.date(2026, 1, 10), 321_457),
        _expense(2, dt.date(2026, 1, 11), 4_599),
        _draw(3, dt.date(2026, 2, 2), 10_000),
    )

    assert project(events, DEFS, dt.date(2026, 3, 15)) == project(
        events, DEFS, dt.date(2026, 3, 15)
    )


def test_shuffling_arrival_order_changes_nothing() -> None:
    events: list[Event] = [
        _income(1, dt.date(2026, 1, 10), 321_457),
        _expense(2, dt.date(2026, 1, 11), 4_599),
        _transfer(3, dt.date(2026, 1, 12), 25_000),
        _draw(4, dt.date(2026, 2, 2), 10_000),
        _gift(5, dt.date(2026, 2, 14), 7_500),
    ]

    forwards = project(tuple(events), DEFS, dt.date(2026, 3, 15))
    backwards = project(tuple(reversed(events)), DEFS, dt.date(2026, 3, 15))

    assert forwards == backwards


def test_recorded_at_never_decides_period_membership() -> None:
    """`recorded_at` is audit and tie-break only (CONTRACTS.md §3.1). An event entered
    in June with a March business date is March's."""
    late = IncomeReceived(
        event_id=_uuid(1),
        date=dt.date(2026, 3, 4),
        recorded_at=dt.datetime(2026, 6, 30, 23, 59, tzinfo=dt.timezone.utc),
        dedupe_key="income:late",
        amount_minor=100_000,
        source="employer",
        account_id=CHECKING,
    )

    state = project((late,), DEFS, dt.date(2026, 6, 30))

    assert _period(state, "2026-03").income_minor == 100_000
    assert _period(state, "2026-06").income_minor == 0


def test_an_equivalent_resolver_instance_gives_an_identical_answer() -> None:
    """`CalendarMonthResolver` is stateless, so two instances are interchangeable —
    and passing one explicitly must equal the default."""
    events = (_income(1, dt.date(2026, 3, 4), 100_001),)

    assert project(events, DEFS, dt.date(2026, 3, 31)) == project(
        events, DEFS, dt.date(2026, 3, 31), resolver=CalendarMonthResolver()
    )


# ========================================================================== voiding


def test_a_voided_event_and_its_void_record_both_drop_out() -> None:
    income = _income(1, dt.date(2026, 3, 4), 100_000)
    mistake = _expense(2, dt.date(2026, 3, 5), 250_000)
    events = (income, mistake, _void(3, dt.date(2026, 3, 6), mistake))

    voided = project(events, DEFS, dt.date(2026, 3, 31))
    never_happened = project((income,), DEFS, dt.date(2026, 3, 31))

    assert voided == never_happened
    assert _period(voided, "2026-03").discretionary_spent_minor == 0


def test_voiding_an_income_removes_its_allocation() -> None:
    income = _income(1, dt.date(2026, 3, 4), 100_000)
    events = (income, _void(2, dt.date(2026, 3, 9), income))

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.income_minor == 0
    assert period.savings_allocated_minor == 0
    assert period.discretionary_allocated_minor == 0


def test_two_voids_aimed_at_one_event_collapse_to_the_same_answer() -> None:
    mistake = _expense(1, dt.date(2026, 3, 5), 250_000)
    once = (mistake, _void(2, dt.date(2026, 3, 6), mistake))
    twice = (mistake, _void(2, dt.date(2026, 3, 6), mistake), _void(3, dt.date(2026, 3, 7), mistake))

    assert project(once, DEFS, dt.date(2026, 3, 31)) == project(
        twice, DEFS, dt.date(2026, 3, 31)
    )


def test_a_void_of_an_event_that_is_not_in_the_ledger_is_inert() -> None:
    """The projection must survive a dangling void rather than raising: the ledger is
    append-only and the target may simply be outside the window."""
    absent = _expense(9, dt.date(2027, 1, 1), 1_000)
    events = (_income(1, dt.date(2026, 3, 4), 100_000), _void(2, dt.date(2026, 3, 5), absent))

    state = project(events, DEFS, dt.date(2026, 3, 31))

    assert _period(state, "2026-03").income_minor == 100_000


# ============================================================== recognition, §1


def test_a_transfer_between_own_accounts_is_budget_neutral() -> None:
    """CLAUDE.md §1: moving money between your own accounts never touches the budget."""
    events = (_income(1, dt.date(2026, 3, 4), 400_000),)
    with_transfer = events + (_transfer(2, dt.date(2026, 3, 10), 150_000),)

    without = project(events, DEFS, dt.date(2026, 3, 31))
    with_ = project(with_transfer, DEFS, dt.date(2026, 3, 31))

    assert _period(with_, "2026-03").discretionary_remaining_minor == _period(
        without, "2026-03"
    ).discretionary_remaining_minor
    assert _period(with_, "2026-03").discretionary_spent_minor == 0


def test_a_payment_against_an_obligation_never_touches_discretionary() -> None:
    """Accrual basis: the bill was recognized when it was raised. Paying it is cash."""
    raised = _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000)
    events = (_income(2, dt.date(2026, 3, 4), 400_000), raised)
    paid = events + (_payment(3, dt.date(2026, 3, 5), 120_000),)

    unpaid_state = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")
    paid_state = _period(project(paid, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert paid_state.discretionary_allocated_minor == unpaid_state.discretionary_allocated_minor
    assert paid_state.discretionary_remaining_minor == unpaid_state.discretionary_remaining_minor
    assert paid_state.fixed_due_minor == unpaid_state.fixed_due_minor == 120_000
    assert paid_state.fixed_paid_minor == 120_000
    assert paid_state.fixed_outstanding_minor == 0


def test_a_card_purchase_reduces_discretionary_at_purchase() -> None:
    defs = _with_card(budget_timing=BudgetTiming.AT_PURCHASE)
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _expense(2, dt.date(2026, 3, 5), 60_000, account_id=CARD),
    )

    period = _period(project(events, defs, dt.date(2026, 3, 20)), "2026-03")

    assert period.discretionary_spent_minor == 60_000


def test_paying_the_card_statement_is_a_pure_transfer_under_at_purchase() -> None:
    """The purchase was already recognized. Recognizing the payment too is the
    double-count CLAUDE.md §1 exists to prevent."""
    defs = _with_card(budget_timing=BudgetTiming.AT_PURCHASE)
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _expense(2, dt.date(2026, 3, 5), 60_000, account_id=CARD),
    )
    settled = events + (
        _transfer(3, dt.date(2026, 3, 18), 60_000, from_account_id=CHECKING, to_account_id=CARD),
    )

    unsettled_period = _period(project(events, defs, dt.date(2026, 3, 20)), "2026-03")
    settled_period = _period(project(settled, defs, dt.date(2026, 3, 20)), "2026-03")

    assert settled_period.discretionary_spent_minor == unsettled_period.discretionary_spent_minor
    assert settled_period.discretionary_spent_minor == 60_000


def test_a_card_purchase_does_not_touch_discretionary_under_at_statement_payment() -> None:
    defs = _with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT)
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _expense(2, dt.date(2026, 3, 5), 60_000, account_id=CARD),
    )

    period = _period(project(events, defs, dt.date(2026, 3, 20)), "2026-03")

    assert period.discretionary_spent_minor == 0


def test_the_statement_payment_recognizes_the_full_amount_under_at_statement_payment() -> None:
    defs = _with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT)
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _expense(2, dt.date(2026, 3, 5), 60_000, account_id=CARD),
        _transfer(3, dt.date(2026, 3, 18), 60_000, from_account_id=CHECKING, to_account_id=CARD),
    )

    period = _period(project(events, defs, dt.date(2026, 3, 20)), "2026-03")

    assert period.discretionary_spent_minor == 60_000


def test_switching_a_cards_mode_cannot_re_recognize_a_past_purchase() -> None:
    """`budget_timing` is resolved at the event's own date, so a mode change tomorrow
    leaves every closed period exactly where it was."""
    defs = _definitions(
        accounts=(
            _account(CHECKING, AccountKind.CHECKING, name="Checking", version=1),
            _account(SAVINGS, AccountKind.SAVINGS, name="Savings", version=2),
            _account(
                CARD,
                AccountKind.CREDIT_CARD,
                name="Visa",
                apr_bps=0,
                statement_close_day=28,
                payment_due_day=15,
                budget_timing=BudgetTiming.AT_PURCHASE,
                effective_to=dt.date(2026, 4, 1),
                version=5,
            ),
            _account(
                CARD,
                AccountKind.CREDIT_CARD,
                name="Visa",
                apr_bps=0,
                statement_close_day=28,
                payment_due_day=15,
                budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT,
                effective_from=dt.date(2026, 4, 1),
                version=6,
            ),
        )
    )
    events = (
        _expense(1, dt.date(2026, 3, 5), 60_000, account_id=CARD),
        _expense(2, dt.date(2026, 4, 5), 30_000, account_id=CARD),
    )

    state = project(events, defs, dt.date(2026, 4, 20))

    assert _period(state, "2026-03").discretionary_spent_minor == 60_000
    assert _period(state, "2026-04").discretionary_spent_minor == 0


def test_a_refund_increases_what_is_left_to_spend() -> None:
    """A negative `ExpenseRecorded` is a refund and correctly reduces the total."""
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _expense(2, dt.date(2026, 3, 5), 60_000),
        _expense(3, dt.date(2026, 3, 9), -20_000),
    )

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.discretionary_spent_minor == 40_000
    assert period.discretionary_remaining_minor == 200_000 - 40_000


def test_a_savings_draw_adds_to_what_is_available_and_is_not_income() -> None:
    """A draw tops discretionary up. Treating it as allocatable income would split it
    50/50 and hand half straight back to savings (PLAN.md §6.2)."""
    events = (
        _income(1, dt.date(2026, 3, 2), 400_000),
        _draw(2, dt.date(2026, 3, 10), 30_000),
    )

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.allocatable_income_minor == 400_000
    assert period.savings_drawn_minor == 30_000
    assert period.discretionary_allocated_minor == 200_000
    assert period.discretionary_remaining_minor == 230_000


def test_discretionary_remaining_is_allocated_plus_drawn_less_spent() -> None:
    events = (
        _income(1, dt.date(2026, 3, 2), 400_001),
        _draw(2, dt.date(2026, 3, 10), 30_000),
        _expense(3, dt.date(2026, 3, 12), 45_000),
    )

    period = _period(project(events, DEFS, dt.date(2026, 3, 31)), "2026-03")

    assert period.discretionary_remaining_minor == (
        period.discretionary_allocated_minor
        + period.savings_drawn_minor
        - period.discretionary_spent_minor
    )
    assert period.discretionary_remaining_minor == 200_000 + 30_000 - 45_000


# ====================================================================== obligations


def test_a_fixed_cost_expands_into_one_expected_obligation_per_period() -> None:
    defs = _definitions(fixed_costs=(_fixed_cost(amount_minor=120_000, due_day=1),))

    state = project((_income(1, dt.date(2026, 1, 5), 500_000),), defs, dt.date(2026, 3, 15))

    expected = tuple(row for row in state.obligations if row.source is ObligationSource.EXPECTED)
    assert tuple(row.obligation_id for row in expected) == (
        "expected:rent:2026-01",
        "expected:rent:2026-02",
        "expected:rent:2026-03",
    )
    assert all(row.amount_minor == 120_000 for row in expected)
    assert _period(state, "2026-02").fixed_due_minor == 120_000


def test_an_explicit_obligation_supersedes_the_expected_one_rather_than_summing() -> None:
    """PLAN.md §8.1. Summing is the recognition-principle failure in obligation form."""
    defs = _definitions(fixed_costs=(_fixed_cost(amount_minor=120_000, due_day=1),))
    events = (
        _income(1, dt.date(2026, 3, 2), 500_000),
        _raised(
            2,
            dt.date(2026, 2, 25),
            dt.date(2026, 3, 1),
            127_500,
            obligation_id="rent:2026-03",
            recurring_id="rent",
        ),
    )

    state = project(events, defs, dt.date(2026, 3, 31))
    march_rows = [row for row in state.obligations if row.period_id == "2026-03"]

    assert len(march_rows) == 1
    assert march_rows[0].obligation_id == "rent:2026-03"
    assert march_rows[0].source is ObligationSource.RAISED
    assert march_rows[0].amount_minor == 127_500
    assert _period(state, "2026-03").fixed_due_minor == 127_500


def test_an_obligation_due_after_as_of_is_not_reported() -> None:
    """Its `period_id` would match no `PeriodSummary`, so its `fixed_due` would be
    recognized nowhere. A caller wanting it advances `as_of_date`."""
    events = (
        _raised(1, dt.date(2026, 3, 2), dt.date(2026, 5, 1), 90_000, obligation_id="bill:may"),
    )

    now = project(events, DEFS, dt.date(2026, 3, 15))
    later = project(events, DEFS, dt.date(2026, 5, 15))

    assert now.obligations == ()
    assert tuple(row.obligation_id for row in later.obligations) == ("bill:may",)
    assert _period(later, "2026-05").fixed_due_minor == 90_000


def test_paid_minor_counts_payments_whenever_they_were_made() -> None:
    """`fixed_paid` is obligation-aligned, not payment-dated: it answers "how much of
    the bills due in this period is settled"."""
    events = (
        _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000, obligation_id="rent:march"),
        _payment(2, dt.date(2026, 4, 3), 120_000, obligation_id="rent:march"),
    )

    state = project(events, DEFS, dt.date(2026, 4, 30))
    row = next(row for row in state.obligations if row.obligation_id == "rent:march")

    assert row.paid_minor == 120_000
    assert row.remaining_minor == 0
    assert row.status is ObligationStatus.PAID
    assert _period(state, "2026-03").fixed_paid_minor == 120_000
    assert _period(state, "2026-04").fixed_paid_minor == 0


def test_fixed_outstanding_reconciles_against_the_obligation_rows() -> None:
    defs = _definitions(
        fixed_costs=(
            _fixed_cost("rent", amount_minor=120_000, due_day=1, version=1),
            _fixed_cost("utilities", amount_minor=8_450, due_day=12, version=2),
        )
    )
    events = (
        _income(1, dt.date(2026, 3, 2), 500_000),
        _payment(2, dt.date(2026, 3, 3), 60_000, obligation_id="expected:rent:2026-03"),
    )

    state = project(events, defs, dt.date(2026, 3, 31))
    period = _period(state, "2026-03")
    rows = [row for row in state.obligations if row.period_id == "2026-03"]

    assert period.fixed_due_minor == sum(row.amount_minor for row in rows)
    assert period.fixed_paid_minor == sum(row.paid_minor for row in rows)
    assert period.fixed_outstanding_minor == sum(row.remaining_minor for row in rows)
    assert period.fixed_outstanding_minor == 120_000 + 8_450 - 60_000


def test_a_partially_paid_obligation_reports_its_status() -> None:
    events = (
        _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000, obligation_id="rent"),
        _payment(2, dt.date(2026, 3, 5), 40_000, obligation_id="rent"),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))
    row = next(row for row in state.obligations if row.obligation_id == "rent")

    assert row.status is ObligationStatus.PARTIALLY_PAID
    assert row.remaining_minor == 80_000


def test_an_overpaid_obligation_reports_negative_remaining_and_warns() -> None:
    events = (
        _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000, obligation_id="rent"),
        _payment(2, dt.date(2026, 3, 5), 130_000, obligation_id="rent"),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))
    row = next(row for row in state.obligations if row.obligation_id == "rent")

    assert row.status is ObligationStatus.OVERPAID
    assert row.remaining_minor == -10_000
    assert WarningCode.OBLIGATION_OVERPAID in _codes(state)


def test_a_past_due_unpaid_obligation_warns() -> None:
    events = (
        _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000, obligation_id="rent"),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))

    assert WarningCode.OBLIGATION_PAST_DUE_UNPAID in _codes(state)
    warning = next(w for w in state.warnings if w.code is WarningCode.OBLIGATION_PAST_DUE_UNPAID)
    assert warning.period_id == "2026-03"


def test_an_obligation_not_yet_due_does_not_warn() -> None:
    events = (
        _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 25), 120_000, obligation_id="rent"),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))

    assert WarningCode.OBLIGATION_PAST_DUE_UNPAID not in _codes(state)


def test_a_payment_naming_an_unknown_obligation_warns_and_does_not_raise() -> None:
    """`UNKNOWN_OBLIGATION` is a write-time error only. By projection time the target
    may have been voided, and the projection must survive it (CONTRACTS.md §7.1)."""
    events = (_payment(1, dt.date(2026, 3, 5), 40_000, obligation_id="ghost"),)

    state = project(events, DEFS, dt.date(2026, 3, 20))

    assert WarningCode.PAYMENT_WITHOUT_OBLIGATION in _codes(state)
    warning = next(w for w in state.warnings if w.code is WarningCode.PAYMENT_WITHOUT_OBLIGATION)
    assert warning.event_id == _uuid(1)


def test_paying_a_bill_that_falls_due_after_as_of_is_not_reported_as_orphaned() -> None:
    events = (
        _raised(1, dt.date(2026, 3, 2), dt.date(2026, 5, 1), 90_000, obligation_id="bill:may"),
        _payment(2, dt.date(2026, 3, 10), 90_000, obligation_id="bill:may"),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))

    assert WarningCode.PAYMENT_WITHOUT_OBLIGATION not in _codes(state)


def test_voiding_an_obligation_orphans_its_payment_as_a_warning() -> None:
    raised = _raised(1, dt.date(2026, 3, 1), dt.date(2026, 3, 1), 120_000, obligation_id="rent")
    events = (
        raised,
        _payment(2, dt.date(2026, 3, 5), 120_000, obligation_id="rent"),
        _void(3, dt.date(2026, 3, 6), raised),
    )

    state = project(events, DEFS, dt.date(2026, 3, 20))

    assert WarningCode.PAYMENT_WITHOUT_OBLIGATION in _codes(state)
    assert _period(state, "2026-03").fixed_due_minor == 0


# ================================================================ cycles, interest


def test_cycles_are_grouped_by_account_and_deterministic() -> None:
    defs = _with_card()
    events = (_expense(1, dt.date(2026, 1, 5), 60_000, account_id=CARD),)

    state = project(events, defs, dt.date(2026, 3, 15))

    account_order = tuple(
        account_id
        for account_id, _ in _runs(tuple(c.account_id for c in state.statement_cycles))
    )
    assert account_order == (CARD, CHECKING, SAVINGS)
    assert all(
        cycle.start_date < cycle.end_date_exclusive for cycle in state.statement_cycles
    )


def _runs(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """`("a","a","b")` -> `(("a",2),("b",1))`. A regrouped run means interleaving."""
    runs: list[tuple[str, int]] = []
    for value in values:
        if runs and runs[-1][0] == value:
            runs[-1] = (value, runs[-1][1] + 1)
        else:
            runs.append((value, 1))
    return tuple(runs)


def test_card_interest_is_recognized_once_in_the_period_its_cycle_closes() -> None:
    """Under `AT_PURCHASE` interest is separately budget-relevant: it was never
    recognized anywhere else (PLAN.md §6.4)."""
    defs = _with_card(budget_timing=BudgetTiming.AT_PURCHASE)
    events = (
        _income(1, dt.date(2026, 1, 2), 500_000),
        _expense(2, dt.date(2026, 1, 3), 200_000, account_id=CARD),
    )

    state = project(events, defs, dt.date(2026, 4, 15))
    card_cycles = [c for c in state.statement_cycles if c.account_id == CARD]
    charged = [c for c in card_cycles if c.interest_minor != 0]

    assert charged, "a carried card balance must accrue something"
    for cycle in charged:
        period_id = RESOLVER.period_for(cycle.end_date_exclusive - dt.timedelta(days=1))
        assert period_id in tuple(p.period_id for p in state.periods)

    total_interest = sum(c.interest_minor for c in card_cycles)
    total_spent = sum(p.discretionary_spent_minor for p in state.periods)
    assert total_spent == 200_000 + total_interest


def test_interest_is_not_separately_recognized_under_at_statement_payment() -> None:
    """The payment already contains it. Charging it again is the double-count."""
    defs = _with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT)
    events = (
        _income(1, dt.date(2026, 1, 2), 500_000),
        _expense(2, dt.date(2026, 1, 3), 200_000, account_id=CARD),
    )

    state = project(events, defs, dt.date(2026, 4, 15))

    assert sum(p.discretionary_spent_minor for p in state.periods) == 0
    assert sum(c.interest_minor for c in state.statement_cycles if c.account_id == CARD) != 0


def test_a_recorded_interest_charge_pins_its_cycle() -> None:
    defs = _with_card()
    events = (
        _expense(1, dt.date(2026, 1, 3), 200_000, account_id=CARD),
        _charged(2, dt.date(2026, 2, 28), "card:2026-02", 3_333),
    )

    state = project(events, defs, dt.date(2026, 3, 15))
    pinned = next(c for c in state.statement_cycles if c.cycle_id == "card:2026-02")

    assert pinned.interest_minor == 3_333
    assert pinned.is_estimate is False


def test_a_pinned_cycle_is_recognized_once_not_twice() -> None:
    """The cycle already resolves estimate against actual. Reading the `InterestCharged`
    event as well would recognize a pinned charge twice."""
    defs = _with_card(budget_timing=BudgetTiming.AT_PURCHASE)
    events = (
        _expense(1, dt.date(2026, 2, 3), 200_000, account_id=CARD),
        _charged(2, dt.date(2026, 2, 28), "card:2026-02", 3_333),
    )

    state = project(events, defs, dt.date(2026, 3, 15))
    card_cycles = [c for c in state.statement_cycles if c.account_id == CARD]

    assert sum(p.discretionary_spent_minor for p in state.periods) == 200_000 + sum(
        c.interest_minor for c in card_cycles
    )


def test_estimated_interest_warns_only_when_it_is_non_zero() -> None:
    """Every asset account gets a cycle per period. Flagging zero-interest estimates
    would bury the real ones under a warning per account per period."""
    quiet = project((), DEFS, dt.date(2026, 3, 15))
    assert WarningCode.ESTIMATED_INTEREST not in _codes(quiet)

    defs = _with_card()
    loud = project(
        (_expense(1, dt.date(2026, 1, 3), 200_000, account_id=CARD),),
        defs,
        dt.date(2026, 3, 15),
    )
    assert WarningCode.ESTIMATED_INTEREST in _codes(loud)


def test_a_backdated_card_expense_cascades_through_later_cycles() -> None:
    """PLAN.md §6.3: the cascade is real. One late receipt changes numbers already
    looked at — and the result is history-independent, which is the point."""
    defs = _with_card()
    original = (_expense(1, dt.date(2026, 3, 3), 50_000, account_id=CARD),)
    backdated = _expense(2, dt.date(2026, 1, 5), 300_000, account_id=CARD)

    before = project(original, defs, dt.date(2026, 4, 15))
    after = project(original + (backdated,), defs, dt.date(2026, 4, 15))

    assert after != before
    assert project((backdated,) + original, defs, dt.date(2026, 4, 15)) == after


# ========================================================================== savings


def test_savings_reconciles_exactly() -> None:
    events = (
        _opening(1, dt.date(2026, 1, 1), 250_000, account_id=SAVINGS),
        _income(2, dt.date(2026, 1, 10), 400_000),
        _income(3, dt.date(2026, 2, 10), 400_000),
        _draw(4, dt.date(2026, 2, 20), 30_000),
        _transfer(5, dt.date(2026, 2, 25), 10_000, from_account_id=CHECKING, to_account_id=SAVINGS),
        _earned(6, dt.date(2026, 2, 28), "savings:2026-02", 1_250),
    )

    state = project(events, DEFS, dt.date(2026, 3, 15))
    savings = state.savings

    assert savings.cumulative_allocated_minor == 200_000 + 200_000
    assert savings.cumulative_drawn_minor == 30_000
    assert savings.cumulative_interest_minor == 1_250
    assert savings.balance_minor == (
        250_000
        + savings.cumulative_allocated_minor
        - savings.cumulative_drawn_minor
        + savings.cumulative_interest_minor
        + 10_000
    )


def test_the_in_progress_periods_allocation_is_pending_not_banked() -> None:
    """The implied transfer posts on the period's last day (PLAN.md §6.2), so the
    open period's allocation is reported separately and is not in the balance."""
    events = (
        _income(1, dt.date(2026, 2, 10), 400_000),
        _income(2, dt.date(2026, 3, 10), 100_000),
    )

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert state.savings.cumulative_allocated_minor == 200_000
    assert state.savings.pending_allocation_minor == 50_000
    assert state.savings.balance_minor == 200_000


def test_the_implied_transfer_reaches_the_account_balances_at_period_close() -> None:
    events = (_income(1, dt.date(2026, 2, 10), 400_000),)

    open_period = project(events, DEFS, dt.date(2026, 2, 20))
    closed_period = project(events, DEFS, dt.date(2026, 3, 5))

    def _balance(state: State, account_id: str) -> Minor:
        return next(a.balance_minor for a in state.accounts if a.account_id == account_id)

    assert _balance(open_period, SAVINGS) == 0
    assert _balance(open_period, CHECKING) == 400_000
    assert _balance(closed_period, SAVINGS) == 200_000
    assert _balance(closed_period, CHECKING) == 200_000


def test_a_negative_allocation_drains_savings_back_to_checking() -> None:
    """The amount is signed and the transfer's direction follows it — no branch."""
    defs = _definitions(fixed_costs=(_fixed_cost(amount_minor=400_000),))
    events = (_income(1, dt.date(2026, 2, 10), 100_000),)

    state = project(events, defs, dt.date(2026, 3, 5))

    assert _period(state, "2026-02").savings_allocated_minor == -150_000
    assert state.savings.cumulative_allocated_minor == -150_000
    assert state.savings.balance_minor == -150_000


def test_a_draw_moves_the_budget_side_only() -> None:
    """`domain/accounts.py` ignores `SavingsDrawn` deliberately: when cash really moves
    the ledger carries a `TransferMade`, and folding both would debit savings twice."""
    events = (
        _income(1, dt.date(2026, 2, 10), 400_000),
        _draw(2, dt.date(2026, 3, 2), 30_000),
    )

    state = project(events, DEFS, dt.date(2026, 3, 15))
    savings_account = next(a for a in state.accounts if a.account_id == SAVINGS)

    assert savings_account.balance_minor == 200_000
    assert state.savings.balance_minor == 200_000 - 30_000
    assert state.savings.balance_minor == (
        savings_account.balance_minor - state.savings.cumulative_drawn_minor
    )


def test_a_draw_exceeding_the_available_balance_warns_and_is_never_rejected() -> None:
    events = (_draw(1, dt.date(2026, 3, 2), 30_000),)

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert WarningCode.SAVINGS_DRAW_EXCEEDS_BALANCE in _codes(state)
    assert _period(state, "2026-03").savings_drawn_minor == 30_000


def test_each_overdrawn_draw_warns_once_judged_against_the_ones_before_it() -> None:
    events = (
        _opening(1, dt.date(2026, 1, 1), 50_000, account_id=SAVINGS),
        _draw(2, dt.date(2026, 3, 2), 30_000),
        _draw(3, dt.date(2026, 3, 3), 30_000),
    )

    state = project(events, DEFS, dt.date(2026, 3, 15))
    draw_warnings = [w for w in state.warnings if w.code is WarningCode.SAVINGS_DRAW_EXCEEDS_BALANCE]

    assert len(draw_warnings) == 1
    assert draw_warnings[0].event_id == _uuid(3)


def test_a_draw_covered_by_a_backdated_income_stops_warning() -> None:
    """Backdating means today's impossible state is tomorrow's ordinary one — which is
    exactly why this is data and not an exception (PLAN.md §6.2)."""
    draw = _draw(2, dt.date(2026, 3, 2), 30_000)
    backdated = _income(1, dt.date(2026, 1, 10), 400_000)

    lonely = project((draw,), DEFS, dt.date(2026, 3, 15))
    covered = project((draw, backdated), DEFS, dt.date(2026, 3, 15))

    assert WarningCode.SAVINGS_DRAW_EXCEEDS_BALANCE in _codes(lonely)
    assert WarningCode.SAVINGS_DRAW_EXCEEDS_BALANCE not in _codes(covered)


def test_checking_overdrawn_warns() -> None:
    events = (_expense(1, dt.date(2026, 3, 2), 30_000),)

    state = project(events, DEFS, dt.date(2026, 3, 15))

    assert WarningCode.CHECKING_OVERDRAWN in _codes(state)


def test_with_no_savings_account_every_term_but_the_allocation_is_zero() -> None:
    """Allocation is a budget figure and does not need an account to exist; opening
    balances, interest and explicit transfers all do. Reporting the allocation and
    zeroing the rest is the honest answer rather than an error."""
    defs = _definitions(
        accounts=(_account(CHECKING, AccountKind.CHECKING, name="Checking", version=1),)
    )

    state = project((_income(1, dt.date(2026, 2, 10), 400_000),), defs, dt.date(2026, 3, 15))

    assert _period(state, "2026-02").savings_allocated_minor == 200_000
    assert state.savings.cumulative_allocated_minor == 200_000
    assert state.savings.cumulative_interest_minor == 0
    assert state.savings.cumulative_drawn_minor == 0
    assert state.savings.balance_minor == 200_000


# ======================================================================= properties
#
# Module-local strategies (PLAN.md §13.3). Biased toward the awkward cases CLAUDE.md
# §5.2 names: amounts that do not divide evenly, zero, exactly one minor unit, and
# ledgers whose fixed costs exceed their income.

_AMOUNTS = st.integers(min_value=-500_000, max_value=500_000).filter(lambda n: n != 0)
_ODD_AMOUNTS = st.one_of(
    st.just(1),
    st.just(100_001),
    st.integers(min_value=1, max_value=999_999),
)
_DATES = st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2026, 4, 30))
_BPS = st.sampled_from([0, 1, 3_333, 5_000, 6_667, 9_999, 10_000])


@st.composite
def _ledgers(draw: st.DrawFn) -> tuple[Event, ...]:
    """A coherent event sequence over the seeded accounts."""
    events: list[Event] = []
    count = draw(st.integers(min_value=0, max_value=8))
    for n in range(count):
        kind = draw(st.sampled_from(["income", "gift", "expense", "transfer", "draw", "raised"]))
        date = draw(_DATES)
        amount = draw(_ODD_AMOUNTS)
        if kind == "income":
            events.append(_income(n, date, amount))
        elif kind == "gift":
            events.append(_gift(n, date, amount))
        elif kind == "expense":
            events.append(_expense(n, date, draw(_AMOUNTS)))
        elif kind == "transfer":
            events.append(_transfer(n, date, amount))
        elif kind == "draw":
            events.append(_draw(n, date, amount))
        else:
            events.append(
                _raised(n, date, date, amount, obligation_id=f"bill:{n}")
            )
    return tuple(events)


@given(events=_ledgers(), savings_bps=_BPS)
@settings(deadline=None, max_examples=150)
def test_property_the_invariant_holds_for_any_ledger(
    events: tuple[Event, ...], savings_bps: int
) -> None:
    """Property 3. Exact, for every period, including ledgers where fixed exceeds
    income and where allocatable income is negative."""
    defs = _definitions(policies=(_policy(savings_bps),))

    _assert_invariant(project(events, defs, dt.date(2026, 4, 30)))


@given(events=_ledgers())
@settings(deadline=None, max_examples=100)
def test_property_project_is_deterministic(events: tuple[Event, ...]) -> None:
    """Property 5. Two calls, identical result, no hidden state."""
    assert project(events, DEFS, dt.date(2026, 4, 30)) == project(
        events, DEFS, dt.date(2026, 4, 30)
    )


@given(events=_ledgers(), data=st.data())
@settings(deadline=None, max_examples=100)
def test_property_shuffling_arrival_order_changes_nothing(
    events: tuple[Event, ...], data: st.DataObject
) -> None:
    """Property 6. Not immunity to backdating — see the cascade property below."""
    shuffled = data.draw(st.permutations(events))

    assert project(tuple(shuffled), DEFS, dt.date(2026, 4, 30)) == project(
        events, DEFS, dt.date(2026, 4, 30)
    )


@given(events=_ledgers(), index=st.integers(min_value=0, max_value=7))
@settings(deadline=None, max_examples=100)
def test_property_void_equivalence(events: tuple[Event, ...], index: int) -> None:
    """Property 8. Folding `events` equals folding it with the voided event and its
    `EventVoided` record both removed."""
    if not events:
        return
    target = events[index % len(events)]
    with_void = events + (_void(500_000, dt.date(2026, 4, 30), target),)
    without = tuple(event for event in events if event.event_id != target.event_id)

    assert project(with_void, DEFS, dt.date(2026, 4, 30)) == project(
        without, DEFS, dt.date(2026, 4, 30)
    )


@given(events=_ledgers(), date=_DATES, amount=_ODD_AMOUNTS)
@settings(deadline=None, max_examples=100)
def test_property_a_backdated_event_lands_where_it_would_always_have_been(
    events: tuple[Event, ...], date: dt.date, amount: Minor
) -> None:
    """Property 9. Inserting a backdated event and recomputing yields the same `State`
    as if it had been present from the start — history-independence, not immunity."""
    late = _expense(999, date, amount, account_id=CARD)
    defs = _with_card()

    assert project(events + (late,), defs, dt.date(2026, 4, 30)) == project(
        (late,) + events, defs, dt.date(2026, 4, 30)
    )


@given(events=_ledgers(), amount=_ODD_AMOUNTS)
@settings(deadline=None, max_examples=100)
def test_property_a_pinned_cycle_ignores_backdated_events(
    events: tuple[Event, ...], amount: Minor
) -> None:
    """Property 10. An actual is a barrier: a cycle with a recorded `InterestCharged`
    reports that figure regardless of anything backdated into it."""
    defs = _with_card()
    pinned = _charged(998, dt.date(2026, 2, 28), "card:2026-02", 4_242)
    backdated = _expense(997, dt.date(2026, 2, 10), amount, account_id=CARD)

    without = project(events + (pinned,), defs, dt.date(2026, 4, 30))
    with_ = project(events + (pinned, backdated), defs, dt.date(2026, 4, 30))

    def _cycle(state: State) -> Minor:
        return next(
            c.interest_minor for c in state.statement_cycles if c.cycle_id == "card:2026-02"
        )

    assert _cycle(with_) == _cycle(without) == 4_242


@given(amount=st.integers(min_value=1, max_value=400_000))
@settings(deadline=None, max_examples=60)
def test_property_a_card_paid_in_full_accrues_nothing(amount: Minor) -> None:
    """Property 11. A card whose every statement is paid in full by its due date
    accrues zero interest."""
    defs = _with_card()
    events = (
        _expense(1, dt.date(2026, 1, 10), amount, account_id=CARD),
        # The 28th closes the January statement; paying before the 15th of February
        # settles it inside the grace window.
        _transfer(2, dt.date(2026, 2, 10), amount, from_account_id=CHECKING, to_account_id=CARD),
    )

    state = project(events, defs, dt.date(2026, 3, 20))

    assert all(
        cycle.interest_minor == 0
        for cycle in state.statement_cycles
        if cycle.account_id == CARD
    )


@given(events=_ledgers(), amount=st.integers(min_value=1, max_value=400_000))
@settings(deadline=None, max_examples=100)
def test_property_interest_and_outstanding_are_mode_invariant(
    events: tuple[Event, ...], amount: Minor
) -> None:
    """Property 12. Only `discretionary` may differ between the two timing modes."""
    charge = _expense(996, dt.date(2026, 1, 20), amount, account_id=CARD)
    ledger = events + (charge,)

    at_purchase = project(ledger, _with_card(budget_timing=BudgetTiming.AT_PURCHASE), dt.date(2026, 4, 30))
    at_payment = project(
        ledger, _with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT), dt.date(2026, 4, 30)
    )

    assert tuple(
        (c.cycle_id, c.interest_minor, c.close_balance_minor) for c in at_purchase.statement_cycles
    ) == tuple(
        (c.cycle_id, c.interest_minor, c.close_balance_minor) for c in at_payment.statement_cycles
    )
    assert tuple((a.account_id, a.balance_minor, a.outstanding_minor) for a in at_purchase.accounts) == tuple(
        (a.account_id, a.balance_minor, a.outstanding_minor) for a in at_payment.accounts
    )


@given(amount=st.integers(min_value=1, max_value=400_000))
@settings(deadline=None, max_examples=60)
def test_property_no_double_counting_under_either_mode(amount: Minor) -> None:
    """Property 14. Once a card is fully paid, the total discretionary reduction
    attributable to it equals what was charged to it — plus, under `AT_PURCHASE`, the
    interest that was never recognized anywhere else. This is the property that catches
    the recognition-principle bug."""
    charge = _expense(1, dt.date(2026, 1, 10), amount, account_id=CARD)
    settle = _transfer(
        2, dt.date(2026, 2, 10), amount, from_account_id=CHECKING, to_account_id=CARD
    )
    ledger = (charge, settle)
    as_of = dt.date(2026, 3, 20)

    at_purchase = project(ledger, _with_card(budget_timing=BudgetTiming.AT_PURCHASE), as_of)
    at_payment = project(
        ledger, _with_card(budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT), as_of
    )

    card_interest = sum(
        c.interest_minor for c in at_purchase.statement_cycles if c.account_id == CARD
    )
    assert card_interest == 0  # paid in full inside the grace window
    assert sum(p.discretionary_spent_minor for p in at_purchase.periods) == amount
    assert sum(p.discretionary_spent_minor for p in at_payment.periods) == amount


@given(events=_ledgers(), amount=_ODD_AMOUNTS, date=_DATES)
@settings(deadline=None, max_examples=100)
def test_property_inserting_a_transfer_changes_nothing(
    events: tuple[Event, ...], amount: Minor, date: dt.date
) -> None:
    """Property 15. A `TransferMade` between own accounts leaves every period's
    `discretionary_remaining` unchanged.

    A transfer earlier than anything else in the ledger does move *genesis*, so `after`
    can carry periods `before` never reported. Those are new periods, not changed ones:
    each is asserted to recognize nothing, which is the same claim in the only form
    available when there is no earlier figure to compare against.
    """
    transfer = _transfer(995, date, amount, from_account_id=CHECKING, to_account_id=SAVINGS)

    before = project(events, DEFS, dt.date(2026, 4, 30))
    after = project(events + (transfer,), DEFS, dt.date(2026, 4, 30))

    shared = {p.period_id: p.discretionary_remaining_minor for p in before.periods}
    for period in after.periods:
        if period.period_id in shared:
            assert period.discretionary_remaining_minor == shared[period.period_id]
        else:
            assert period.discretionary_remaining_minor == 0


@given(events=_ledgers())
@settings(deadline=None, max_examples=100)
def test_property_savings_reconciles_exactly(events: tuple[Event, ...]) -> None:
    """Property 13. Balance equals opening + Σallocated − Σdrawn + Σinterest ±
    explicit transfers, exactly, for any generated sequence."""
    state = project(events, DEFS, dt.date(2026, 4, 30))

    opening = sum(
        e.amount_minor
        for e in events
        if isinstance(e, AccountOpeningBalance) and e.account_id == SAVINGS
    )
    transfers = sum(
        (e.amount_minor if e.to_account_id == SAVINGS else 0)
        - (e.amount_minor if e.from_account_id == SAVINGS else 0)
        for e in events
        if isinstance(e, TransferMade)
    )

    assert state.savings.balance_minor == (
        opening
        + state.savings.cumulative_allocated_minor
        - state.savings.cumulative_drawn_minor
        + state.savings.cumulative_interest_minor
        + transfers
    )


@given(events=_ledgers(), savings_bps=_BPS, later_bps=_BPS)
@settings(deadline=None, max_examples=100)
def test_property_a_later_policy_leaves_a_closed_period_bit_identical(
    events: tuple[Event, ...], savings_bps: int, later_bps: int
) -> None:
    """Property 4. A policy whose `effective_from` is after a period's start cannot
    move that period's numbers."""
    base = _definitions(policies=(_policy(savings_bps, effective_to=dt.date(2026, 3, 1)),))
    amended = _definitions(
        policies=(
            _policy(savings_bps, effective_to=dt.date(2026, 3, 1), version=1),
            _policy(later_bps, effective_from=dt.date(2026, 3, 1), version=2),
        )
    )

    before = project(events, base, dt.date(2026, 4, 30))
    after = project(events, amended, dt.date(2026, 4, 30))

    assert tuple(p for p in before.periods if p.period_id < "2026-03") == tuple(
        p for p in after.periods if p.period_id < "2026-03"
    )


@given(events=_ledgers())
@settings(deadline=None, max_examples=100)
def test_property_anomalies_are_data_and_never_raised(events: tuple[Event, ...]) -> None:
    """CONTRACTS.md §7. No ledger, however pathological, makes `project()` raise."""
    state = project(events, DEFS, dt.date(2026, 4, 30))

    assert isinstance(state, State)
    assert state.current_period_id in tuple(p.period_id for p in state.periods)
