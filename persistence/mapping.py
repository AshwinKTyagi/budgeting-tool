"""Row <-> model translation. The only place that knows a column name.

Owned by `module/persistence` (PLAN.md §13.2).

The contract this module has to hold is exact losslessness: for every event type,
`row_to_event(row_from(event_to_values(event))) == event`. Not "equivalent", not
"equal after normalization" — equal, because the projection is a pure fold over these
models and any field the database quietly altered would change every period after it.

Two decisions make that provable rather than hoped for:

* **Field names are the mapping.** Column keys are derived from
  `model_fields` at import time, not written out by hand, so a field that gained a
  column but not a mapping entry cannot exist. The one place a name is translated —
  `date` -> the `event_date` column — is read off the mapper rather than hardcoded.
* **Reconstruction goes through the discriminated union.** `row_to_event` validates
  through `TypeAdapter(Event)` keyed on the stored `event_type`, so the tag is exercised
  on every read and a row whose columns do not form a valid event of its own type raises
  instead of loading. Validation on read is not redundant with validation on write: it
  is what makes a hand-edited or partially-migrated row loud.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, get_args

from pydantic import TypeAdapter

from domain.definitions import (
    Account,
    AllocationPolicy,
    DefinitionBase,
    FixedCost,
    RecurringIncome,
)
from domain.events import Event, EventBase, ExternalRef
from persistence.base import Base
from persistence.models import (
    AccountRow,
    AllocationPolicyRow,
    EventRow,
    FixedCostRow,
    RecurringIncomeRow,
)


class DefinitionKind(StrEnum):
    """The four definition kinds, spelled as CONTRACTS.md §6.2 spells them.

    The values are the `/definitions/{kind}` path segments verbatim
    (`recurring-income | fixed-cost | allocation-policy | account`) so `api/` can route
    on them without a second translation table, which is where the two would drift.
    """

    RECURRING_INCOME = "recurring-income"
    FIXED_COST = "fixed-cost"
    ALLOCATION_POLICY = "allocation-policy"
    ACCOUNT = "account"


#: The concrete definition models, as a union. Every repository read and write of a
#: definition version is typed against this.
type DefinitionVersion = RecurringIncome | FixedCost | AllocationPolicy | Account

DEFINITION_MODEL_BY_KIND: dict[DefinitionKind, type[DefinitionBase]] = {
    DefinitionKind.RECURRING_INCOME: RecurringIncome,
    DefinitionKind.FIXED_COST: FixedCost,
    DefinitionKind.ALLOCATION_POLICY: AllocationPolicy,
    DefinitionKind.ACCOUNT: Account,
}

DEFINITION_ROW_BY_KIND: dict[DefinitionKind, type[Base]] = {
    DefinitionKind.RECURRING_INCOME: RecurringIncomeRow,
    DefinitionKind.FIXED_COST: FixedCostRow,
    DefinitionKind.ALLOCATION_POLICY: AllocationPolicyRow,
    DefinitionKind.ACCOUNT: AccountRow,
}

DEFINITION_KIND_BY_MODEL: dict[type[DefinitionBase], DefinitionKind] = {
    model: kind for kind, model in DEFINITION_MODEL_BY_KIND.items()
}


# ------------------------------------------------------------------------- events

#: The concrete event classes, read off the discriminated union rather than listed.
#: `Event` is `Annotated[A | B | ..., Field(discriminator=...)]`, so the first arg is
#: the union and its args are the classes.
EVENT_CLASSES: tuple[type[EventBase], ...] = get_args(get_args(Event)[0])

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _event_type_of(event_cls: type[EventBase]) -> str:
    """The discriminator value declared on `event_cls`."""
    return str(event_cls.model_fields["event_type"].default)


EVENT_CLASS_BY_TYPE: dict[str, type[EventBase]] = {
    _event_type_of(event_cls): event_cls for event_cls in EVENT_CLASSES
}

#: `external_ref` is the one field that is not a column: it is a nested model, flattened
#: into `external_ref_provider` / `external_ref_provider_txn_id`.
_NESTED_FIELDS = frozenset({"external_ref"})


def _union_of_event_fields() -> frozenset[str]:
    names: set[str] = set()
    for event_cls in EVENT_CLASSES:
        names.update(event_cls.model_fields)
    return frozenset(names - _NESTED_FIELDS)


#: Every scalar field of every event type. A column exists for each; the test suite
#: asserts that, so this set and the table cannot diverge.
EVENT_SCALAR_FIELDS: frozenset[str] = _union_of_event_fields()

#: Mapped-attribute name -> column name. Identity for all but `date` -> `event_date`.
#: Read off the mapper so the exception stays true if it ever moves.
EVENT_COLUMN_BY_FIELD: dict[str, str] = {
    field: EventRow.__mapper__.columns[field].name for field in EVENT_SCALAR_FIELDS
}

EVENT_EXTERNAL_REF_COLUMNS = (
    "external_ref_provider",
    "external_ref_provider_txn_id",
)


def event_to_values(
    event: Event, *, receipt_blob_id: str | None = None
) -> dict[str, Any]:
    """Flatten `event` into an INSERT payload keyed by **column name**.

    Column names rather than attribute names because the payload goes to a Core /
    dialect-specific `insert()`, where `on_conflict_do_nothing(index_elements=...)` also
    speaks column names. Mixing the two vocabularies in one statement is how the
    `date` / `event_date` distinction turns into a silent NULL.

    Fields the event's own type does not declare are set to NULL explicitly, so every
    INSERT names the same columns and a stale default cannot leak in.

    `receipt_blob_id` is provenance outside the domain model (CONTRACTS.md §6.4) and is
    write-once; it never comes back out through `row_to_event`.
    """
    values: dict[str, Any] = {
        EVENT_COLUMN_BY_FIELD[field]: None for field in EVENT_SCALAR_FIELDS
    }
    for field in type(event).model_fields:
        if field in _NESTED_FIELDS:
            continue
        values[EVENT_COLUMN_BY_FIELD[field]] = getattr(event, field)

    external_ref = event.external_ref
    values["external_ref_provider"] = (
        None if external_ref is None else external_ref.provider
    )
    values["external_ref_provider_txn_id"] = (
        None if external_ref is None else external_ref.provider_txn_id
    )
    values["receipt_blob_id"] = receipt_blob_id
    return values


def row_to_event(row: EventRow) -> Event:
    """Reconstruct the exact event model a row was written from.

    Postconditions:
        row_to_event(insert(event_to_values(e))) == e, for every event type
        the returned model is the concrete class named by `row.event_type`

    Raises:
        KeyError for an unrecognized `event_type` — a row written by a schema version
        this code does not know about, which must not be guessed at.
        pydantic.ValidationError / AppError if the row's columns do not form a valid
        event of its type (e.g. a `PaymentMade` whose split does not reconcile).
    """
    event_cls = EVENT_CLASS_BY_TYPE[row.event_type]
    payload: dict[str, Any] = {
        field: getattr(row, field)
        for field in event_cls.model_fields
        if field not in _NESTED_FIELDS
    }
    if row.external_ref_provider is not None:
        # Constructed rather than passed as a dict: strict mode's treatment of a dict
        # for a nested model is not something to depend on at a storage boundary.
        payload["external_ref"] = ExternalRef(
            provider=row.external_ref_provider,
            provider_txn_id=str(row.external_ref_provider_txn_id),
        )
    return _EVENT_ADAPTER.validate_python(payload)


def rows_to_events(rows: Iterable[EventRow]) -> tuple[Event, ...]:
    """`row_to_event` over a result set, order preserved."""
    return tuple(row_to_event(row) for row in rows)


# -------------------------------------------------------------------- definitions


def definition_to_values(version: DefinitionBase) -> dict[str, Any]:
    """Flatten a definition version into an INSERT payload.

    Definition tables name their columns exactly as the models name their fields — there
    is no `date`-shaped exception — so attribute names and column names coincide here.
    """
    return {field: getattr(version, field) for field in type(version).model_fields}


def row_to_definition[T: DefinitionBase](row: object, model_cls: type[T]) -> T:
    """Reconstruct a definition version from its row.

    Validation runs on the way out for the same reason it does for events: an invalid
    stored version (an inverted effective range, a policy whose bps do not total
    10_000) must raise rather than reach `project()`.
    """
    payload: dict[str, Any] = {
        field: getattr(row, field) for field in model_cls.model_fields
    }
    return model_cls.model_validate(payload)
