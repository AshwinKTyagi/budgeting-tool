"""Unit tests for `domain/definitions.py` (CONTRACTS.md §4, §8.5).

Owned by `module/domain-definitions` (PLAN.md §13.2).

Two conventions worth stating once:

* No tolerance anywhere. Every assertion is exact equality (CLAUDE.md §4.6).
* No clock read. Every date and instant is an explicit literal (CLAUDE.md §4.4), so
  these tests answer the same way in 2026 as in 2036.

`core/periods.py` belongs to a different Phase-1 branch and is still a stub, so the two
things this module borrows from it are supplied locally: `_MonthResolver` implements the
`PeriodResolver` protocol, and `clamp_day_to_month` is patched with a reference
implementation. Both are written against the documented postconditions in CONTRACTS.md
§8.2, not against an implementation, which is the point of testing against a contract.

The Hypothesis strategies here are deliberately module-local. `tests/properties/` and its
shared `strategies.py` belong to `module/properties` in Phase 4 (PLAN.md §13.3).
"""

from __future__ import annotations

import calendar
import datetime as dt
import random
from collections.abc import Iterator, Sequence
from typing import Final
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import domain.definitions as definitions_module
from core.periods import PeriodResolver
from core.types import (
    AccountKind,
    AllocationPolicyLike,
    AppError,
    BudgetTiming,
    Cadence,
    ErrorCode,
    ObligationSource,
    PeriodId,
)
from domain.definitions import (
    expand_recurring_incomes,
    Account,
    AllocationPolicy,
    DefinitionBase,
    Definitions,
    ExpectedObligation,
    FixedCost,
    RecurringIncome,
    expand_fixed_costs,
    resolve_version,
    supersede_expected,
    validate_no_overlap,
)
from domain.events import ObligationRaised

# --------------------------------------------------------------------------- helpers

RECORDED_AT: Final = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def _uuid(n: int) -> UUID:
    return UUID(int=n)


def _clamp_day_to_month(year: int, month: int, day: int) -> dt.date:
    """Reference implementation of `core.periods.clamp_day_to_month`.

    Contract (CONTRACTS.md §8.2): clamp `day` to the last valid day of the month, so
    `(2026, 2, 31) -> 2026-02-28`.
    """
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day, last_day))


class _MonthResolver:
    """Local stand-in for `CalendarMonthResolver` (CONTRACTS.md §8.2).

    Half-open calendar months, `PeriodId` is "YYYY-MM".
    """

    def period_for(self, d: dt.date) -> PeriodId:
        return f"{d.year:04d}-{d.month:02d}"

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        year, month = (int(part) for part in period_id.split("-"))
        start = dt.date(year, month, 1)
        end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
        return (start, end)

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        return tuple(self.period_for(d) for d in _month_starts(start, end))


