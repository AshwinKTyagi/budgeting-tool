"""Shared Hypothesis strategies for the invariant suite (CLAUDE.md §5.2).

Owned by `module/properties` (PLAN.md §13.2, §13.3). Every module in
`tests/properties/` imports from here; nothing redefines a strategy locally. That is
not tidiness — a generator redefined per module drifts, and two properties that
disagree about what a "ledger" is cannot both be checked against the same code.

Three rules this file follows, all of them from `CLAUDE.md`:

* **No clock read.** Every date and instant below is an explicit literal (§4.4), so a
  generated ledger means the same thing in 2026 as in 2036 and a failure reproduces
  from its printed input rather than from the day it was found.
* **No floats.** Every amount is an `int` in minor units and every rate an `int` in
  basis points (§2.1).
* **Bias hard toward the awkward cases.** §5.2 names them: amounts that do not divide
  evenly, zero, exactly one minor unit, negative allocatable income, magnitudes near
  `sys.maxsize`, and 3- or 4-way splits with prime-ish bps like `3333/3333/3334`. Round
  numbers are what a person picks, and they are exactly what must not dominate here —
  so every numeric strategy below is a `one_of` whose first branch is a hand-built list
  of hostile values, giving them roughly half the draws rather than the vanishing
  probability a uniform range would.

The generated world
-------------------
One fixed set of account definitions (`CHECKING`, `SAVINGS`, `CARD`, `LOAN`) and one
window, 2026-01-01 through 2026-04-30, with `AS_OF` at its end. Fixing the world is
what makes a ledger *coherent* in the sense §5.2 asks for: every `account_id` in a
generated event names a real `Account` version, every `PaymentMade` names an obligation
raised earlier in the same ledger, and every `InterestCharged` names a cycle of the
account it is charged to.

Every ledger opens with an anchor `AccountOpeningBalance` dated `ANCHOR_DATE`, so
genesis is the same for every generated example. Several properties compare two
projections period by period, and a genesis that moved with the draw would turn "this
period's number changed" into "this period did not exist before" — a different claim,
and a much weaker one.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Sequence
from typing import Final, NamedTuple
from uuid import UUID

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

from core.periods import CalendarMonthResolver
from core.types import (
    AccountKind,
    Bps,
    BudgetTiming,
    Cadence,
    Minor,
    PeriodId,
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
from domain.projection import State

UTC: Final = dt.timezone.utc

#: The only resolver built (PLAN.md §4.1). Stateless, so one instance is shared.
RESOLVER: Final = CalendarMonthResolver()

# ------------------------------------------------------------------ the fixed world

#: Definitions are effective from long before any generated event, so version
#: resolution is never the thing that makes a property fail.
EPOCH: Final = dt.date(2020, 1, 1)

#: Genesis. Every generated ledger carries an event on this date, so `State.periods`
#: always spans 2026-01 .. 2026-04 regardless of what else was drawn.
ANCHOR_DATE: Final = dt.date(2026, 1, 1)

#: The last date a generated event may carry, and the `as_of_date` every property
#: projects at. Four periods is enough for a card to accumulate five statement cycles
#: and for the grace chain to be exercised end to end, and small enough to stay fast.
AS_OF: Final = dt.date(2026, 4, 30)

#: Instants are audit and tie-break only (CONTRACTS.md §3.1). One per event index keeps
#: the ledger order total, so a shuffle cannot change the answer for the trivial reason
#: that two events tied.
RECORDED_AT: Final = dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

CHECKING: Final = "checking"
SAVINGS: Final = "savings"
CARD: Final = "card"
LOAN: Final = "loan"

#: The card's statement geometry, referenced by the interest properties: it closes on
#: the 28th and is due on the 15th of the following month, so every statement has a
#: grace window that spans a period boundary.
CARD_CLOSE_DAY: Final = 28
CARD_DUE_DAY: Final = 15
CARD_APR_BPS: Final = 2_199
SAVINGS_APR_BPS: Final = 450
LOAN_APR_BPS: Final = 599


def uid(n: int) -> UUID:
    """A stable UUID from an integer. Literal ids keep every failure reproducible."""
    return UUID(int=n)


def recorded_at_for(index: int) -> dt.datetime:
    """A distinct, aware, UTC instant per event index (CLAUDE.md §4.5)."""
    return RECORDED_AT + dt.timedelta(seconds=index)


# --------------------------------------------------------------------- test settings
# Explicit, shared, and applied per test rather than loaded as a global profile: a
# profile loaded from a conftest changes the defaults for every other suite in the
# repository too, and `tests/unit/` is not this branch's to retune.
#
# `deadline=None` everywhere. A per-example deadline turns a slow machine into a red
# suite, and none of these properties is about speed.

#: For pure-arithmetic properties over `core/money.py`. Cheap, so run many.
ARITHMETIC_SETTINGS: Final = settings(max_examples=400, deadline=None)

#: For properties that project a whole generated ledger. Each example folds several
#: accounts over five statement cycles, so the count is lower and the health check for
#: slow data generation is suppressed rather than left to flake.
LEDGER_SETTINGS: Final = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: For properties that build a real SQLite database per example (property 7).
DATABASE_SETTINGS: Final = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ----------------------------------------------------------------------------- money


#: Hostile amounts, drawn about half the time. Every entry is here for a reason:
#: zero and ±1 are the boundaries of the sign logic; `100_001` is PLAN.md §5.2's worked
#: example and does not divide evenly by two; the primes and near-primes defeat any
#: bucket weighting; and `sys.maxsize` is what CLAUDE.md §5.1 property 1 names
#: explicitly, because `magnitude * bps` at that scale is where a language with fixed
#: width integers would overflow and where a float would already have lost the low bits.
_AWKWARD_MINOR: Final[tuple[Minor, ...]] = (
    0,
    1,
    -1,
    2,
    -2,
    3,
    -3,
    7,
    -7,
    99,
    101,
    999,
    1_001,
    9_999,
    100_001,
    -100_001,
    999_999,
    -999_999,
    1_000_003,
    2**31 - 1,
    -(2**31),
    2**53 + 1,
    sys.maxsize,
    -sys.maxsize,
    sys.maxsize - 1,
    -(sys.maxsize - 1),
    sys.maxsize // 3,
)


def minor_amounts() -> st.SearchStrategy[Minor]:
    """Signed minor units, spanning zero, one unit, and magnitudes near `sys.maxsize`.

    CLAUDE.md §5.2's first named strategy. The three branches are the three regimes the
    arithmetic has to survive: hand-picked hostile values, everyday amounts small enough
    that a human could check them by hand, and the full signed integer range.
    """
    return st.one_of(
        st.sampled_from(_AWKWARD_MINOR),
        st.integers(min_value=-999_999, max_value=999_999),
        st.integers(min_value=-sys.maxsize, max_value=sys.maxsize),
    )


#: Positive amounts for the event types whose contract says `> 0` (CONTRACTS.md §3.2).
#: Bounded well below `sys.maxsize`: these feed a ledger whose balances are summed and
#: then multiplied by an APR, and the properties are about recognition, not overflow —
#: `minor_amounts()` is where the extreme magnitudes are checked.
_AWKWARD_POSITIVE: Final[tuple[Minor, ...]] = (
    1,
    2,
    3,
    7,
    99,
    101,
    999,
    1_001,
    4_999,
    5_001,
    100_001,
    123_457,
    999_999,
)


def positive_minor_amounts() -> st.SearchStrategy[Minor]:
    """Strictly positive amounts, biased toward values that do not divide evenly."""
    return st.one_of(
        st.sampled_from(_AWKWARD_POSITIVE),
        st.integers(min_value=1, max_value=999_999),
    )


def expense_amounts() -> st.SearchStrategy[Minor]:
    """Amounts for `ExpenseRecorded`, where a negative is a refund (CONTRACTS.md §3.2)."""
    return st.one_of(
        st.sampled_from(_AWKWARD_POSITIVE),
        st.sampled_from(tuple(-value for value in _AWKWARD_POSITIVE)),
        st.just(0),
        st.integers(min_value=-999_999, max_value=999_999),
    )


def bounded_minor_amounts() -> st.SearchStrategy[Minor]:
    """Signed amounts small enough to fold into a ledger without dwarfing everything else.

    `AccountOpeningBalance` is signed — that is what lets one event type cover both
    "opened checking with $500" and a loan disbursement (CONTRACTS.md §3.2) — so the
    sign has to be drawn. The magnitude is capped because these feed balances that are
    then multiplied by an APR over a cycle; `minor_amounts()` is where the `sys.maxsize`
    end of the range is checked, against the arithmetic that has to survive it.
    """
    return st.one_of(
        st.sampled_from(_AWKWARD_POSITIVE),
        st.sampled_from(tuple(-value for value in _AWKWARD_POSITIVE)),
        st.just(0),
        st.integers(min_value=-9_999_999, max_value=9_999_999),
    )


# ------------------------------------------------------------------------------- bps


#: Bucket sets that a person would not think to try. `3333/3333/3334` is named in
#: CLAUDE.md §5.2; the rest are the shapes that stress each step of PLAN.md §5.1 —
#: a single bucket taking everything, a bucket taking nothing, one-unit slivers, and
#: four-way splits where three remainders tie and declared order has to break it.
_AWKWARD_BPS_SPLITS: Final[tuple[tuple[Bps, ...], ...]] = (
    (10_000,),
    (5_000, 5_000),
    (0, 10_000),
    (10_000, 0),
    (1, 9_999),
    (9_999, 1),
    (4_999, 5_001),
    (5_001, 4_999),
    (6_667, 3_333),
    (3_333, 3_333, 3_334),
    (3_334, 3_333, 3_333),
    (3_333, 3_334, 3_333),
    (2_500, 2_500, 2_500, 2_500),
    (1, 1, 1, 9_997),
    (9_997, 1, 1, 1),
    (7, 11, 13, 9_969),
    (2_501, 2_499, 2_501, 2_499),
    (0, 0, 0, 10_000),
)


@st.composite
def _bps_partition(draw: st.DrawFn, bucket_count: int) -> tuple[Bps, ...]:
    """`bucket_count` non-negative bps summing to exactly 10_000.

    Cut points rather than a normalize-and-fix-up, so the sum is 10_000 by construction
    and the generator can never emit a bucket set that `split_bps` is entitled to
    reject. Uniform cuts land on non-round weights far more often than they land on
    multiples of a thousand, which is the bias this file wants.
    """
    cuts = sorted(
        draw(
            st.lists(
                st.integers(min_value=0, max_value=10_000),
                min_size=bucket_count - 1,
                max_size=bucket_count - 1,
            )
        )
    )
    edges = (0, *cuts, 10_000)
    # `strict=False` is correct here and not a shortcut: `edges[1:]` is one shorter than
    # `edges` by construction, which is what pairs each edge with its successor.
    return tuple(upper - lower for lower, upper in zip(edges, edges[1:], strict=False))


@st.composite
def bps_splits(draw: st.DrawFn, *, max_buckets: int = 4) -> tuple[tuple[str, Bps], ...]:
    """Ordered `(name, bps)` bucket sets summing to exactly 10_000 (CLAUDE.md §5.2).

    Postconditions, all of them `split_bps`'s stated preconditions (CONTRACTS.md §8.1),
    so a draw from here can never be the reason a property fails:

        sum(bps) == 10_000, every bps >= 0, names unique, non-empty, ORDERED
    """
    weights = draw(
        st.one_of(
            st.sampled_from(_AWKWARD_BPS_SPLITS),
            st.integers(min_value=1, max_value=max_buckets).flatmap(_bps_partition),
        )
    )
    return tuple(
        (f"bucket_{index}", bps) for index, bps in enumerate(weights)
    )


def policy_bps() -> st.SearchStrategy[Bps]:
    """A savings share for an `AllocationPolicy`; discretionary takes the remainder."""
    return st.one_of(
        st.sampled_from((0, 1, 3_333, 4_999, 5_000, 5_001, 6_667, 9_999, 10_000)),
        st.integers(min_value=0, max_value=10_000),
    )


# ----------------------------------------------------------------------------- dates


#: Dates a person would not pick: period boundaries, the card's close and due days, and
#: the day either side of each. February 2026 has 28 days, so the 28th is simultaneously
#: the card's close day and the month's last day — the clamp and the boundary at once.
_BOUNDARY_DATES: Final[tuple[dt.date, ...]] = (
    dt.date(2026, 1, 1),
    dt.date(2026, 1, 15),
    dt.date(2026, 1, 27),
    dt.date(2026, 1, 28),
    dt.date(2026, 1, 29),
    dt.date(2026, 1, 31),
    dt.date(2026, 2, 1),
    dt.date(2026, 2, 15),
    dt.date(2026, 2, 27),
    dt.date(2026, 2, 28),
    dt.date(2026, 3, 1),
    dt.date(2026, 3, 15),
    dt.date(2026, 3, 28),
    dt.date(2026, 3, 31),
    dt.date(2026, 4, 1),
    dt.date(2026, 4, 15),
    dt.date(2026, 4, 28),
    dt.date(2026, 4, 30),
)


def business_dates(
    *,
    min_value: dt.date = ANCHOR_DATE,
    max_value: dt.date = AS_OF,
) -> st.SearchStrategy[dt.date]:
    """Business dates in the generated window (CLAUDE.md §5.2).

    `dt.date`, no time, no zone — that is the type, not a convention (CLAUDE.md §4.5).
    Boundary dates get roughly half the draws.
    """
    uniform = st.dates(min_value=min_value, max_value=max_value)
    boundaries = tuple(
        date for date in _BOUNDARY_DATES if min_value <= date <= max_value
    )
    if not boundaries:
        return uniform
    return st.one_of(st.sampled_from(boundaries), uniform)


def period_ids(*, min_value: dt.date = ANCHOR_DATE, max_value: dt.date = AS_OF) -> (
    st.SearchStrategy[PeriodId]
):
    """A period id inside the window, for building a coherent `cycle_id`."""
    return st.sampled_from(tuple(RESOLVER.periods_between(min_value, max_value)))


# ----------------------------------------------------------------------- definitions


def account(
    entity_id: str,
    kind: AccountKind,
    *,
    name: str | None = None,
    apr_bps: Bps = 0,
    statement_close_day: int | None = None,
    payment_due_day: int | None = None,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
    version: int = 1,
) -> Account:
    """One `Account` version. Defaults describe a plain, non-interest-bearing account."""
    return Account(
        version_id=uid(900_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name=entity_id if name is None else name,
        kind=kind,
        apr_bps=apr_bps,
        statement_close_day=statement_close_day,
        payment_due_day=payment_due_day,
        budget_timing=budget_timing,
    )


def card_account(
    *,
    apr_bps: Bps = CARD_APR_BPS,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    statement_close_day: int = CARD_CLOSE_DAY,
    payment_due_day: int = CARD_DUE_DAY,
    version: int = 3,
) -> Account:
    """The credit card. `budget_timing` is the flag PLAN.md §6.4 is about."""
    return account(
        CARD,
        AccountKind.CREDIT_CARD,
        name="Visa",
        apr_bps=apr_bps,
        statement_close_day=statement_close_day,
        payment_due_day=payment_due_day,
        budget_timing=budget_timing,
        version=version,
    )


def allocation_policy(
    savings_bps: Bps = 5_000,
    *,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
    entity_id: str = "policy",
    version: int = 1,
) -> AllocationPolicy:
    """One `AllocationPolicy` version. Discretionary takes whatever savings does not."""
    return AllocationPolicy(
        version_id=uid(800_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        savings_bps=savings_bps,
        discretionary_bps=10_000 - savings_bps,
    )


def fixed_cost(
    entity_id: str = "rent",
    *,
    amount_minor: Minor = 120_003,
    due_day: int = 31,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
    version: int = 1,
) -> FixedCost:
    """A recurring bill. `due_day=31` exercises the short-month clamp every February."""
    return FixedCost(
        version_id=uid(700_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name=entity_id,
        amount_minor=amount_minor,
        cadence=Cadence.MONTHLY,
        due_day=due_day,
        payee="Landlord",
        category="housing",
    )


def recurring_income(
    entity_id: str = "salary",
    *,
    amount_minor: Minor = 450_000,
    version: int = 1,
    cadence: Cadence = Cadence.MONTHLY,
    anchor_day: int = 1,
    effective_from: dt.date = EPOCH,
    effective_to: dt.date | None = None,
) -> RecurringIncome:
    """A forecast paycheck.

    Still never contributes to allocatable income (PLAN.md §8.2) — only an actual
    `IncomeReceived` does, and confirming an occurrence is what appends one. What this
    builder feeds is `expand_recurring_incomes`, the forecast view §8.2 promises.
    """
    return RecurringIncome(
        version_id=uid(600_000 + version),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name=entity_id,
        amount_minor=amount_minor,
        cadence=cadence,
        anchor_day=anchor_day,
        account_id=CHECKING,
    )


@st.composite
def recurring_incomes(draw: st.DrawFn) -> RecurringIncome:
    """One `RecurringIncome` spanning every cadence and the awkward anchor days.

    Biased at the anchors that break naive arithmetic: 29/30/31 exercise the month-end
    clamp, and 17..31 are the ones whose semimonthly second date would ask
    `clamp_day_to_month` for a day past 31 if it were not capped.
    """
    return recurring_income(
        cadence=draw(st.sampled_from(tuple(Cadence))),
        anchor_day=draw(
            st.one_of(
                st.integers(min_value=1, max_value=31),
                st.sampled_from((1, 15, 16, 17, 20, 28, 29, 30, 31)),
            )
        ),
        amount_minor=draw(st.integers(min_value=1, max_value=10_000_000)),
        effective_from=draw(business_dates()),
    )


def base_accounts(
    *,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    card_apr_bps: Bps = CARD_APR_BPS,
    savings_apr_bps: Bps = SAVINGS_APR_BPS,
) -> tuple[Account, ...]:
    """The four accounts every generated ledger is coherent against.

    One of each `AccountKind`: an asset that bears no interest, an asset that does, a
    card with a statement geometry, and a liability with none. Between them they cover
    every branch `fold_account_balances` and `fold_statement_cycles` can take.
    """
    return (
        account(CHECKING, AccountKind.CHECKING, name="Checking"),
        account(SAVINGS, AccountKind.SAVINGS, name="Savings", apr_bps=savings_apr_bps, version=2),
        card_account(apr_bps=card_apr_bps, budget_timing=budget_timing),
        account(LOAN, AccountKind.LOAN, name="Car loan", apr_bps=LOAN_APR_BPS, version=4),
    )


def definitions_bundle(
    *,
    accounts: Sequence[Account] | None = None,
    policies: Sequence[AllocationPolicy] | None = None,
    fixed_costs: Sequence[FixedCost] = (),
    recurring_incomes: Sequence[RecurringIncome] = (),
) -> Definitions:
    """The immutable bundle `project()` takes (CONTRACTS.md §4)."""
    return Definitions(
        recurring_incomes=tuple(recurring_incomes),
        fixed_costs=tuple(fixed_costs),
        allocation_policies=(
            (allocation_policy(),) if policies is None else tuple(policies)
        ),
        accounts=base_accounts() if accounts is None else tuple(accounts),
    )


#: The default world: four accounts, a 50/50 policy, no fixed costs. Used by every
#: property that does not need to vary the definitions.
DEFAULT_DEFINITIONS: Final = definitions_bundle()


def definitions_with_card(
    *,
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE,
    card_apr_bps: Bps = CARD_APR_BPS,
    savings_bps: Bps = 5_000,
    fixed_costs: Sequence[FixedCost] = (),
) -> Definitions:
    """The default world with the card's timing mode and APR under the caller's control."""
    return definitions_bundle(
        accounts=base_accounts(budget_timing=budget_timing, card_apr_bps=card_apr_bps),
        policies=(allocation_policy(savings_bps),),
        fixed_costs=fixed_costs,
    )


# ---------------------------------------------------------------------------- events
# Builders, not strategies. Each takes an index that becomes both the `event_id` and the
# `dedupe_key`, so every event in a generated ledger is distinct under both identities —
# which property 7 needs, since it re-appends a ledger and asserts the second write is a
# no-op decided by the key rather than by accident.


def _key(index: int, event_type: str) -> str:
    return f"manual:{event_type}:prop:{index}"


def income(index: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING) -> IncomeReceived:
    return IncomeReceived(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "IncomeReceived"),
        amount_minor=amount_minor,
        source="Employer",
        account_id=account_id,
    )


def gift(index: int, date: dt.date, amount_minor: Minor, *, account_id: str = CHECKING) -> GiftReceived:
    return GiftReceived(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "GiftReceived"),
        amount_minor=amount_minor,
        source="Aunt",
        account_id=account_id,
    )


def expense(
    index: int,
    date: dt.date,
    amount_minor: Minor,
    *,
    account_id: str = CHECKING,
    category: str = "groceries",
) -> ExpenseRecorded:
    return ExpenseRecorded(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "ExpenseRecorded"),
        amount_minor=amount_minor,
        category=category,
        account_id=account_id,
        merchant="A shop",
    )


def transfer(
    index: int,
    date: dt.date,
    amount_minor: Minor,
    *,
    from_account_id: str = CHECKING,
    to_account_id: str = SAVINGS,
) -> TransferMade:
    return TransferMade(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "TransferMade"),
        amount_minor=amount_minor,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
    )


def savings_drawn(index: int, date: dt.date, amount_minor: Minor) -> SavingsDrawn:
    return SavingsDrawn(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "SavingsDrawn"),
        amount_minor=amount_minor,
        reason="a deliberate top-up",
    )


def obligation_raised(
    index: int,
    date: dt.date,
    due_date: dt.date,
    amount_minor: Minor,
    *,
    obligation_id: str | None = None,
    recurring_id: str | None = None,
) -> ObligationRaised:
    return ObligationRaised(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "ObligationRaised"),
        obligation_id=f"bill:{index}" if obligation_id is None else obligation_id,
        due_date=due_date,
        amount_minor=amount_minor,
        payee="Utility",
        category="utilities",
        recurring_id=recurring_id,
    )


def payment(
    index: int,
    date: dt.date,
    amount_minor: Minor,
    obligation_id: str,
    *,
    account_id: str = CHECKING,
) -> PaymentMade:
    return PaymentMade(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "PaymentMade"),
        amount_minor=amount_minor,
        obligation_id=obligation_id,
        account_id=account_id,
    )


def opening_balance(
    index: int, date: dt.date, amount_minor: Minor, *, account_id: str
) -> AccountOpeningBalance:
    return AccountOpeningBalance(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "AccountOpeningBalance"),
        account_id=account_id,
        amount_minor=amount_minor,
    )


def interest_charged(
    index: int, date: dt.date, cycle_id: str, amount_minor: Minor, *, account_id: str = CARD
) -> InterestCharged:
    return InterestCharged(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "InterestCharged"),
        account_id=account_id,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def interest_earned(
    index: int, date: dt.date, cycle_id: str, amount_minor: Minor, *, account_id: str = SAVINGS
) -> InterestEarned:
    return InterestEarned(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "InterestEarned"),
        account_id=account_id,
        cycle_id=cycle_id,
        amount_minor=amount_minor,
    )


def void(index: int, date: dt.date, target: Event) -> EventVoided:
    return EventVoided(
        event_id=uid(index),
        date=date,
        recorded_at=recorded_at_for(index),
        dedupe_key=_key(index, "EventVoided"),
        target_event_id=target.event_id,
        reason="entered twice",
    )


# --------------------------------------------------------------------------- ledgers

#: The anchor's index. Chosen high enough that no drawn event can collide with it.
_ANCHOR_INDEX: Final = 500_000

#: Index of the first drawn event. Distinct from the anchor and from the indices the
#: properties reserve for events they splice in themselves (900_000 and up).
_FIRST_INDEX: Final = 1

_LEDGER_EVENT_KINDS: Final = (
    "income",
    "gift",
    "expense",
    "transfer",
    "draw",
    "raised",
    "payment",
    "opening",
    "interest_earned",
    "interest_charged",
    "void",
)


def anchor_event(amount_minor: Minor = 500_000) -> AccountOpeningBalance:
    """The opening balance every generated ledger starts from.

    It pins genesis at `ANCHOR_DATE`, so two projections of two different ledgers report
    the same periods and a period-by-period comparison is a comparison of numbers rather
    than of which periods exist.
    """
    return opening_balance(_ANCHOR_INDEX, ANCHOR_DATE, amount_minor, account_id=CHECKING)


@st.composite
def ledgers(
    draw: st.DrawFn,
    *,
    max_events: int = 8,
    include_card: bool = True,
    include_voids: bool = True,
    dates: st.SearchStrategy[dt.date] | None = None,
) -> tuple[Event, ...]:
    """A coherent event sequence over the fixed world (CLAUDE.md §5.2).

    "Coherent" is doing real work here:

    * every `account_id` names an `Account` in `DEFAULT_DEFINITIONS`;
    * every `PaymentMade` names an obligation raised earlier in the same ledger, so the
      ledger exercises the settled path rather than only the orphan warning;
    * every `EventVoided` targets an earlier non-void event, so `CANNOT_VOID_A_VOID`
      never has to be considered;
    * every `InterestCharged` / `InterestEarned` names `f"{account_id}:{period}"` for a
      period inside the window, which is the id shape `build_statement_cycles` emits.

    Income and gifts always land in `CHECKING`, and expenses and payments always leave
    `CHECKING` or the card — never `SAVINGS`. That restriction is what makes the savings
    account's balance reconcile term for term against `SavingsSummary` (property 13):
    savings is touched only by openings, recorded interest, explicit transfers and the
    projection's own implied transfer, which is exactly the set of terms the contract's
    formula names.

    `include_card=False` produces a ledger that never mentions the card — no charges to
    it, no transfers to or from it, no opening balance on it, no `InterestCharged`. That
    is what lets property 14 attribute a discretionary reduction *to* the card by
    difference: a background the card cannot reach, plus a card story spliced into it.

    The returned tuple is in *generation* order, which is deliberately not ledger order.
    Properties that care about arrival order shuffle it further; properties that do not
    are relying on `project()` to sort, which is the thing under test.
    """
    date_strategy = business_dates() if dates is None else dates
    spend_accounts = (CHECKING, CARD) if include_card else (CHECKING,)
    transfer_accounts = (
        (CHECKING, SAVINGS, CARD) if include_card else (CHECKING, SAVINGS)
    )
    opening_accounts = (SAVINGS, CARD, LOAN) if include_card else (SAVINGS, LOAN)

    events: list[Event] = [anchor_event(draw(positive_minor_amounts()))]
    obligation_ids: list[str] = []
    kinds = tuple(
        kind
        for kind in _LEDGER_EVENT_KINDS
        if (include_voids or kind != "void")
        and (include_card or kind != "interest_charged")
    )

    count = draw(st.integers(min_value=0, max_value=max_events))
    for offset in range(count):
        index = _FIRST_INDEX + offset
        kind = draw(st.sampled_from(kinds))
        date = draw(date_strategy)

        if kind == "income":
            events.append(income(index, date, draw(positive_minor_amounts())))
        elif kind == "gift":
            events.append(gift(index, date, draw(positive_minor_amounts())))
        elif kind == "expense":
            events.append(
                expense(
                    index,
                    date,
                    draw(expense_amounts()),
                    account_id=draw(st.sampled_from(spend_accounts)),
                )
            )
        elif kind == "transfer":
            pair = draw(
                st.lists(
                    st.sampled_from(transfer_accounts),
                    min_size=2,
                    max_size=2,
                    unique=True,
                )
            )
            events.append(
                transfer(
                    index,
                    date,
                    draw(positive_minor_amounts()),
                    from_account_id=pair[0],
                    to_account_id=pair[1],
                )
            )
        elif kind == "draw":
            events.append(savings_drawn(index, date, draw(positive_minor_amounts())))
        elif kind == "raised":
            raised = obligation_raised(
                index, date, draw(date_strategy), draw(positive_minor_amounts())
            )
            obligation_ids.append(raised.obligation_id)
            events.append(raised)
        elif kind == "payment":
            if not obligation_ids:
                # Nothing to pay yet. An income event keeps the draw meaningful rather
                # than silently shrinking the ledger by one.
                events.append(income(index, date, draw(positive_minor_amounts())))
            else:
                events.append(
                    payment(
                        index,
                        date,
                        draw(positive_minor_amounts()),
                        draw(st.sampled_from(tuple(obligation_ids))),
                    )
                )
        elif kind == "opening":
            events.append(
                opening_balance(
                    index,
                    date,
                    draw(bounded_minor_amounts()),
                    account_id=draw(st.sampled_from(opening_accounts)),
                )
            )
        elif kind == "interest_earned":
            events.append(
                interest_earned(
                    index,
                    date,
                    f"{SAVINGS}:{draw(period_ids())}",
                    draw(positive_minor_amounts()),
                )
            )
        elif kind == "interest_charged":
            events.append(
                interest_charged(
                    index,
                    date,
                    f"{CARD}:{draw(period_ids())}",
                    draw(positive_minor_amounts()),
                )
            )
        else:
            # Never the anchor, and never another void. Voiding a void is
            # `CANNOT_VOID_A_VOID`, a write-time error the projection never sees
            # (CONTRACTS.md §7.1). Voiding the *anchor* is legal and interesting, but it
            # empties the ledger's earliest date and so moves genesis — which would
            # silently retract this generator's promise that every example reports the
            # same four periods. `test_property_8a_...` voids the anchor deliberately,
            # comparing two projections that both lose it, where genesis moves equally
            # on each side.
            targets = tuple(
                event
                for event in events
                if not isinstance(event, EventVoided)
                and event.event_id != uid(_ANCHOR_INDEX)
            )
            if not targets:
                events.append(income(index, date, draw(positive_minor_amounts())))
            else:
                events.append(void(index, date, draw(st.sampled_from(targets))))

    return tuple(events)


class BackdatedLedger(NamedTuple):
    """A ledger plus an event to insert out of order (CLAUDE.md §5.2).

    `late_event` is dated at or before everything already in `ledger` about half the
    time, which is what makes it genuinely backdated rather than merely appended: it
    lands inside a statement cycle that has already been folded, so its arrival changes
    that cycle's close balance and cascades into every later one (PLAN.md §7.4).
    """

    ledger: tuple[Event, ...]
    late_event: Event


@st.composite
def backdated_ledgers(draw: st.DrawFn, *, max_events: int = 6) -> BackdatedLedger:
    """A coherent ledger and one event whose date precedes part of it."""
    ledger = draw(ledgers(max_events=max_events))
    date = draw(
        st.one_of(
            business_dates(min_value=dt.date(2025, 11, 1), max_value=ANCHOR_DATE),
            business_dates(),
        )
    )
    amount = draw(expense_amounts())
    # Annotated as `tuple[Event, ...]` rather than left to inference: mypy joins a tuple
    # of four different event classes to their common base, `EventBase`, which is not a
    # member of the `Event` union and so is not what `late_event` is declared to hold.
    candidates: tuple[Event, ...] = (
        expense(900_001, date, amount, account_id=CARD),
        expense(900_002, date, amount, account_id=CHECKING),
        income(900_003, date, abs(amount) + 1),
        transfer(
            900_004,
            date,
            abs(amount) + 1,
            from_account_id=CHECKING,
            to_account_id=CARD,
        ),
    )
    return BackdatedLedger(ledger=ledger, late_event=draw(st.sampled_from(candidates)))


# ------------------------------------------------------- purpose-built card ledgers
# Two shapes the interest properties need and that a general ledger generator cannot be
# relied on to produce: one where every statement is settled inside its grace window,
# and one where the card ends fully paid. Both are strategies rather than fixtures
# because the amounts, and therefore every balance and every interest figure, are drawn.


class PaidInFullLedger(NamedTuple):
    """A card whose every statement is paid in full by its due date (property 11).

    Charges land between the 1st and the 20th of January, February and March. The card
    closes on the 28th, so a month's charges are exactly that month's statement; each is
    settled on the 10th of the following month, inside a grace window that runs to the
    15th. By induction every cycle opens at zero, so `total_charged_minor` is also what
    was paid.
    """

    events: tuple[Event, ...]
    total_charged_minor: Minor


@st.composite
def paid_in_full_ledgers(draw: st.DrawFn) -> PaidInFullLedger:
    """Charges settled inside every grace window, over drawn amounts."""
    months = (1, 2, 3)
    charges: list[Event] = []
    settlements: list[Event] = []
    index = 910_000

    for month in months:
        monthly = draw(st.lists(positive_minor_amounts(), min_size=0, max_size=2))
        for amount in monthly:
            index += 1
            day = draw(st.integers(min_value=1, max_value=20))
            charges.append(
                expense(index, dt.date(2026, month, day), amount, account_id=CARD)
            )
        owed = sum(monthly)
        if owed > 0:
            index += 1
            settlements.append(
                transfer(
                    index,
                    dt.date(2026, month + 1, 10),
                    owed,
                    from_account_id=CHECKING,
                    to_account_id=CARD,
                )
            )

    total = sum(
        event.amount_minor for event in charges if isinstance(event, ExpenseRecorded)
    )
    return PaidInFullLedger(
        events=(anchor_event(), *charges, *settlements),
        total_charged_minor=total,
    )


class SettledCardLedger(NamedTuple):
    """A card charged, optionally charged interest, then settled to a zero balance.

    This is the shape property 14 needs. `total_recognized_minor` is what the
    recognition principle says must come off discretionary exactly once, under either
    timing mode: what was charged, plus the interest that was never recognized anywhere
    else. Under `AT_PURCHASE` it arrives as the purchases and the cycle's interest;
    under `AT_STATEMENT_PAYMENT` it arrives as the single settling payment, whose amount
    is the same number by construction.
    """

    events: tuple[Event, ...]
    charged_minor: Minor
    interest_minor: Minor
    total_recognized_minor: Minor


@st.composite
def settled_card_ledgers(draw: st.DrawFn) -> SettledCardLedger:
    """Card charges in January, an optional actual interest charge, settled in February.

    The interest is a recorded `InterestCharged` rather than an estimate so that the
    figure is pinned (PLAN.md §7.4) and the settling amount can be computed exactly —
    an estimate would depend on the very balance the payment is about to change.
    """
    amounts = draw(st.lists(positive_minor_amounts(), min_size=1, max_size=3))
    charges: tuple[Event, ...] = tuple(
        expense(
            920_001 + offset,
            dt.date(2026, 1, draw(st.integers(min_value=2, max_value=20))),
            amount,
            account_id=CARD,
        )
        for offset, amount in enumerate(amounts)
    )
    charged = sum(amounts)
    interest = draw(st.one_of(st.just(0), positive_minor_amounts()))
    pins: tuple[Event, ...] = (
        ()
        if interest == 0
        else (
            interest_charged(
                920_100, dt.date(2026, 1, CARD_CLOSE_DAY), f"{CARD}:2026-01", interest
            ),
        )
    )
    settlement = transfer(
        920_200,
        dt.date(2026, 2, 10),
        charged + interest,
        from_account_id=CHECKING,
        to_account_id=CARD,
    )
    return SettledCardLedger(
        events=(anchor_event(), *charges, *pins, settlement),
        charged_minor=charged,
        interest_minor=interest,
        total_recognized_minor=charged + interest,
    )


# --------------------------------------------------------------------------- readers
# Small total readers over `State`. They live here rather than in each property module
# for the same reason the strategies do: two modules that each write their own "the
# interest of this cycle" can disagree about what happens when the cycle is missing,
# and then one of them passes vacuously.


def cycle_interest(state: State, cycle_id: str) -> Minor:
    """The interest figure of `cycle_id`.

    Raises `KeyError` when the cycle is absent, deliberately: a property about a cycle's
    figure that silently reads zero for a cycle that was never built is a property that
    passes for the wrong reason.
    """
    for cycle in state.statement_cycles:
        if cycle.cycle_id == cycle_id:
            return cycle.interest_minor
    raise KeyError(cycle_id)


def card_interest_total(state: State, *, account_id: str = CARD) -> Minor:
    """Every cycle's interest for one account, summed."""
    return sum(
        cycle.interest_minor
        for cycle in state.statement_cycles
        if cycle.account_id == account_id
    )


