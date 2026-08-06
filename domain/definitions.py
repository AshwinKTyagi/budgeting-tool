"""Versioned, effective-dated definitions and their resolution.

Owned by `module/domain-definitions` (PLAN.md §13.2). Pure: no I/O, no clock, no DB.

All definitions carry `effective_from` (inclusive) and `effective_to` (exclusive,
nullable for open-ended). Versions of the same `entity_id` may not overlap; that is
enforced at write time.

Resolving at a boundary rather than per-event is what makes "changing the split must not
retroactively alter closed periods" mechanically true rather than merely intended: a
closed period's policy was pinned by a date that has already passed (PLAN.md §8.3).

`RecurringIncome` is deliberately asymmetric with `FixedCost` — an unpaid bill is still
owed, so it reserves money; an unreceived paycheck cannot be spent, so it must not
(PLAN.md §8.2).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel

from core.periods import PeriodResolver
from core.types import (
    MONEY_MODEL_CONFIG,
    AccountKind,
    Bps,
    BudgetTiming,
    Cadence,
    Minor,
    ObligationSource,
    PeriodId,
    UtcInstant,
)
from domain.events import ObligationRaised


class DefinitionBase(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    version_id: UUID
    entity_id: str  # stable logical identity across versions
    effective_from: dt.date  # inclusive
    effective_to: dt.date | None  # exclusive; None == open-ended
    recorded_at: UtcInstant


class RecurringIncome(DefinitionBase):
    """FORECAST ONLY. Never contributes to allocatable_income — only actual
    IncomeReceived / GiftReceived events do (PLAN.md §8.2)."""

    name: str
    amount_minor: Minor  # > 0
    cadence: Cadence
    anchor_day: int  # 1..31, clamped to month length
    account_id: str


class FixedCost(DefinitionBase):
    """Expanded by the projection into expected obligations. An ObligationRaised
    with the same recurring_id (== entity_id) in the same due-period supersedes
    the expected one (PLAN.md §8.1)."""

    name: str
    amount_minor: Minor  # > 0
    cadence: Cadence
    due_day: int  # 1..31, clamped to month length
    payee: str
    category: str


class AllocationPolicy(DefinitionBase):
    """Resolved at PERIOD START. One policy governs a whole period; a policy
    effective mid-period applies from the next period. This is what makes closed
    periods immune to policy change (PLAN.md §8.3).

    Buckets are ordered; order breaks rounding ties (PLAN.md §5.1). Savings is
    declared first.
    """

    savings_bps: Bps
    discretionary_bps: Bps
    # INVARIANT: savings_bps + discretionary_bps == 10_000, validated on construction.
    # The validator is module/domain-definitions' to write; it raises
    # AppError(POLICY_BPS_NOT_10000) (CONTRACTS.md §7.1).


class Account(DefinitionBase):
    """entity_id is the account_id. APR is resolved at STATEMENT CYCLE START
    (PLAN.md §7.4)."""

    name: str
    kind: AccountKind
    apr_bps: Bps  # 0 for non-interest-bearing
    statement_close_day: int | None  # CREDIT_CARD only; 1..31
    payment_due_day: int | None  # CREDIT_CARD only; 1..31
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE  # CREDIT_CARD only


class Definitions(BaseModel):
    """Immutable bundle passed to project(). Contains ALL versions, not just
    currently-effective ones — the projection resolves per period and per cycle."""

    model_config = MONEY_MODEL_CONFIG

    recurring_incomes: tuple[RecurringIncome, ...]
    fixed_costs: tuple[FixedCost, ...]
    allocation_policies: tuple[AllocationPolicy, ...]
    accounts: tuple[Account, ...]


def resolve_version[T: DefinitionBase](
    versions: Sequence[T],
    entity_id: str,
    at: dt.date,
) -> T | None:
    """The version of `entity_id` effective at `at`.

    Preconditions:
        versions of a given entity_id do not overlap

    Postconditions:
        returns v where v.effective_from <= at < (v.effective_to or +inf)
        None when no version is effective
        at most one version can match — overlap is a write-time error
    """
    raise NotImplementedError


def validate_no_overlap(versions: Sequence[DefinitionBase]) -> None:
    """Raise AppError(OVERLAPPING_VERSIONS) if two versions of the same entity_id
    have intersecting [effective_from, effective_to) ranges.

    Postcondition: returns None, or raises. Never mutates.
    """
    raise NotImplementedError


class ExpectedObligation(BaseModel):
    """An obligation materialized from a FixedCost, before the projection turns it
    into an ObligationRow.

    This type exists to keep the dependency graph acyclic: `domain/definitions.py`
    must not import from `domain/projection.py`, so it cannot return ObligationRow.
    The projection converts ExpectedObligation -> ObligationRow when assembling State.
    """

    model_config = MONEY_MODEL_CONFIG

    obligation_id: str
    period_id: PeriodId
    due_date: dt.date
    amount_minor: Minor
    payee: str
    category: str
    recurring_id: str | None
    source: ObligationSource


def expand_fixed_costs(
    fixed_costs: Sequence[FixedCost],
    period_id: PeriodId,
    resolver: PeriodResolver,
) -> Sequence[ExpectedObligation]:
    """Materialize expected obligations for `period_id` (PLAN.md §8.1).

    Preconditions:
        the FixedCost version is resolved at the PERIOD START date

    Postconditions:
        every row has source == EXPECTED
        obligation_id is deterministic: f"expected:{entity_id}:{period_id}"
        due_date is clamp_day_to_month(period, due_day)
        no I/O, no clock
    """
    raise NotImplementedError


def supersede_expected(
    expected: Sequence[ExpectedObligation],
    raised: Sequence[ObligationRaised],
    resolver: PeriodResolver,
) -> Sequence[ExpectedObligation]:
    """Replace expected obligations with matching explicit ones.

    Match key: (recurring_id, period of due_date).

    Postconditions:
        an expected row with a match is REPLACED, not summed
        a raised event with no expected match is included as source == RAISED
        result contains no duplicate (recurring_id, period) pairs
    """
    raise NotImplementedError