def _month_starts(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    cursor = dt.date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        cursor = (
            dt.date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else dt.date(cursor.year, cursor.month + 1, 1)
        )


RESOLVER: Final[PeriodResolver] = _MonthResolver()  # static conformance assertion


@pytest.fixture(autouse=True)
def _patch_clamp_day_to_month(monkeypatch: pytest.MonkeyPatch) -> None:
    """`core.periods.clamp_day_to_month` is another branch's stub in Phase 1."""
    monkeypatch.setattr(
        definitions_module, "clamp_day_to_month", _clamp_day_to_month
    )


def _policy(
    *,
    version_id: int = 1,
    entity_id: str = "policy",
    effective_from: dt.date = dt.date(2026, 1, 1),
    effective_to: dt.date | None = None,
    savings_bps: int = 5_000,
    discretionary_bps: int = 5_000,
    recorded_at: dt.datetime = RECORDED_AT,
) -> AllocationPolicy:
    return AllocationPolicy(
        version_id=_uuid(version_id),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=recorded_at,
        savings_bps=savings_bps,
        discretionary_bps=discretionary_bps,
    )


def _fixed_cost(
    *,
    version_id: int = 1,
    entity_id: str = "rent",
    effective_from: dt.date = dt.date(2026, 1, 1),
    effective_to: dt.date | None = None,
    amount_minor: int = 150_000,
    due_day: int = 1,
    name: str = "Rent",
    payee: str = "Landlord",
    category: str = "housing",
    cadence: Cadence = Cadence.MONTHLY,
    recorded_at: dt.datetime = RECORDED_AT,
) -> FixedCost:
    return FixedCost(
        version_id=_uuid(version_id),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=recorded_at,
        name=name,
        amount_minor=amount_minor,
        cadence=cadence,
        due_day=due_day,
        payee=payee,
        category=category,
    )


def _recurring_income(
    *,
    version_id: int = 1,
    entity_id: str = "salary",
    effective_from: dt.date = dt.date(2026, 1, 1),
    effective_to: dt.date | None = None,
    amount_minor: int = 500_000,
    anchor_day: int = 15,
    cadence: Cadence = Cadence.MONTHLY,
) -> RecurringIncome:
    return RecurringIncome(
        version_id=_uuid(version_id),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name="Salary",
        amount_minor=amount_minor,
        cadence=cadence,
        anchor_day=anchor_day,
        account_id="checking",
    )


def _raised(
    *,
    event_id: int = 100,
    obligation_id: str = "ob-1",
    due_date: dt.date = dt.date(2026, 3, 1),
    amount_minor: int = 160_000,
    recurring_id: str | None = "rent",
    date: dt.date = dt.date(2026, 2, 20),
    recorded_at: dt.datetime = RECORDED_AT,
    payee: str = "Landlord",
    category: str = "housing",
) -> ObligationRaised:
    return ObligationRaised(
        event_id=_uuid(event_id),
        date=date,
        recorded_at=recorded_at,
        dedupe_key=f"manual:ObligationRaised:{obligation_id}",
        obligation_id=obligation_id,
        due_date=due_date,
        amount_minor=amount_minor,
        payee=payee,
        category=category,
        recurring_id=recurring_id,
    )


# ------------------------------------------------------- strict mode: money is int


def test_strict_mode_rejects_a_float_amount_minor() -> None:
    """The single most important test in this file (CLAUDE.md §2.3).

    Without strict mode `19.99` is coerced to `19`, and a float has silently reached the
    boundary of a system whose central invariant is an exact integer sum.
    """
    with pytest.raises(ValidationError):
        _fixed_cost(amount_minor=19.99)  # type: ignore[arg-type]


def test_strict_mode_rejects_a_float_that_happens_to_be_whole() -> None:
    """`1999.0` is exactly representable and still rejected. The type is the rule, not
    the value: a float reaching here means money was computed in floating point."""
    with pytest.raises(ValidationError):
        _fixed_cost(amount_minor=1999.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _recurring_income(amount_minor=1999.0)  # type: ignore[arg-type]


def test_strict_mode_rejects_a_float_bps() -> None:
    with pytest.raises(ValidationError):
        _policy(savings_bps=5000.0, discretionary_bps=5_000)  # type: ignore[arg-type]


def test_strict_mode_rejects_numeric_strings_and_bools_for_int_fields() -> None:
    with pytest.raises(ValidationError):
        _fixed_cost(amount_minor="150000")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _fixed_cost(due_day=True)


def test_strict_mode_rejects_a_datetime_where_a_business_date_is_declared() -> None:
    """Business dates are `dt.date` with no time component at all (CLAUDE.md §4.5)."""
    with pytest.raises(ValidationError):
        _fixed_cost(effective_from=dt.datetime(2026, 1, 1, 12))


def test_naive_recorded_at_is_rejected() -> None:
    """`UtcInstant`, not a bare `dt.datetime` — a naive instant has no correct zone to
    assume and guessing is how boundary bugs get in (CLAUDE.md §4.5)."""
    with pytest.raises(ValidationError):
        _fixed_cost(recorded_at=dt.datetime(2026, 1, 1, 12, 0))


def test_aware_non_utc_recorded_at_is_normalized_to_utc_exactly() -> None:
    pacific = dt.timezone(dt.timedelta(hours=-8))
    cost = _fixed_cost(recorded_at=dt.datetime(2026, 3, 31, 16, 30, tzinfo=pacific))
    assert cost.recorded_at.tzinfo == dt.timezone.utc
    assert cost.recorded_at == dt.datetime(
        2026, 4, 1, 0, 30, tzinfo=dt.timezone.utc
    )


# ------------------------------------------------------------ frozen / extra fields


def test_definitions_are_frozen() -> None:
    cost = _fixed_cost()
    with pytest.raises(ValidationError):
        cost.amount_minor = 1
    policy = _policy()
    with pytest.raises(ValidationError):
        policy.savings_bps = 6_000


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        AllocationPolicy(
            version_id=_uuid(1),
            entity_id="policy",
            effective_from=dt.date(2026, 1, 1),
            effective_to=None,
            recorded_at=RECORDED_AT,
            savings_bps=5_000,
            discretionary_bps=5_000,
            savings_pct=50,  # type: ignore[call-arg]
        )


def test_definitions_bundle_is_frozen_and_holds_all_versions() -> None:
    bundle = Definitions(
        recurring_incomes=(_recurring_income(),),
        fixed_costs=(
            _fixed_cost(version_id=1, effective_to=dt.date(2026, 6, 1)),
            _fixed_cost(version_id=2, effective_from=dt.date(2026, 6, 1)),
        ),
        allocation_policies=(_policy(),),
        accounts=(
            Account(
                version_id=_uuid(9),
                entity_id="checking",
                effective_from=dt.date(2026, 1, 1),
                effective_to=None,
                recorded_at=RECORDED_AT,
                name="Checking",
                kind=AccountKind.CHECKING,
                apr_bps=0,
                statement_close_day=None,
                payment_due_day=None,
                budget_timing=BudgetTiming.AT_PURCHASE,
            ),
        ),
    )
    assert len(bundle.fixed_costs) == 2
    with pytest.raises(ValidationError):
        bundle.fixed_costs = ()


# ------------------------------------------------------------- AllocationPolicy bps


def test_policy_rejects_bps_not_totalling_10000() -> None:
    with pytest.raises(AppError) as under:
        _policy(savings_bps=5_000, discretionary_bps=4_000)
    assert under.value.code == ErrorCode.POLICY_BPS_NOT_10000
    assert under.value.details["total_bps"] == 9_000

    with pytest.raises(AppError) as over:
        _policy(savings_bps=5_000, discretionary_bps=6_000)
    assert over.value.code == ErrorCode.POLICY_BPS_NOT_10000
    assert over.value.details["total_bps"] == 11_000


def test_policy_accepts_every_exact_partition() -> None:
    for savings_bps in (0, 1, 3_333, 5_000, 9_999, 10_000):
        policy = _policy(
            savings_bps=savings_bps, discretionary_bps=10_000 - savings_bps
        )
        assert policy.savings_bps + policy.discretionary_bps == 10_000


def test_policy_satisfies_the_allocation_policy_like_protocol() -> None:
    """`core/money.py::allocate_period` signs against `AllocationPolicyLike` because
    `core/` may not import `domain/` (CLAUDE.md §3.1). The compatibility therefore runs
    the other way, and this assignment is what checks it — statically under
    `mypy --strict`, not merely at runtime.
    """
    policy: AllocationPolicy = _policy(savings_bps=3_333, discretionary_bps=6_667)
    policy_like: AllocationPolicyLike = policy
    assert policy_like.savings_bps == 3_333
    assert policy_like.discretionary_bps == 6_667


# ---------------------------------------------------------------- effective ranges


def test_effective_to_equal_to_effective_from_is_rejected() -> None:
    """A zero-width range is effective for no date at all."""
    with pytest.raises(AppError) as exc:
        _fixed_cost(
            effective_from=dt.date(2026, 3, 1), effective_to=dt.date(2026, 3, 1)
        )
    assert exc.value.code == ErrorCode.EFFECTIVE_RANGE_INVALID


def test_inverted_effective_range_is_rejected() -> None:
    with pytest.raises(AppError) as exc:
        _policy(
            effective_from=dt.date(2026, 3, 1), effective_to=dt.date(2026, 2, 1)
        )
    assert exc.value.code == ErrorCode.EFFECTIVE_RANGE_INVALID
    assert exc.value.details["entity_id"] == "policy"


def test_open_ended_and_one_day_ranges_are_accepted() -> None:
    assert _fixed_cost(effective_to=None).effective_to is None
    single_day = _fixed_cost(
        effective_from=dt.date(2026, 3, 1), effective_to=dt.date(2026, 3, 2)
    )
    assert single_day.effective_to == dt.date(2026, 3, 2)


# --------------------------------------------------------------- resolve_version


def _three_version_timeline() -> tuple[FixedCost, FixedCost, FixedCost]:
    """v1 [01-01, 03-01), v2 [03-01, 06-01), v3 [09-01, open) — with a gap in Jun-Aug."""
    return (
        _fixed_cost(
            version_id=1,
            amount_minor=100_000,
            effective_from=dt.date(2026, 1, 1),
            effective_to=dt.date(2026, 3, 1),
        ),
        _fixed_cost(
            version_id=2,
            amount_minor=110_000,
            effective_from=dt.date(2026, 3, 1),
            effective_to=dt.date(2026, 6, 1),
        ),
        _fixed_cost(
            version_id=3,
            amount_minor=120_000,
            effective_from=dt.date(2026, 9, 1),
            effective_to=None,
        ),
    )


def test_resolve_version_picks_the_version_covering_the_date() -> None:
    versions = _three_version_timeline()
    resolved = resolve_version(versions, "rent", dt.date(2026, 4, 15))
    assert resolved is not None
    assert resolved.version_id == _uuid(2)
    assert resolved.amount_minor == 110_000


def test_resolve_version_effective_from_is_inclusive() -> None:
    versions = _three_version_timeline()
    resolved = resolve_version(versions, "rent", dt.date(2026, 3, 1))
    assert resolved is not None
    assert resolved.version_id == _uuid(2)


def test_resolve_version_effective_to_is_exclusive() -> None:
    """The day a version ends belongs to its successor, or to nobody. Half-open ranges
    are what make a handover leave no date in two versions and none in neither."""
    versions = _three_version_timeline()
    last_day_of_v1 = resolve_version(versions, "rent", dt.date(2026, 2, 28))
    assert last_day_of_v1 is not None
    assert last_day_of_v1.version_id == _uuid(1)

    end_day_of_v2 = resolve_version(versions, "rent", dt.date(2026, 6, 1))
    assert end_day_of_v2 is None


def test_resolve_version_handles_an_open_ended_current_version() -> None:
    versions = _three_version_timeline()
    for at in (dt.date(2026, 9, 1), dt.date(2031, 12, 31)):
        resolved = resolve_version(versions, "rent", at)
        assert resolved is not None
        assert resolved.version_id == _uuid(3)


def test_resolve_version_returns_none_before_any_version_exists() -> None:
    versions = _three_version_timeline()
    assert resolve_version(versions, "rent", dt.date(2025, 12, 31)) is None


def test_resolve_version_returns_none_inside_a_gap() -> None:
    versions = _three_version_timeline()
    for at in (dt.date(2026, 6, 1), dt.date(2026, 7, 15), dt.date(2026, 8, 31)):
        assert resolve_version(versions, "rent", at) is None


def test_resolve_version_ignores_other_entities_and_empty_input() -> None:
    versions = (
        *_three_version_timeline(),
        _fixed_cost(version_id=4, entity_id="internet", amount_minor=7_000),
    )
    other = resolve_version(versions, "internet", dt.date(2026, 7, 15))
    assert other is not None
    assert other.amount_minor == 7_000
    assert resolve_version(versions, "unknown", dt.date(2026, 4, 1)) is None
    assert resolve_version((), "rent", dt.date(2026, 4, 1)) is None


def test_resolve_version_is_independent_of_input_order() -> None:
    versions = list(_three_version_timeline())
    shuffled = list(reversed(versions))
    at = dt.date(2026, 4, 15)
    assert resolve_version(versions, "rent", at) == resolve_version(shuffled, "rent", at)


def test_resolve_version_preserves_the_concrete_type() -> None:
    """PEP 695 `resolve_version[T: DefinitionBase]` returns the element type, not the
    base — checked statically by the annotation and at runtime by the isinstance."""
    resolved: FixedCost | None = resolve_version(
        _three_version_timeline(), "rent", dt.date(2026, 1, 2)
    )
    assert isinstance(resolved, FixedCost)
    policy: AllocationPolicy | None = resolve_version(
        (_policy(),), "policy", dt.date(2026, 1, 2)
    )
    assert isinstance(policy, AllocationPolicy)


# ------------------------------------------------------------- validate_no_overlap


def test_validate_no_overlap_accepts_adjacent_versions_sharing_a_boundary() -> None:
    """Closing a version on the day the next begins is the documented way to supersede
    a definition (CLAUDE.md §4.3). It must not read as an overlap."""
    validate_no_overlap(
        [
            _fixed_cost(
                version_id=1,
                effective_from=dt.date(2026, 1, 1),
                effective_to=dt.date(2026, 3, 1),
            ),
            _fixed_cost(
                version_id=2,
                effective_from=dt.date(2026, 3, 1),
                effective_to=dt.date(2026, 6, 1),
            ),
            _fixed_cost(
                version_id=3, effective_from=dt.date(2026, 6, 1), effective_to=None
            ),
        ]
    )


def test_validate_no_overlap_accepts_empty_single_and_gapped_timelines() -> None:
    validate_no_overlap([])
    validate_no_overlap([_policy()])
    validate_no_overlap(list(_three_version_timeline()))


def test_validate_no_overlap_rejects_intersecting_ranges() -> None:
    with pytest.raises(AppError) as exc:
        validate_no_overlap(
            [
                _fixed_cost(
                    version_id=1,
                    effective_from=dt.date(2026, 1, 1),
                    effective_to=dt.date(2026, 4, 1),
                ),
                _fixed_cost(
                    version_id=2,
                    effective_from=dt.date(2026, 3, 1),
                    effective_to=dt.date(2026, 6, 1),
                ),
            ]
        )
    assert exc.value.code == ErrorCode.OVERLAPPING_VERSIONS
    assert exc.value.details["entity_id"] == "rent"
    assert exc.value.details["version_ids"] == [str(_uuid(1)), str(_uuid(2))]


def test_validate_no_overlap_rejects_an_unclosed_earlier_version() -> None:
    """The commonest real failure: appending a new version without closing the old one."""
    with pytest.raises(AppError) as exc:
        validate_no_overlap(
            [
                _policy(version_id=1, effective_from=dt.date(2026, 1, 1)),
                _policy(
                    version_id=2,
                    effective_from=dt.date(2026, 4, 1),
                    savings_bps=6_000,
                    discretionary_bps=4_000,
                ),
            ]
        )
    assert exc.value.code == ErrorCode.OVERLAPPING_VERSIONS


def test_validate_no_overlap_rejects_identical_duplicate_ranges() -> None:
    with pytest.raises(AppError):
        validate_no_overlap(
            [
                _fixed_cost(
                    version_id=1,
                    effective_from=dt.date(2026, 1, 1),
                    effective_to=dt.date(2026, 4, 1),
                ),
                _fixed_cost(
                    version_id=2,
                    effective_from=dt.date(2026, 1, 1),
                    effective_to=dt.date(2026, 4, 1),
                ),
            ]
        )


def test_validate_no_overlap_never_compares_across_entities() -> None:
    validate_no_overlap(
        [
            _fixed_cost(version_id=1, entity_id="rent"),
            _fixed_cost(version_id=2, entity_id="internet"),
            _fixed_cost(version_id=3, entity_id="gym"),
        ]
    )


def test_validate_no_overlap_is_independent_of_input_order() -> None:
    overlapping = [
        _fixed_cost(
            version_id=1,
            effective_from=dt.date(2026, 1, 1),
            effective_to=dt.date(2026, 4, 1),
        ),
        _fixed_cost(
            version_id=2,
            effective_from=dt.date(2026, 3, 1),
            effective_to=dt.date(2026, 6, 1),
        ),
    ]
    with pytest.raises(AppError):
        validate_no_overlap(list(reversed(overlapping)))


def test_validate_no_overlap_accepts_a_mixed_definition_sequence() -> None:
    """The signature is `Sequence[DefinitionBase]`, so a caller may hand it a whole
    bundle's worth of heterogeneous versions."""
    mixed: list[DefinitionBase] = [
        _policy(version_id=1, effective_to=dt.date(2026, 4, 1)),
        _policy(version_id=2, effective_from=dt.date(2026, 4, 1)),
        _fixed_cost(version_id=3),
        _recurring_income(version_id=4),
    ]
    validate_no_overlap(mixed)


# -------------------------------------------- policy change does not reach backwards


def test_a_later_policy_leaves_an_earlier_period_bit_identical() -> None:
    """PLAN.md §8.3 / CLAUDE.md §5.1 property 4, in its unit-test form.

    A policy effective mid-period applies from the *next* period, because resolution
    happens at the period start date. Adding the new version must leave the earlier
    period's resolved policy identical — same version, same bps, same object value.
    """
    period_start, _end = RESOLVER.bounds("2026-03")
    original = _policy(
        version_id=1,
        effective_from=dt.date(2026, 1, 1),
        savings_bps=5_000,
        discretionary_bps=5_000,
    )
    before = resolve_version((original,), "policy", period_start)

    closed = _policy(
        version_id=1,
        effective_from=dt.date(2026, 1, 1),
        effective_to=dt.date(2026, 3, 15),
        savings_bps=5_000,
        discretionary_bps=5_000,
    )
    amended = _policy(
        version_id=2,
        effective_from=dt.date(2026, 3, 15),
        savings_bps=8_000,
        discretionary_bps=2_000,
    )
    validate_no_overlap([closed, amended])
    after = resolve_version((closed, amended), "policy", period_start)

    assert before is not None
    assert after is not None
    assert after.version_id == _uuid(1)
    assert (after.savings_bps, after.discretionary_bps) == (
        before.savings_bps,
        before.discretionary_bps,
    )

    next_period_start, _next_end = RESOLVER.bounds("2026-04")
    next_policy = resolve_version((closed, amended), "policy", next_period_start)
    assert next_policy is not None
    assert next_policy.version_id == _uuid(2)
    assert next_policy.savings_bps == 8_000


# --------------------------------------------------------------- expand_fixed_costs


def test_expand_fixed_costs_materializes_one_row_per_effective_entity() -> None:
    costs = (
        _fixed_cost(version_id=1, entity_id="rent", amount_minor=150_000, due_day=1),
        _fixed_cost(
            version_id=2,
            entity_id="internet",
            amount_minor=7_000,
            due_day=12,
            name="Internet",
            payee="ISP",
            category="utilities",
        ),
    )
    rows = expand_fixed_costs(costs, "2026-03", RESOLVER)
    assert [row.obligation_id for row in rows] == [
        "expected:rent:2026-03",
        "expected:internet:2026-03",
    ]
    assert all(row.source == ObligationSource.EXPECTED for row in rows)
    assert all(row.period_id == "2026-03" for row in rows)
    assert [row.due_date for row in rows] == [
        dt.date(2026, 3, 1),
        dt.date(2026, 3, 12),
    ]
    assert [row.recurring_id for row in rows] == ["rent", "internet"]
    assert [row.amount_minor for row in rows] == [150_000, 7_000]
    assert [row.payee for row in rows] == ["Landlord", "ISP"]
    assert [row.category for row in rows] == ["housing", "utilities"]


def test_expand_fixed_costs_clamps_the_due_day_to_the_month() -> None:
    costs = (_fixed_cost(due_day=31),)
    february = expand_fixed_costs(costs, "2026-02", RESOLVER)
    assert [row.due_date for row in february] == [dt.date(2026, 2, 28)]
    march = expand_fixed_costs(costs, "2026-03", RESOLVER)
    assert [row.due_date for row in march] == [dt.date(2026, 3, 31)]


def test_expand_fixed_costs_resolves_the_version_at_the_period_start() -> None:
    """A raise effective mid-period governs the *next* period, exactly as a policy
    change does — the expansion is pinned by a date, not by the period's contents."""
    costs = (
        _fixed_cost(
            version_id=1,
            amount_minor=150_000,
            effective_from=dt.date(2026, 1, 1),
            effective_to=dt.date(2026, 3, 20),
        ),
        _fixed_cost(
            version_id=2,
            amount_minor=160_000,
            effective_from=dt.date(2026, 3, 20),
        ),
    )
    march = expand_fixed_costs(costs, "2026-03", RESOLVER)
    assert [row.amount_minor for row in march] == [150_000]
    april = expand_fixed_costs(costs, "2026-04", RESOLVER)
    assert [row.amount_minor for row in april] == [160_000]


def test_expand_fixed_costs_omits_entities_with_no_effective_version() -> None:
    costs = (
        _fixed_cost(version_id=1, entity_id="rent"),
        _fixed_cost(
            version_id=2, entity_id="future", effective_from=dt.date(2027, 1, 1)
        ),
        _fixed_cost(
            version_id=3,
            entity_id="cancelled",
            effective_from=dt.date(2025, 1, 1),
            effective_to=dt.date(2026, 2, 1),
        ),
    )
    rows = expand_fixed_costs(costs, "2026-03", RESOLVER)
    assert [row.recurring_id for row in rows] == ["rent"]
    assert expand_fixed_costs((), "2026-03", RESOLVER) == ()


def test_expand_fixed_costs_is_independent_of_input_order() -> None:
    costs = [
        _fixed_cost(version_id=1, entity_id="rent", due_day=1),
        _fixed_cost(version_id=2, entity_id="internet", due_day=12),
        _fixed_cost(version_id=3, entity_id="gym", due_day=5),
    ]
    shuffled = list(costs)
    random.Random(20260806).shuffle(shuffled)
    assert expand_fixed_costs(costs, "2026-03", RESOLVER) == expand_fixed_costs(
        shuffled, "2026-03", RESOLVER
    )


# -------------------------------------------------------------- supersede_expected


def _expected_rows(period_id: PeriodId = "2026-03") -> Sequence[ExpectedObligation]:
    return expand_fixed_costs(
        (
            _fixed_cost(version_id=1, entity_id="rent", amount_minor=150_000, due_day=1),
            _fixed_cost(
                version_id=2,
                entity_id="internet",
                amount_minor=7_000,
                due_day=12,
                name="Internet",
                payee="ISP",
                category="utilities",
            ),
        ),
        period_id,
        RESOLVER,
    )


def test_supersede_replaces_the_expected_row_and_never_sums_it() -> None:
    """Actual beats forecast (PLAN.md §8.1). Summing the two is the recognition-principle
    failure in its obligation-shaped form: the bill would reserve money twice."""
    rows = supersede_expected(
        _expected_rows(),
        (_raised(obligation_id="rent-mar", amount_minor=160_000),),
        RESOLVER,
    )
    rent = [row for row in rows if row.recurring_id == "rent"]
    assert len(rent) == 1
    assert rent[0].source == ObligationSource.RAISED
    assert rent[0].obligation_id == "rent-mar"
    assert rent[0].amount_minor == 160_000
    assert rent[0].period_id == "2026-03"
    assert sum(row.amount_minor for row in rows) == 160_000 + 7_000


def test_supersede_keeps_expected_rows_with_no_match() -> None:
    rows = supersede_expected(
        _expected_rows(),
        (_raised(obligation_id="rent-mar"),),
        RESOLVER,
    )
    internet = [row for row in rows if row.recurring_id == "internet"]
    assert len(internet) == 1
    assert internet[0].source == ObligationSource.EXPECTED
    assert internet[0].amount_minor == 7_000


def test_supersede_includes_an_unmatched_raised_obligation_as_raised() -> None:
    rows = supersede_expected(
        _expected_rows(),
        (
            _raised(
                event_id=101,
                obligation_id="dentist",
                recurring_id=None,
                due_date=dt.date(2026, 3, 20),
                amount_minor=25_000,
                payee="Dentist",
                category="health",
            ),
        ),
        RESOLVER,
    )
    assert [row.obligation_id for row in rows] == [
        "expected:rent:2026-03",
        "expected:internet:2026-03",
        "dentist",
    ]
    dentist = rows[2]
    assert dentist.source == ObligationSource.RAISED
    assert dentist.recurring_id is None
    assert dentist.period_id == "2026-03"
    assert dentist.amount_minor == 25_000


def test_supersede_matches_on_the_period_of_due_date_not_on_date() -> None:
    """`ObligationRaised.due_date` decides period membership, never `date`
    (CONTRACTS.md §3.2). A bill entered in February for an April due date supersedes the
    April expected row and leaves March untouched."""
    rows = supersede_expected(
        _expected_rows("2026-03"),
        (
            _raised(
                obligation_id="rent-apr",
                date=dt.date(2026, 2, 20),
                due_date=dt.date(2026, 4, 1),
                amount_minor=160_000,
            ),
        ),
        RESOLVER,
    )
    march_rent = [
        row for row in rows if row.obligation_id == "expected:rent:2026-03"
    ]
    assert len(march_rent) == 1
    assert march_rent[0].source == ObligationSource.EXPECTED
    april_rent = [row for row in rows if row.obligation_id == "rent-apr"]
    assert len(april_rent) == 1
    assert april_rent[0].period_id == "2026-04"


def test_supersede_produces_no_duplicate_recurring_period_pairs() -> None:
    """Two explicit obligations for one recurring key in one period: the later in ledger
    order wins, so the postcondition holds and the answer stays deterministic."""
    earlier = _raised(
        event_id=101,
        obligation_id="rent-mar-v1",
        amount_minor=155_000,
        date=dt.date(2026, 2, 20),
    )
    later = _raised(
        event_id=102,
        obligation_id="rent-mar-v2",
        amount_minor=160_000,
        date=dt.date(2026, 2, 25),
    )
    rows = supersede_expected(_expected_rows(), (earlier, later), RESOLVER)
    keys = [(row.recurring_id, row.period_id) for row in rows]
    assert len(keys) == len(set(keys))
    rent = [row for row in rows if row.recurring_id == "rent"]
    assert len(rent) == 1
    assert rent[0].obligation_id == "rent-mar-v2"
    assert rent[0].amount_minor == 160_000


def test_supersede_is_independent_of_arrival_order() -> None:
    events = [
        _raised(event_id=101, obligation_id="rent-mar", date=dt.date(2026, 2, 20)),
        _raised(
            event_id=102,
            obligation_id="dentist",
            recurring_id=None,
            due_date=dt.date(2026, 3, 20),
            amount_minor=25_000,
            date=dt.date(2026, 3, 2),
        ),
        _raised(
            event_id=103,
            obligation_id="internet-mar",
            recurring_id="internet",
            due_date=dt.date(2026, 3, 12),
            amount_minor=7_500,
            date=dt.date(2026, 3, 5),
        ),
    ]
    shuffled = list(events)
    random.Random(19700101).shuffle(shuffled)
    assert supersede_expected(
        _expected_rows(), events, RESOLVER
    ) == supersede_expected(_expected_rows(), shuffled, RESOLVER)


def test_supersede_with_no_events_returns_the_expected_rows_unchanged() -> None:
    expected = _expected_rows()
    assert supersede_expected(expected, (), RESOLVER) == tuple(expected)


# ----------------------------------------------------------------------- properties
# Module-local Hypothesis strategies. The shared `tests/properties/strategies.py` and the
# 15 named invariants belong to `module/properties` in Phase 4 (PLAN.md §13.3).

_BUSINESS_DATES = st.dates(
    min_value=dt.date(2020, 1, 1), max_value=dt.date(2035, 12, 31)
)


@st.composite
def _contiguous_timelines(draw: st.DrawFn) -> tuple[FixedCost, ...]:
    """A non-overlapping timeline for one entity: ascending, half-open, possibly gapped,
    with the last version open-ended."""
    boundaries = draw(
        st.lists(_BUSINESS_DATES, min_size=2, max_size=8, unique=True).map(sorted)
    )
    keep = draw(
        st.lists(st.booleans(), min_size=len(boundaries) - 1, max_size=len(boundaries) - 1)
    )
    versions = tuple(
        _fixed_cost(
            version_id=index + 1,
            amount_minor=(index + 1) * 1_000,
            effective_from=boundaries[index],
            effective_to=boundaries[index + 1],
        )
        for index, wanted in enumerate(keep)
        if wanted
    )
    return versions


@settings(max_examples=200)
@given(versions=_contiguous_timelines(), at=_BUSINESS_DATES)
def test_property_resolve_version_agrees_with_the_half_open_definition(
    versions: tuple[FixedCost, ...], at: dt.date
) -> None:
    covering = [
        v
        for v in versions
        if v.effective_from <= at
        and (v.effective_to is None or at < v.effective_to)
    ]
    resolved = resolve_version(versions, "rent", at)
    assert len(covering) <= 1
    assert resolved == (covering[0] if covering else None)


@settings(max_examples=200)
@given(versions=_contiguous_timelines(), seed=st.integers(min_value=0, max_value=2**32))
def test_property_generated_timelines_never_overlap_in_any_order(
    versions: tuple[FixedCost, ...], seed: int
) -> None:
    shuffled = list(versions)
    random.Random(seed).shuffle(shuffled)
    validate_no_overlap(shuffled)
    assert resolve_version(shuffled, "rent", dt.date(2020, 1, 1)) == resolve_version(
        list(versions), "rent", dt.date(2020, 1, 1)
    )


@settings(max_examples=200)
@given(savings_bps=st.integers(min_value=-20_000, max_value=30_000))
def test_property_policy_construction_is_exactly_the_10000_partition(
    savings_bps: int,
) -> None:
    policy = _policy(savings_bps=savings_bps, discretionary_bps=10_000 - savings_bps)
    assert policy.savings_bps + policy.discretionary_bps == 10_000
    with pytest.raises(AppError) as exc:
        _policy(savings_bps=savings_bps, discretionary_bps=10_001 - savings_bps)
    assert exc.value.code == ErrorCode.POLICY_BPS_NOT_10000


# --- expand_recurring_incomes
#
# The forecast view PLAN.md §8.2 promises. Expanding does NOT allocate — that stays
# true and `tests/unit/domain/test_projection.py` guards it. These tests are about the
# schedule: which dates a definition names, and which version names them.

_WINDOW_START = dt.date(2026, 1, 1)
_WINDOW_END = dt.date(2026, 3, 31)


def _dates(rows: object) -> list[dt.date]:
    return [row.date for row in rows]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        (Cadence.WEEKLY, 13),
        (Cadence.BIWEEKLY, 7),
        (Cadence.SEMIMONTHLY, 6),
        (Cadence.MONTHLY, 3),
        (Cadence.QUARTERLY, 1),
        (Cadence.ANNUAL, 1),
    ],
)
def test_expand_recurring_incomes_honours_every_cadence(
    cadence: Cadence, expected: int
) -> None:
    """`expand_fixed_costs` ignores cadence and emits one row per calendar month. This
    is the first expansion that does not, which is the whole point: a fortnightly
    paycheck is not a monthly one."""
    rows = expand_recurring_incomes(
        (_recurring_income(cadence=cadence, anchor_day=1),), _WINDOW_START, _WINDOW_END
    )

    assert len(rows) == expected


