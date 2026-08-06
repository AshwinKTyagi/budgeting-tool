"""Balance folding, statement-cycle folding, and obligation status derivation.

Owned by `module/domain-accounts` (PLAN.md §13.2). Pure: no I/O, no clock, no DB.

`AccountBalance` and `StatementCycleSummary` are declared here rather than in
`domain/projection.py`, even though CONTRACTS.md presents them in §5 alongside `State`.
They have to be: `fold_account_balances` and `fold_statement_cycles` return them, and
`domain/projection.py` imports those functions — so declaring them there would make
`accounts -> projection -> accounts`, and a cycle is a build failure (CLAUDE.md §3.1).
This is the same reasoning that puts `ExpectedObligation` in `domain/definitions.py`
(CONTRACTS.md §8.5). No field or signature changes; only the file.

Statement cycles CANNOT be computed in isolation. They fold strictly in order, each
carrying forward the closing balance and whether the statement was paid in full by its
due date, because that decides the next cycle's grace (PLAN.md §7.4).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from pydantic import BaseModel

from core.types import (
    MONEY_MODEL_CONFIG,
    AccountKind,
    Bps,
    CycleId,
    Minor,
    ObligationStatus,
)
from domain.definitions import Account
from domain.events import Event


class AccountBalance(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    account_id: str
    name: str
    kind: AccountKind
    balance_minor: Minor  # SIGNED; negative == liability
    outstanding_minor: Minor | None  # abs(balance) for liabilities; None for assets
    apr_bps: Bps
    cumulative_interest_minor: Minor


class StatementCycleSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    cycle_id: CycleId
    account_id: str
    start_date: dt.date
    end_date_exclusive: dt.date
    close_balance_minor: Minor
    interest_minor: Minor
    is_estimate: bool  # False once an InterestCharged pins the cycle
    paid_in_full_by_due_date: bool
    grace_applied: bool  # True == interest waived this cycle


def derive_obligation_status(
    amount_minor: Minor,
    paid_minor: Minor,
) -> ObligationStatus:
    """Postconditions:
        paid == 0            -> UNPAID
        0 < paid < amount    -> PARTIALLY_PAID
        paid == amount       -> PAID
        paid > amount        -> OVERPAID   (permitted; raises a warning, not an error)
    """
    raise NotImplementedError


def fold_account_balances(
    events: Sequence[Event],
    accounts: Sequence[Account],
    implied_transfers: Sequence[tuple[dt.date, str, str, Minor]],
    as_of_date: dt.date,
) -> Sequence[AccountBalance]:
    """Fold every balance-affecting event plus the projection's implied savings
    transfers (PLAN.md §6.2).

    Preconditions:
        events are sorted by (date, recorded_at, event_id)
        events are already void-filtered

    Postconditions:
        balance_minor is SIGNED; negative == liability
        outstanding_minor == abs(balance) for liability kinds, else None
        pure — no mutation of inputs, no accumulator reassignment across the fold
    """
    raise NotImplementedError


def fold_statement_cycles(
    account: Account,
    account_versions: Sequence[Account],
    events: Sequence[Event],
    cycles: Sequence[tuple[CycleId, dt.date, dt.date]],
) -> Sequence[StatementCycleSummary]:
    """Fold cycles IN ORDER, carrying close balance and paid-in-full status forward.

    Cycles cannot be computed in isolation (PLAN.md §7.4).

    Preconditions:
        cycles ascending, contiguous, non-overlapping
        events sorted and void-filtered

    Postconditions:
        APR resolved at each cycle's START date
        grace_applied is True iff the PREVIOUS cycle was paid in full by its due
            date; when True, interest_minor == 0
        an InterestCharged for a cycle PINS it: interest_minor is the recorded
            amount and is_estimate is False
        a pinned cycle's figure is independent of any backdated event within it
        interest is identical under both BudgetTiming modes — timing affects only
            budget recognition, never computation (PLAN.md §6.4)
    """
    raise NotImplementedError
