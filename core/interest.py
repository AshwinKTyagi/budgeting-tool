"""Integer interest engine, day count, cycle math.

Owned by `module/core-interest` (PLAN.md §13.2). Pure integer arithmetic: floor
division, actual/365, no compounding within a cycle, multiplication always before
division (CLAUDE.md §2.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from core.periods import PeriodResolver
from core.types import Bps, CycleId, Minor, StatementCycleAccountLike


def interest_for_cycle(
    outstanding_minor: Minor,
    apr_bps: Bps,
    cycle_days: int,
) -> Minor:
    """Integer interest, floor division, actual/365, no intra-cycle compounding.

        outstanding_minor * apr_bps * cycle_days // (10_000 * 365)

    Preconditions:
        outstanding_minor >= 0   -- an ABSOLUTE amount, never a signed balance
        apr_bps >= 0
        cycle_days > 0

        What the caller passes, by account kind:
          liability (CREDIT_CARD, LOAN) -> AccountBalance.outstanding_minor
          asset (CHECKING, SAVINGS)     -> AccountBalance.balance_minor, which is
                                           non-negative in the normal case
        An overdrawn asset account (balance_minor < 0) accrues no interest; the
        caller skips it or passes 0. It must NOT pass the negative balance.

        Passing a signed `balance_minor` for a liability is a bug: it is NEGATIVE
        for liabilities (§5.2), and raw floor division on a negative operand
        rounds toward -inf, producing a larger-magnitude charge than the balance
        warrants.

        This does NOT use the abs-then-reapply-sign discipline of split_bps, and
        the difference is deliberate. split_bps must accept signed input because
        negative allocatable income is a real state (PLAN.md §6.1). A negative
        card balance is a credit, which earns no interest rather than negative
        interest — so the correct treatment is a precondition, not a sign flip.

    Postconditions:
        result is int; no float anywhere in the computation
        result >= 0
        multiplication happens before division (CLAUDE.md §2.1)
        outstanding_minor == 0 or apr_bps == 0  =>  result == 0
        worked example: (120_000, 2199, 31) -> 2241        (PLAN.md §7.2)
                        (500_000,  450, 30) -> 1849

    Raises:
        AppError(VALIDATION_FAILED) if outstanding_minor < 0. Never silently
        clamps -- a negative here means the caller used the wrong field.
    """
    raise NotImplementedError


def build_statement_cycles(
    account: StatementCycleAccountLike,
    genesis: dt.date,
    as_of_date: dt.date,
    resolver: PeriodResolver,
) -> Sequence[tuple[CycleId, dt.date, dt.date]]:
    """Enumerate an account's statement cycles as (cycle_id, start, end_exclusive).

    Preconditions:
        account.kind == CREDIT_CARD implies statement_close_day is not None
        genesis <= as_of_date

    Postconditions:
        ascending, contiguous, non-overlapping
        cycle_id == f"{account.entity_id}:{period_id}"
        no cycle starts after as_of_date

    `account` is annotated against the structural `StatementCycleAccountLike` rather
    than `domain.definitions.Account`, which it accepts unchanged; `core/` may not
    import `domain/` (CLAUDE.md §3.1).
    """
    raise NotImplementedError
