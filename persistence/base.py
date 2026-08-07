"""The declarative base, the metadata naming convention, and the UTC column type.

Owned by `module/persistence` (PLAN.md §13.2).

Two things live here rather than in `models.py` because both are schema-wide policy
rather than a property of any one table:

* `NAMING_CONVENTION` — every index, unique constraint, check constraint and foreign
  key gets a deterministic name derived from the table and column. Without it SQLite
  invents anonymous names, Alembic autogenerate cannot match a constraint it did not
  create, and `alembic check` reports phantom differences forever. It also makes the
  hand-written initial migration verifiable against the models.
* `UtcDateTime` — the column type behind every `_at` field. See its docstring; SQLite
  is the reason it exists.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

#: Deterministic constraint names. `%(constraint_name)s` for `ck` means every
#: `CheckConstraint` in `models.py` must be given a short `name=`; that is deliberate,
#: since an unnamed check constraint cannot be altered or dropped portably.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[dt.datetime]):
    """A `DateTime(timezone=True)` that is actually timezone-aware and UTC on the way
    back out.

    CLAUDE.md §4.5 requires every instant to be aware and UTC, and `core.types`
    `UtcInstant` enforces that at the model boundary. The database is a second boundary
    and SQLite does not hold the line on its own: the SQLite dialect serializes a
    datetime with a storage format that has no offset field, so an aware value goes in
    and a **naive** value comes back. Pydantic would then reject the row on
    reconstruction — or, worse, a caller comparing instants would silently be comparing
    a naive value against an aware one and get a `TypeError` at the least convenient
    moment.

    So the conversion is explicit in both directions:

    * bind — a naive value is rejected outright (there is no correct zone to assume,
      exactly as in `core.types._require_utc`); an aware value is normalized to UTC,
      which preserves the instant exactly.
    * result — a naive value read back is *known* to be UTC, because bind is the only
      way a value gets in, so the UTC zone is reattached rather than guessed. A value
      that arrives already aware (PostgreSQL `timestamptz`) is normalized to UTC.

    `cache_ok = True`: the type carries no per-instance configuration, so SQLAlchemy may
    cache compiled statements that use it.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "naive datetime reached the database boundary: every instant must be "
                "timezone-aware and UTC (CLAUDE.md §4.5)"
            )
        return value.astimezone(dt.timezone.utc)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class Base(DeclarativeBase):
    """The declarative base for every table in the schema.

    `type_annotation_map` is what lets `Mapped[dt.datetime]` mean `UtcDateTime`
    everywhere without repeating it per column — a column that forgot it would be the
    one that loses a timezone.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: dict[Any, Any] = {dt.datetime: UtcDateTime}
