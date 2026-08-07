"""Effective-dated definition versions: append, resolve, and the one permitted UPDATE.

CLAUDE.md §4.3 allows exactly one mutation anywhere in this codebase — setting
`effective_to` to close a definition version. `DefinitionRepository.close_version` is
that mutation, and these tests pin its edges: it touches one column of one row, it
refuses to run twice, and it cannot produce a range that `DefinitionBase` would have
rejected on construction.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from core.types import AccountKind, AppError, BudgetTiming, Cadence, ErrorCode
from domain.definitions import (
    Account,
    AllocationPolicy,
    FixedCost,
    RecurringIncome,
    resolve_version,
)
from persistence.mapping import DefinitionKind
from persistence.models import AccountRow, AllocationPolicyRow, table_for
from persistence.repositories import DefinitionRepository

UTC = dt.timezone.utc
RECORDED_AT = dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def uid(n: int) -> UUID:
    return UUID(int=n)


def _policy(
    version_id: int,
    effective_from: dt.date,
    effective_to: dt.date | None = None,
    *,
    entity_id: str = "default",
    savings_bps: int = 5_000,
) -> AllocationPolicy:
    return AllocationPolicy(
        version_id=uid(version_id),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        savings_bps=savings_bps,
        discretionary_bps=10_000 - savings_bps,
    )


def _account(
    version_id: int,
    effective_from: dt.date,
    effective_to: dt.date | None = None,
    *,
    entity_id: str = "visa",
    apr_bps: int = 2_199,
) -> Account:
    return Account(
        version_id=uid(version_id),
        entity_id=entity_id,
        effective_from=effective_from,
        effective_to=effective_to,
        recorded_at=RECORDED_AT,
        name="Visa",
        kind=AccountKind.CREDIT_CARD,
        apr_bps=apr_bps,
        statement_close_day=15,
        payment_due_day=10,
        budget_timing=BudgetTiming.AT_STATEMENT_PAYMENT,
    )


# ---------------------------------------------------------------------- round-trip


def test_every_definition_kind_round_trips(session: Session) -> None:
    """All four kinds, stored and reconstructed exactly, enums included."""
    repository = DefinitionRepository(session)
    income = RecurringIncome(
        version_id=uid(1),
        entity_id="salary",
        effective_from=dt.date(2026, 1, 1),
        effective_to=None,
        recorded_at=RECORDED_AT,
        name="Salary",
        amount_minor=450_000,
        cadence=Cadence.SEMIMONTHLY,
        anchor_day=15,
        account_id="checking",
    )
    cost = FixedCost(
        version_id=uid(2),
        entity_id="rent",
        effective_from=dt.date(2026, 1, 1),
        effective_to=None,
        recorded_at=RECORDED_AT,
        name="Rent",
        amount_minor=120_000,
        cadence=Cadence.MONTHLY,
        due_day=5,
        payee="Landlord",
        category="housing",
    )
    policy = _policy(3, dt.date(2026, 1, 1))
    account = _account(4, dt.date(2026, 1, 1))

    for version in (income, cost, policy, account):
        repository.add_version(version)
    session.commit()
    session.expunge_all()

    definitions = repository.load_definitions()
    assert definitions.recurring_incomes == (income,)
    assert definitions.fixed_costs == (cost,)
    assert definitions.allocation_policies == (policy,)
    assert definitions.accounts == (account,)


def test_load_definitions_returns_all_versions_not_only_the_current_one(
    session: Session,
) -> None:
    """`project()` resolves per period and per cycle, so it needs the whole history.

    PLAN.md §8.3 — a closed period's policy was pinned by a date that has already
    passed. Handing the projection a pre-resolved current version would silently
    re-price every closed period at today's split.
    """
    repository = DefinitionRepository(session)
    old = _policy(1, dt.date(2026, 1, 1), dt.date(2026, 4, 1), savings_bps=5_000)
    new = _policy(2, dt.date(2026, 4, 1), savings_bps=7_000)
    repository.add_version(old)
    repository.add_version(new)
    session.commit()

    policies = repository.load_definitions().allocation_policies
    assert policies == (old, new)

    assert resolve_version(policies, "default", dt.date(2026, 3, 1)) == old
    assert resolve_version(policies, "default", dt.date(2026, 4, 1)) == new


def test_reads_are_ordered_totally_and_stably(session: Session) -> None:
    """Ordering is `(entity_id, effective_from, recorded_at, version_id)`.

    `project()`'s determinism must not rest on the order rows came back in
    (CLAUDE.md §5.1 property 5), so the order is imposed here rather than inherited from
    the storage engine.
    """
    repository = DefinitionRepository(session)
    versions = [
        _policy(3, dt.date(2026, 4, 1), entity_id="b"),
        _policy(1, dt.date(2026, 1, 1), dt.date(2026, 4, 1), entity_id="a"),
        _policy(2, dt.date(2026, 4, 1), entity_id="a"),
    ]
    for version in versions:
        repository.add_version(version)
    session.commit()

    first = repository.list_allocation_policies()
    second = repository.list_allocation_policies()
    assert [version.version_id for version in first] == [uid(1), uid(2), uid(3)]
    assert first == second


def test_entity_filter_narrows_the_read(session: Session) -> None:
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1), entity_id="a"))
    repository.add_version(_policy(2, dt.date(2026, 1, 1), entity_id="b"))
    session.commit()

    only_a = repository.list_allocation_policies(entity_id="a")
    assert [version.version_id for version in only_a] == [uid(1)]


def test_kind_dispatched_read_matches_the_typed_one(session: Session) -> None:
    """`list_versions(kind)` backs `GET /definitions/{kind}`; the two must agree."""
    repository = DefinitionRepository(session)
    policy = _policy(1, dt.date(2026, 1, 1))
    repository.add_version(policy)
    session.commit()

    assert repository.list_versions(DefinitionKind.ALLOCATION_POLICY) == (policy,)
    assert repository.get_version(DefinitionKind.ALLOCATION_POLICY, uid(1)) == policy
    assert repository.get_version(DefinitionKind.ALLOCATION_POLICY, uid(99)) is None


# ------------------------------------------------------------------- non-overlap


def test_an_overlapping_version_is_rejected(session: Session) -> None:
    """`OVERLAPPING_VERSIONS`, 409 (CONTRACTS.md §7.1), and nothing is written.

    Checked with `domain.definitions.validate_no_overlap` rather than reimplemented in
    SQL, so there is one definition of "overlap" in the codebase.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1), dt.date(2026, 6, 1)))
    session.flush()

    with pytest.raises(AppError) as raised:
        repository.add_version(_policy(2, dt.date(2026, 3, 1), dt.date(2026, 9, 1)))
    assert raised.value.code == ErrorCode.OVERLAPPING_VERSIONS
    assert len(repository.list_allocation_policies()) == 1


