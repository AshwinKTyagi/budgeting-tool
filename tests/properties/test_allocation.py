"""Allocation invariants: CLAUDE.md §5.1 properties 1-4.

Owned by `module/properties` (PLAN.md §13.2).

  1  exact sum          -> `test_property_1_split_bps_sums_to_the_total_exactly`
  2  sign symmetry      -> `test_property_2_split_bps_is_symmetric_about_zero`
  3  top-level invariant-> `test_property_3_the_top_level_invariant_holds_for_every_period`
  4  policy is pinned   -> `test_property_4_a_later_policy_leaves_earlier_periods_identical`

Every assertion is `==` on integers. There is no tolerance anywhere in this file and
there is nowhere one could be added (CLAUDE.md §4.6): the arithmetic under test is
integer arithmetic and its postcondition is exactness, so a test that needed slack would
be a test of something else.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from core.money import allocate_period, split_bps
from core.types import Bps, Minor
from domain.events import Event
from domain.projection import project

from tests.properties.strategies import (
    ARITHMETIC_SETTINGS,
    AS_OF,
    LEDGER_SETTINGS,
    allocation_policy,
    bps_splits,
    business_dates,
    definitions_bundle,
    definitions_with_card,
    fixed_cost,
    ledgers,
    minor_amounts,
    policy_bps,
)

# The amounts a `FixedCost` is drawn from. `0` is not among them — a fixed cost is
# `> 0` by contract (CONTRACTS.md §4) — so "no fixed costs at all" is expressed by the
# empty definition tuple instead. `5_000_000` is far larger than any generated income,
# which is how CLAUDE.md §5.1 property 3's "including ledgers where fixed exceeds
# income" is reached on purpose rather than by luck.
_FIXED_AMOUNTS = st.sampled_from((1, 99, 100_001, 123_457, 5_000_000))


# ------------------------------------------------------------------------ property 1


@given(total=minor_amounts(), buckets=bps_splits())
@ARITHMETIC_SETTINGS
def test_property_1_split_bps_sums_to_the_total_exactly(
    total: Minor, buckets: tuple[tuple[str, Bps], ...]
) -> None:
    """Property 1. `sum(split_bps(total, buckets)) == total`, for every total —
    negative, zero, and near `sys.maxsize` — and every bucket set summing to 10000 bps.

    PLAN.md §5.1 makes this exact *by construction*: step 3 measures the shortfall the
    flooring produced and step 4 distributes all of it. The property is here because
    "by construction" is a claim about the code that was written, not about the code
    that is there now.
    """
    shares = split_bps(total, buckets)

    assert sum(shares.values()) == total
    assert set(shares) == {name for name, _ in buckets}
    assert all(isinstance(share, int) for share in shares.values())


@given(total=minor_amounts(), buckets=bps_splits())
@ARITHMETIC_SETTINGS
def test_property_1a_no_share_is_off_by_more_than_one_unit_from_its_exact_value(
    total: Minor, buckets: tuple[tuple[str, Bps], ...]
) -> None:
    """Property 1, strengthened. Exactness of the sum is not enough on its own — one
    bucket taking everything and the rest taking nothing would satisfy it.

    Each share must be its own floor, plus at most the single leftover unit step 4 may
    hand it. This is an equality on integers, not a tolerance: `floor` and `floor + 1`
    are the only two values the algorithm is allowed to produce for a bucket
    (CLAUDE.md §4.6).
    """
    shares = split_bps(total, buckets)
    magnitude = abs(total)
    sign = -1 if total < 0 else 1

    for name, bps in buckets:
        floor_share = sign * (magnitude * bps // 10_000)
        assert shares[name] in (floor_share, floor_share + sign)


# ------------------------------------------------------------------------ property 2


@given(total=minor_amounts(), buckets=bps_splits())
@ARITHMETIC_SETTINGS
def test_property_2_split_bps_is_symmetric_about_zero(
    total: Minor, buckets: tuple[tuple[str, Bps], ...]
) -> None:
    """Property 2. `split_bps(-n, b) == [-x for x in split_bps(n, b)]`.

    Working on `abs(total)` and reapplying the sign is what makes this true (PLAN.md
    §5.1). It matters because allocatable income is genuinely signed — a shortfall is a
    negative remainder split across two buckets (PLAN.md §6.1) — and floor division on a
    negative numerator would bias whichever bucket was declared first.
    """
    positive = split_bps(total, buckets)
    negated = split_bps(-total, buckets)

    assert negated == {name: -share for name, share in positive.items()}


# ------------------------------------------------------------------------ property 3


@given(
    allocatable=minor_amounts(),
    fixed=minor_amounts(),
    savings_bps=policy_bps(),
)
@ARITHMETIC_SETTINGS
def test_property_3_allocate_period_leaves_nothing_over(
    allocatable: Minor, fixed: Minor, savings_bps: Bps
) -> None:
    """Property 3, at the level of `allocate_period` itself (PLAN.md §5.3).

    `fixed + savings + discretionary == allocatable`, exactly, including when the
    remainder is negative and both shares go negative. Nothing is clamped.
    """
    shares = allocate_period(allocatable, fixed, allocation_policy(savings_bps))

    assert fixed + shares["savings"] + shares["discretionary"] == allocatable


@given(
    events=ledgers(),
    savings_bps=policy_bps(),
    fixed_amount=_FIXED_AMOUNTS,
    with_fixed_cost=st.booleans(),
)
@LEDGER_SETTINGS
def test_property_3_the_top_level_invariant_holds_for_every_period(
    events: tuple[Event, ...],
    savings_bps: Bps,
    fixed_amount: Minor,
    with_fixed_cost: bool,
) -> None:
    """Property 3. Exact, for every period of every generated ledger, **including**
    ledgers where fixed exceeds income (CLAUDE.md §5.1).

    The fixed cost is drawn as large as `5_000_000` against incomes capped at `999_999`,
    so the shortfall branch is reached routinely rather than as an edge case, and both
    allocated shares go negative there (PLAN.md §6.1).
    """
    definitions = definitions_with_card(
        savings_bps=savings_bps,
        fixed_costs=(fixed_cost(amount_minor=fixed_amount),) if with_fixed_cost else (),
    )

    state = project(events, definitions, AS_OF)

    for period in state.periods:
        assert (
            period.fixed_due_minor
            + period.savings_allocated_minor
            + period.discretionary_allocated_minor
            == period.allocatable_income_minor
        )


@given(events=ledgers(), savings_bps=policy_bps())
@LEDGER_SETTINGS
def test_property_3a_a_shortfall_drives_both_buckets_negative(
    events: tuple[Event, ...], savings_bps: Bps
) -> None:
    """Property 3, the branch a hand-picked example would skip.

    A fixed cost of `50_000_000` a month against a ledger that can hold at most eight
    income events of at most `999_999` each guarantees a shortfall in every period. Nothing is clamped, so the invariant holds
    with *negative* shares — and a period whose policy gives a bucket zero bps takes
    zero of the shortfall, which is why the assertion is `<= 0` per bucket and exact on
    the sum.
    """
    definitions = definitions_with_card(
        savings_bps=savings_bps,
        fixed_costs=(fixed_cost(amount_minor=50_000_000),),
    )

    state = project(events, definitions, AS_OF)

    for period in state.periods:
        assert period.fixed_due_minor > period.allocatable_income_minor
        assert period.savings_allocated_minor <= 0
        assert period.discretionary_allocated_minor <= 0
        assert (
            period.fixed_due_minor
            + period.savings_allocated_minor
            + period.discretionary_allocated_minor
            == period.allocatable_income_minor
        )


# ------------------------------------------------------------------------ property 4


@given(
    events=ledgers(),
    first_bps=policy_bps(),
    second_bps=policy_bps(),
    change_date=business_dates(),
)
@LEDGER_SETTINGS
def test_property_4_a_later_policy_leaves_earlier_periods_identical(
    events: tuple[Event, ...],
    first_bps: Bps,
    second_bps: Bps,
    change_date: dt.date,
) -> None:
    """Property 4. Changing an `AllocationPolicy` with `effective_from` after a period's
    start leaves that period's numbers **bit-identical**.

    The comparison is on whole `PeriodSummary` models, not on a chosen field: "bit
    identical" is the claim, and comparing the frozen models is the only way to make the
    test say it. `start_date < change_date` is exactly the set of periods PLAN.md §8.3
    protects — a policy is resolved at the period start, so a period that had already
    started when the change took effect keeps the version that was pinned by a date
    which has already passed. A change landing precisely on a period start governs that
    period, and is correctly excluded.
    """
    unchanged = definitions_bundle(policies=(allocation_policy(first_bps, version=1),))
    amended = definitions_bundle(
        policies=(
            allocation_policy(first_bps, effective_to=change_date, version=1),
            allocation_policy(second_bps, effective_from=change_date, version=2),
        )
    )

    before = project(events, unchanged, AS_OF)
    after = project(events, amended, AS_OF)

    assert tuple(p for p in before.periods if p.start_date < change_date) == tuple(
        p for p in after.periods if p.start_date < change_date
    )
