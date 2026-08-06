"""Ingestion: the `IngestionSource` seam, receipt hashing, idempotent append.

Owned by `module/ingestion` (PLAN.md §13.2).

Ingestion sits behind a protocol so that a future bank/card provider is just another
implementation producing the same canonical events as a receipt upload. Nothing in
`core/` or `domain/` changes when a provider is added — that is the whole purpose of the
seam (PLAN.md §9).

Duplicate ingestion is NOT an error. Re-uploading an identical receipt returns 200 with
`deduplicated: true` and the existing `event_id` (CONTRACTS.md §6.1, §7.1).
"""
