"""Fixtures for the persistence suite.

Owned by `module/persistence` (PLAN.md §13.2).

Every test here runs against a real SQLite database, in memory unless the test needs a
file. That is deliberate: the failures this module is exposed to — a dropped timezone, a
unique constraint that is not actually enforced, an ordering that depends on insertion
order — are all failures a mock would reproduce incorrectly, because the mock would be
written by the same person who wrote the bug.

No clock reads: every date and instant below is an explicit literal (CLAUDE.md §4.4).
No tolerance: every assertion in this suite is `==` (CLAUDE.md §4.6).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from domain.events import (
    AccountOpeningBalance,
    Event,
    EventVoided,
    ExpenseRecorded,
    ExternalRef,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
)
from persistence.base import Base
from persistence.engine import create_db_engine, create_session_factory

UTC = dt.timezone.utc


def uid(n: int) -> UUID:
    """A stable UUID. Literal ids keep every assertion reproducible."""
    return UUID(int=n)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory database with the schema applied.

    `create_all` from the models rather than `alembic upgrade head`, which would cost a
    subprocess per test. That the two produce the same schema is asserted separately, in
    `test_migrations.py`, so this shortcut cannot hide a migration that has drifted.
    """
    built = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as opened:
        yield opened


# ------------------------------------------------------------------ sample events
# One instance of every member of the discriminated union, plus the variants that carry
# a field a simpler instance leaves at None: an external_ref, a note, and a PaymentMade
# with its principal/interest split populated. A round-trip that only ever sees NULL in
# a nullable column proves nothing about that column.


