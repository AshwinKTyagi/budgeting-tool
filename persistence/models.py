"""The tables. SQLAlchemy 2.0 declarative, fully typed.

Owned by `module/persistence` (PLAN.md §13.2).

Three shapes and nothing else:

* `EventRow` — the immutable, append-only ledger. One row per event, one column per
  field of the discriminated union (`domain/events.py`), with `event_type` carrying the
  tag. Columns that do not apply to a row's type are NULL.
* the four definition tables — `recurring_incomes`, `fixed_costs`,
  `allocation_policies`, `accounts` — each a version table keyed by `version_id` and
  effective-dated by `[effective_from, effective_to)`.
* `ReceiptBlobRow` — content-addressed receipt bytes behind `ingestion.store_receipt`.

**Nothing derived is stored.** There is no balance column, no period summary table, no
running total, no materialized obligation. Every read recomputes from genesis
(PLAN.md §3), which is exactly why a backdated receipt entered today correctly changes
every period after it. A cache added here would be the thing that goes stale.

**Why one wide events table rather than a JSON payload column.** CLAUDE.md §2.1 requires
money to be an integer in minor units, and §2.2 requires the `_minor` suffix to survive
"into JSON, database columns, and log lines". A JSON blob satisfies neither in any way a
database can check: it admits `19.99` in a column typed `JSON`, and no index or check
constraint can reach inside it. A typed column per field means the schema itself carries
the money rule, and `GET /ledger`'s filters (`types[]`, `account_id`, `category`) are
ordinary indexed predicates rather than JSON extraction. The cost is a wide, sparse
table; for a single-user ledger accumulating ~24k rows per decade that cost is nothing.

The set of columns is checked against the union at test time
(`test_every_event_field_has_a_column`), so adding an event type without a column here
fails a test rather than silently dropping a field.
"""

from __future__ import annotations

import datetime as dt
import enum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem, Table

from core.types import AccountKind, BudgetTiming, Cadence
from persistence.base import Base

# --------------------------------------------------------------------------- enums
# Enums persist as their VALUE, never their member name. The two coincide for every
# enum in `core/types.py` today, but relying on that would make a future
# `FOO = "foo"` silently rewrite every stored row.
#
# `create_constraint=False` and an explicit `CheckConstraint` per column instead. The
# constraint SQLAlchemy generates for a non-native enum is attached by the *type* rather
# than declared in the table's metadata, and Alembic's constraint comparison then sees
# it in the reflected database and not in the model — reporting a spurious "removed
# check constraint" on every `alembic check`, forever. Declaring it makes the check
# real and the drift detector honest, which matters more than the two lines saved.


def table_for(model: type[Base]) -> Table:
    """The `Table` a declarative model maps to.

    `DeclarativeBase.__table__` is annotated `FromClause` because a mapping *may* target
    a join or a select. Every model here maps to a plain table, and this narrows that
    once — with a runtime check rather than a cast, so the assumption is verified rather
    than asserted — instead of at each of the dozen Core statements in `repositories.py`.
    """
    table = model.__table__
    if not isinstance(table, Table):  # pragma: no cover - all mappings are plain tables
        raise TypeError(f"{model.__name__} is not mapped to a Table")
    return table


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


def _enum_check(column: str, enum_cls: type[enum.Enum], name: str) -> CheckConstraint:
    """`column IN (...)`, listing the enum's values in declaration order."""
    allowed = ", ".join(f"'{value}'" for value in _enum_values(enum_cls))
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


ACCOUNT_KIND = Enum(
    AccountKind,
    name="account_kind",
    native_enum=False,
    length=16,
    create_constraint=False,
    validate_strings=True,
    values_callable=_enum_values,
)

BUDGET_TIMING = Enum(
    BudgetTiming,
    name="budget_timing",
    native_enum=False,
    length=24,
    create_constraint=False,
    validate_strings=True,
    values_callable=_enum_values,
)

CADENCE = Enum(
    Cadence,
    name="cadence",
    native_enum=False,
    length=16,
    create_constraint=False,
    validate_strings=True,
    values_callable=_enum_values,
)


# ---------------------------------------------------------------------- the ledger


