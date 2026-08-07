"""Engine and session construction.

Owned by `module/persistence` (PLAN.md §13.2).

`ingestion.store_receipt(blob, content_type)` is a frozen signature with no `Session`
parameter (CONTRACTS.md §8.8), so *something* has to hand it a session it did not
receive. That something is `session_scope()` here, rather than a module-level `Session`
in `ingestion/`: the process-wide engine is a persistence concern and this keeps the one
piece of mutable module state in the package that owns the database.

Nothing in `core/` or `domain/` may import this module — a projection that could reach a
session is a projection that could stop being pure (CLAUDE.md §3.1, §4.2).

The environment variable is read here and only here inside `persistence/`. That is not
a clock read and not a domain decision; §4.4's prohibition is on `core/` and `domain/`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

#: Environment variable naming the database. A SQLAlchemy URL.
DATABASE_URL_ENV = "BUDGET_DATABASE_URL"

#: Used when `DATABASE_URL_ENV` is unset. A file, not `:memory:` — an in-memory default
#: would silently discard an append-only ledger, which is the one failure this codebase
#: is built to make impossible.
DEFAULT_DATABASE_URL = "sqlite+pysqlite:///budget.db"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """Turn on foreign keys and WAL for SQLite.

    SQLite ships with foreign key enforcement OFF, per connection. The single FK in the
    schema (`events.receipt_blob_id`) would otherwise be documentation.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an `Engine` for `url`, defaulting to `$BUDGET_DATABASE_URL`.

    Postconditions:
        SQLite connections have `PRAGMA foreign_keys=ON`
        no schema is created — that is Alembic's job (`alembic upgrade head`)
    """
    resolved = url or os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
    engine = create_engine(resolved, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A `sessionmaker` bound to `engine`.

    `expire_on_commit=False` so that a model read out of a session stays usable after
    the transaction closes; every read path here returns frozen Pydantic models rather
    than ORM instances, so there is nothing to refresh.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_engine() -> Engine:
    """The process-wide engine, built on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """The process-wide session factory, built on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


def configure_engine(engine: Engine) -> None:
    """Install `engine` as the process-wide one.

    For `api/` startup and for tests that need `session_scope()` to reach a temporary
    database. Replacing the engine replaces the session factory with it.
    """
    global _engine, _session_factory
    _engine = engine
    _session_factory = create_session_factory(engine)


def reset_engine() -> None:
    """Forget the process-wide engine. Test teardown."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional session: commit on clean exit, roll back on exception.

    This is what `ingestion.store_receipt` uses, since its frozen signature takes no
    session. Callers that already hold a session must pass it to the repository
    directly instead of nesting a second one.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
