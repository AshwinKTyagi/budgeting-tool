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

Implementation notes, all of them consequences of the contracts rather than choices:

* Two invariants are enforced **on construction**, so an invalid version cannot exist
  even transiently: `effective_to > effective_from` on every definition
  (`EFFECTIVE_RANGE_INVALID`) and `savings_bps + discretionary_bps == 10_000` on
  `AllocationPolicy` (`POLICY_BPS_NOT_10000`). Both raise `AppError`, not `ValueError`,
  because CONTRACTS.md §7.1 gives each its own code and `api/` maps codes to HTTP.
  Pydantic propagates a non-`ValueError` out of a validator unwrapped, so the code
  survives to the boundary intact.
* Non-overlap is *not* a construction-time invariant — it is a property of a **set** of
  versions rather than of one version, so it lives in `validate_no_overlap`, which the
  write path calls before appending a new version (CONTRACTS.md §4, "enforced at write
  time").
* Every function here is total and order-independent: results are sorted on a total key
  before being returned, so a caller that shuffles its input gets an identical answer.
  That is what keeps the projection's ingestion-order independence (CLAUDE.md §5.1
  property 6) from depending on the order a repository happened to return rows in.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Mapping, Sequence
from typing import Self
from uuid import UUID

from pydantic import BaseModel, model_validator

