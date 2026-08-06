"""The pure fold and the `State` model.

Owned by `module/domain-projection` (PLAN.md §13.2).

`project()` is pure: same inputs, same output, always, with no observable side effects.
No logging, no metrics, no file access, no network, no database session, no environment
reads, and no clock reads inside it or anything it calls (CLAUDE.md §4.2, §4.4).

Fold with immutable accumulators — build a new value per step, or use
`functools.reduce` with frozen carriers. Never mutate an accumulator and never mutate
the output. `State` and everything reachable from it is frozen; construct it once at the
end.

Every read recomputes from genesis. There is no cached state, no materialized balance
column, and no incremental update path — which is exactly why a backdated receipt
entered today correctly changes every period after it (PLAN.md §3). Do not add a cache.

Anomalies surface as `State.warnings`, never as raised exceptions. Backdating means
today's impossible state is tomorrow's ordinary one (CONTRACTS.md §7).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel

from core.periods import PeriodResolver
from core.types import (
    MONEY_MODEL_CONFIG,
    Bps,
    Minor,
    ObligationSource,
    ObligationStatus,
    PeriodId,
    WarningCode,
)
from domain.accounts import AccountBalance, StatementCycleSummary
from domain.definitions import Definitions
from domain.events import Event


class PeriodSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    period_id: PeriodId
    start_date: dt.date  # inclusive
    end_date_exclusive: dt.date
    is_closed: bool  # end_date_exclusive <= as_of_date

    policy_version_id: UUID
    savings_bps: Bps
    discretionary_bps: Bps

    income_minor: Minor
    gifts_minor: Minor
    allocatable_income_minor: Minor  # income + gifts

    fixed_due_minor: Minor  # accrual: all obligations due this period
    fixed_paid_minor: Minor  # cash
    fixed_outstanding_minor: Minor  # fixed_due - fixed_paid

    savings_allocated_minor: Minor  # signed
    discretionary_allocated_minor: Minor  # signed
    savings_drawn_minor: Minor

    discretionary_spent_minor: Minor
    discretionary_remaining_minor: Minor
    # == discretionary_allocated + savings_drawn - discretionary_spent

    # INVARIANT (exact, always, including negative allocatable income):
    #   fixed_due_minor
    #     + savings_allocated_minor
    #     + discretionary_allocated_minor
    #     == allocatable_income_minor


class ObligationRow(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    obligation_id: str
    source: ObligationSource
    period_id: PeriodId
    due_date: dt.date
    payee: str
    category: str
    recurring_id: str | None

    amount_minor: Minor
    paid_minor: Minor
    remaining_minor: Minor  # amount - paid; negative when overpaid
    status: ObligationStatus


class SavingsSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    balance_minor: Minor
    cumulative_allocated_minor: Minor
    cumulative_drawn_minor: Minor
    cumulative_interest_minor: Minor
    pending_allocation_minor: Minor  # in-progress period; not yet in balance

    # INVARIANT: balance_minor ==
    #   opening + cumulative_allocated - cumulative_drawn
    #   + cumulative_interest ± explicit transfers


class Warning(BaseModel):
    """A surprising but legitimate state. DATA, never raised (CONTRACTS.md §7).

    Shadows the builtin `Warning` inside this module. The name is fixed by the
    contract, so it stays; nothing here raises or catches the builtin.
    """

    model_config = MONEY_MODEL_CONFIG

    code: WarningCode
    message: str
    period_id: PeriodId | None = None
    event_id: UUID | None = None
    account_id: str | None = None


class State(BaseModel):
    """The complete answer at a point in time. Immutable; constructed once."""

    model_config = MONEY_MODEL_CONFIG

    as_of_date: dt.date
    current_period_id: PeriodId
    periods: tuple[PeriodSummary, ...]  # genesis .. as_of, ascending
    obligations: tuple[ObligationRow, ...]
    accounts: tuple[AccountBalance, ...]
    statement_cycles: tuple[StatementCycleSummary, ...]
    savings: SavingsSummary
    warnings: tuple[Warning, ...]


def project(
    events: Sequence[Event],
    definitions: Definitions,
    as_of_date: dt.date,
    *,
    resolver: PeriodResolver | None = None,  # default CalendarMonthResolver()
) -> State:
    """Fold events and definitions into State. PURE.

    Preconditions:
        events may arrive in ANY order, including backdated
        definitions contains ALL versions; resolution happens inside
        as_of_date may be past, present, or future

    Postconditions:
        for every period:
            fixed_due + savings_allocated + discretionary_allocated
                == allocatable_income                       EXACTLY
        savings.balance == opening + Σallocated - Σdrawn + Σinterest
                           ± explicit transfers              EXACTLY
        project(e, d, t) == project(e, d, t)                 always
        shuffling the order of `events` yields an identical State
        no I/O, no clock read, no DB access, no mutation, no logging
        the returned State and everything reachable from it is frozen
        anomalies surface as State.warnings, never as raised exceptions

    Order of operations is fixed — see §5.1. Do not reorder.

        1. Filter events voided by an `EventVoided`, and the `EventVoided` records
           themselves.
        2. Sort by `(date, recorded_at, event_id)`.
        3. Resolve periods from genesis (earliest event date) through `as_of_date`.
        4. Expand `FixedCost` definitions into expected obligations; supersede by
           matching `ObligationRaised`.
        5. Fold statement cycles **in order, per account**, carrying close balance and
           paid-in-full status forward (PLAN.md §7.4).
        6. Fold per-period allocation, applying implied savings transfers at period
           close.
        7. Assemble `State`.
    """
    raise NotImplementedError