@pytest.mark.parametrize(
    "cadence", [Cadence.WEEKLY, Cadence.BIWEEKLY, Cadence.SEMIMONTHLY]
)
def test_sub_monthly_cadences_land_more_than_once_in_a_month(
    cadence: Cadence,
) -> None:
    """The case a period-shaped expansion cannot express at all, and the reason income
    expands by date rather than by period."""
    rows = expand_recurring_incomes(
        (_recurring_income(cadence=cadence, anchor_day=1),),
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )

    assert len(rows) > 1
    assert {row.date.month for row in rows} == {1}


def test_weekly_steps_from_effective_from_and_ignores_anchor_day() -> None:
    """`effective_from` is the first payday. There is no day-of-month that survives
    stepping seven days at a time, so `anchor_day` is meaningless here."""
    rows = expand_recurring_incomes(
        (
            _recurring_income(
                cadence=Cadence.WEEKLY,
                anchor_day=28,
                effective_from=dt.date(2026, 1, 5),
            ),
        ),
        _WINDOW_START,
        dt.date(2026, 2, 2),
    )

    assert _dates(rows) == [
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 12),
        dt.date(2026, 1, 19),
        dt.date(2026, 1, 26),
        dt.date(2026, 2, 2),
    ]


def test_monthly_clamps_the_anchor_day_to_the_month() -> None:
    """Same clamp `expand_fixed_costs` applies to `due_day` — paid on the 31st still
    lands in February."""
    rows = expand_recurring_incomes(
        (_recurring_income(anchor_day=31),), _WINDOW_START, _WINDOW_END
    )

    assert _dates(rows) == [
        dt.date(2026, 1, 31),
        dt.date(2026, 2, 28),
        dt.date(2026, 3, 31),
    ]