from core.periods import PeriodResolver, add_months, clamp_day_to_month
from core.types import (
    MONEY_MODEL_CONFIG,
    AccountKind,
    AppError,
    Bps,
    BudgetTiming,
    Cadence,
    ErrorCode,
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

    @model_validator(mode="after")
    def _check_effective_range(self) -> Self:
        """A version's range is half-open and non-empty: `[from, to)` with `to > from`.

        `effective_to == effective_from` describes a version effective for no date at
        all, which is never a valid thing to write; `effective_to < effective_from` is
        inverted. Both are `EFFECTIVE_RANGE_INVALID` (CONTRACTS.md §7.1). Enforcing it
        here rather than at the API boundary means `resolve_version` never has to
        consider an empty range and no repository can persist one.
        """
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise AppError(
                ErrorCode.EFFECTIVE_RANGE_INVALID,
                (
                    f"effective_to ({self.effective_to.isoformat()}) must be strictly "
                    f"after effective_from ({self.effective_from.isoformat()}); the "
                    f"range is half-open [from, to)"
                ),
                {
                    "entity_id": self.entity_id,
                    "version_id": str(self.version_id),
                    "effective_from": self.effective_from.isoformat(),
                    "effective_to": self.effective_to.isoformat(),
                },
            )
        return self


class RecurringIncome(DefinitionBase):
    """FORECAST ONLY. Never contributes to allocatable_income — only actual
    IncomeReceived / GiftReceived events do (PLAN.md §8.2).

    `expand_recurring_incomes` below is the "separate forecast view" §8.2 names. It
    materializes occurrences so `api/` can offer them for confirmation; confirming
    appends a real `IncomeReceived`, and that is what allocates (PLAN.md §8.5).
    Expanding, by itself, allocates nothing."""

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

    @model_validator(mode="after")
    def _check_bps_total(self) -> Self:
        """The two buckets must partition the period exactly.

        `core/money.py::split_bps` states `sum(bps) == 10_000` as a precondition and
        raises `POLICY_BPS_NOT_10000` when it is violated. Validating on construction
        discharges that precondition by the type: a policy that reaches
        `allocate_period` cannot break the top-level invariant (PLAN.md §5.3).
        """
        total_bps = self.savings_bps + self.discretionary_bps
        if total_bps != 10_000:
            raise AppError(
                ErrorCode.POLICY_BPS_NOT_10000,
                (
                    f"savings_bps ({self.savings_bps}) + discretionary_bps "
                    f"({self.discretionary_bps}) == {total_bps}, must be 10_000"
                ),
                {
                    "entity_id": self.entity_id,
                    "version_id": str(self.version_id),
                    "savings_bps": self.savings_bps,
                    "discretionary_bps": self.discretionary_bps,
                    "total_bps": total_bps,
                },
            )
        return self


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


def _is_effective_at(version: DefinitionBase, at: dt.date) -> bool:
    """True iff `at` falls in the half-open range `[effective_from, effective_to)`.

    `effective_to is None` is open-ended, i.e. `+inf`. The asymmetry of the two
    comparisons is the whole point: a version ending on the day another begins hands
    over cleanly, with no date belonging to both and none belonging to neither.
    """
    if at < version.effective_from:
        return False
    return version.effective_to is None or at < version.effective_to


def _version_sort_key(version: DefinitionBase) -> tuple[dt.date, dt.datetime, str]:
    """A total, stable ordering key over versions.

    Mirrors the ledger's `(date, recorded_at, event_id)` (CONTRACTS.md §3.1): business
    date first, instant to break ties, identity to make it total. `recorded_at` is a
    UTC-normalized aware datetime, so it compares by instant.
    """
    return (version.effective_from, version.recorded_at, str(version.version_id))


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

    `versions` may hold versions of *any* entity — the whole `Definitions` bundle is the
    expected argument — so entity filtering happens here rather than at the call site.
    `None` is returned both for a date before any version begins and for a date inside a
    gap between two versions; the two are indistinguishable to a caller and mean the
    same thing, that nothing governs `at`.

    Should the non-overlap precondition be violated, the latest-starting match wins, ties
    broken by `(effective_from, recorded_at, version_id)`. That is not a licence to
    overlap — `validate_no_overlap` is what prevents it — but it keeps the answer
    independent of the order `versions` arrived in, so a bad write cannot make the
    projection non-deterministic on top of being wrong.
    """
    matches = [
        v for v in versions if v.entity_id == entity_id and _is_effective_at(v, at)
    ]
    if not matches:
        return None
    return max(matches, key=_version_sort_key)


def validate_no_overlap(versions: Sequence[DefinitionBase]) -> None:
    """Raise AppError(OVERLAPPING_VERSIONS) if two versions of the same entity_id
    have intersecting [effective_from, effective_to) ranges.

    Postcondition: returns None, or raises. Never mutates.

    Ranges are half-open, so versions that share a boundary — one ending exactly where
    the next begins — do **not** overlap, and that is the normal way a definition is
    superseded (CLAUDE.md §4.3: close the prior version, append a new one).

    Versions of different entities never interact. Within one entity, sorting by start
    date makes adjacent-pair comparison sufficient: if any two ranges intersect, some
    adjacent pair does.
    """
    ordered = sorted(versions, key=lambda v: (v.entity_id, *_version_sort_key(v)))
    for entity_id, group in itertools.groupby(ordered, key=lambda v: v.entity_id):
        members = tuple(group)
        for earlier, later in zip(members, members[1:], strict=False):
            overlaps = (
                earlier.effective_to is None
                or later.effective_from < earlier.effective_to
            )
            if overlaps:
                raise AppError(
                    ErrorCode.OVERLAPPING_VERSIONS,
                    (
                        f"versions of entity_id {entity_id!r} overlap: "
                        f"[{earlier.effective_from.isoformat()}, "
                        f"{_bound_repr(earlier.effective_to)}) intersects "
                        f"[{later.effective_from.isoformat()}, "
                        f"{_bound_repr(later.effective_to)})"
                    ),
                    {
                        "entity_id": entity_id,
                        "version_ids": [
                            str(earlier.version_id),
                            str(later.version_id),
                        ],
                    },
                )


def _bound_repr(effective_to: dt.date | None) -> str:
    """Render an exclusive end bound; open-ended reads as unbounded."""
    return "open" if effective_to is None else effective_to.isoformat()


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

    `fixed_costs` is the full version history — the same bundle the projection holds —
    and the precondition is discharged *here*, by resolving each distinct `entity_id` at
    the period's start date. Resolving inside rather than trusting the caller is what
    makes "one row per entity per period" structurally true: a caller cannot pass two
    versions of one cost and get two obligations for the same period, which is exactly
    the shape the deterministic `obligation_id` forbids.

    An entity with no version effective at the period start contributes nothing, which is
    how a cost that has not started yet, or has been closed out, drops out of a period
    without any special case.

    `recurring_id` is the `entity_id`, per the `FixedCost` docstring — that is the key an
    explicit `ObligationRaised` supersedes on (`supersede_expected`).

    Rows come back sorted by `(due_date, obligation_id)`, so the result does not depend
    on the order `fixed_costs` arrived in.
    """
    period_start, _period_end_exclusive = resolver.bounds(period_id)
    entity_ids = sorted({fc.entity_id for fc in fixed_costs})
    effective = [
        version
        for version in (
            resolve_version(fixed_costs, entity_id, period_start)
            for entity_id in entity_ids
        )
        if version is not None
    ]
    return tuple(
        sorted(
            (
                ExpectedObligation(
                    obligation_id=f"expected:{fc.entity_id}:{period_id}",
                    period_id=period_id,
                    due_date=clamp_day_to_month(
                        period_start.year, period_start.month, fc.due_day
                    ),
                    amount_minor=fc.amount_minor,
                    payee=fc.payee,
                    category=fc.category,
                    recurring_id=fc.entity_id,
                    source=ObligationSource.EXPECTED,
                )
                for fc in effective
            ),
            key=lambda o: (o.due_date, o.obligation_id),
        )
    )


def _match_key(
    recurring_id: str | None,
    obligation_id: str,
    period_id: PeriodId,
) -> tuple[str, str, str]:
    """The key an expected row and an explicit obligation match on.

    `(recurring_id, period of due_date)` per the contract, when there is a
    `recurring_id`. A one-off bill has none and can only ever match itself, so it is
    keyed by `obligation_id` instead — otherwise every ad-hoc obligation in a period
    would collide with every other one under a shared `None`.
    """
    if recurring_id is None:
        return ("obligation", obligation_id, "")
    return ("recurring", recurring_id, period_id)


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

    Actual beats forecast (PLAN.md §8.1): a match drops the expected row entirely and
    keeps the explicit one, carrying the event's own `obligation_id` so that a
    `PaymentMade` referencing it resolves. Summing the two would double-count the bill —
    the recognition-principle failure in its obligation-shaped form.

    Two explicit obligations sharing one `(recurring_id, period)` would break the
    no-duplicates postcondition, so the last in ledger order `(date, recorded_at,
    event_id)` wins: the same "later supersedes earlier" reading, applied within the
    ledger rather than between ledger and forecast. Sorting the events here rather than
    trusting the caller keeps the result independent of arrival order.

    A raised obligation with no `recurring_id` is a one-off: it matches nothing, is
    always kept, and never suppresses an expected row.

    Note that period membership for an `ObligationRaised` comes from `due_date`, never
    from `date` (CONTRACTS.md §3.2).
    """
    ordered = sorted(raised, key=lambda e: (e.date, e.recorded_at, str(e.event_id)))
    by_key = {
        _match_key(
            event.recurring_id,
            event.obligation_id,
            resolver.period_for(event.due_date),
        ): event
        for event in ordered
    }
    survivors = [
        row
        for row in expected
        if _match_key(
            row.recurring_id, row.obligation_id, resolver.period_for(row.due_date)
        )
        not in by_key
    ]
    replacements = [
        ExpectedObligation(
            obligation_id=event.obligation_id,
            period_id=resolver.period_for(event.due_date),
            due_date=event.due_date,
            amount_minor=event.amount_minor,
            payee=event.payee,
            category=event.category,
            recurring_id=event.recurring_id,
            source=ObligationSource.RAISED,
        )
        for event in by_key.values()
    ]
    return tuple(
        sorted(
            survivors + replacements,
            key=lambda o: (o.due_date, o.obligation_id),
        )
    )


# --------------------------------------------------------------- recurring income
# The "separate forecast view" PLAN.md §8.2 promises. Expanding a `RecurringIncome`
# does NOT make it allocatable: §8.2's rule is unchanged, and `domain/projection.py`
# never calls anything below. These rows are offered to the user for confirmation, and
# confirming appends a real `IncomeReceived` — which is what allocates (PLAN.md §8.5).
#
# This is the first cadence-aware expansion in the codebase. `expand_fixed_costs`
# ignores `cadence` entirely and emits one row per calendar month; that asymmetry is
# deliberate and out of scope here.

#: Cadences that step by a fixed number of days from the first occurrence. `anchor_day`
#: is meaningless for these — a weekly payday is pinned by `effective_from`, and there
#: is no "day of the month" that survives stepping seven days at a time.
_CADENCE_STEP_DAYS: Mapping[Cadence, int] = {
    Cadence.WEEKLY: 7,
    Cadence.BIWEEKLY: 14,
}

#: Cadences that step whole months and land on `anchor_day`, clamped to month length.
_CADENCE_STEP_MONTHS: Mapping[Cadence, int] = {
    Cadence.MONTHLY: 1,
    Cadence.QUARTERLY: 3,
    Cadence.ANNUAL: 12,
}

#: Days between the two halves of a SEMIMONTHLY month — the 1st and the 16th, the 5th
#: and the 20th, and so on.
_SEMIMONTHLY_OFFSET = 15

#: `clamp_day_to_month` raises above this, so the second semimonthly date is capped
#: here rather than allowed to reach `anchor_day + 15`. An `anchor_day` of 20 would
#: otherwise ask for day 35 and take down the whole expansion.
_LAST_POSSIBLE_DAY = 31

_ONE_DAY = dt.timedelta(days=1)


class ExpectedIncome(BaseModel):
    """One forecast paycheck, before `api/` offers it for confirmation.

    Exists for the same reason as `ExpectedObligation`: `domain/definitions.py` must
    not import `domain/projection.py`, and this type keeps the dependency graph acyclic.

    `income_id` doubles as the `dedupe_key` of the event a confirmation appends, which
    is what makes confirming, editing, and rejecting each suppress the occurrence
    permanently — the events table already holds a UNIQUE index on that column
    (PLAN.md §8.5).
    """

    model_config = MONEY_MODEL_CONFIG

    income_id: str  # f"expected:income:{entity_id}:{date}"
    entity_id: str
    date: dt.date
    amount_minor: Minor
    name: str
    account_id: str


def _occurrence_date(income: RecurringIncome, step: int) -> dt.date:
    """The `step`-th occurrence of `income`, counting from zero.

    Postconditions:
        non-decreasing in `step`, for every cadence -- which is what lets the caller
            stop at the first date past its window instead of generating all of them
        always a valid date inside the month the cadence selects

    Month-stepping cadences seed from the month of `effective_from` rather than from
    `effective_from` itself, so a job effective the 20th with `anchor_day=1` still lands
    on the 1st. The seeded occurrence may therefore precede `effective_from`; the caller
    drops it.
    """
    start = income.effective_from
    step_days = _CADENCE_STEP_DAYS.get(income.cadence)
    if step_days is not None:
        return start + dt.timedelta(days=step_days * step)
    if income.cadence is Cadence.SEMIMONTHLY:
        year, month = add_months(start.year, start.month, step // 2)
        day = (
            income.anchor_day
            if step % 2 == 0
            else min(income.anchor_day + _SEMIMONTHLY_OFFSET, _LAST_POSSIBLE_DAY)
        )
        return clamp_day_to_month(year, month, day)
    year, month = add_months(
        start.year, start.month, _CADENCE_STEP_MONTHS[income.cadence] * step
    )
    return clamp_day_to_month(year, month, income.anchor_day)


def _version_occurrences(
    income: RecurringIncome,
    from_date: dt.date,
    to_date: dt.date,
) -> Sequence[ExpectedIncome]:
    """Every occurrence of one version, inside both its own range and the window.

    A version is expanded only within its own half-open `[effective_from,
    effective_to)`. Because versions of one entity may not overlap, that is what
    resolves the version at the OCCURRENCE date rather than at a period start — a
    cadence change mid-month cannot rewrite a paycheck that already landed.
    """
    last = (
        to_date
        if income.effective_to is None
        else min(to_date, income.effective_to - _ONE_DAY)
    )
    if last < income.effective_from:
        return ()
    # An upper bound on the step count, never the count itself: no cadence yields more
    # than one occurrence per day, so the window's length always over-counts. It exists
    # to keep the generator finite; `takewhile` is what actually stops it.
    cap = (last - income.effective_from).days + 2
    dates = (_occurrence_date(income, step) for step in range(cap))
    return tuple(
        ExpectedIncome(
            income_id=f"expected:income:{income.entity_id}:{occurrence.isoformat()}",
            entity_id=income.entity_id,
            date=occurrence,
            amount_minor=income.amount_minor,
            name=income.name,
            account_id=income.account_id,
        )
        for occurrence in itertools.takewhile(lambda d: d <= last, dates)
        if occurrence >= income.effective_from and occurrence >= from_date
    )


def expand_recurring_incomes(
    recurring_incomes: Sequence[RecurringIncome],
    from_date: dt.date,
    to_date: dt.date,
) -> Sequence[ExpectedIncome]:
    """Materialize forecast paychecks in `[from_date, to_date]`, inclusive both ends.

    Preconditions:
        `recurring_incomes` is the full version history, as the projection holds it

    Postconditions:
        every row's date is inside [from_date, to_date] and inside its own version's
            [effective_from, effective_to)
        income_id is deterministic: f"expected:income:{entity_id}:{date}"
        no two rows share an income_id
        sorted by (date, income_id); independent of the order the input arrived in
        no I/O, no clock

    This does **not** make recurring income allocatable. PLAN.md §8.2 stands: only an
    actual `IncomeReceived` counts, and nothing in `domain/projection.py` calls this.
    The rows exist so `api/` can offer them for confirmation.

    An inverted window yields nothing rather than raising, matching
    `periods_between` — callers fold over the result, so empty composes where a raise
    would force a guard at every call site.

    Deduplication by `income_id` is not defensive: a SEMIMONTHLY job anchored on the
    30th asks for the 30th and the 31st, and February clamps both to the 28th. One
    occurrence, one id, one suggestion.
    """
    rows = {
        row.income_id: row
        for version in sorted(recurring_incomes, key=_version_sort_key)
        for row in _version_occurrences(version, from_date, to_date)
    }
    return tuple(sorted(rows.values(), key=lambda row: (row.date, row.income_id)))
