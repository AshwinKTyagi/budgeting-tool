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

Recognition, once (PLAN.md §1)
------------------------------
This module is the only place in the codebase that decides what reduces
`discretionary`, so it is the only place the recognition principle can be broken. Every
outflow is recognized in exactly one of three ways and never in two:

* a **fixed obligation** is recognized off the top, by its `due_date`, as `fixed_due`;
* a **discretionary expense** is recognized at purchase, unless it was charged to a card
  running `AT_STATEMENT_PAYMENT`, in which case the statement payment — a `TransferMade`
  *into* the card — is recognized instead, for its full amount;
* **card interest** is recognized once, from the statement cycle that computed it, and
  only for a card running `AT_PURCHASE` (under `AT_STATEMENT_PAYMENT` the payment amount
  already contains it, so charging it again is the double-count PLAN.md §6.4 warns of).

Nothing else moves `discretionary`. In particular a `TransferMade` between own accounts
is budget-neutral, a `PaymentMade` against an obligation changes status only (the
obligation was recognized when it was raised, on the accrual basis), and a
`SavingsDrawn` adds to what is *available* rather than subtracting from it.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterable, Mapping, Sequence
from uuid import UUID

from pydantic import BaseModel

from core.interest import build_statement_cycles
from core.money import allocate_period
from core.periods import CalendarMonthResolver, PeriodResolver
from core.types import (
    MONEY_MODEL_CONFIG,
    AccountKind,
    Bps,
    BudgetTiming,
    Minor,
    ObligationSource,
    ObligationStatus,
    PeriodId,
    WarningCode,
)
from domain.accounts import (
    AccountBalance,
    StatementCycleSummary,
    derive_obligation_status,
    fold_account_balances,
    fold_statement_cycles,
)
from domain.definitions import (
    Account,
    AllocationPolicy,
    Definitions,
    ExpectedObligation,
    expand_fixed_costs,
    resolve_version,
    supersede_expected,
)
from domain.events import (
    AccountOpeningBalance,
    Event,
    EventVoided,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
    is_voided,
)


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


# --------------------------------------------------------------------------- constants

#: Business dates are half-open everywhere in this codebase, so "the day before the
#: exclusive end" is the only date arithmetic this module needs.
_ONE_DAY = dt.timedelta(days=1)

#: The policy used for a period that no `AllocationPolicy` version governs.
#:
#: `PeriodSummary.policy_version_id` is a non-optional `UUID`, so every period must name
#: a policy, yet a period can legitimately have none: a backdated event lands before the
#: first policy's `effective_from`, or a version history has a gap. Raising would
#: contradict the postcondition that anomalies surface as warnings, and `WarningCode`
#: (CONTRACTS.md §7.2) has no code for a missing policy, so the fallback is silent and
#: deterministic instead.
#:
#: Its numbers are the documented seed — `savings_bps=5000, discretionary_bps=5000`
#: (CONTRACTS.md §4) — so a projection over a ledger that predates its own policy answers
#: the same way the seeded system would have. `UUID(int=0)` marks it as synthetic: no
#: persisted version can carry that id, so a reader can tell a fallback period from a
#: governed one without a second field. Every date is a literal; nothing here reads a
#: clock (CLAUDE.md §4.4).
_FALLBACK_POLICY = AllocationPolicy(
    version_id=UUID(int=0),
    entity_id="",
    effective_from=dt.date.min,
    effective_to=None,
    recorded_at=dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc),
    savings_bps=5_000,
    discretionary_bps=5_000,
)

_SAVINGS_BUCKET = "savings"
_DISCRETIONARY_BUCKET = "discretionary"


# ----------------------------------------------------------------------------- helpers


def _ledger_key(event: Event) -> tuple[dt.date, dt.datetime, str]:
    """The total, stable ledger order: `(date, recorded_at, event_id)` (CONTRACTS.md §3.1).

    `recorded_at` is a UTC-normalized aware datetime, so it compares by instant
    regardless of the offset it arrived with. `event_id` is stringified rather than
    compared as a `UUID` so the key is a tuple of three ordinary, obviously-total
    comparands.
    """
    return (event.date, event.recorded_at, str(event.event_id))


def _period_date(event: Event) -> dt.date:
    """The business date that decides `event`'s period membership.

    `ObligationRaised` is the one event whose period comes from `due_date` rather than
    from `date` (CONTRACTS.md §3.2) — a bill entered in March and due in April is an
    April obligation. Everything else answers with its own business date. `recorded_at`
    never participates (PLAN.md §4.2).
    """
    if isinstance(event, ObligationRaised):
        return event.due_date
    return event.date


