"""Repositories: the only code in the project that issues SQL.

Owned by `module/persistence` (PLAN.md §13.2).

Three of them, one per shape of thing that is stored:

* `EventRepository`   — append-only ledger. One writer (`append`), several readers.
* `DefinitionRepository` — versioned definitions, and the single permitted `UPDATE`.
* `ReceiptRepository` — content-addressed blobs behind `ingestion.store_receipt`.

Everything they return is a frozen `domain/` model, never an ORM instance. That is not
tidiness: `project()` is a pure fold over immutable inputs, and handing it objects
attached to a `Session` would give it a lazy loader — an I/O path — reachable from
inside a function that is required to have none (CLAUDE.md §4.2).

**What is forbidden here, and why each rule has exactly one hole:**

* No `DELETE`, no `DROP`, no `TRUNCATE`, anywhere, ever (CLAUDE.md §4.3). Corrections are
  `EventVoided` rows.
* No `UPDATE` against `events`. The table is immutable.
* Exactly one `UPDATE` exists in this module: `DefinitionRepository.close_version`,
  which sets `effective_to` on one version and touches no other column. It is its own
  method precisely so that "the only UPDATE" is a statement you can verify by grepping
  for `update(` and finding one call site.

**Transactions belong to the caller.** No method here commits. `api/` commits at the end
of a request and `session_scope()` commits at the end of its block; a repository that
committed on its own would make a two-statement operation (store a blob, append the
event that references it) non-atomic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, Table, and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.definitions import (
    Account,
    AllocationPolicy,
    DefinitionBase,
    Definitions,
    FixedCost,
    RecurringIncome,
    validate_no_overlap,
)
from domain.events import Event, EventVoided
from persistence.mapping import (
    DEFINITION_KIND_BY_MODEL,
    DEFINITION_MODEL_BY_KIND,
    DEFINITION_ROW_BY_KIND,
    DefinitionKind,
    DefinitionVersion,
    definition_to_values,
    event_to_values,
    row_to_definition,
    row_to_event,
    rows_to_events,
)
from persistence.models import EventRow, ReceiptBlobRow, table_for

#: Content types `store_receipt` accepts (CONTRACTS.md §8.8: anything else is
#: `UNSUPPORTED_MEDIA_TYPE`, HTTP 415). Kept here rather than in `ingestion/` because
#: this is the layer that would have to store the bytes.
ACCEPTED_RECEIPT_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/tiff",
        "application/pdf",
    }
)

#: The keyset a `GET /ledger` page resumes from: the canonical ledger order,
#: `(date, recorded_at, event_id)` (CONTRACTS.md §3.1).
type LedgerCursor = tuple[dt.date, dt.datetime, UUID]


def _dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


def _insert_ignoring_conflict(
    session: Session,
    table: Table,
    values: dict[str, Any],
    conflict_columns: Sequence[str],
    returning_column: Column[Any],
) -> Any | None:
    """`INSERT ... ON CONFLICT (cols) DO NOTHING RETURNING col`.

    Returns the returned value when a row was written, `None` when the insert was a
    no-op because a conflicting row already existed. Distinguishing the two is the whole
    point — it is what lets `append` report `deduplicated` truthfully without a
    read-then-write race, since the database decides.

    SQLite and PostgreSQL both spell this natively and both support `RETURNING` on it.
    Any other backend falls back to a `SAVEPOINT` plus `IntegrityError`, which is
    equivalent but costs a round trip; there is no such backend in this project today.
    """
    dialect = _dialect_name(session)
    if dialect == "sqlite":
        sqlite_stmt = (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
            .returning(returning_column)
        )
        return session.execute(sqlite_stmt).scalars().first()
    if dialect == "postgresql":
        pg_stmt = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
            .returning(returning_column)
        )
        return session.execute(pg_stmt).scalars().first()

    try:
        with session.begin_nested():
            session.execute(
                insert(table).values(**values).returning(returning_column)
            )
    except IntegrityError:
        return None
    return values[returning_column.key]


# ------------------------------------------------------------------------- events


class EventRepository:
    """The append-only ledger.

    Reads return `domain.events.Event` models in canonical order. `list_all()` is the
    read the projection uses: every read recomputes from genesis, so the full stream is
    the normal case rather than an unusual one (PLAN.md §3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- write ----------------------------------------------------------------

    def append(
        self, event: Event, *, receipt_blob_id: str | None = None
    ) -> tuple[UUID, bool]:
        """Append idempotently. Backs `ingestion.append_event` (CONTRACTS.md §8.8).

        Preconditions:
            event.dedupe_key is set and non-empty

        Postconditions:
            returns (event_id, deduplicated)
            deduplicated=True  -> nothing was written; event_id is the EXISTING row's
            deduplicated=False -> exactly one row was written
            never UPDATEs, never DELETEs
            appending the same event twice leaves the table and State unchanged

        Note the returned `event_id` on a duplicate is the **stored** row's, not the one
        on the event passed in: two attempts at the same event carry different
        `event_id`s by construction (a fresh UUID per attempt), and the ledger's answer
        is the one already written.

        Does not commit. The caller's transaction decides.
        """
        if not event.dedupe_key:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "dedupe_key must be set and non-empty before append",
                {"event_id": str(event.event_id), "event_type": event.event_type},
            )

        values = event_to_values(event, receipt_blob_id=receipt_blob_id)
        table = table_for(EventRow)
        inserted = _insert_ignoring_conflict(
            self._session,
            table,
            values,
            ("dedupe_key",),
            table.c.event_id,
        )
        if inserted is not None:
            written_id: UUID = inserted
            return (written_id, False)

        existing = self._session.execute(
            select(table.c.event_id).where(table.c.dedupe_key == event.dedupe_key)
        ).scalars().first()
        if existing is None:  # pragma: no cover - only reachable under a lost row
            raise AppError(
                ErrorCode.INTERNAL,
                "insert reported a conflict but no row carries the dedupe_key",
                {"dedupe_key": event.dedupe_key},
            )
        existing_id: UUID = existing
        return (existing_id, True)

    # -- read -----------------------------------------------------------------

    def get(self, event_id: UUID) -> Event | None:
        """The event with `event_id`, or None."""
        row = self._session.get(EventRow, event_id)
        return None if row is None else row_to_event(row)

    def get_by_dedupe_key(self, dedupe_key: str) -> Event | None:
        """The event carrying `dedupe_key`, or None. At most one can exist."""
        row = self._session.scalars(
            select(EventRow).where(EventRow.dedupe_key == dedupe_key)
        ).first()
        return None if row is None else row_to_event(row)

    def exists(self, event_id: UUID) -> bool:
        """Whether `event_id` names a stored event.

        The write-time check behind `UNKNOWN_EVENT` (CONTRACTS.md §7.1): a void whose
        target does not exist is a 404.
        """
        found = self._session.execute(
            select(EventRow.event_id).where(EventRow.event_id == event_id)
        ).first()
        return found is not None

    def find_void_for(self, target_event_id: UUID) -> EventVoided | None:
        """The `EventVoided` targeting `target_event_id`, or None.

        The write-time check behind `ALREADY_VOIDED` (409). Reading it rather than
        deriving it from a full projection keeps the void endpoint O(1).
        """
        row = self._session.scalars(
            select(EventRow).where(
                EventRow.event_type == "EventVoided",
                EventRow.target_event_id == target_event_id,
            )
        ).first()
        if row is None:
            return None
        event = row_to_event(row)
        if not isinstance(event, EventVoided):  # pragma: no cover - type tag is the filter
            raise AppError(
                ErrorCode.INTERNAL,
                "row tagged EventVoided did not reconstruct as one",
                {"event_id": str(row.event_id)},
            )
        return event

    def list_all(self) -> tuple[Event, ...]:
        """The whole ledger in canonical `(date, recorded_at, event_id)` order.

        This is the input to `project()`. Voided events and their `EventVoided` records
        are **included** — filtering them is step 1 of the fold, not a storage concern,
        and `GET /ledger` shows them with `is_voided: true` (CONTRACTS.md §6.2).
        """
        return self.list_events()

    def count(self) -> int:
        """How many events are stored."""
        return len(self._session.execute(select(EventRow.event_id)).all())

    def list_events(
        self,
        *,
        from_date: dt.date | None = None,
        to_date: dt.date | None = None,
        event_types: Sequence[str] | None = None,
        account_id: str | None = None,
        category: str | None = None,
        newest_first: bool = False,
        after: LedgerCursor | None = None,
        limit: int | None = None,
    ) -> tuple[Event, ...]:
        """Filtered, ordered slice of the ledger. Backs `GET /ledger`.

        `from_date` / `to_date` are both **inclusive** and filter on the business `date`,
        never on `recorded_at` — period membership is a business-date question
        (CONTRACTS.md §3.1). `ObligationRaised` is the one type whose period comes from
        `due_date` instead; that is a projection concern, and this filter deliberately
        does not special-case it, so a date window here means "entered for these days".

        `account_id` matches any of `account_id`, `from_account_id`, `to_account_id`, so
        a transfer shows up under both ends.

        `after` is a keyset cursor on `(date, recorded_at, event_id)` — the same triple
        the ordering uses, so pages neither skip nor repeat a row when an event is
        appended mid-pagination. The comparison follows `newest_first`: strictly after in
        ascending order, strictly before in descending. Expanded into three OR'd
        comparisons rather than a row-value comparison, which not every backend has.

        Postconditions:
            ascending by (date, recorded_at, event_id), or descending if newest_first
            total and stable — event_id makes ties impossible
        """
        stmt = select(EventRow)

        if from_date is not None:
            stmt = stmt.where(EventRow.date >= from_date)
        if to_date is not None:
            stmt = stmt.where(EventRow.date <= to_date)
        if event_types is not None:
            stmt = stmt.where(EventRow.event_type.in_(list(event_types)))
        if account_id is not None:
            stmt = stmt.where(
                or_(
                    EventRow.account_id == account_id,
                    EventRow.from_account_id == account_id,
                    EventRow.to_account_id == account_id,
                )
            )
        if category is not None:
            stmt = stmt.where(EventRow.category == category)

        if after is not None:
            cursor_date, cursor_recorded_at, cursor_event_id = after
            if newest_first:
                stmt = stmt.where(
                    or_(
                        EventRow.date < cursor_date,
                        and_(
                            EventRow.date == cursor_date,
                            EventRow.recorded_at < cursor_recorded_at,
                        ),
                        and_(
                            EventRow.date == cursor_date,
                            EventRow.recorded_at == cursor_recorded_at,
                            EventRow.event_id < cursor_event_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        EventRow.date > cursor_date,
                        and_(
                            EventRow.date == cursor_date,
                            EventRow.recorded_at > cursor_recorded_at,
                        ),
                        and_(
                            EventRow.date == cursor_date,
                            EventRow.recorded_at == cursor_recorded_at,
                            EventRow.event_id > cursor_event_id,
                        ),
                    )
                )

        if newest_first:
            stmt = stmt.order_by(
                EventRow.date.desc(),
                EventRow.recorded_at.desc(),
                EventRow.event_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                EventRow.date.asc(),
                EventRow.recorded_at.asc(),
                EventRow.event_id.asc(),
            )

        if limit is not None:
            stmt = stmt.limit(limit)

        return rows_to_events(self._session.scalars(stmt).all())


# -------------------------------------------------------------------- definitions


class DefinitionRepository:
    """Versioned, effective-dated definitions.

    `load_definitions()` returns **all** versions, not the currently-effective ones:
    `project()` resolves per period and per statement cycle, so handing it a
    pre-resolved view would silently pin every period to one policy and break the
    "closed periods are immune to policy change" property (PLAN.md §8.3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- read -----------------------------------------------------------------

    def load_definitions(self) -> Definitions:
        """The complete bundle `project()` takes.

        Four ordered reads, no join. Ordering is `(entity_id, effective_from,
        recorded_at, version_id)` — total, so the bundle is byte-identical across calls
        and `project()`'s determinism does not rest on the order rows came back in.
        """
        return Definitions(
            recurring_incomes=self.list_recurring_incomes(),
            fixed_costs=self.list_fixed_costs(),
            allocation_policies=self.list_allocation_policies(),
            accounts=self.list_accounts(),
        )

    def list_recurring_incomes(
        self, *, entity_id: str | None = None
    ) -> tuple[RecurringIncome, ...]:
        return self._list(DefinitionKind.RECURRING_INCOME, RecurringIncome, entity_id)

    def list_fixed_costs(
        self, *, entity_id: str | None = None
    ) -> tuple[FixedCost, ...]:
        return self._list(DefinitionKind.FIXED_COST, FixedCost, entity_id)

    def list_allocation_policies(
        self, *, entity_id: str | None = None
    ) -> tuple[AllocationPolicy, ...]:
        return self._list(DefinitionKind.ALLOCATION_POLICY, AllocationPolicy, entity_id)

    def list_accounts(self, *, entity_id: str | None = None) -> tuple[Account, ...]:
        return self._list(DefinitionKind.ACCOUNT, Account, entity_id)

    def list_versions(
        self, kind: DefinitionKind, *, entity_id: str | None = None
    ) -> tuple[DefinitionBase, ...]:
        """Kind-dispatched read for `GET /definitions/{kind}`.

        Returns the base type because the caller routed on a string; the four typed
        methods above exist for callers that know which kind they want.
        """
        model_cls = DEFINITION_MODEL_BY_KIND[kind]
        return self._list(kind, model_cls, entity_id)

    def get_version(
        self, kind: DefinitionKind, version_id: UUID
    ) -> DefinitionBase | None:
        """One version by id, or None."""
        row_table = self._table(kind)
        row = self._session.execute(
            select(row_table).where(row_table.c.version_id == version_id)
        ).mappings().first()
        if row is None:
            return None
        return row_to_definition(_AttrView(dict(row)), DEFINITION_MODEL_BY_KIND[kind])

    # -- write ----------------------------------------------------------------

    def add_version(self, version: DefinitionVersion) -> UUID:
        """Append a new version of a definition.

        Preconditions:
            the version's own invariants already hold — they are enforced on
            construction (`EFFECTIVE_RANGE_INVALID`, `POLICY_BPS_NOT_10000`), so an
            invalid one cannot reach here

        Postconditions:
            raises AppError(OVERLAPPING_VERSIONS) if it intersects an existing version
            of the same entity_id; nothing is written in that case
            never modifies an existing row — closing the prior version is a separate,
            explicit `close_version` call

        Non-overlap is checked with `domain.definitions.validate_no_overlap` over the
        existing versions plus this one, rather than reimplemented in SQL: one
        definition of "overlap" for the whole codebase, and the half-open convention
        (a version ending exactly where the next begins does *not* overlap) stays in
        one place. The table's `(entity_id, effective_from)` UNIQUE and the partial
        unique index on open-ended versions are the backstop if someone writes around
        this method.

        Deliberately does **not** auto-close the previous open version. Superseding is
        two decisions — when the old one stops and when the new one starts — and
        guessing the first from the second is how a one-day gap or a silent overwrite
        gets in. `api/` calls `close_version` then `add_version`.
        """
        kind = DEFINITION_KIND_BY_MODEL[type(version)]
        existing = self._list(
            kind, DEFINITION_MODEL_BY_KIND[kind], version.entity_id
        )
        validate_no_overlap([*existing, version])

        table = self._table(kind)
        try:
            with self._session.begin_nested():
                self._session.execute(
                    insert(table).values(**definition_to_values(version))
                )
        except IntegrityError as exc:
            raise AppError(
                ErrorCode.OVERLAPPING_VERSIONS,
                "a conflicting version of this entity already exists",
                {
                    "entity_id": version.entity_id,
                    "version_id": str(version.version_id),
                    "kind": kind.value,
                },
            ) from exc
        return version.version_id

    def close_version(
        self, kind: DefinitionKind, version_id: UUID, effective_to: dt.date
    ) -> None:
        """Set `effective_to` on an open version. THE ONLY `UPDATE` IN THIS CODEBASE.

        CLAUDE.md §4.3: "closing a version is the single permitted `UPDATE`, it touches
        only `effective_to`, and it goes through the repository method that exists for
        it". This is that method.

        Preconditions:
            version_id names a version of `kind` whose effective_to IS NULL
            effective_to > that version's effective_from

        Postconditions:
            exactly one row changes, and only its `effective_to` column
            closing can never create an overlap — it only shrinks a range

        Raises:
            AppError(VALIDATION_FAILED) if the version is unknown or already closed.
            Re-closing is not idempotent housekeeping; it would be a second mutation of
            a row that already has its final value, which is the thing §4.3 forbids.
            AppError(EFFECTIVE_RANGE_INVALID) if the result would be an empty or
            inverted range — the same rule `DefinitionBase` enforces on construction.
        """
        table = self._table(kind)
        current = self._session.execute(
            select(table.c.effective_from, table.c.effective_to).where(
                table.c.version_id == version_id
            )
        ).first()
        if current is None:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "no such definition version",
                {"kind": kind.value, "version_id": str(version_id)},
            )
        effective_from: dt.date = current[0]
        already_closed: dt.date | None = current[1]
        if already_closed is not None:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "definition version is already closed",
                {
                    "kind": kind.value,
                    "version_id": str(version_id),
                    "effective_to": already_closed.isoformat(),
                },
            )
        if effective_to <= effective_from:
            raise AppError(
                ErrorCode.EFFECTIVE_RANGE_INVALID,
                (
                    f"effective_to ({effective_to.isoformat()}) must be strictly after "
                    f"effective_from ({effective_from.isoformat()})"
                ),
                {
                    "kind": kind.value,
                    "version_id": str(version_id),
                    "effective_from": effective_from.isoformat(),
                    "effective_to": effective_to.isoformat(),
                },
            )

        self._session.execute(
            update(table)
            .where(table.c.version_id == version_id, table.c.effective_to.is_(None))
            .values(effective_to=effective_to)
        )

    # -- internals -------------------------------------------------------------

    def _table(self, kind: DefinitionKind) -> Table:
        return table_for(DEFINITION_ROW_BY_KIND[kind])

    def _list[T: DefinitionBase](
        self, kind: DefinitionKind, model_cls: type[T], entity_id: str | None
    ) -> tuple[T, ...]:
        table = self._table(kind)
        stmt = select(table)
        if entity_id is not None:
            stmt = stmt.where(table.c.entity_id == entity_id)
        stmt = stmt.order_by(
            table.c.entity_id.asc(),
            table.c.effective_from.asc(),
            table.c.recorded_at.asc(),
            table.c.version_id.asc(),
        )
        rows = self._session.execute(stmt).mappings().all()
        return tuple(row_to_definition(_AttrView(dict(row)), model_cls) for row in rows)


class _AttrView:
    """Attribute access over a result mapping.

    `row_to_definition` reads fields by name off an object, which keeps it usable with
    an ORM instance in a test. Selecting the `Table` rather than the mapped class avoids
    every `type[Base]` attribute-access dance in the four kind-dispatched reads, and this
    is the two-line adapter that reconciles the two.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)


