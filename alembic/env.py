"""Alembic environment. Owned by `module/persistence` (PLAN.md §13.2).

`target_metadata` is `persistence.base.Base.metadata`, so `alembic check` compares the
migrations against the models directly. That is the mechanism that keeps a hand-edited
migration from diverging from the schema the application actually uses — run it, do not
assume.

The URL comes from `persistence.engine.create_db_engine`, which reads
`$BUDGET_DATABASE_URL`. `-x db_url=...` overrides it for a scratch run.

`render_as_batch=True` because SQLite is the local target and has no meaningful
`ALTER TABLE`: batch mode rewrites the table instead. Note that a batch rewrite is
copy-create-swap, which is not a `DROP` of live data in the CLAUDE.md §4.3 sense — but
it is also not something to reach for casually on the events table.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Engine

from persistence.base import Base
from persistence.engine import create_db_engine

# `persistence.models` is imported for its side effect: it is what registers the tables
# on `Base.metadata`. Without it autogenerate sees an empty schema and cheerfully
# proposes dropping everything.
import persistence.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _engine() -> Engine:
    """The database to migrate, in precedence order.

    1. `-x db_url=...` — an explicit, one-off override for a scratch run.
    2. `sqlalchemy.url` in the config, when a caller has set it programmatically
       (`tests/unit/persistence/test_migrations.py` does, to point at a tmp file).
       The shipped `alembic.ini` leaves it empty precisely so this does not fire by
       accident.
    3. `persistence.engine`, i.e. `$BUDGET_DATABASE_URL` or the default file.

    Order matters: an ini value that silently outranked `-x` would make a scratch run
    migrate the real database, which is the one mistake a migration tool must not make
    easy.
    """
    overrides = context.get_x_argument(as_dictionary=True)
    configured = config.get_main_option("sqlalchemy.url")
    return create_db_engine(overrides.get("db_url") or configured or None)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=str(_engine().url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    engine = _engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
