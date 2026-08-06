"""Integer money primitives. Zero domain knowledge, zero dependencies on `domain/`.

Owned by `module/core-money` (PLAN.md §13.2). Functions only — the types this module
signs against live in `core/types.py` so that no Phase-1 agent owns a type another
agent depends on (CONTRACTS.md §1).
"""

from __future__ import annotations

from collections.abc import Sequence

from core.types import AllocationPolicyLike, AppError, Bps, ErrorCode, Minor

# One whole. `bps` is hundredths of a percent, so 10_000 bps == 100% (CONTRACTS.md §1).
FULL_BPS: Bps = 10_000

# The two bucket names `allocate_period` returns. Savings is declared FIRST, which is
# what makes savings win a rounding tie (PLAN.md §5.1 step 4, §5.2).
_SAVINGS = "savings"
_DISCRETIONARY = "discretionary"


def _validate_buckets(buckets: Sequence[tuple[str, Bps]]) -> None:
    """Check `split_bps`'s preconditions before any arithmetic runs.

    The bps preconditions — the sum is exactly 10_000, and no bucket is negative —
    both raise `POLICY_BPS_NOT_10000` (CONTRACTS.md §8.1). Neither is cosmetic: a
    negative bps makes that bucket's floor share negative, which pushes `leftover`
    above the bucket count, and the one-unit-at-a-time distribution then cannot place
    every leftover unit. The exact-sum postcondition would fail silently rather than
    raise. An empty `buckets` sums to 0 and is caught by the same check.

    Duplicate names collapse in the returned dict, losing a bucket's share and
    breaking the exact-sum postcondition just as quietly. CONTRACTS.md §7.1 assigns no
    dedicated code to that case, so it raises the general `VALIDATION_FAILED` — input
    that could never be valid, which is exactly the §7 definition of an error.
    """
    total_bps = sum(bps for _, bps in buckets)
    if total_bps != FULL_BPS:
        raise AppError(
            ErrorCode.POLICY_BPS_NOT_10000,
            f"bucket bps must sum to {FULL_BPS}, got {total_bps}",
            {"total_bps": total_bps, "buckets": [name for name, _ in buckets]},
        )

    negative = [name for name, bps in buckets if bps < 0]
    if negative:
        raise AppError(
            ErrorCode.POLICY_BPS_NOT_10000,
            "every bucket bps must be non-negative",
            {"negative_buckets": negative},
        )

    names = [name for name, _ in buckets]
    if len(set(names)) != len(names):
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "bucket names must be unique",
            {"buckets": names},
        )


def split_bps(
    total_minor: Minor,
    buckets: Sequence[tuple[str, Bps]],
) -> dict[str, Minor]:
    """Split `total_minor` across `buckets` by basis points, exactly.

    Algorithm (PLAN.md §5.1): work on abs(total); floor-divide each bucket;
    distribute the leftover one minor unit at a time in descending fractional
    remainder, ties broken by position in `buckets`; reapply sign(total).

    Preconditions:
        sum(bps for _, bps in buckets) == 10_000
        every bps >= 0
        bucket names are unique
        buckets is non-empty and ORDERED — order is significant for tie-breaking

    Postconditions:
        sum(result.values()) == total_minor            EXACTLY, always
        result.keys() == {name for name, _ in buckets}
        split_bps(-t, b) == {k: -v for k, v in split_bps(t, b).items()}
        every value is int; no float is produced at any point

    Raises:
        AppError(POLICY_BPS_NOT_10000) if the bps precondition fails.
    """
    _validate_buckets(buckets)

    # Step 1. Work on the magnitude. Rounding is then symmetric about zero, so the
    # sign-symmetry postcondition holds by construction rather than by inspection —
    # floor division on a negative numerator would bias whichever bucket came first.
    magnitude = abs(total_minor)

    # Steps 2 and 3. Multiply before dividing (CLAUDE.md §2.1): `magnitude * bps` is
    # formed at full precision and only then floored, so the discarded fraction is
    # still recoverable as `% FULL_BPS`. Dividing first would throw it away.
    scaled = tuple(magnitude * bps for _, bps in buckets)
    floors = tuple(value // FULL_BPS for value in scaled)
    remainders = tuple(value % FULL_BPS for value in scaled)

    # `leftover` is what the flooring shed, measured rather than assumed. Because the
    # bps sum to exactly FULL_BPS it is strictly less than the number of buckets, so
    # step 4 never has to give one bucket two units.
    leftover = magnitude - sum(floors)

    # Step 4. Descending fractional remainder; ties fall back to declared position,
    # which is the whole reason `buckets` is a Sequence and not a mapping.
    ranked = sorted(range(len(buckets)), key=lambda i: (-remainders[i], i))
    promoted = frozenset(ranked[:leftover])

    # Step 5. Reapply the sign. Built in one pass into a new dict — nothing is
    # accumulated into or mutated (CLAUDE.md §4.2).
    sign = -1 if total_minor < 0 else 1
    return {
        name: sign * (floor + (1 if index in promoted else 0))
        for index, ((name, _), floor) in enumerate(zip(buckets, floors, strict=True))
    }


def allocate_period(
    allocatable_income_minor: Minor,
    fixed_due_minor: Minor,
    policy: AllocationPolicyLike,
) -> dict[str, Minor]:
    """Apply the allocation rule: fixed off the top, remainder split by policy.

    Preconditions:
        policy.savings_bps + policy.discretionary_bps == 10_000

    Postconditions:
        returns keys {"savings", "discretionary"}
        fixed_due_minor + result["savings"] + result["discretionary"]
            == allocatable_income_minor        EXACTLY

        Holds when the remainder is negative (income below fixed costs), in which
        case both shares are negative. Nothing is clamped (PLAN.md §6.1).

    `policy` is annotated against the structural `AllocationPolicyLike` rather than
    `domain.definitions.AllocationPolicy`, which it accepts unchanged; `core/` may not
    import `domain/` (CLAUDE.md §3.1). Savings is declared first, so savings wins
    rounding ties (PLAN.md §5.1 step 4).
    """
    # Fixed comes off the top; whatever is left — positive, zero, or negative — is
    # what the policy divides. The top-level invariant (PLAN.md §5.3) then reduces to
    # `split_bps`'s own exact-sum postcondition, so there is nothing extra to check.
    remainder_minor = allocatable_income_minor - fixed_due_minor

    return split_bps(
        remainder_minor,
        (
            (_SAVINGS, policy.savings_bps),
            (_DISCRETIONARY, policy.discretionary_bps),
        ),
    )
