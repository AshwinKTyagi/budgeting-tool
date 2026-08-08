"""Fixtures for the API suite.

Owned by `module/api` (PLAN.md §13.2). A sibling of, and not an import from, the
persistence and ingestion conftests — path ownership is what keeps the merges
conflict-free (§13.2), and a fixture module shared across three branches is the one
thing that structure cannot absorb.

**Every test gets its own SQLite file under `tmp_path`.** Not `:memory:`, because
`ingestion.store_receipt` reaches the database through `session_scope()` on the
process-wide engine and the TestClient runs the app in a portal thread; a file is the
only shape that is unambiguously the same database from both. Not the repository's
`budget.db` either — a suite that silently creates a real ledger file passes for the
wrong reason exactly once.

No tolerance: every assertion in this suite is `==` (CLAUDE.md §4.6). No clock reads
except the two the API itself makes, which are pinned by monkeypatching
`api.clock._now_utc` where a test needs a fixed answer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from api.app import create_app
from core.types import AccountKind, BudgetTiming, Cadence
from domain.definitions import Account, AllocationPolicy, FixedCost
from persistence.base import Base
from persistence.engine import (
    configure_engine,
    create_db_engine,
    create_session_factory,
    reset_engine,
)
from persistence.repositories import DefinitionRepository

UTC = dt.timezone.utc

API = "/api/v1"

#: A recorded instant for the seeded definitions. Fixed, so nothing in this suite
#: depends on when it ran.
SEEDED_AT = dt.datetime(2025, 12, 1, 0, 0, tzinfo=UTC)

#: The date every seeded definition becomes effective. Before any event in the suite.
GENESIS = dt.date(2026, 1, 1)

CHECKING = "checking"
SAVINGS = "savings"
VISA = "visa"


def uid(n: int) -> UUID:
    """A stable UUID. Literal ids keep every assertion reproducible."""
    return UUID(int=n)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A fresh file-backed database with the schema applied, installed process-wide.

    `create_all` from the models rather than `alembic upgrade head`, which would cost a
    subprocess per test. `tests/unit/persistence/test_migrations.py` asserts the two
    produce the same schema, so the shortcut cannot hide a drifted migration.
    """
    built = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'api-test.db'}")
    Base.metadata.create_all(built)
    configure_engine(built)
    yield built
    reset_engine()
    built.dispose()


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    """The first-run seed of CONTRACTS.md §4, plus a credit card and a fixed cost.

    > Seeded on first run: one `CHECKING` and one `SAVINGS` account, and a default
    > `AllocationPolicy` of `savings_bps=5000, discretionary_bps=5000`.

    The card and the rent `FixedCost` are here rather than in individual tests because
    almost every endpoint needs an account that exists — `UNKNOWN_ACCOUNT` is checked at
    write time — and a per-test seed would be the same eight lines nine times.
    """
    factory = create_session_factory(engine)
    with factory() as session:
        repository = DefinitionRepository(session)
        repository.add_version(
            Account(
                version_id=uid(101),
                entity_id=CHECKING,
                effective_from=GENESIS,
                effective_to=None,
                recorded_at=SEEDED_AT,
                name="Checking",
                kind=AccountKind.CHECKING,
                apr_bps=0,
                statement_close_day=None,
                payment_due_day=None,
            )
        )
        repository.add_version(
            Account(
                version_id=uid(102),
                entity_id=SAVINGS,
                effective_from=GENESIS,
                effective_to=None,
                recorded_at=SEEDED_AT,
                name="Savings",
                kind=AccountKind.SAVINGS,
                apr_bps=450,
                statement_close_day=None,
                payment_due_day=None,
            )
        )
        repository.add_version(
            Account(
                version_id=uid(103),
                entity_id=VISA,
                effective_from=GENESIS,
                effective_to=None,
                recorded_at=SEEDED_AT,
                name="Visa",
                kind=AccountKind.CREDIT_CARD,
                apr_bps=2199,
                statement_close_day=15,
                payment_due_day=10,
                budget_timing=BudgetTiming.AT_PURCHASE,
            )
        )
        repository.add_version(
            AllocationPolicy(
                version_id=uid(104),
                entity_id="default",
                effective_from=GENESIS,
                effective_to=None,
                recorded_at=SEEDED_AT,
                savings_bps=5000,
                discretionary_bps=5000,
            )
        )
        repository.add_version(
            FixedCost(
                version_id=uid(105),
                entity_id="rent",
                effective_from=GENESIS,
                effective_to=None,
                recorded_at=SEEDED_AT,
                name="Rent",
                amount_minor=120_000,
                cadence=Cadence.MONTHLY,
                due_day=1,
                payee="Landlord",
                category="housing",
            )
        )
        session.commit()
    return engine


@pytest.fixture
def client(seeded: Engine) -> Iterator[TestClient]:
    """The whole application, pointed at the temporary database."""
    with TestClient(create_app(engine=seeded)) as opened:
        yield opened


@pytest.fixture
def bare_client(engine: Engine) -> Iterator[TestClient]:
    """The application over an empty database — no accounts, no policy.

    For the tests that are about the empty case or about `UNKNOWN_ACCOUNT`, where a seed
    would be the thing under test.
    """
    with TestClient(create_app(engine=engine)) as opened:
        yield opened


def income(date: str, amount_minor: int, **extra: object) -> dict[str, object]:
    """An `IncomeReceived` body. A helper, because six tests need one."""
    return {
        "event_type": "IncomeReceived",
        "date": date,
        "amount_minor": amount_minor,
        "source": "Employer",
        "account_id": CHECKING,
        **extra,
    }


def expense(date: str, amount_minor: int, **extra: object) -> dict[str, object]:
    """An `ExpenseRecorded` body."""
    return {
        "event_type": "ExpenseRecorded",
        "date": date,
        "amount_minor": amount_minor,
        "category": "groceries",
        "account_id": CHECKING,
        **extra,
    }


# ------------------------------------------------------------------- module naming
# Every test module in this package is named `test_api_*.py` rather than `test_*.py`.
# `tests/` has no `__init__.py` chain, so pytest derives a module name from the file's
# BASENAME, and `tests/unit/api/test_events.py` would collide with the pre-existing
# `tests/unit/domain/test_events.py` — an import-file-mismatch that aborts collection
# for the whole suite, not just this package. The prefix makes every basename unique
# across every branch's tests, which matters because the integrator merges them all.