def test_semimonthly_pays_twice_a_month_fifteen_days_apart() -> None:
    rows = expand_recurring_incomes(
        (_recurring_income(cadence=Cadence.SEMIMONTHLY, anchor_day=5),),
        _WINDOW_START,
        dt.date(2026, 2, 28),
    )

    assert _dates(rows) == [
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 20),
        dt.date(2026, 2, 5),
        dt.date(2026, 2, 20),
    ]


def test_semimonthly_with_a_late_anchor_does_not_ask_for_day_35() -> None:
    """`clamp_day_to_month` RAISES above day 31, so a bare `anchor_day + 15` takes the
    whole expansion down for any anchor past the 16th. The second date is capped, not
    clamped after the fact."""
    rows = expand_recurring_incomes(
        (_recurring_income(cadence=Cadence.SEMIMONTHLY, anchor_day=20),),
        _WINDOW_START,
        dt.date(2026, 2, 28),
    )

    assert _dates(rows) == [
        dt.date(2026, 1, 20),
        dt.date(2026, 1, 31),
        dt.date(2026, 2, 20),
        dt.date(2026, 2, 28),
    ]


def test_semimonthly_collapsing_onto_one_date_yields_one_occurrence() -> None:
    """Anchored on the 30th, February asks for the 30th and the 31st and clamps both to
    the 28th. One date is one paycheck — a duplicate id would be offered twice."""
    rows = expand_recurring_incomes(
        (_recurring_income(cadence=Cadence.SEMIMONTHLY, anchor_day=30),),
        dt.date(2026, 2, 1),
        dt.date(2026, 2, 28),
    )

    assert _dates(rows) == [dt.date(2026, 2, 28)]
    assert len({row.income_id for row in rows}) == 1