class EventRow(Base):
    """One row per event. Immutable and append-only.

    Nothing ever UPDATEs or DELETEs this table (CLAUDE.md §4.3). A correction is an
    `EventVoided` row pointing at `target_event_id`; the projection filters both before
    folding.

    `dedupe_key` is UNIQUE, and that single constraint is what makes idempotent
    re-ingestion work: `ingestion.append_event` is `INSERT ... ON CONFLICT (dedupe_key)
    DO NOTHING`, so a second write of the same event is a no-op decided by the database
    rather than by a read-then-write race in application code.

    Column naming mirrors the model field names exactly, including the `_minor` and
    `_bps` suffixes, so `persistence.mapping` can move a field by name and a missing
    column is a test failure rather than a silent data loss. The one exception is
    `date`, whose *column* is `event_date` (`date` is a type name in SQL and a poor
    identifier); the mapped attribute is still `date`.
    """

    __tablename__ = "events"

    # -- EventBase -------------------------------------------------------------
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    date: Mapped[dt.date] = mapped_column("event_date", Date, nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1024))

    # `ExternalRef` is flattened rather than nested: two nullable columns, both NULL or
    # both set (enforced below). A one-field-per-column table has no room for a JSON
    # sub-object, and provenance is worth querying.
    external_ref_provider: Mapped[str | None] = mapped_column(String(64))
    external_ref_provider_txn_id: Mapped[str | None] = mapped_column(String(256))

    # -- type-specific ---------------------------------------------------------
    # Every column below is nullable because it applies to a subset of event types.
    # Which subset is enforced by the domain models on reconstruction, not by the
    # schema: a CHECK per type would restate `domain/events.py` in SQL and the two
    # would drift.
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    principal_minor: Mapped[int | None] = mapped_column(BigInteger)
    interest_minor: Mapped[int | None] = mapped_column(BigInteger)

    account_id: Mapped[str | None] = mapped_column(String(128))
    from_account_id: Mapped[str | None] = mapped_column(String(128))
    to_account_id: Mapped[str | None] = mapped_column(String(128))
    obligation_id: Mapped[str | None] = mapped_column(String(128))
    recurring_id: Mapped[str | None] = mapped_column(String(128))
    cycle_id: Mapped[str | None] = mapped_column(String(160))
    target_event_id: Mapped[UUID | None] = mapped_column()

    due_date: Mapped[dt.date | None] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(128))
    payee: Mapped[str | None] = mapped_column(String(256))
    merchant: Mapped[str | None] = mapped_column(String(256))
    source: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str | None] = mapped_column(String(1024))

    # -- provenance ------------------------------------------------------------
    # Set once at INSERT for an event created from a receipt upload (CONTRACTS.md §6.4)
    # and never updated. It is not part of any domain model, so it does not participate
    # in the round-trip; it exists so a ledger row can be traced back to its bytes
    # without parsing the `receipt:{sha256}` prefix off `dedupe_key`.
    receipt_blob_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("receipt_blobs.blob_id")
    )

    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint(
            "(external_ref_provider IS NULL) "
            "= (external_ref_provider_txn_id IS NULL)",
            name="external_ref_complete",
        ),
        # The canonical ledger order, `(date, recorded_at, event_id)` (CONTRACTS.md
        # §3.1). Indexing it is what lets the full-stream read and the `GET /ledger`
        # keyset pagination share one ordering.
        Index("ix_events_ledger_order", "event_date", "recorded_at", "event_id"),
        Index("ix_events_event_type", "event_type"),
        Index("ix_events_account_id", "account_id"),
        Index("ix_events_category", "category"),
        Index("ix_events_target_event_id", "target_event_id"),
    )


# ----------------------------------------------------------------- definitions


class DefinitionVersionMixin:
    """The five `DefinitionBase` columns, shared by all four definition tables.

    A mixin rather than a base table: the four kinds have disjoint payloads and are
    always read as four separate collections into `Definitions`, so single-table or
    joined inheritance would buy nothing and cost a discriminator plus a join.
    """

    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    recorded_at: Mapped[dt.datetime] = mapped_column(nullable=False)


def _definition_table_args(
    table_name: str, *extra: SchemaItem
) -> tuple[SchemaItem, ...]:
    """Constraints every definition version table carries.

    * `(entity_id, effective_from)` UNIQUE — two versions of one entity cannot begin on
      the same day. That is a *necessary* condition for non-overlap, not a sufficient
      one; full non-overlap is a property of a set of ranges and no portable SQL
      constraint expresses it (PostgreSQL's `EXCLUDE` would; SQLite has nothing). The
      complete check is `domain.definitions.validate_no_overlap`, which
      `DefinitionRepository.add_version` runs against the existing versions before
      inserting. These two constraints are the backstop that survives a caller going
      around the repository.
    * one open-ended version per entity — a partial unique index on `entity_id` where
      `effective_to IS NULL`. Two open-ended versions always overlap (both run to
      +infinity), so this is implied by non-overlap and safe to enforce absolutely.
    * `effective_to > effective_from` — the same half-open, non-empty range invariant
      `DefinitionBase._check_effective_range` enforces on construction
      (`EFFECTIVE_RANGE_INVALID`), restated where the data actually lives.
    """
    return (
        UniqueConstraint("entity_id", "effective_from"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range",
        ),
        Index(
            f"uq_{table_name}_open_version",
            "entity_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL"),
            postgresql_where=text("effective_to IS NULL"),
        ),
        Index(f"ix_{table_name}_entity_id_effective_from", "entity_id",
              "effective_from"),
        *extra,
    )