def sample_events() -> tuple[Event, ...]:
    """A coherent-enough ledger covering every event type and both optional shapes."""
    return (
        IncomeReceived(
            event_id=uid(1),
            date=dt.date(2026, 3, 2),
            recorded_at=dt.datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
            dedupe_key="manual:IncomeReceived:2026-03-02:450000:aaa",
            amount_minor=450_000,
            source="Employer",
            account_id="checking",
        ),
        IncomeReceived(
            event_id=uid(2),
            date=dt.date(2026, 3, 16),
            recorded_at=dt.datetime(2026, 3, 16, 9, 0, tzinfo=UTC),
            dedupe_key="ext:acme:txn-1",
            external_ref=ExternalRef(provider="acme", provider_txn_id="txn-1"),
            note="second paycheck",
            amount_minor=450_000,
            source="Employer",
            account_id="checking",
        ),
        GiftReceived(
            event_id=uid(3),
            date=dt.date(2026, 3, 4),
            recorded_at=dt.datetime(2026, 3, 4, 11, 15, tzinfo=UTC),
            dedupe_key="manual:GiftReceived:2026-03-04:10000:bbb",
            amount_minor=10_000,
            source="Grandmother",
            account_id="checking",
        ),
        ObligationRaised(
            event_id=uid(4),
            date=dt.date(2026, 3, 1),
            recorded_at=dt.datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
            dedupe_key="manual:ObligationRaised:2026-03-01:120000:ccc",
            obligation_id="rent-2026-03",
            due_date=dt.date(2026, 3, 5),
            amount_minor=120_000,
            payee="Landlord",
            category="housing",
            recurring_id="rent",
        ),
        ObligationRaised(
            event_id=uid(5),
            date=dt.date(2026, 3, 10),
            recorded_at=dt.datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
            dedupe_key="manual:ObligationRaised:2026-03-10:4500:ddd",
            obligation_id="one-off-plumber",
            due_date=dt.date(2026, 3, 20),
            amount_minor=4_500,
            payee="Plumber",
            category="maintenance",
        ),
        PaymentMade(
            event_id=uid(6),
            date=dt.date(2026, 3, 5),
            recorded_at=dt.datetime(2026, 3, 5, 12, 0, tzinfo=UTC),
            dedupe_key="manual:PaymentMade:2026-03-05:120000:eee",
            amount_minor=120_000,
            obligation_id="rent-2026-03",
            account_id="checking",
        ),
        PaymentMade(
            event_id=uid(7),
            date=dt.date(2026, 3, 25),
            recorded_at=dt.datetime(2026, 3, 25, 12, 0, tzinfo=UTC),
            dedupe_key="manual:PaymentMade:2026-03-25:118000:fff",
            amount_minor=118_000,
            obligation_id="loan-2026-03",
            account_id="checking",
            principal_minor=100_000,
            interest_minor=18_000,
        ),
        ExpenseRecorded(
            event_id=uid(8),
            date=dt.date(2026, 3, 7),
            recorded_at=dt.datetime(2026, 3, 7, 18, 45, tzinfo=UTC),
            dedupe_key="receipt:" + "0" * 64,
            amount_minor=4_599,
            category="groceries",
            account_id="visa",
            merchant="Corner Store",
        ),
        ExpenseRecorded(
            event_id=uid(9),
            date=dt.date(2026, 3, 8),
            recorded_at=dt.datetime(2026, 3, 8, 10, 0, tzinfo=UTC),
            dedupe_key="manual:ExpenseRecorded:2026-03-08:-1500:ggg",
            amount_minor=-1_500,  # a refund; negative is permitted
            category="groceries",
            account_id="visa",
        ),
        SavingsDrawn(
            event_id=uid(10),
            date=dt.date(2026, 3, 12),
            recorded_at=dt.datetime(2026, 3, 12, 7, 0, tzinfo=UTC),
            dedupe_key="manual:SavingsDrawn:2026-03-12:25000:hhh",
            amount_minor=25_000,
            reason="car repair",
        ),
        TransferMade(
            event_id=uid(11),
            date=dt.date(2026, 3, 20),
            recorded_at=dt.datetime(2026, 3, 20, 16, 0, tzinfo=UTC),
            dedupe_key="manual:TransferMade:2026-03-20:50000:iii",
            amount_minor=50_000,
            from_account_id="checking",
            to_account_id="visa",
        ),
        AccountOpeningBalance(
            event_id=uid(12),
            date=dt.date(2026, 1, 1),
            recorded_at=dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            dedupe_key="manual:AccountOpeningBalance:2026-01-01:-1180000:jjj",
            account_id="loan",
            amount_minor=-1_180_000,  # signed: a liability
        ),
        InterestCharged(
            event_id=uid(13),
            date=dt.date(2026, 3, 15),
            recorded_at=dt.datetime(2026, 3, 15, 3, 0, tzinfo=UTC),
            dedupe_key="manual:InterestCharged:2026-03-15:2199:kkk",
            account_id="visa",
            cycle_id="visa:2026-03",
            amount_minor=2_199,
        ),
        InterestEarned(
            event_id=uid(14),
            date=dt.date(2026, 3, 31),
            recorded_at=dt.datetime(2026, 3, 31, 23, 0, tzinfo=UTC),
            dedupe_key="manual:InterestEarned:2026-03-31:311:lll",
            account_id="savings",
            cycle_id="savings:2026-03",
            amount_minor=311,
        ),
        EventVoided(
            event_id=uid(15),
            date=dt.date(2026, 3, 9),
            recorded_at=dt.datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
            dedupe_key="manual:EventVoided:2026-03-09::mmm",
            target_event_id=uid(9),
            reason="entered twice",
        ),
    )


@pytest.fixture
def events() -> tuple[Event, ...]:
    return sample_events()


@pytest.fixture(
    params=sample_events(),
    ids=[f"{event.event_type}-{event.event_id.int}" for event in sample_events()],
)
def any_event(request: pytest.FixtureRequest) -> Event:
    """One event per member of the union, plus the optional-field variants.

    A parametrized fixture rather than `@pytest.mark.parametrize` on the test, because
    `tests/` has no `__init__.py` chain — `tests.unit.persistence.conftest` is not an
    importable module and a bare `from conftest import ...` would collide with the
    conftest of any sibling suite. Fixtures reach the test without an import at all.
    """
    event: Event = request.param
    return event
