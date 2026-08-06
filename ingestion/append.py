"""Idempotent append (CONTRACTS.md §8.8)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from domain.events import Event


def append_event(session: Session, event: Event) -> tuple[UUID, bool]:
    """Append idempotently.

    Implementation: INSERT ... ON CONFLICT (dedupe_key) DO NOTHING.

    Preconditions:
        event.dedupe_key is set and non-empty

    Postconditions:
        returns (event_id, deduplicated)
        deduplicated=True  -> nothing was written; event_id is the EXISTING row's
        deduplicated=False -> exactly one row was written
        never UPDATEs, never DELETEs
        appending the same event twice leaves the table and State unchanged
    """
    raise NotImplementedError