def test_expand_recurring_incomes_stops_at_effective_to() -> None:
    """Exclusive end, like every other definition range."""
    rows = expand_recurring_incomes(
        (
            _recurring_income(
                anchor_day=1, effective_to=dt.date(2026, 3, 1)
            ),
        ),
        _WINDOW_START,
        _WINDOW_END,
    )

    assert _dates(rows) == [dt.date(2026, 1, 1), dt.date(2026, 2, 1)]


def test_a_later_version_does_not_rewrite_earlier_occurrences() -> None:
    """A cadence change is a new version, and each version is expanded only inside its
    own range. January keeps January's schedule and January's amount — which is what
    makes changing a pay period safe once paychecks have already landed."""
    rows = expand_recurring_incomes(
        (
            _recurring_income(
                version_id=1,
                anchor_day=1,
                effective_to=dt.date(2026, 2, 15),
                amount_minor=100_000,
            ),
            _recurring_income(
                version_id=2,
                cadence=Cadence.BIWEEKLY,
                effective_from=dt.date(2026, 2, 15),
                amount_minor=200_000,
            ),
        ),
        _WINDOW_START,
        dt.date(2026, 3, 15),
    )

    assert [(row.date, row.amount_minor) for row in rows] == [
        (dt.date(2026, 1, 1), 100_000),
        (dt.date(2026, 2, 1), 100_000),
        (dt.date(2026, 2, 15), 200_000),
        (dt.date(2026, 3, 1), 200_000),
        (dt.date(2026, 3, 15), 200_000),
    ]


