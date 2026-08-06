"""Integer money primitives. Zero domain knowledge, zero dependencies on `domain/`.

Owned by `module/core-money` (PLAN.md §13.2). Functions only — the types this module
signs against live in `core/types.py` so that no Phase-1 agent owns a type another
agent depends on (CONTRACTS.md §1).
"""

from __future__ import annotations

from collections.abc import Sequence

from core.types import AllocationPolicyLike, Bps, Minor


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
    raise NotImplementedError


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
    raise NotImplementedError
