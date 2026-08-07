"""Unit tests for `core/money.py` — owned by `module/core-money` (PLAN.md §13.2).

Two kinds of test live here and they answer different questions.

The example tests transcribe PLAN.md literally. `test_plan_5_2_worked_example` is the
`100_001` 50/50 split from §5.2 written out share by share; if it and a property test
ever disagree, the documentation is the specification and the code is wrong
(CLAUDE.md §5.3).

The Hypothesis properties cover what hand-picked examples structurally cannot, because
a person choosing examples picks round numbers: rounding drift, sign asymmetry, and
totals that do not divide evenly (CLAUDE.md §5). The strategies here are deliberately
LOCAL to this file. `tests/properties/strategies.py` and the fifteen named invariant
tests belong to `module/properties` in Phase 4 (PLAN.md §13.3), and a Phase-1 branch
must not create that file.

No tolerance anywhere: integer arithmetic is exact, so every assertion is `==`
(CLAUDE.md §4.6).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from core.money import FULL_BPS, allocate_period, split_bps
from core.types import AppError, Bps, ErrorCode, Minor

Buckets = tuple[tuple[str, Bps], ...]

FIFTY_FIFTY: Final[Buckets] = (("savings", 5000), ("discretionary", 5000))


@dataclass(frozen=True)
class Policy:
    """Structural stand-in for `domain.definitions.AllocationPolicy`.

    `allocate_period` signs against `AllocationPolicyLike` precisely so that `core/`
    need not import `domain/` (CLAUDE.md §3.1); this class is what that buys, and its
    existence here is why this module has no `domain/` import either.
    """

    savings_bps: Bps
    discretionary_bps: Bps


# --------------------------------------------------------------------------------
# PLAN.md §5.2 — the worked example, transcribed literally.


def test_plan_5_2_worked_example() -> None:
    """`allocatable_income = 100_001`, policy 50/50 -> savings 50001, disc 50000.

    Both buckets floor to 50000 and both fractional remainders tie at 5000, so the
    single leftover unit goes to whichever bucket is declared first. Savings is.
    """
    result = split_bps(100_001, FIFTY_FIFTY)

    assert result == {"savings": 50001, "discretionary": 50000}
    assert result["savings"] + result["discretionary"] == 100_001


def test_plan_5_2_worked_example_negative() -> None:
    """"Negative case, `allocatable_income = -100_001`: shares are `-50001` and
    `-50000`. Same magnitudes, sign reapplied, still exact." — PLAN.md §5.2.
    """
    result = split_bps(-100_001, FIFTY_FIFTY)

    assert result == {"savings": -50001, "discretionary": -50000}
    assert result["savings"] + result["discretionary"] == -100_001


def test_plan_5_2_via_allocate_period() -> None:
    """The same example reached through `allocate_period` with no fixed costs.

    §5.2 states the example in terms of `allocatable_income`, which is what
    `allocate_period` divides once fixed has come off the top.
    """
    result = allocate_period(100_001, 0, Policy(savings_bps=5000, discretionary_bps=5000))

    assert result == {"savings": 50001, "discretionary": 50000}


# --------------------------------------------------------------------------------
# split_bps — exactness across the awkward cases.


def test_split_is_exact_when_it_divides_evenly() -> None:
    assert split_bps(100_000, FIFTY_FIFTY) == {"savings": 50_000, "discretionary": 50_000}


def test_split_of_zero_is_all_zeros() -> None:
    """Zero has no sign and no leftover; every bucket is exactly 0, not -0 or 1."""
    result = split_bps(0, (("a", 3333), ("b", 3333), ("c", 3334)))

    assert result == {"a": 0, "b": 0, "c": 0}


def test_split_of_one_minor_unit() -> None:
    """The whole total is leftover. It goes to one bucket; the rest get nothing.

    This is the smallest case where flooring gives every bucket zero and the
    distribution step is doing all the work.
    """
    assert split_bps(1, FIFTY_FIFTY) == {"savings": 1, "discretionary": 0}
    assert split_bps(-1, FIFTY_FIFTY) == {"savings": -1, "discretionary": 0}


def test_split_of_one_minor_unit_three_ways() -> None:
    """3333/3333/3334: the largest fractional remainder wins outright, no tie."""
    assert split_bps(1, (("a", 3333), ("b", 3333), ("c", 3334))) == {
        "a": 0,
        "b": 0,
        "c": 1,
    }


def test_split_three_ways_prime_ish() -> None:
    """`10` across 3333/3333/3334 — nothing divides evenly, leftover is 1.

    3 * 3333 = 9999 and 3 * 3334 = 10002, so a and b floor to 3 with remainder 3330
    while c floors to 3 with remainder 3340. c is strictly ahead and takes the unit.
    """
    result = split_bps(10, (("a", 3333), ("b", 3333), ("c", 3334)))

    assert result == {"a": 3, "b": 3, "c": 4}
    assert sum(result.values()) == 10


def test_split_four_ways_prime_ish() -> None:
    """A 4-way split with a leftover of 3 — three buckets are promoted, one is not."""
    buckets: Buckets = (("a", 2500), ("b", 2501), ("c", 2499), ("d", 2500))
    result = split_bps(101, buckets)

    assert sum(result.values()) == 101
    assert result == {"a": 25, "b": 26, "c": 25, "d": 25}


def test_leftover_follows_remainder_not_position() -> None:
    """The unit goes to the largest fractional remainder, not the first bucket.

    `tiny` is declared first but its remainder is 1 against `bulk`'s 9999, so
    position never comes into it. Position is the tie-break only.
    """
    result = split_bps(1, (("tiny", 1), ("bulk", 9999)))

    assert result == {"tiny": 0, "bulk": 1}


def test_declared_order_breaks_ties() -> None:
    """Identical bps, identical remainders — the earlier bucket wins.

    Reversing the sequence reverses the winner, which is what makes `buckets` a
    Sequence rather than a mapping (PLAN.md §5.1 step 4).
    """
    forward = split_bps(100_001, (("savings", 5000), ("discretionary", 5000)))
    reversed_ = split_bps(100_001, (("discretionary", 5000), ("savings", 5000)))

    assert forward == {"savings": 50001, "discretionary": 50000}
    assert reversed_ == {"discretionary": 50001, "savings": 50000}


def test_zero_bps_bucket_receives_nothing() -> None:
    """A 0-bps bucket has remainder 0, so it can never be promoted — it is still a key."""
    result = split_bps(99_999, (("none", 0), ("all", 10_000)))

    assert result == {"none": 0, "all": 99_999}


def test_single_bucket_takes_everything() -> None:
    assert split_bps(-7, (("only", 10_000),)) == {"only": -7}


def test_split_near_maxsize() -> None:
    """Large magnitudes stay exact. Python ints do not overflow; floats would."""
    total: Minor = sys.maxsize
    result = split_bps(total, (("a", 3333), ("b", 3333), ("c", 3334)))

    assert sum(result.values()) == total


def test_result_keys_match_declared_buckets() -> None:
    buckets: Buckets = (("x", 1000), ("y", 2000), ("z", 7000))
    result = split_bps(12_345, buckets)

    assert set(result) == {"x", "y", "z"}
    assert list(result) == ["x", "y", "z"]


def test_every_share_is_an_int() -> None:
    """No float is produced at any point — not even one that happens to be whole."""
    result = split_bps(100_001, FIFTY_FIFTY)

    for value in result.values():
        assert type(value) is int


def test_input_sequence_is_not_mutated() -> None:
    buckets: list[tuple[str, Bps]] = [("a", 5000), ("b", 5000)]
    split_bps(100_001, buckets)

    assert buckets == [("a", 5000), ("b", 5000)]


# --------------------------------------------------------------------------------
# split_bps — precondition failures.


def test_bps_not_summing_to_10000_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        split_bps(100, (("a", 4000), ("b", 5000)))

    assert excinfo.value.code is ErrorCode.POLICY_BPS_NOT_10000


def test_bps_summing_over_10000_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        split_bps(100, (("a", 6000), ("b", 5000)))

    assert excinfo.value.code is ErrorCode.POLICY_BPS_NOT_10000


def test_empty_buckets_raises() -> None:
    """An empty sequence sums to 0, so it fails the same precondition."""
    with pytest.raises(AppError) as excinfo:
        split_bps(100, ())

    assert excinfo.value.code is ErrorCode.POLICY_BPS_NOT_10000


def test_negative_bps_raises_even_when_the_sum_is_right() -> None:
    """12000/-2000 sums to 10_000 but is still rejected.

    A negative bps makes that bucket's floor share negative, which drives `leftover`
    above the bucket count. The one-unit-at-a-time distribution could then not place
    every unit and the exact-sum postcondition would fail silently.
    """
    with pytest.raises(AppError) as excinfo:
        split_bps(100, (("a", 12_000), ("b", -2_000)))

    assert excinfo.value.code is ErrorCode.POLICY_BPS_NOT_10000


def test_duplicate_bucket_names_raise() -> None:
    """Duplicates collapse in the returned dict, silently losing a share."""
    with pytest.raises(AppError) as excinfo:
        split_bps(100, (("a", 5000), ("a", 5000)))

    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED


def test_error_is_raised_before_any_arithmetic() -> None:
    """Preconditions are checked first, so a bad policy fails the same way for any total."""
    for total in (0, 1, -1, sys.maxsize):
        with pytest.raises(AppError):
            split_bps(total, (("a", 9999),))


# --------------------------------------------------------------------------------
# allocate_period.


def test_fixed_comes_off_the_top() -> None:
    """Income 300_000, fixed 100_001, 50/50 -> the policy divides 199_999."""
    result = allocate_period(300_000, 100_001, Policy(savings_bps=5000, discretionary_bps=5000))

    assert result == {"savings": 100_000, "discretionary": 99_999}
    assert 100_001 + result["savings"] + result["discretionary"] == 300_000


def test_negative_remainder_is_not_clamped() -> None:
    """PLAN.md §6.1: fixed exceeding income sends BOTH buckets negative.

    Nothing is clamped and there is no separate shortfall field — the single
    invariant of §5.3 still holds, which is the entire point of signed arithmetic
    here.
    """
    result = allocate_period(100_000, 250_001, Policy(savings_bps=5000, discretionary_bps=5000))

    assert result == {"savings": -75_001, "discretionary": -75_000}
    assert result["savings"] < 0
    assert result["discretionary"] < 0
    assert 250_001 + result["savings"] + result["discretionary"] == 100_000


def test_remainder_of_exactly_zero() -> None:
    result = allocate_period(250_000, 250_000, Policy(savings_bps=7000, discretionary_bps=3000))

    assert result == {"savings": 0, "discretionary": 0}


def test_uneven_policy_split() -> None:
    """70/30 over 100_001: 70_000 rem 7000, 30_000 rem 3000 -> savings takes the unit."""
    result = allocate_period(100_001, 0, Policy(savings_bps=7000, discretionary_bps=3000))

    assert result == {"savings": 70_001, "discretionary": 30_000}


def test_all_to_discretionary() -> None:
    result = allocate_period(100_001, 1, Policy(savings_bps=0, discretionary_bps=10_000))

    assert result == {"savings": 0, "discretionary": 100_000}


def test_allocate_period_returns_exactly_two_keys() -> None:
    result = allocate_period(1, 0, Policy(savings_bps=5000, discretionary_bps=5000))

    assert set(result) == {"savings", "discretionary"}


def test_allocate_period_negative_remainder_of_one_unit() -> None:
    """Remainder -1 lands entirely on savings, mirroring the +1 case."""
    result = allocate_period(0, 1, Policy(savings_bps=5000, discretionary_bps=5000))

    assert result == {"savings": -1, "discretionary": 0}


def test_policy_bps_not_10000_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        allocate_period(100, 0, Policy(savings_bps=5000, discretionary_bps=4000))

    assert excinfo.value.code is ErrorCode.POLICY_BPS_NOT_10000


# --------------------------------------------------------------------------------
# Hypothesis strategies — local to this module. See the module docstring.


def _to_buckets(cuts: list[Bps]) -> Buckets:
    """Turn cut points in [0, 10_000] into contiguous bps slices summing to 10_000.

    Cutting an interval guarantees the sum by construction, so every generated bucket
    set satisfies the precondition and no test is wasted on rejected input.
    """
    bounds = [0, *sorted(cuts), FULL_BPS]
    return tuple(
        (f"b{index}", bounds[index + 1] - bounds[index])
        for index in range(len(bounds) - 1)
    )


AWKWARD_BUCKETS: Final[tuple[Buckets, ...]] = (
    (("savings", 5000), ("discretionary", 5000)),
    (("a", 3333), ("b", 3333), ("c", 3334)),
    (("a", 2500), ("b", 2501), ("c", 2499), ("d", 2500)),
    (("a", 1), ("b", 9999)),
    (("a", 0), ("b", 10_000)),
    (("only", 10_000),),
    (("a", 1667), ("b", 1667), ("c", 1666), ("d", 5000)),
)


def bps_splits() -> st.SearchStrategy[Buckets]:
    """Bucket sets summing to exactly 10_000, biased toward the awkward ones."""
    generated = st.lists(
        st.integers(min_value=0, max_value=FULL_BPS), min_size=1, max_size=4
    ).map(_to_buckets)
    return st.one_of(st.sampled_from(AWKWARD_BUCKETS), generated)


def minor_amounts() -> st.SearchStrategy[Minor]:
    """Signed minor amounts spanning zero, one unit, and large magnitudes."""
    return st.one_of(
        st.sampled_from([0, 1, -1, 2, -2, 99, 100_001, -100_001, sys.maxsize, -sys.maxsize]),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        st.integers(min_value=-(2**62), max_value=2**62),
    )


# --------------------------------------------------------------------------------
# Hypothesis properties.


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_split_sums_exactly(total: Minor, buckets: Buckets) -> None:
    """The shares reconstitute the total, exactly, for every input.

    This is exact by construction — `leftover` measures the shortfall the flooring
    created and the distribution places all of it — so there is no tolerance and none
    would be legitimate (CLAUDE.md §4.6).
    """
    assert sum(split_bps(total, buckets).values()) == total


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_sign_symmetry(total: Minor, buckets: Buckets) -> None:
    """`split_bps(-t, b) == {k: -v for k, v in split_bps(t, b).items()}`.

    This is what working on the magnitude buys. Flooring a negative numerator would
    bias whichever bucket came first and break it.
    """
    positive = split_bps(total, buckets)
    negative = split_bps(-total, buckets)

    assert negative == {name: -share for name, share in positive.items()}


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_keys_are_the_declared_buckets(total: Minor, buckets: Buckets) -> None:
    result = split_bps(total, buckets)

    assert set(result) == {name for name, _ in buckets}
    assert len(result) == len(buckets)


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_every_share_is_int(total: Minor, buckets: Buckets) -> None:
    for share in split_bps(total, buckets).values():
        assert type(share) is int


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_share_is_floor_or_floor_plus_one(total: Minor, buckets: Buckets) -> None:
    """No bucket is ever off its exact proportional floor by more than one unit.

    The exact-sum property alone would still be satisfied by dumping the whole total
    into one bucket; this is what pins each share to its own bps.
    """
    magnitude = abs(total)
    result = split_bps(magnitude, buckets)

    for name, bps in buckets:
        floor_share = magnitude * bps // FULL_BPS
        assert result[name] == floor_share or result[name] == floor_share + 1


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_split_is_deterministic(total: Minor, buckets: Buckets) -> None:
    """Same inputs, same output. No hidden state, no ordering nondeterminism."""
    assert split_bps(total, buckets) == split_bps(total, buckets)


@given(buckets=bps_splits())
def test_property_zero_splits_to_zeros(buckets: Buckets) -> None:
    result = split_bps(0, buckets)

    assert set(result.values()) == {0}


@given(total=minor_amounts(), buckets=bps_splits())
def test_property_leftover_never_exceeds_bucket_count(
    total: Minor, buckets: Buckets
) -> None:
    """At most one extra unit is handed out per bucket, so the promotion set fits.

    Stated as: the number of buckets sitting above their floor is strictly less than
    the number of buckets whenever the total does not divide evenly.
    """
    magnitude = abs(total)
    result = split_bps(magnitude, buckets)
    promoted = sum(
        1 for name, bps in buckets if result[name] != magnitude * bps // FULL_BPS
    )

    assert promoted < len(buckets)


@given(
    income=minor_amounts(),
    fixed=minor_amounts(),
    savings_bps=st.integers(min_value=0, max_value=FULL_BPS),
)
def test_property_allocation_invariant(
    income: Minor, fixed: Minor, savings_bps: Bps
) -> None:
    """PLAN.md §5.3, the top-level invariant:

        fixed_due + savings_allocated + discretionary_allocated == allocatable_income

    Holds for every period including when the remainder is negative, which is why
    `fixed` is drawn from the same unconstrained strategy as `income`.
    """
    policy = Policy(savings_bps=savings_bps, discretionary_bps=FULL_BPS - savings_bps)
    result = allocate_period(income, fixed, policy)

    assert fixed + result["savings"] + result["discretionary"] == income


@given(
    income=minor_amounts(),
    fixed=minor_amounts(),
    savings_bps=st.integers(min_value=0, max_value=FULL_BPS),
)
def test_property_allocation_is_never_clamped(
    income: Minor, fixed: Minor, savings_bps: Bps
) -> None:
    """A negative remainder produces non-positive shares on both sides (PLAN.md §6.1)."""
    assume(income - fixed < 0)
    policy = Policy(savings_bps=savings_bps, discretionary_bps=FULL_BPS - savings_bps)
    result = allocate_period(income, fixed, policy)

    assert result["savings"] <= 0
    assert result["discretionary"] <= 0


@given(
    income=minor_amounts(),
    fixed=minor_amounts(),
    savings_bps=st.integers(min_value=0, max_value=FULL_BPS),
)
def test_property_allocation_sign_symmetry(
    income: Minor, fixed: Minor, savings_bps: Bps
) -> None:
    """Negating the remainder negates both shares — the shortfall mirrors the surplus."""
    policy = Policy(savings_bps=savings_bps, discretionary_bps=FULL_BPS - savings_bps)
    surplus = allocate_period(income, fixed, policy)
    shortfall = allocate_period(fixed, income, policy)

    assert shortfall == {name: -share for name, share in surplus.items()}


@given(half=st.integers(min_value=-(2**62), max_value=2**62))
def test_property_savings_wins_ties(half: Minor) -> None:
    """When both remainders tie, savings takes the unit — never discretionary.

    An odd remainder under a 50/50 policy is the tie case and the only one: both
    buckets floor to the same value with the same fractional remainder, so declared
    order decides it. Savings is declared first in `allocate_period` for exactly this
    reason (PLAN.md §5.1 step 4). The remainder is built as `2 * half + 1` so every
    generated example is odd rather than filtered down to the odd ones.
    """
    remainder: Minor = 2 * half + 1
    policy = Policy(savings_bps=5000, discretionary_bps=5000)
    result = allocate_period(remainder, 0, policy)

    assert abs(result["savings"]) == abs(result["discretionary"]) + 1
    assert result["savings"] + result["discretionary"] == remainder