def _genesis(known: Sequence[Event], as_of_date: dt.date) -> dt.date:
    """The first date the projection reports on.

    CONTRACTS.md §5.1 step 3 says "genesis (earliest event date)", read here as the
    earliest date that decides a *period*, so a backdated `ObligationRaised` due before
    every other event still gets a period to live in.

    Two clamps make the answer total:

    * an empty ledger has no earliest event, so genesis is `as_of_date` and `State`
      reports exactly the current period, all zeros;
    * a ledger whose earliest event is *after* `as_of_date` — every event in the future —
      would otherwise produce an inverted range and no periods at all, so genesis is
      capped at `as_of_date`.

    Both keep `current_period_id` inside `periods`, which every consumer of `State` may
    assume, and both discharge `build_statement_cycles`'s `genesis <= as_of_date`
    precondition (CONTRACTS.md §8.3).

    Definitions deliberately do not widen the range. A `FixedCost` effective from 2020
    against an empty ledger expands nothing, because nothing has happened yet; periods
    follow the ledger, not the forecast.
    """
    if not known:
        return as_of_date
    return min(min(_period_date(event) for event in known), as_of_date)


def _sum_by_key(pairs: Iterable[tuple[str, Minor]]) -> Mapping[str, Minor]:
    """Total each key's amounts. Sort-then-group, so nothing is accumulated into.

    `sorted` + `itertools.groupby` is what lets this be a dict *comprehension* rather
    than a running accumulator (CLAUDE.md §4.2), and it makes the result independent of
    the order the pairs were generated in.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    return {
        key: sum(amount for _, amount in group)
        for key, group in itertools.groupby(ordered, key=lambda pair: pair[0])
    }


def _resolve_account(
    accounts: Sequence[Account],
    account_id: str,
    at: dt.date,
) -> Account | None:
    """The version of `account_id` effective at `at`, falling back to its last version.

    The fallback mirrors `domain/accounts.py::_build_balance`: an account whose versions
    have all been closed out, or whose first version starts after `at`, still describes a
    real account and must not vanish from the answer. `None` means no version of this
    `entity_id` exists at all, which by projection time can only be a definition that was
    never written (`UNKNOWN_ACCOUNT` is a write-time error, CONTRACTS.md §7.1).
    """
    resolved = resolve_version(accounts, account_id, at)
    if resolved is not None:
        return resolved
    candidates = [
        account for account in accounts if account.entity_id == account_id
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda account: (
            account.effective_from,
            account.recorded_at,
            str(account.version_id),
        ),
    )


def _card_budget_timing(
    accounts: Sequence[Account],
    account_id: str,
    at: dt.date,
) -> BudgetTiming | None:
    """The card's `budget_timing` at `at`; `None` when `account_id` is not a card then.

    `budget_timing` is `CREDIT_CARD` only (CONTRACTS.md §4), so returning `None` for
    every other kind — and for an account with no definition — collapses "not a card",
    "unknown account" and "not applicable" into the single answer every call site wants:
    recognize the expense at purchase, which is what happens for cash, checking and
    savings anyway.

    Resolved at the **event's own date**, not at the period start or `as_of_date`.
    PLAN.md §8.3 pins `AllocationPolicy` at the period start and APR at the cycle start
    because each governs a whole *interval*; `budget_timing` governs a single event, so
    the date of that event is what decides it. The consequence is the one that matters:
    switching a card to a different mode tomorrow cannot re-recognize a purchase made
    yesterday, so no closed period moves.
    """
    version = _resolve_account(accounts, account_id, at)
    if version is None or version.kind is not AccountKind.CREDIT_CARD:
        return None
    return version.budget_timing


def _first_account_of_kind(
    accounts: Sequence[Account],
    kind: AccountKind,
    as_of_date: dt.date,
) -> str | None:
    """The lowest-sorting `account_id` of `kind`, or `None` when there is none.

    The implied savings transfer needs one checking account and one savings account to
    move money between (PLAN.md §6.2), and CONTRACTS.md §4 seeds exactly one of each.
    Picking the lexicographically first of whatever exists keeps the choice total and
    deterministic when a user has added a second, rather than making the answer depend on
    definition order.
    """
    for account_id in sorted({account.entity_id for account in accounts}):
        version = _resolve_account(accounts, account_id, as_of_date)
        if version is not None and version.kind is kind:
            return account_id
    return None


def _resolve_policy(
    policies: Sequence[AllocationPolicy],
    period_start: dt.date,
) -> AllocationPolicy:
    """The policy governing a period that starts on `period_start`.

    Resolved at the **period start date**, which is what makes a closed period immune to
    a policy change: its policy was pinned by a date that has already passed (PLAN.md
    §8.3). A policy effective mid-period applies from the next period.

    `AllocationPolicy` carries an `entity_id` like every other definition, and nothing in
    the contracts names a single canonical one, so more than one policy entity may be
    effective at once. Each entity is resolved on its own — that is what `resolve_version`
    guarantees non-overlap for — and the latest-starting winner is taken, ties broken by
    `(effective_from, recorded_at, version_id)`. The tie-break is the same total order
    `domain/definitions.py` uses, so the answer never depends on the order the bundle
    arrived in.

    Falls back to `_FALLBACK_POLICY` when nothing governs the period; see its docstring.
    """
    resolved = [
        version
        for version in (
            resolve_version(policies, entity_id, period_start)
            for entity_id in sorted({policy.entity_id for policy in policies})
        )
        if version is not None
    ]
    if not resolved:
        return _FALLBACK_POLICY
    return max(
        resolved,
        key=lambda policy: (
            policy.effective_from,
            policy.recorded_at,
            str(policy.version_id),
        ),
    )


# ------------------------------------------------------------------------- recognition


def _income_pairs(
    known: Sequence[Event],
    resolver: PeriodResolver,
) -> tuple[tuple[PeriodId, Minor], ...]:
    """`IncomeReceived` amounts, by period. Actual receipts only.

    `RecurringIncome` is forecast and never lands here (PLAN.md §8.2). The asymmetry with
    `FixedCost`, which *is* expanded, is the point: an unpaid bill is still owed, an
    unreceived paycheck cannot be spent.
    """
    return tuple(
        (resolver.period_for(event.date), event.amount_minor)
        for event in known
        if isinstance(event, IncomeReceived)
    )


def _gift_pairs(
    known: Sequence[Event],
    resolver: PeriodResolver,
) -> tuple[tuple[PeriodId, Minor], ...]:
    """`GiftReceived` amounts, by period. Folds into allocatable income identically to
    income; the separate total exists only to label the source (CONTRACTS.md §3.2)."""
    return tuple(
        (resolver.period_for(event.date), event.amount_minor)
        for event in known
        if isinstance(event, GiftReceived)
    )


def _draw_pairs(
    known: Sequence[Event],
    resolver: PeriodResolver,
) -> tuple[tuple[PeriodId, Minor], ...]:
    """`SavingsDrawn` amounts, by period.

    A draw is a deliberate top-up of discretionary from savings (PLAN.md §6.2), so it
    *adds* to what is available to spend and is never an expense. It is not allocatable
    income either — allocating it would split it 50/50 and hand half of it straight back
    to savings, which is the opposite of what the user asked for.
    """
    return tuple(
        (resolver.period_for(event.date), event.amount_minor)
        for event in known
        if isinstance(event, SavingsDrawn)
    )


def _spend_pairs(
    known: Sequence[Event],
    cycles: Sequence[StatementCycleSummary],
    accounts: Sequence[Account],
    resolver: PeriodResolver,
) -> tuple[tuple[PeriodId, Minor], ...]:
    """Everything that reduces discretionary, by period. The recognition principle, coded.

    Three disjoint sources, and the disjointness is the whole invariant (PLAN.md §1):

    1. **Expenses recognized at purchase** — every `ExpenseRecorded` except those charged
       to a card running `AT_STATEMENT_PAYMENT`. A negative amount is a refund and
       correctly reduces the total.
    2. **Statement payments** — a `TransferMade` *into* a card running
       `AT_STATEMENT_PAYMENT`, for its full amount, which is what that mode means
       (PLAN.md §6.4). The same transfer into any other account recognizes nothing, so
       transfers stay budget-neutral everywhere else.
    3. **Card interest** — one figure per statement cycle of a card running
       `AT_PURCHASE`, recognized in the period containing the cycle's close date.

    Why interest comes from the *cycle* and never from the `InterestCharged` event: the
    cycle already resolves estimate against actual. A recorded `InterestCharged` pins its
    cycle, so `interest_minor` **is** the recorded amount (CONTRACTS.md §8.6) — reading
    both would recognize a pinned charge twice. Taking the cycle also means a user who
    never enters an actual still sees what carrying the balance costs, which PLAN.md §7.3
    gives as the reason the estimate exists at all.

    Under `AT_STATEMENT_PAYMENT` interest is deliberately absent from every branch: the
    payment recognized in (2) already contains it, and charging it again is the
    double-count PLAN.md §6.4 calls the easiest bug this flag produces.

    Every cycle closes on or before `as_of_date` and on or after genesis, so the period a
    cycle's interest is recognized in is always one of the reported periods.
    """
    expenses = (
        (resolver.period_for(event.date), event.amount_minor)
        for event in known
        if isinstance(event, ExpenseRecorded)
        and _card_budget_timing(accounts, event.account_id, event.date)
        is not BudgetTiming.AT_STATEMENT_PAYMENT
    )
    statement_payments = (
        (resolver.period_for(event.date), event.amount_minor)
        for event in known
        if isinstance(event, TransferMade)
        and _card_budget_timing(accounts, event.to_account_id, event.date)
        is BudgetTiming.AT_STATEMENT_PAYMENT
    )
    card_interest = (
        (
            resolver.period_for(cycle.end_date_exclusive - _ONE_DAY),
            cycle.interest_minor,
        )
        for cycle in cycles
        if _card_budget_timing(accounts, cycle.account_id, cycle.start_date)
        is BudgetTiming.AT_PURCHASE
    )
    return tuple(itertools.chain(expenses, statement_payments, card_interest))


# -------------------------------------------------------------------------- obligations


def _obligation_rows(
    known: Sequence[Event],
    definitions: Definitions,
    period_ids: Sequence[PeriodId],
    resolver: PeriodResolver,
) -> tuple[ObligationRow, ...]:
    """Expand, supersede, and settle every obligation in the reported window.

    `FixedCost` definitions expand into expected obligations per period, and an explicit
    `ObligationRaised` carrying the same `recurring_id` in the same due-period replaces
    the expected one — actual beats forecast, and *replaces* rather than sums, because
    summing is the recognition-principle failure in its obligation-shaped form
    (PLAN.md §8.1, CONTRACTS.md §8.5).

    Raised obligations are restricted to due-periods inside the window. An obligation due
    after `as_of_date` is a forecast the caller can see by advancing `as_of_date`
    (CONTRACTS.md §6.3); leaving it in would put a row in `State.obligations` whose
    `period_id` matches no `PeriodSummary`, and its `fixed_due` would then be recognized
    nowhere.

    `paid_minor` sums every `PaymentMade` naming the obligation, whenever it was paid —
    it answers "how much of this bill is settled", which is what `status` and
    `remaining_minor` are derived from. Payments are cash and never touch allocation
    (accrual basis, CONTRACTS.md §3.2): the bill was recognized when it was raised.
    """
    window = frozenset(period_ids)
    raised = tuple(
        event
        for event in known
        if isinstance(event, ObligationRaised)
        and resolver.period_for(event.due_date) in window
    )
    expected: tuple[ExpectedObligation, ...] = tuple(
        itertools.chain.from_iterable(
            expand_fixed_costs(definitions.fixed_costs, period_id, resolver)
            for period_id in period_ids
        )
    )
    settled = _sum_by_key(
        (event.obligation_id, event.amount_minor)
        for event in known
        if isinstance(event, PaymentMade)
    )
    return tuple(
        ObligationRow(
            obligation_id=row.obligation_id,
            source=row.source,
            period_id=row.period_id,
            due_date=row.due_date,
            payee=row.payee,
            category=row.category,
            recurring_id=row.recurring_id,
            amount_minor=row.amount_minor,
            paid_minor=settled.get(row.obligation_id, 0),
            remaining_minor=row.amount_minor - settled.get(row.obligation_id, 0),
            status=derive_obligation_status(
                row.amount_minor, settled.get(row.obligation_id, 0)
            ),
        )
        for row in supersede_expected(expected, raised, resolver)
    )


# ------------------------------------------------------------------------------ cycles


def _statement_cycles(
    known: Sequence[Event],
    definitions: Definitions,
    genesis: dt.date,
    as_of_date: dt.date,
    resolver: PeriodResolver,
) -> tuple[StatementCycleSummary, ...]:
    """Fold every account's cycles, in order, per account (CONTRACTS.md §5.1 step 5).

    Cycles are enumerated from the ledger's genesis for every account, not from each
    account's own first version: an account opened later simply carries a zero balance
    through its early cycles, and one shared origin keeps every account's cycle
    boundaries aligned with the same period grid.

    The account handed to `build_statement_cycles` is the version effective at
    `as_of_date` (falling back to the last one), because `kind` and `statement_close_day`
    decide the *shape* of the whole cycle series and that shape has to be stable.
    `fold_statement_cycles` then re-resolves per cycle at the cycle's start date, which is
    what PLAN.md §7.4 requires of the APR — so a rate change never moves a past cycle's
    interest.

    Accounts are visited in sorted `entity_id` order, so `State.statement_cycles` is
    deterministic and grouped by account.
    """
    account_ids = sorted({account.entity_id for account in definitions.accounts})
    return tuple(
        itertools.chain.from_iterable(
            fold_statement_cycles(
                version,
                definitions.accounts,
                known,
                build_statement_cycles(version, genesis, as_of_date, resolver),
            )
            for version in (
                _resolve_account(definitions.accounts, account_id, as_of_date)
                for account_id in account_ids
            )
            if version is not None
        )
    )


# ------------------------------------------------------------------------------ savings


def _savings_summary(
    known: Sequence[Event],
    periods: Sequence[PeriodSummary],
    savings_account_id: str | None,
) -> SavingsSummary:
    """The budget-side view of savings (CONTRACTS.md §5.2).

    Its invariant is satisfied by construction — every term below is a summand of
    `balance_minor`, so there is nothing to check afterward:

        balance == opening + cumulative_allocated - cumulative_drawn
                 + cumulative_interest ± explicit transfers

    Three choices the contract leaves open:

    * **`cumulative_allocated_minor` counts closed periods only.** The implied transfer
      posts on the period's last day (PLAN.md §6.2), so the in-progress period's
      allocation is not in the balance yet — it is reported separately as
      `pending_allocation_minor`, which is exactly what that field is for. A negative
      allocation counts negatively and drains savings, which is the automatic
      shortfall drain, not a special case.
    * **`cumulative_interest_minor` counts recorded `InterestEarned` events only**, never
      the projection's estimates. This is the same rule `domain/accounts.py` applies to
      `AccountBalance.cumulative_interest_minor`, and keeping the two identical is what
      makes the relationship between the two views a single sentence: this balance is the
      savings `AccountBalance` minus `cumulative_drawn_minor`. A figure mixing actuals
      with estimates would reconcile against nothing.
    * **A draw does not move the savings account balance.** `domain/accounts.py`
      deliberately ignores `SavingsDrawn`, because when cash really moves the ledger
      carries a `TransferMade` for it and folding both would debit savings twice for one
      movement. The draw is counted here, on the budget side, and only here.

    With no savings account defined every term but the allocations is zero, which is the
    honest answer rather than an error.
    """
    opening_minor = sum(
        event.amount_minor
        for event in known
        if isinstance(event, AccountOpeningBalance)
        and event.account_id == savings_account_id
    )
    interest_minor = sum(
        event.amount_minor
        for event in known
        if isinstance(event, InterestEarned)
        and event.account_id == savings_account_id
    )
    transfers_minor = sum(
        _transfer_delta(event, savings_account_id)
        for event in known
        if isinstance(event, TransferMade)
    )
    allocated_minor = sum(
        period.savings_allocated_minor for period in periods if period.is_closed
    )
    pending_minor = sum(
        period.savings_allocated_minor for period in periods if not period.is_closed
    )
    drawn_minor = sum(
        event.amount_minor for event in known if isinstance(event, SavingsDrawn)
    )

    return SavingsSummary(
        balance_minor=(
            opening_minor
            + allocated_minor
            - drawn_minor
            + interest_minor
            + transfers_minor
        ),
        cumulative_allocated_minor=allocated_minor,
        cumulative_drawn_minor=drawn_minor,
        cumulative_interest_minor=interest_minor,
        pending_allocation_minor=pending_minor,
    )


def _transfer_delta(transfer: TransferMade, account_id: str | None) -> Minor:
    """The signed effect of an explicit transfer on `account_id`; 0 if it misses it."""
    if account_id is None:
        return 0
    if transfer.from_account_id == account_id:
        return -transfer.amount_minor
    if transfer.to_account_id == account_id:
        return transfer.amount_minor
    return 0


# ----------------------------------------------------------------------------- warnings


def _savings_available_minor(
    known: Sequence[Event],
    periods: Sequence[PeriodSummary],
    savings_account_id: str | None,
    at: dt.date,
) -> Minor:
    """What savings held on `at`, before any draw is subtracted.

    The same terms as `_savings_summary`, restricted to what had happened by `at`:
    openings and explicit transfers dated on or before it, and allocations whose implied
    transfer had already posted — that is, periods that closed on or before it.
    """
    posted_minor = sum(
        period.savings_allocated_minor
        for period in periods
        if period.end_date_exclusive <= at + _ONE_DAY
    )
    ledger_minor = sum(
        _savings_ledger_delta(event, savings_account_id)
        for event in known
        if event.date <= at
    )
    return posted_minor + ledger_minor


def _savings_ledger_delta(event: Event, savings_account_id: str | None) -> Minor:
    """One event's effect on the savings balance, draws excluded.

    Draws are excluded because the caller subtracts only the draws that are *strictly
    earlier* in ledger order, which is what makes "did this draw exceed the balance"
    answerable per draw rather than only in aggregate.
    """
    if savings_account_id is None:
        return 0
    if isinstance(event, (AccountOpeningBalance, InterestEarned)):
        return event.amount_minor if event.account_id == savings_account_id else 0
    if isinstance(event, TransferMade):
        return _transfer_delta(event, savings_account_id)
    return 0


def _draw_warnings(
    known: Sequence[Event],
    periods: Sequence[PeriodSummary],
    savings_account_id: str | None,
    resolver: PeriodResolver,
) -> tuple[Warning, ...]:
    """`SAVINGS_DRAW_EXCEEDS_BALANCE`, one per draw that outran the balance.

    A draw is never rejected. Backdating legitimately reorders events, so a draw that
    looks overdrawn today may be fine once an earlier income event arrives tomorrow
    (PLAN.md §6.2) — which is precisely why this is data and not an exception.

    Each draw is judged against the balance on its own date *less* the draws that precede
    it in ledger order. `itertools.accumulate` with `initial=0` gives that running total
    without an accumulator to reassign: element *i* is the sum of the first *i* draws.
    """
    draws = tuple(event for event in known if isinstance(event, SavingsDrawn))
    already_drawn = tuple(
        itertools.accumulate((draw.amount_minor for draw in draws), initial=0)
    )
    return tuple(
        Warning(
            code=WarningCode.SAVINGS_DRAW_EXCEEDS_BALANCE,
            message=(
                f"savings draw of {draw.amount_minor} exceeds the "
                f"{available_minor} available on {draw.date.isoformat()}"
            ),
            period_id=resolver.period_for(draw.date),
            event_id=draw.event_id,
            account_id=savings_account_id,
        )
        for draw, available_minor in (
            (
                draw,
                _savings_available_minor(
                    known, periods, savings_account_id, draw.date
                )
                - prior_minor,
            )
            for draw, prior_minor in zip(draws, already_drawn, strict=False)
        )
        if draw.amount_minor > available_minor
    )


def _build_warnings(
    known: Sequence[Event],
    periods: Sequence[PeriodSummary],
    obligations: Sequence[ObligationRow],
    accounts: Sequence[AccountBalance],
    cycles: Sequence[StatementCycleSummary],
    savings_account_id: str | None,
    as_of_date: dt.date,
    resolver: PeriodResolver,
) -> tuple[Warning, ...]:
    """Every anomaly in `State`, as data (CONTRACTS.md §7.2).

    None of these prevents a projection and none is ever raised. They are built in a
    fixed order — periods ascending, then obligations, then draws in ledger order, then
    payments in ledger order, then cycles, then accounts — over inputs that were
    themselves sorted before folding, so the sequence is deterministic and unaffected by
    ingestion order without needing a sort of its own.

    `ESTIMATED_INTEREST` is emitted only for a cycle whose estimate is non-zero. Every
    asset account gets a cycle per period whether or not it bears interest, so flagging
    zero-interest estimates would bury the real ones under a warning per account per
    period.

    `PAYMENT_WITHOUT_OBLIGATION` checks the payment's target against the obligations in
    the window *and* every `ObligationRaised` in the ledger, so paying a bill that falls
    due after `as_of_date` is not reported as orphaned. A payment genuinely is orphaned
    when its obligation was voided — the ledger is append-only and the projection must
    survive it, which is why this is a warning and `UNKNOWN_OBLIGATION` is a write-time
    error only (CONTRACTS.md §7.1).
    """
    negative_allocations = tuple(
        Warning(
            code=WarningCode.NEGATIVE_ALLOCATION,
            message=(
                f"fixed costs exceed income: savings "
                f"{period.savings_allocated_minor}, discretionary "
                f"{period.discretionary_allocated_minor}"
            ),
            period_id=period.period_id,
        )
        for period in periods
        if period.savings_allocated_minor < 0
        or period.discretionary_allocated_minor < 0
    )
    overpaid = tuple(
        Warning(
            code=WarningCode.OBLIGATION_OVERPAID,
            message=(
                f"obligation {row.obligation_id} is overpaid by "
                f"{-row.remaining_minor}"
            ),
            period_id=row.period_id,
        )
        for row in obligations
        if row.status is ObligationStatus.OVERPAID
    )
    past_due = tuple(
        Warning(
            code=WarningCode.OBLIGATION_PAST_DUE_UNPAID,
            message=(
                f"obligation {row.obligation_id} was due "
                f"{row.due_date.isoformat()} with {row.remaining_minor} outstanding"
            ),
            period_id=row.period_id,
        )
        for row in obligations
        if row.due_date < as_of_date
        and row.status in (ObligationStatus.UNPAID, ObligationStatus.PARTIALLY_PAID)
    )
    known_obligation_ids = frozenset(
        row.obligation_id for row in obligations
    ) | frozenset(
        event.obligation_id
        for event in known
        if isinstance(event, ObligationRaised)
    )
    orphaned = tuple(
        Warning(
            code=WarningCode.PAYMENT_WITHOUT_OBLIGATION,
            message=(
                f"payment of {event.amount_minor} names unknown obligation "
                f"{event.obligation_id}"
            ),
            period_id=resolver.period_for(event.date),
            event_id=event.event_id,
            account_id=event.account_id,
        )
        for event in known
        if isinstance(event, PaymentMade)
        and event.obligation_id not in known_obligation_ids
    )
    estimated = tuple(
        Warning(
            code=WarningCode.ESTIMATED_INTEREST,
            message=(
                f"cycle {cycle.cycle_id} carries an estimated "
                f"{cycle.interest_minor}; a recorded interest event supersedes it"
            ),
            account_id=cycle.account_id,
        )
        for cycle in cycles
        if cycle.is_estimate and cycle.interest_minor != 0
    )
    overdrawn = tuple(
        Warning(
            code=WarningCode.CHECKING_OVERDRAWN,
            message=f"checking balance is {account.balance_minor}",
            account_id=account.account_id,
        )
        for account in accounts
        if account.kind is AccountKind.CHECKING and account.balance_minor < 0
    )
    return (
        negative_allocations
        + overpaid
        + past_due
        + _draw_warnings(known, periods, savings_account_id, resolver)
        + orphaned
        + estimated
        + overdrawn
    )


# ------------------------------------------------------------------------------ periods


def _build_period(
    period_id: PeriodId,
    bounds: tuple[dt.date, dt.date],
    as_of_date: dt.date,
    policy: AllocationPolicy,
    income: Mapping[str, Minor],
    gifts: Mapping[str, Minor],
    drawn: Mapping[str, Minor],
    spent: Mapping[str, Minor],
    due: Mapping[str, Minor],
    paid: Mapping[str, Minor],
) -> PeriodSummary:
    """One period's numbers.

    The top-level invariant holds by construction rather than by check (PLAN.md §5.3):
    `allocate_period` takes fixed off the top and hands the exact remainder — positive,
    zero, or negative — to `split_bps`, whose own postcondition is that the shares sum to
    it exactly. Nothing is clamped, so a period whose fixed costs exceed its income
    reports negative savings *and* negative discretionary, split by the same policy
    (PLAN.md §6.1).

    `fixed_paid_minor` is obligation-aligned: it is what has been paid against the bills
    *due in this period*, whenever those payments were made, so
    `fixed_outstanding_minor` equals the sum of this period's `remaining_minor` values
    and the summary reconciles against the obligation rows. The alternative reading —
    payments bucketed by their own date — makes `fixed_due - fixed_paid` a difference of
    two different periods' quantities, which is not a number anything can use.
    """
    start_date, end_date_exclusive = bounds
    allocatable_income_minor = income.get(period_id, 0) + gifts.get(period_id, 0)
    fixed_due_minor = due.get(period_id, 0)
    shares = allocate_period(allocatable_income_minor, fixed_due_minor, policy)
    savings_drawn_minor = drawn.get(period_id, 0)
    discretionary_spent_minor = spent.get(period_id, 0)

    return PeriodSummary(
        period_id=period_id,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        is_closed=end_date_exclusive <= as_of_date,
        policy_version_id=policy.version_id,
        savings_bps=policy.savings_bps,
        discretionary_bps=policy.discretionary_bps,
        income_minor=income.get(period_id, 0),
        gifts_minor=gifts.get(period_id, 0),
        allocatable_income_minor=allocatable_income_minor,
        fixed_due_minor=fixed_due_minor,
        fixed_paid_minor=paid.get(period_id, 0),
        fixed_outstanding_minor=fixed_due_minor - paid.get(period_id, 0),
        savings_allocated_minor=shares[_SAVINGS_BUCKET],
        discretionary_allocated_minor=shares[_DISCRETIONARY_BUCKET],
        savings_drawn_minor=savings_drawn_minor,
        discretionary_spent_minor=discretionary_spent_minor,
        discretionary_remaining_minor=(
            shares[_DISCRETIONARY_BUCKET]
            + savings_drawn_minor
            - discretionary_spent_minor
        ),
    )


def _implied_savings_transfers(
    periods: Sequence[PeriodSummary],
    checking_account_id: str | None,
    savings_account_id: str | None,
) -> tuple[tuple[dt.date, str, str, Minor], ...]:
    """The derived checking->savings movements, one per closed period (PLAN.md §6.2).

    Allocation to savings *implies* a transfer, so the budget figure and the account
    balance are equal by construction and there is no "budgeted versus actually moved"
    gap to reconcile. Three properties of the shape, each load-bearing:

    * **Derived, never persisted.** The projection cannot write events; this is a value
      computed during the fold and handed to `fold_account_balances`.
    * **Posts on the period's last day, in one movement.** The in-progress period has not
      posted, which is what `SavingsSummary.pending_allocation_minor` reports.
    * **A negative allocation reverses the direction.** The amount is signed and
      `domain/accounts.py::_implied_transfer_delta` negates for the `from` side and adds
      for the `to` side, so savings drains back to checking during a shortfall with no
      branch anywhere for the sign.

    A zero allocation is omitted: it moves nothing, and emitting it would put an
    empty movement in the fold for every period a user had no income.
    """
    if checking_account_id is None or savings_account_id is None:
        return ()
    return tuple(
        (
            period.end_date_exclusive - _ONE_DAY,
            checking_account_id,
            savings_account_id,
            period.savings_allocated_minor,
        )
        for period in periods
        if period.is_closed and period.savings_allocated_minor != 0
    )


# ------------------------------------------------------------------------------ project


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

    Two decisions that shape everything below, taken where the contracts are silent:

    * **`as_of_date` truncates knowledge, not just the report.** An event whose business
      date is after `as_of_date` is invisible to every figure in `State` — balances,
      cycles, allocation and obligations alike. That is what makes a time-travel query
      honest: `project(events, definitions, t)` answers with what was known on `t`,
      whether `t` is today or three months ago, and re-running it after more events
      arrive gives the same answer unless one of them is backdated to on or before `t`.
    * **`State` is closed under its own period range.** Every obligation row, every
       statement cycle and every recognized amount belongs to a period in `periods`, so a
       consumer can fold `State.periods` and know nothing was left out. A caller wanting
       the future advances `as_of_date`, which CONTRACTS.md §6.3 explicitly permits.

    Anomalies are warnings; malformed *definitions* are still errors. A `CREDIT_CARD`
    with no `statement_close_day` propagates `AppError(VALIDATION_FAILED)` out of
    `build_statement_cycles`, because that is input that could never be valid rather than
    a surprising state (CONTRACTS.md §7). Nothing about the ledger raises.
    """
    active_resolver = CalendarMonthResolver() if resolver is None else resolver

    # 1. Voided events and the void records themselves both drop out. `voids` is keyed by
    #    TARGET id, which is what makes `is_voided` an O(1) lookup — the projection
    #    filters every event through it on every read (PLAN.md §3). Two voids aimed at one
    #    event collapse to the same answer, so which of them survives the comprehension
    #    cannot change the result.
    voids: dict[UUID, EventVoided] = {
        event.target_event_id: event
        for event in events
        if isinstance(event, EventVoided)
    }
    surviving: tuple[Event, ...] = tuple(
        event
        for event in events
        if not isinstance(event, EventVoided) and not is_voided(event, voids)
    )

    # 2. One total, stable order, established once and relied on by everything after it:
    #    `fold_account_balances` and `fold_statement_cycles` both state "events sorted"
    #    as a precondition. Sorting here rather than trusting the caller is what makes
    #    ingestion order irrelevant to the answer (CLAUDE.md §5.1 property 6).
    ordered: tuple[Event, ...] = tuple(sorted(surviving, key=_ledger_key))
    known: tuple[Event, ...] = tuple(
        event for event in ordered if event.date <= as_of_date
    )

    # 3. Periods, genesis through as_of, ascending and contiguous.
    genesis = _genesis(known, as_of_date)
    period_ids = tuple(active_resolver.periods_between(genesis, as_of_date))
    period_bounds = {
        period_id: active_resolver.bounds(period_id) for period_id in period_ids
    }

    # 4. Obligations: expand the forecast, let the actual supersede it, then settle.
    obligations = _obligation_rows(known, definitions, period_ids, active_resolver)

    # 5. Statement cycles, in order, per account. This must precede allocation because
    #    card interest under AT_PURCHASE is recognized from the cycle that computed it,
    #    and a cycle cannot be computed in isolation (PLAN.md §7.4).
    cycles = _statement_cycles(
        known, definitions, genesis, as_of_date, active_resolver
    )

    # 6. Per-period allocation. Every input is bucketed by period once, up front, so each
    #    period is built from mappings rather than by rescanning the ledger.
    income = _sum_by_key(_income_pairs(known, active_resolver))
    gifts = _sum_by_key(_gift_pairs(known, active_resolver))
    drawn = _sum_by_key(_draw_pairs(known, active_resolver))
    spent = _sum_by_key(
        _spend_pairs(known, cycles, definitions.accounts, active_resolver)
    )
    due = _sum_by_key((row.period_id, row.amount_minor) for row in obligations)
    paid = _sum_by_key((row.period_id, row.paid_minor) for row in obligations)

    periods = tuple(
        _build_period(
            period_id,
            period_bounds[period_id],
            as_of_date,
            _resolve_policy(
                definitions.allocation_policies, period_bounds[period_id][0]
            ),
            income,
            gifts,
            drawn,
            spent,
            due,
            paid,
        )
        for period_id in period_ids
    )

    # 7. Assemble. The implied savings transfers are derived from the periods just built
    #    and handed to the balance fold — the one place where allocation feeds back into
    #    account balances (PLAN.md §6.2).
    checking_account_id = _first_account_of_kind(
        definitions.accounts, AccountKind.CHECKING, as_of_date
    )
    savings_account_id = _first_account_of_kind(
        definitions.accounts, AccountKind.SAVINGS, as_of_date
    )
    accounts = tuple(
        fold_account_balances(
            known,
            definitions.accounts,
            _implied_savings_transfers(
                periods, checking_account_id, savings_account_id
            ),
            as_of_date,
        )
    )

    return State(
        as_of_date=as_of_date,
        current_period_id=active_resolver.period_for(as_of_date),
        periods=periods,
        obligations=obligations,
        accounts=accounts,
        statement_cycles=cycles,
        savings=_savings_summary(known, periods, savings_account_id),
        warnings=_build_warnings(
            known,
            periods,
            obligations,
            accounts,
            cycles,
            savings_account_id,
            as_of_date,
            active_resolver,
        ),
    )
