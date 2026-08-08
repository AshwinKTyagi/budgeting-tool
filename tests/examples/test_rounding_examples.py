"""PLAN.md §5.2, transcribed literally. Owned by `module/properties` (PLAN.md §13.2).

CLAUDE.md §5.3: these are regression anchors on the *documented* behavior. **If a
property test and a worked example disagree, the documentation is the specification and
the code is wrong.**

Two conventions make that promise real, and both are the reason this file looks
repetitive:

* **Every number is a literal copied from the document.** Not one assertion recomputes
  the formula it is checking. `assert split_bps(...) == {"savings": total * 5000 // 10000
  + 1, ...}` would pass against any implementation that agreed with itself about the
  rounding rule, including a wrong one. `50_001` cannot.
* **Every intermediate is asserted, not just the answer.** §5.2 shows its working —
  the floor, the remainder, the leftover, the tie-break — so the transcription shows the
  same working. An implementation that reached `50001/50000` by some other route (say,
  rounding half up, which agrees here and diverges at three buckets) would satisfy only
  the final line.

No tolerance anywhere: integer arithmetic is exact and a test needing slack would be
testing broken code (CLAUDE.md §4.6). No clock read: every date is a literal (§4.4).
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from core.money import allocate_period, split_bps
from core.types import AccountKind, Bps, Minor
from domain.definitions import Account, AllocationPolicy
from domain.projection import project

from tests.properties.strategies import (
    CHECKING,
    SAVINGS,
    account,
    allocation_policy,
    definitions_bundle,
    fixed_cost,
    income,
)

# ---------------------------------------------------------------- the example, verbatim
#
#   `allocatable_income = 100_001` ($1,000.01), policy 50/50:
#
#       abs(total) = 100001
#
#       savings        = 100001 * 5000 // 10000 = 50000    remainder ... = 5000
#       discretionary  = 100001 * 5000 // 10000 = 50000    remainder     = 5000
#
#       leftover = 100001 - (50000 + 50000) = 1
#       remainders tie at 5000 -> declared order -> savings takes the unit
#
#       savings       = 50001
#       discretionary = 50000
#                       -------
#                       100001   == allocatable_income   ✓

#: The dollar amount that does not halve. $1,000.01.
ALLOCATABLE_INCOME_MINOR: Final[Minor] = 100_001

#: The 50/50 policy, as the buckets `split_bps` receives them. Savings is declared
#: FIRST, and that ordering is the whole mechanism of the tie-break (PLAN.md §5.1 step 4).
FIFTY_FIFTY: Final[tuple[tuple[str, Bps], ...]] = (
    ("savings", 5_000),
    ("discretionary", 5_000),
)

#: Every figure §5.2 prints, as a literal.
_SCALED: Final = 500_005_000  # 100001 * 5000
_FLOOR_SHARE: Final[Minor] = 50_000
_REMAINDER: Final = 5_000
_LEFTOVER: Final[Minor] = 1
_SAVINGS_SHARE: Final[Minor] = 50_001
_DISCRETIONARY_SHARE: Final[Minor] = 50_000

_APRIL_30: Final = dt.date(2026, 4, 30)


def _policy(savings_bps: Bps = 5_000) -> AllocationPolicy:
    return allocation_policy(savings_bps)


def _two_asset_accounts() -> tuple[Account, ...]:
    """One checking and one savings account — the pair CONTRACTS.md §4 seeds."""
    return (
        account(CHECKING, AccountKind.CHECKING),
        account(SAVINGS, AccountKind.SAVINGS, version=2),
    )


# ------------------------------------------------------------------- the working, step by step


def test_the_scaled_product_and_its_remainder_are_what_plan_5_2_prints() -> None:
    """§5.2's first two lines: the floor share and the fractional remainder.

    `100001 * 5000` is formed at full precision and only then floored, which is what
    leaves the discarded fraction recoverable as `% 10_000` — multiply before dividing
    (CLAUDE.md §2.1). Scaling the rate down first would produce `100001 * 0` and throw
    the entire share away.
    """
    assert ALLOCATABLE_INCOME_MINOR * 5_000 == _SCALED
    assert _SCALED // 10_000 == _FLOOR_SHARE
    assert _SCALED % 10_000 == _REMAINDER


def test_the_leftover_is_exactly_one_minor_unit() -> None:
    """§5.2's third line: `leftover = 100001 - (50000 + 50000) = 1`.

    The leftover is *measured*, not predicted. That is why the exact-sum postcondition
    cannot fail: step 3 asks how much the flooring shed and step 4 hands out all of it.
    """
    assert ALLOCATABLE_INCOME_MINOR - (_FLOOR_SHARE + _FLOOR_SHARE) == _LEFTOVER


def test_split_bps_resolves_100_001_to_50001_and_50000() -> None:
    """§5.2's conclusion. The remainders tie at 5000, so declared order decides and
    savings — declared first — takes the single leftover unit.

    This is the anchor the whole rounding policy rests on. A change that made
    discretionary take the tie would still sum to `100_001` and would still satisfy
    property 1; it would fail here.
    """
    shares = split_bps(ALLOCATABLE_INCOME_MINOR, FIFTY_FIFTY)

    assert shares == {"savings": _SAVINGS_SHARE, "discretionary": _DISCRETIONARY_SHARE}
    assert shares["savings"] + shares["discretionary"] == ALLOCATABLE_INCOME_MINOR


def test_the_negative_case_is_the_same_magnitudes_with_the_sign_reapplied() -> None:
    """§5.2's closing paragraph: "Negative case, `allocatable_income = -100_001`: shares
    are `-50001` and `-50000`. Same magnitudes, sign reapplied, still exact."

    Savings still takes the tie. Working on `abs(total)` is what guarantees that —
    floor division on `-100001` would round toward negative infinity and hand the
    extra unit to the other bucket, which is the asymmetry PLAN.md §5.1 strips the sign
    to avoid.
    """
    shares = split_bps(-ALLOCATABLE_INCOME_MINOR, FIFTY_FIFTY)

    assert shares == {"savings": -50_001, "discretionary": -50_000}
    assert shares["savings"] + shares["discretionary"] == -ALLOCATABLE_INCOME_MINOR


def test_allocate_period_reaches_the_same_two_numbers() -> None:
    """The same example one layer up, with nothing taken off the top.

    `allocate_period` is what the projection actually calls, so the anchor has to hold
    there too — `split_bps` alone being right would not help if the caller passed the
    buckets in the other order and quietly moved the tie.
    """
    shares = allocate_period(ALLOCATABLE_INCOME_MINOR, 0, _policy())

    assert shares == {"savings": _SAVINGS_SHARE, "discretionary": _DISCRETIONARY_SHARE}


# ------------------------------------------------------------- the same example in State


def test_a_period_earning_100_001_allocates_50001_to_savings() -> None:
    """§5.2 as the user meets it: one paycheck of $1,000.01, a 50/50 policy, no fixed
    costs.

    Transcribing the example at this level as well is what makes it an anchor on the
    *system* rather than on one function. Every layer between the event and the reported
    figure — period resolution, policy resolution at the period start, `allocate_period`,
    the `PeriodSummary` — has to agree on `50001/50000`, and the top-level invariant of
    PLAN.md §5.3 has to close over it.
    """
    definitions = definitions_bundle(
        accounts=_two_asset_accounts(),
        policies=(_policy(),),
    )
    events = (income(1, dt.date(2026, 4, 10), ALLOCATABLE_INCOME_MINOR),)

    state = project(events, definitions, _APRIL_30)
    period = state.periods[-1]

    assert period.period_id == "2026-04"
    assert period.allocatable_income_minor == ALLOCATABLE_INCOME_MINOR
    assert period.fixed_due_minor == 0
    assert period.savings_allocated_minor == _SAVINGS_SHARE
    assert period.discretionary_allocated_minor == _DISCRETIONARY_SHARE
    assert (
        period.fixed_due_minor
        + period.savings_allocated_minor
        + period.discretionary_allocated_minor
        == period.allocatable_income_minor
    )


def test_a_shortfall_of_100_001_drives_both_buckets_to_the_mirrored_shares() -> None:
    """§5.2's negative case as the user meets it: a $1,000.01 bill and no income.

    Nothing is clamped, so both buckets go negative and by the mirrored magnitudes —
    `-50001` and `-50000` (PLAN.md §6.1). This is the branch a clamp-to-zero design
    would have hidden behind a separate `shortfall` field, and the reason the codebase
    has one invariant instead of two.

    The ledger is deliberately empty: with no events, genesis is `as_of_date`, so exactly
    one period is reported and the `FixedCost` expands into it alone.
    """
    definitions = definitions_bundle(
        accounts=_two_asset_accounts(),
        policies=(_policy(),),
        fixed_costs=(fixed_cost(amount_minor=ALLOCATABLE_INCOME_MINOR, due_day=15),),
    )

    state = project((), definitions, _APRIL_30)
    period = state.periods[-1]

    assert period.period_id == "2026-04"
    assert period.allocatable_income_minor == 0
    assert period.fixed_due_minor == ALLOCATABLE_INCOME_MINOR
    assert period.savings_allocated_minor == -50_001
    assert period.discretionary_allocated_minor == -50_000
    assert (
        period.fixed_due_minor
        + period.savings_allocated_minor
        + period.discretionary_allocated_minor
        == period.allocatable_income_minor
    )