class RecurringIncomeRow(DefinitionVersionMixin, Base):
    """`domain.definitions.RecurringIncome`. FORECAST ONLY — no row here ever
    contributes to `allocatable_income` (PLAN.md §8.2). Its occurrences are offered for
    confirmation, and it is the confirmed `IncomeReceived` in `events` that allocates
    (PLAN.md §8.5)."""

    __tablename__ = "recurring_incomes"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cadence: Mapped[Cadence] = mapped_column(CADENCE, nullable=False)
    anchor_day: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__: tuple[SchemaItem, ...] = _definition_table_args(
        "recurring_incomes",
        CheckConstraint("anchor_day BETWEEN 1 AND 31", name="anchor_day_range"),
        _enum_check("cadence", Cadence, "cadence_values"),
    )


class FixedCostRow(DefinitionVersionMixin, Base):
    """`domain.definitions.FixedCost`. Expanded into expected obligations by the
    projection; `entity_id` is the `recurring_id` an `ObligationRaised` supersedes
    on (PLAN.md §8.1)."""

    __tablename__ = "fixed_costs"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cadence: Mapped[Cadence] = mapped_column(CADENCE, nullable=False)
    due_day: Mapped[int] = mapped_column(Integer, nullable=False)
    payee: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__: tuple[SchemaItem, ...] = _definition_table_args(
        "fixed_costs",
        CheckConstraint("due_day BETWEEN 1 AND 31", name="due_day_range"),
        _enum_check("cadence", Cadence, "cadence_values"),
    )


class AllocationPolicyRow(DefinitionVersionMixin, Base):
    """`domain.definitions.AllocationPolicy`.

    The CHECK on the bps total is the schema-level statement of the invariant
    `split_bps` takes as a precondition and `AllocationPolicy` validates on
    construction (`POLICY_BPS_NOT_10000`). A policy row that violated it would make the
    top-level allocation invariant unprovable, so it is worth stating twice.
    """

    __tablename__ = "allocation_policies"

    savings_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    discretionary_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__: tuple[SchemaItem, ...] = _definition_table_args(
        "allocation_policies",
        CheckConstraint(
            "savings_bps + discretionary_bps = 10000", name="bps_total_10000"
        ),
        CheckConstraint(
            "savings_bps >= 0 AND discretionary_bps >= 0", name="bps_non_negative"
        ),
    )


class AccountRow(DefinitionVersionMixin, Base):
    """`domain.definitions.Account`. `entity_id` is the `account_id`; APR is versioned
    exactly like any other field and resolved at statement cycle start (PLAN.md §7.4).
    """

    __tablename__ = "accounts"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[AccountKind] = mapped_column(ACCOUNT_KIND, nullable=False)
    apr_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    statement_close_day: Mapped[int | None] = mapped_column(Integer)
    payment_due_day: Mapped[int | None] = mapped_column(Integer)
    budget_timing: Mapped[BudgetTiming] = mapped_column(BUDGET_TIMING, nullable=False)

    __table_args__: tuple[SchemaItem, ...] = _definition_table_args(
        "accounts",
        CheckConstraint("apr_bps >= 0", name="apr_bps_non_negative"),
        CheckConstraint(
            "statement_close_day IS NULL OR statement_close_day BETWEEN 1 AND 31",
            name="statement_close_day_range",
        ),
        CheckConstraint(
            "payment_due_day IS NULL OR payment_due_day BETWEEN 1 AND 31",
            name="payment_due_day_range",
        ),
        _enum_check("kind", AccountKind, "kind_values"),
        _enum_check("budget_timing", BudgetTiming, "budget_timing_values"),
    )


# -------------------------------------------------------------------- receipts


class ReceiptBlobRow(Base):
    """Receipt bytes, content-addressed by SHA-256 (CONTRACTS.md §6.4, §8.8).

    `content_sha256` is UNIQUE, which is what makes "identical bytes reuse the same
    blob" a database fact rather than an application convention. `blob_id` is separate
    and opaque because the §6.4 response carries both — they are not the same value and
    a caller must not infer one from the other.

    The bytes are stored in-row. For a single user's receipts that is the simplest
    thing that works; an object store would be a swap of this one table's
    implementation and nothing else, because no other module touches it.
    """

    __tablename__ = "receipt_blobs"

    blob_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__: tuple[SchemaItem, ...] = (
        UniqueConstraint("content_sha256"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
    )
