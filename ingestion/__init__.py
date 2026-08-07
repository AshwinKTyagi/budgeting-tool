"""Ingestion: the `IngestionSource` seam, receipt hashing, idempotent append.

Owned by `module/ingestion` (PLAN.md §13.2).

Ingestion sits behind a protocol so that a future bank/card provider is just another
implementation producing the same canonical events as a receipt upload. Nothing in
`core/` or `domain/` changes when a provider is added — that is the whole purpose of the
seam (PLAN.md §9).

Duplicate ingestion is NOT an error. Re-uploading an identical receipt returns 200 with
`deduplicated: true` and the existing `event_id` (CONTRACTS.md §6.1, §7.1).

What this package is, in one line: steps 1-3 of PLAN.md §3 — normalize a payload to a
canonical event, compute its `dedupe_key`, and hand it to the one place that spells
`INSERT ... ON CONFLICT DO NOTHING`. Step 3 is `persistence.EventRepository.append` and
is *delegated to*, never reimplemented: two answers to idempotency would be one too
many, and the database is the one that can decide without a read-then-write race.

Layout:
  append.py    `normalize_event`, `append_event`, `append_events`, `AppendResult`
  receipts.py  content hashing, `store_receipt`, `ReceiptUpload`, `ingest_receipt`
  sources.py   the `IngestionSource` protocol, its two implementations, `ingest`

No clock is read anywhere in this package. Every `recorded_at` arrives as a parameter,
threaded from `api/` (CLAUDE.md §4.4, CONTRACTS.md §6.3).
"""

from ingestion.append import (
    AppendResult,
    append_event,
    append_events,
    normalize_event,
)
from ingestion.receipts import (
    ReceiptIngestResult,
    ReceiptUpload,
    content_sha256,
    ingest_receipt,
    store_receipt,
    store_receipt_in_session,
)
from ingestion.sources import (
    IngestionSource,
    ManualEntry,
    ManualEntrySource,
    ReceiptUploadSource,
    ingest,
)

__all__ = [
    "AppendResult",
    "IngestionSource",
    "ManualEntry",
    "ManualEntrySource",
    "ReceiptIngestResult",
    "ReceiptUpload",
    "ReceiptUploadSource",
    "append_event",
    "append_events",
    "content_sha256",
    "ingest",
    "ingest_receipt",
    "normalize_event",
    "store_receipt",
    "store_receipt_in_session",
]
