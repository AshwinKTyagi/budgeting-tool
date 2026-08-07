"""Integer interest engine, day count, cycle math.

Owned by `module/core-interest` (PLAN.md §13.2). Pure integer arithmetic: floor
division, actual/365, no compounding within a cycle, multiplication always before
division (CLAUDE.md §2.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from core.periods import PeriodResolver
from core.types import (
    AccountKind,
    AppError,
    Bps,
    CycleId,
    ErrorCode,
    Minor,
    PeriodId,
    StatementCycleAccountLike,
)

# The actual/365 denominator, kept as its two factors so the shape of the formula in
# PLAN.md §7.1 survives into the code: bps are hundredths of a percent, and the day
# count is actual days over a fixed 365-day year.
_BPS_PER_UNIT = 10_000
_DAYS_PER_YEAR = 365

# Business dates are half-open everywhere in this codebase, so "the day after" is the
# only date arithmetic this module needs.
_ONE_DAY = dt.timedelta(days=1)


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

        AppError(VALIDATION_FAILED) is also raised for apr_bps < 0 and for
        cycle_days <= 0. Those two are stated as preconditions rather than as
        documented raises, but each of them alone breaks the `result >= 0`
        postcondition, so checking them is what makes that postcondition true of
        the function rather than merely asserted about it. Same code, same
        taxonomy: input that could never be valid (CONTRACTS.md §7.1).
    """
    if outstanding_minor < 0:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "outstanding_minor must be non-negative: it is an absolute amount, "
            "never a signed balance (PLAN.md §7.1). A negative here means the "
            "caller passed balance_minor for a liability, which is negative by "
            "convention (CONTRACTS.md §5.2).",
            {"outstanding_minor": outstanding_minor},
        )
    if apr_bps < 0:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "apr_bps must be non-negative.",
            {"apr_bps": apr_bps},
        )
    if cycle_days <= 0:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "cycle_days must be positive.",
            {"cycle_days": cycle_days},
        )

    # Multiply before dividing (CLAUDE.md §2.1). Scaling the rate down first would
    # discard precision that integer arithmetic cannot recover.
    return outstanding_minor * apr_bps * cycle_days // (_BPS_PER_UNIT * _DAYS_PER_YEAR)


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

    Boundary construction, where the contract is silent:

    * **Cards close on their close day, inclusive.** With `statement_close_day = D`,
      the cycle whose statement closes on `close` is `[previous close + 1 day,
      close + 1 day)`. The close day itself therefore belongs to the cycle it
      closes, which is what "statement-close balance" (PLAN.md §7.1) means: the
      balance at the end of the close date, purchases made that day included.
    * **Accounts with no close day are period-aligned.** Asset accounts accrue on
      the balance at *period* close (PLAN.md §7.1), so their cycles are exactly the
      resolver's periods.
    * **The close day is clamped into short months** — a card closing on the 31st
      closes on 28 February. The clamp is computed against the period's own bounds
      rather than by calling `core.periods.clamp_day_to_month`, which keeps the
      month assumption inside `core/periods.py` where PLAN.md §4.1 puts it: the
      close date is `period_start + (D - 1) days`, capped at the period's last day.
    * **The first cycle starts at `genesis`, not at a boundary.** It is short
      whenever the account did not come into existence on a boundary, and it is
      labelled with the period of the boundary it runs *to* — the statement it will
      land on. A period whose close date falls at or before `genesis` contributes
      no cycle at all, because it has no days to account for.
    * **The last cycle is truncated at `as_of_date`, inclusive.** It is the
      in-progress cycle, and its `end_exclusive` is `as_of_date + 1 day` whenever
      the natural boundary is still in the future. Running it out to its natural
      end would charge interest for days that have not happened; the estimate
      (PLAN.md §7.3) covers elapsed days only. It keeps the id of the period its
      *natural* boundary falls in — the statement it is going to land on — so the
      id is stable as `as_of_date` advances and the cycle fills out.

    Every emitted cycle is therefore at least one day long, which is what
    `interest_for_cycle`'s `cycle_days > 0` precondition needs. `genesis ==
    as_of_date` yields exactly one one-day cycle.

    Raises:
        AppError(VALIDATION_FAILED) if genesis > as_of_date, if a CREDIT_CARD has
        no statement_close_day, or if statement_close_day falls outside 1..31. Each
        is a stated precondition, and each describes input that could never be
        valid (CONTRACTS.md §7.1).
    """
    if genesis > as_of_date:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "genesis must be on or before as_of_date.",
            {"genesis": genesis.isoformat(), "as_of_date": as_of_date.isoformat()},
        )

    close_day = account.statement_close_day

    if account.kind is AccountKind.CREDIT_CARD and close_day is None:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "a CREDIT_CARD account must declare statement_close_day.",
            {"entity_id": account.entity_id},
        )
    if close_day is not None and not 1 <= close_day <= 31:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "statement_close_day must be in 1..31.",
            {"entity_id": account.entity_id, "statement_close_day": close_day},
        )

    # as_of_date is inclusive throughout this codebase, so the exclusive end of the
    # observed window is the day after it.
    window_end_exclusive = as_of_date + _ONE_DAY

    cycles: list[tuple[CycleId, dt.date, dt.date]] = []
    cursor = genesis

    for period_id in _periods_covering(resolver, genesis, as_of_date):
        if cursor >= window_end_exclusive:
            break
        boundary = _cycle_end_exclusive(resolver, period_id, close_day)
        if boundary <= cursor:
            # This period's statement closed at or before genesis. It has no days to
            # account for, so it contributes no cycle and its id goes unused.
            continue
        end_exclusive = min(boundary, window_end_exclusive)
        cycles.append((f"{account.entity_id}:{period_id}", cursor, end_exclusive))
        cursor = end_exclusive

    return tuple(cycles)


def _periods_covering(
    resolver: PeriodResolver,
    genesis: dt.date,
    as_of_date: dt.date,
) -> Sequence[PeriodId]:
    """The periods spanning [genesis, as_of_date], plus the one immediately after.

    The trailing period is what gives the in-progress cycle an id. A card closing on
    the 15th, viewed on the 20th, is four days into the *next* statement — that cycle
    starts before `as_of_date`, so it must be emitted, and the period it closes in is
    one past the period containing `as_of_date`.

    The extra period is obtained through the protocol rather than by incrementing a
    month: `bounds(last)[1]` is the first day of the following period by definition
    (`core/periods.py`), so asking for the periods between `genesis` and that date
    returns the same list with exactly one more entry. Nothing here assumes months
    (PLAN.md §4.1).
    """
    spanned = resolver.periods_between(genesis, as_of_date)
    if not spanned:
        return ()
    _, following = resolver.bounds(spanned[-1])
    return resolver.periods_between(genesis, following)


def _cycle_end_exclusive(
    resolver: PeriodResolver,
    period_id: PeriodId,
    close_day: int | None,
) -> dt.date:
    """The exclusive end of the cycle that closes within `period_id`.

    With no close day the cycle is the period, so the answer is the period's own
    exclusive end. With one, the cycle ends the day after the statement closes, so
    that the close date is inclusive in the cycle it closes.
    """
    period_start, period_end_exclusive = resolver.bounds(period_id)
    if close_day is None:
        return period_end_exclusive
    # Clamped against the period's own last day, so a close day past the end of a
    # short month lands on that last day: 31 -> 28 February.
    nominal = period_start + dt.timedelta(days=close_day - 1)
    last_day = period_end_exclusive - _ONE_DAY
    return min(nominal, last_day) + _ONE_DAY
