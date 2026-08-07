"""Fixtures for the ingestion suite.

Owned by `module/ingestion` (PLAN.md §13.2). Deliberately a sibling of, and not an
import from, `tests/unit/persistence/conftest.py` — path ownership is what keeps the
merges conflict-free (§13.2), and a shared fixture module across two branches is the one
thing that structure cannot absorb.

Every test here runs against a real SQLite database. Ingestion's whole contract is
"the second write is a no-op decided by the database", and a mocked session decides it
in application code instead — which is precisely the implementation this module is
forbidden to have.

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

from persistence.base import Base
from persistence.engine import (
    configure_engine,
    create_db_engine,
    create_session_factory,
    reset_engine,
)

UTC = dt.timezone.utc

#: A recorded instant, fixed. `recorded_at` is a parameter everywhere in `ingestion/`
#: precisely so a test can pin it (CLAUDE.md §4.4).
RECORDED_AT = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

#: Bytes that are a plausible receipt. The content is irrelevant to every assertion —
#: only that it hashes to one value and different bytes hash to another.
RECEIPT_BYTES = b"%PDF-1.7 fake receipt bytes"
OTHER_RECEIPT_BYTES = b"%PDF-1.7 a different receipt"


def uid(n: int) -> UUID:
    """A stable UUID. Literal ids keep every assertion reproducible."""
    return UUID(int=n)


def build_engine() -> Engine:
    """A fresh in-memory database with the schema applied.

    A plain function as well as a fixture, because a Hypothesis test may not take a
    function-scoped fixture — the fixture is set up once and reused across every
    generated example, which would let one example's rows leak into the next and make a
    dedupe assertion pass for the wrong reason.
    """
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    built = build_engine()
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    with factory() as opened:
        yield opened


@pytest.fixture
def process_engine(engine: Engine) -> Iterator[Engine]:
    """Install the test database as the process-wide one.

    `ingestion.store_receipt` takes no `Session` (CONTRACTS.md §8.8) and reaches the
    database through `persistence.engine.session_scope()`, which uses the process-wide
    engine. Without this fixture that engine would be built from
    `DEFAULT_DATABASE_URL` — a *file*, `budget.db`, in the working directory. A test
    suite that silently creates a real database file is a test suite that passes for the
    wrong reason once.
    """
    configure_engine(engine)
    yield engine
    reset_engine()
