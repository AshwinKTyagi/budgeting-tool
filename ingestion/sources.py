"""The `IngestionSource` seam (CONTRACTS.md §8.8, PLAN.md §9).

Owned by `module/ingestion` (PLAN.md §13.2).

PLAN.md §9 states the point: live bank/card aggregation is a deliberate non-goal, but
staying ready for it is architecturally cheap, and the price of readiness is this
protocol. A provider is another implementation of `fetch`, producing the same canonical
events a receipt upload produces. **Nothing in `core/` or `domain/` changes when one is
added** — that is the whole purpose of the seam, and the two implementations below exist
so the shape is exercised rather than merely declared.

`fetch` takes `since` and no clock, like everything else below `api/` (CLAUDE.md §4.4).
A future provider will interpret it as "transactions posted on or after this business
date"; the two local sources interpret it identically, against `Event.date`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from core.types import UtcInstant
from domain.events import Event
from ingestion.append import AppendResult, append_events, normalize_event
from ingestion.receipts import ReceiptUpload, content_sha256


class IngestionSource(Protocol):
    """A producer of canonical events. Receipt upload and manual entry implement
    this today; a bank/card aggregator would implement it unchanged (PLAN.md §9).
    """

    def fetch(self, since: dt.date) -> Sequence[Event]: ...


def ingest(
    session: Session, source: IngestionSource, since: dt.date
) -> tuple[AppendResult, ...]:
    """Append everything `source` has produced since `since`. Idempotent throughout.

    Preconditions:
        none beyond the source's own — a source that yields nothing is not an error

    Postconditions:
        one AppendResult per fetched event, in the source's order
        running it twice over an unchanged source writes nothing the second time and
        reports deduplicated=True for every event
        never UPDATEs, never DELETEs

    Does not commit; the caller's transaction decides. This is the generic path and it
    appends events only — a source whose events reference stored bytes (a receipt) must
    go through `ingest_receipt`, which writes the blob first because the foreign key
    demands it. `ReceiptUploadSource` exists to show that the events the two paths
    produce are the same events, not to bypass the blob.
    """
    return append_events(session, source.fetch(since))


# --------------------------------------------------------------------- manual entry


@dataclass(frozen=True)
class ManualEntry:
    """One hand-entered event, before it has a key or an id.

    A plain frozen dataclass rather than a model: `payload` is opaque here and is
    validated — strictly, against the discriminated union — by `normalize_event`. A
    second validation pass over it at this level would be a second place for the money
    rules to be stated, and the weaker of the two would be the one that ran first.
    """

    payload: Mapping[str, object]
    #: `UtcInstant` rather than a bare `dt.datetime` per CLAUDE.md §4.5. On a dataclass
    #: the annotation does not itself validate — `normalize_event` is what rejects a
    #: naive value, one call later — but the rule is unconditional and the annotation is
    #: how a reader knows a naive datetime is not accepted here either.
    recorded_at: UtcInstant
    event_id: UUID | None = None
    #: Appended to the manual dedupe key so a genuinely-duplicate entry survives
    #: (CONTRACTS.md §3.1, §6.1). Two identical $4.50 coffees on the same day collide
    #: by design; this is how the user says "no, there really were two".
    client_nonce: str | None = None


@dataclass(frozen=True)
class ManualEntrySource:
    """Hand-entered events. The source `POST /events` is (CONTRACTS.md §6.1)."""

    entries: Sequence[ManualEntry] = field(default_factory=tuple)

    def fetch(self, since: dt.date) -> Sequence[Event]:
        """The entries dated on or after `since`, in canonical ledger order.

        Postconditions:
            every returned event carries a non-empty dedupe_key
            ordered by (date, recorded_at, event_id) — CONTRACTS.md §3.1
            pure: no database, no clock

        Ordering here is a convenience, not a correctness requirement: `project()` sorts
        its own input and property 6 requires the result to be independent of arrival
        order anyway. It costs nothing and makes a batch's `AppendResult` sequence
        readable.
        """
        events = [
            normalize_event(
                entry.payload,
                recorded_at=entry.recorded_at,
                event_id=entry.event_id,
                client_nonce=entry.client_nonce,
            )
            for entry in self.entries
        ]
        return _in_ledger_order(event for event in events if event.date >= since)


# -------------------------------------------------------------------- receipt upload


@dataclass(frozen=True)
class ReceiptUploadSource:
    """Receipt uploads, viewed through the seam.

    The events this yields are exactly the events `ingest_receipt` appends — same type,
    same fields, same `receipt:{sha256}` key — because both go through
    `ReceiptUpload.to_event`. That equivalence is the seam's actual claim (PLAN.md §9:
    a provider produces "the same events as a receipt upload"), and it is asserted in
    the test suite rather than left as a comment.

    It does not store bytes. `ingest_receipt` does, in the order the foreign key
    requires; this type is the read-side view of the same uploads.
    """

    uploads: Sequence[ReceiptUpload] = field(default_factory=tuple)

    def fetch(self, since: dt.date) -> Sequence[Event]:
        """The uploads dated on or after `since`, in canonical ledger order.

        Postconditions:
            dedupe_key == f"receipt:{sha256(blob)}" for every returned event
            pure: hashes the bytes it was handed, touches no database
        """
        return _in_ledger_order(
            upload.to_event(content_sha256(upload.blob))
            for upload in self.uploads
            if upload.date >= since
        )


def _in_ledger_order(events: Iterable[Event]) -> tuple[Event, ...]:
    """Sort by `(date, recorded_at, event_id)` — total and stable (CONTRACTS.md §3.1).

    `event_id` makes ties impossible, so the order is a function of the event set alone.
    """
    return tuple(
        sorted(events, key=lambda event: (event.date, event.recorded_at, event.event_id))
    )
