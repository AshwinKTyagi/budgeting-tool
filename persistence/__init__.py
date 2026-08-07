"""SQLAlchemy 2.0 models, Alembic migrations, repositories, uniqueness constraints.

Owned by `module/persistence`, which owns `persistence/**` and `alembic/**` (PLAN.md
§13.2). Two agents generating migrations produces two Alembic heads, which cannot be
resolved automatically — hence the single owner.

CONTRACTS.md §8 defines no stubs for `persistence/`, so this package's public surface is
designed here rather than transcribed. It is designed to fit the frozen surfaces above
it without amending them:

* `ingestion.append_event(session, event) -> tuple[UUID, bool]` is
  `EventRepository(session).append(event)`.
* `ingestion.store_receipt(blob, content_type) -> tuple[str, str]` — which takes no
  session — is `ReceiptRepository(session).store(blob, content_type)` inside
  `session_scope()`.
* `project(events, definitions, as_of_date)` takes `EventRepository(session).list_all()`
  and `DefinitionRepository(session).load_definitions()`.

Non-negotiable constraints this package is built under:
  * There is no DELETE in this codebase. No `session.delete()`, no `DROP`, no
    `TRUNCATE`, no `UPDATE` against the events table (CLAUDE.md §4.3).
  * The events table is immutable and append-only. `dedupe_key` is UNIQUE.
  * Closing a definition version by setting `effective_to` is the single permitted
    `UPDATE`, and it goes through the repository method that exists for it —
    `DefinitionRepository.close_version`.
  * Nothing derived is stored. No balance column, no period rollup, no cached state.
    Every read recomputes from genesis (PLAN.md §3).

Layout:
  base.py          `Base`, the metadata naming convention, the `UtcDateTime` column type
  models.py        the six tables
  mapping.py       row <-> frozen domain model, both directions, lossless
  repositories.py  `EventRepository`, `DefinitionRepository`, `ReceiptRepository`
  engine.py        engine/session construction and `session_scope()`
"""

from persistence.base import NAMING_CONVENTION, Base, UtcDateTime
from persistence.engine import (
    DATABASE_URL_ENV,
    DEFAULT_DATABASE_URL,
    configure_engine,
    create_db_engine,
    create_session_factory,
    get_engine,
    get_session_factory,
    reset_engine,
    session_scope,
)
from persistence.mapping import (
    DefinitionKind,
    DefinitionVersion,
    definition_to_values,
    event_to_values,
    row_to_definition,
    row_to_event,
    rows_to_events,
)
from persistence.models import (
    AccountRow,
    AllocationPolicyRow,
    EventRow,
    FixedCostRow,
    ReceiptBlobRow,
    RecurringIncomeRow,
)
from persistence.repositories import (
    ACCEPTED_RECEIPT_CONTENT_TYPES,
    DefinitionRepository,
    EventRepository,
    LedgerCursor,
    ReceiptRepository,
)

__all__ = [
    "ACCEPTED_RECEIPT_CONTENT_TYPES",
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "NAMING_CONVENTION",
    "AccountRow",
    "AllocationPolicyRow",
    "Base",
    "DefinitionKind",
    "DefinitionRepository",
    "DefinitionVersion",
    "EventRepository",
    "EventRow",
    "FixedCostRow",
    "LedgerCursor",
    "ReceiptBlobRow",
    "ReceiptRepository",
    "RecurringIncomeRow",
    "UtcDateTime",
    "configure_engine",
    "create_db_engine",
    "create_session_factory",
    "definition_to_values",
    "event_to_values",
    "get_engine",
    "get_session_factory",
    "reset_engine",
    "row_to_definition",
    "row_to_event",
    "rows_to_events",
    "session_scope",
]