def test_an_open_ended_version_blocks_a_later_one(session: Session) -> None:
    """Two open-ended versions always overlap — both run to +infinity.

    This is the shape the partial unique index also forbids, and it is the common
    mistake: adding the replacement version before closing the one it replaces.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1)))
    session.flush()

    with pytest.raises(AppError) as raised:
        repository.add_version(_policy(2, dt.date(2026, 6, 1)))
    assert raised.value.code == ErrorCode.OVERLAPPING_VERSIONS


def test_adjacent_versions_do_not_overlap(session: Session) -> None:
    """`[from, to)` is half-open: one ending exactly where the next begins is legal.

    This is the normal way a definition is superseded (CLAUDE.md §4.3) — no date belongs
    to both versions and none belongs to neither.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1), dt.date(2026, 4, 1)))
    repository.add_version(_policy(2, dt.date(2026, 4, 1)))
    session.commit()

    assert len(repository.list_allocation_policies()) == 2


def test_different_entities_never_interact(session: Session) -> None:
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1), entity_id="a"))
    repository.add_version(_policy(2, dt.date(2026, 1, 1), entity_id="b"))
    session.commit()

    assert len(repository.list_allocation_policies()) == 2


def test_the_database_rejects_a_second_open_version_written_around_the_repository(
    session: Session,
) -> None:
    """The partial unique index is the backstop, not decoration.

    `add_version` is the intended door, but a constraint that only exists in Python is
    one refactor away from not existing.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1)))
    session.flush()

    values = {
        "version_id": uid(2),
        "entity_id": "default",
        "effective_from": dt.date(2026, 6, 1),
        "effective_to": None,
        "recorded_at": RECORDED_AT,
        "savings_bps": 5_000,
        "discretionary_bps": 5_000,
    }
    with pytest.raises(IntegrityError):
        session.execute(insert(table_for(AllocationPolicyRow)).values(**values))
    session.rollback()


def test_the_database_rejects_a_bps_total_that_is_not_10000(session: Session) -> None:
    """`POLICY_BPS_NOT_10000` is a construction-time invariant; the CHECK restates it.

    A policy row that broke it would make the top-level allocation invariant
    (`fixed + savings + discretionary == allocatable_income`) unprovable, which is worth
    a duplicated constraint.
    """
    values = {
        "version_id": uid(1),
        "entity_id": "default",
        "effective_from": dt.date(2026, 1, 1),
        "effective_to": None,
        "recorded_at": RECORDED_AT,
        "savings_bps": 5_000,
        "discretionary_bps": 4_000,
    }
    with pytest.raises(IntegrityError):
        session.execute(insert(table_for(AllocationPolicyRow)).values(**values))
    session.rollback()


def test_the_database_rejects_an_inverted_effective_range(session: Session) -> None:
    """`EFFECTIVE_RANGE_INVALID`, restated where the data lives."""
    values = {
        "version_id": uid(1),
        "entity_id": "default",
        "effective_from": dt.date(2026, 6, 1),
        "effective_to": dt.date(2026, 1, 1),
        "recorded_at": RECORDED_AT,
        "savings_bps": 5_000,
        "discretionary_bps": 5_000,
    }
    with pytest.raises(IntegrityError):
        session.execute(insert(table_for(AllocationPolicyRow)).values(**values))
    session.rollback()


def test_an_unknown_enum_value_is_rejected_by_the_type(session: Session) -> None:
    """`validate_strings=True`: a value outside the enum never reaches the wire."""
    values = {
        "version_id": uid(1),
        "entity_id": "visa",
        "effective_from": dt.date(2026, 1, 1),
        "effective_to": None,
        "recorded_at": RECORDED_AT,
        "name": "Visa",
        "kind": "CRYPTO_WALLET",
        "apr_bps": 2_199,
        "statement_close_day": 15,
        "payment_due_day": 10,
        "budget_timing": "AT_PURCHASE",
    }
    with pytest.raises(StatementError):
        session.execute(insert(table_for(AccountRow)).values(**values))
    session.rollback()


def test_the_database_rejects_an_unknown_enum_value(session: Session) -> None:
    """And so does the database, for a write that never passes through SQLAlchemy.

    Raw SQL, because the type layer intercepts an invalid value first — which is the
    right behavior but tests the wrong thing. The enum CHECK constraints are declared
    explicitly in `models.py` precisely so they end up in the schema; this is what
    confirms they did.
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO accounts (version_id, entity_id, effective_from, "
                "effective_to, recorded_at, name, kind, apr_bps, "
                "statement_close_day, payment_due_day, budget_timing) VALUES "
                "('x', 'visa', '2026-01-01', NULL, '2026-01-01 00:00:00.000000', "
                "'Visa', 'CRYPTO_WALLET', 2199, 15, 10, 'AT_PURCHASE')"
            )
        )
    session.rollback()