def test_expand_recurring_incomes_ids_are_deterministic() -> None:
    rows = expand_recurring_incomes(
        (_recurring_income(anchor_day=1),), _WINDOW_START, dt.date(2026, 1, 31)
    )

    assert [row.income_id for row in rows] == ["expected:income:salary:2026-01-01"]


def test_expand_recurring_incomes_is_independent_of_input_order() -> None:
    versions = [
        _recurring_income(version_id=1, entity_id="salary", anchor_day=1),
        _recurring_income(version_id=2, entity_id="stipend", anchor_day=20),
    ]

    forward = expand_recurring_incomes(tuple(versions), _WINDOW_START, _WINDOW_END)
    backward = expand_recurring_incomes(
        tuple(reversed(versions)), _WINDOW_START, _WINDOW_END
    )

    assert forward == backward


def test_expand_recurring_incomes_is_empty_for_an_inverted_window() -> None:
    """Empty rather than a raise, matching `periods_between`: callers fold over the
    result, so empty composes where a raise forces a guard at every call site."""
    assert (
        expand_recurring_incomes(
            (_recurring_income(),), _WINDOW_END, _WINDOW_START
        )
        == ()
    )


def test_expand_recurring_incomes_of_nothing_is_nothing() -> None:
    assert expand_recurring_incomes((), _WINDOW_START, _WINDOW_END) == ()
