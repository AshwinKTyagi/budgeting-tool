"""The migrations produce exactly the schema the models describe, on one head.

A migration that has drifted from the models is silent: the application works against
the schema `create_all` would have produced in a test, and fails against the schema
`alembic upgrade head` actually produced in production. Comparing the two is the only
way to know, so it is a test rather than a note in a docstring.
"""

from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import produce_migrations
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from persistence.base import Base
from persistence.engine import create_db_engine

REPO_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def test_there_is_exactly_one_head() -> None:
    """Two heads cannot be resolved automatically — the reason `alembic/versions/*` has
    a single owner (PLAN.md §13.3)."""
    script = ScriptDirectory.from_config(_alembic_config())
    assert len(script.get_heads()) == 1


def test_upgrade_head_produces_the_schema_the_models_describe(
    tmp_path: Path,
) -> None:
    """`alembic upgrade head` against a scratch SQLite file, then diff against metadata.

    `produce_migrations` returning an empty upgrade op list is the same assertion
    `alembic check` makes on the command line; running it here means the gate is part of
    the suite rather than a step someone has to remember.
    """
    from alembic import command

    db_path = tmp_path / "scratch.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", url)
    config.cmd_opts = None
    command.upgrade(config, "head")

    engine = create_db_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": Base.metadata}
        )
        diff = produce_migrations(context, Base.metadata)
    engine.dispose()

    assert diff.upgrade_ops is not None
    assert diff.upgrade_ops.as_diffs() == []


def test_the_migration_creates_every_table(tmp_path: Path) -> None:
    """Named explicitly, so a table dropped from the migration is loud."""
    from alembic import command

    db_path = tmp_path / "scratch.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", url)
    config.cmd_opts = None
    command.upgrade(config, "head")

    engine = create_db_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert {
        "events",
        "recurring_incomes",
        "fixed_costs",
        "allocation_policies",
        "accounts",
        "receipt_blobs",
    } <= tables


def test_the_dedupe_key_unique_constraint_reaches_the_migrated_schema(
    tmp_path: Path,
) -> None:
    """The one constraint idempotent ingestion depends on, checked in the real schema.

    `test_dedupe.py` proves it holds in a `create_all` database. This proves the
    migration carries it too — the two are different code paths and only one of them
    runs in production.
    """
    from alembic import command

    db_path = tmp_path / "scratch.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", url)
    config.cmd_opts = None
    command.upgrade(config, "head")

    engine = create_db_engine(url)
    inspector = inspect(engine)
    unique_columns = [
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("events")
    ]
    index_names = {index["name"] for index in inspector.get_indexes("events")}
    engine.dispose()

    assert ("dedupe_key",) in unique_columns
    assert "ix_events_ledger_order" in index_names