# ----------------------------------------------------------------------- receipts


class ReceiptRepository:
    """Content-addressed receipt blobs (CONTRACTS.md §6.4, §8.8)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def store(self, blob: bytes, content_type: str) -> tuple[str, str]:
        """Persist receipt bytes. Backs `ingestion.store_receipt`.

        Preconditions:
            content_type is an accepted image or PDF type, else
            AppError(UNSUPPORTED_MEDIA_TYPE)

        Postconditions:
            returns (blob_id, sha256_hex)
            identical bytes always yield the identical sha256 and reuse the same blob

        Reuse is a `UNIQUE(content_sha256)` plus `ON CONFLICT DO NOTHING`, so two
        concurrent uploads of the same receipt converge on one blob rather than racing
        between a SELECT and an INSERT. `content_type` is stored from the first upload
        and not overwritten by a later one — the bytes are the identity, and rewriting
        the row would be an `UPDATE` for no gain.
        """
        normalized = content_type.split(";")[0].strip().lower()
        if normalized not in ACCEPTED_RECEIPT_CONTENT_TYPES:
            raise AppError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"content type {content_type!r} is not an accepted receipt type",
                {
                    "content_type": content_type,
                    "accepted": sorted(ACCEPTED_RECEIPT_CONTENT_TYPES),
                },
            )

        sha256_hex = hashlib.sha256(blob).hexdigest()
        table = table_for(ReceiptBlobRow)
        inserted = _insert_ignoring_conflict(
            self._session,
            table,
            {
                "blob_id": str(uuid4()),
                "content_sha256": sha256_hex,
                "content_type": normalized,
                "byte_size": len(blob),
                "content": blob,
            },
            ("content_sha256",),
            table.c.blob_id,
        )
        if inserted is not None:
            new_blob_id: str = inserted
            return (new_blob_id, sha256_hex)

        existing = self._session.execute(
            select(table.c.blob_id).where(table.c.content_sha256 == sha256_hex)
        ).scalars().first()
        if existing is None:  # pragma: no cover - only reachable under a lost row
            raise AppError(
                ErrorCode.INTERNAL,
                "insert reported a conflict but no blob carries the sha256",
                {"content_sha256": sha256_hex},
            )
        existing_blob_id: str = existing
        return (existing_blob_id, sha256_hex)

    def get(self, blob_id: str) -> ReceiptBlobRow | None:
        """The blob row, bytes included, or None."""
        return self._session.get(ReceiptBlobRow, blob_id)

    def find_by_sha256(self, content_sha256: str) -> ReceiptBlobRow | None:
        """The blob with these content bytes, or None. At most one can exist."""
        return self._session.scalars(
            select(ReceiptBlobRow).where(
                ReceiptBlobRow.content_sha256 == content_sha256
            )
        ).first()