def account_balance(state: State, account_id: str) -> Minor:
    """One account's signed balance. Raises when the account is absent."""
    for balance in state.accounts:
        if balance.account_id == account_id:
            return balance.balance_minor
    raise KeyError(account_id)


def discretionary_spent_total(state: State) -> Minor:
    """Everything recognized against discretionary, across every reported period."""
    return sum(period.discretionary_spent_minor for period in state.periods)


def discretionary_remaining_by_period(state: State) -> dict[PeriodId, Minor]:
    """`period_id -> discretionary_remaining_minor`, for period-by-period comparison."""
    return {
        period.period_id: period.discretionary_remaining_minor
        for period in state.periods
    }


def without_voided(events: tuple[Event, ...]) -> tuple[Event, ...]:
    """`events` with every `EventVoided` and every event one targets removed.

    Several properties compute an expected total from the raw ledger and compare it to
    `State`. A voided event is invisible to the projection (CONTRACTS.md §5.1 step 1),
    so summing over the raw tuple would count a different set. This computes the
    surviving set independently of the fold — a test that asked the projection what it
    had filtered would be checking the fold against itself.
    """
    targets = frozenset(
        event.target_event_id for event in events if isinstance(event, EventVoided)
    )
    return tuple(
        event
        for event in events
        if not isinstance(event, EventVoided) and event.event_id not in targets
    )