# ----------------------------------------------------------------- close_version


def test_close_version_sets_effective_to_and_nothing_else(session: Session) -> None:
    """The single permitted UPDATE, and the assertion that it stays single-column.

    Every other column of the row is compared before and after. A `close_version` that
    also refreshed `recorded_at`, say, would silently rewrite the version ordering key.
    """
    repository = DefinitionRepository(session)
    original = _account(1, dt.date(2026, 1, 1), apr_bps=2_199)
    repository.add_version(original)
    session.commit()

    before = session.execute(
        select(table_for(AccountRow)).where(table_for(AccountRow).c.version_id == uid(1))
    ).mappings().one()

    repository.close_version(DefinitionKind.ACCOUNT, uid(1), dt.date(2026, 6, 1))
    session.commit()

    after = session.execute(
        select(table_for(AccountRow)).where(table_for(AccountRow).c.version_id == uid(1))
    ).mappings().one()

    assert after["effective_to"] == dt.date(2026, 6, 1)
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"effective_to"}


def test_close_then_add_is_how_a_definition_is_superseded(session: Session) -> None:
    """The documented two-step (CLAUDE.md §4.3), end to end.

    `add_version` deliberately does not auto-close: superseding is two decisions, and
    inferring the first from the second is how a one-day gap gets in.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_account(1, dt.date(2026, 1, 1), apr_bps=2_199))
    session.flush()

    repository.close_version(DefinitionKind.ACCOUNT, uid(1), dt.date(2026, 6, 1))
    repository.add_version(_account(2, dt.date(2026, 6, 1), apr_bps=2_499))
    session.commit()

    accounts = repository.list_accounts()
    assert [version.apr_bps for version in accounts] == [2_199, 2_499]
    assert accounts[0].effective_to == dt.date(2026, 6, 1)
    assert accounts[1].effective_to is None

    resolved_before = resolve_version(accounts, "visa", dt.date(2026, 5, 31))
    resolved_after = resolve_version(accounts, "visa", dt.date(2026, 6, 1))
    assert resolved_before is not None
    assert resolved_after is not None
    assert resolved_before.apr_bps == 2_199
    assert resolved_after.apr_bps == 2_499


def test_closing_an_already_closed_version_is_refused(session: Session) -> None:
    """Re-closing would be a second mutation of a row that already has its final value.

    Not idempotent housekeeping — the thing §4.3 forbids.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 1, 1)))
    repository.close_version(
        DefinitionKind.ALLOCATION_POLICY, uid(1), dt.date(2026, 6, 1)
    )
    session.flush()

    with pytest.raises(AppError) as raised:
        repository.close_version(
            DefinitionKind.ALLOCATION_POLICY, uid(1), dt.date(2026, 9, 1)
        )
    assert raised.value.code == ErrorCode.VALIDATION_FAILED

    stored = repository.list_allocation_policies()
    assert stored[0].effective_to == dt.date(2026, 6, 1)


def test_closing_an_unknown_version_is_refused(session: Session) -> None:
    repository = DefinitionRepository(session)
    with pytest.raises(AppError) as raised:
        repository.close_version(
            DefinitionKind.ALLOCATION_POLICY, uid(99), dt.date(2026, 6, 1)
        )
    assert raised.value.code == ErrorCode.VALIDATION_FAILED


def test_closing_before_or_on_effective_from_is_refused(session: Session) -> None:
    """A version effective for no date at all is never a valid thing to write.

    Same rule `DefinitionBase._check_effective_range` enforces on construction, applied
    to the one path that can change a range after the fact.
    """
    repository = DefinitionRepository(session)
    repository.add_version(_policy(1, dt.date(2026, 6, 1)))
    session.flush()

    for attempted in (dt.date(2026, 6, 1), dt.date(2026, 1, 1)):
        with pytest.raises(AppError) as raised:
            repository.close_version(
                DefinitionKind.ALLOCATION_POLICY, uid(1), attempted
            )
        assert raised.value.code == ErrorCode.EFFECTIVE_RANGE_INVALID

    assert repository.list_allocation_policies()[0].effective_to is None
