"""Receipt blob storage and content hashing (CONTRACTS.md §8.8, §6.4).

Owned by `module/ingestion` (PLAN.md §13.2).

The receipt path is the reason `dedupe_key` has a content-hash form at all: the bytes
are the identity of the purchase, so re-uploading the same photo is a no-op decided by
`sha256`, not by whether the user remembers having uploaded it. CONTRACTS.md §6.1 is
explicit that this returns 200 with `deduplicated: true` and "must not be reported as"
an error.

**The session question.** `store_receipt(blob, content_type)` is a frozen signature with
no `Session` parameter (CONTRACTS.md §8.8) while `ReceiptRepository.store` needs one.
Phase 2 anticipated this and put the answer in `persistence.engine.session_scope()` —
see that module's docstring, which names `ingestion.store_receipt` as its caller. So
`store_receipt` opens its own transaction and commits it.

That is correct for a bare blob upload and *wrong* for `POST /receipts`, which must
write two rows — the blob and the `ExpenseRecorded` that references it via the
`events.receipt_blob_id` foreign key. Two transactions there would let the event fail
after the blob committed. `ingest_receipt` is the two-row path and takes the caller's
session, so `api/` commits both together. Both functions delegate to the same
repository method, so there is still exactly one implementation of "store a blob".
"""

from __future__ import annotations

import datetime as dt
import hashlib
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.types import MONEY_MODEL_CONFIG, Minor, UtcInstant
from domain.events import Event
from ingestion.append import normalize_event
from persistence.engine import session_scope
from persistence.repositories import EventRepository, ReceiptRepository


def content_sha256(blob: bytes) -> str:
    """The lowercase hex SHA-256 of `blob` — the receipt's content address.

    One definition, used by both the dedupe key (`receipt:{sha256}`, CONTRACTS.md §3.1)
    and the `receipt_blobs.content_sha256` unique constraint. A source can compute the
    key it *will* get without touching the database, which is what lets
    `ReceiptUploadSource.fetch` be a pure function of the upload.
    """
    return hashlib.sha256(blob).hexdigest()


def store_receipt(blob: bytes, content_type: str) -> tuple[str, str]:
    """Persist a receipt blob.

    Preconditions:
        content_type is an accepted image or PDF type, else
        AppError(UNSUPPORTED_MEDIA_TYPE)

    Postconditions:
        returns (blob_id, sha256_hex)
        identical bytes always yield the identical sha256 and reuse the same blob

    Opens and commits its own transaction via `persistence.engine.session_scope()`,
    because the signature carries no session. A caller that already holds one — every
    caller that also appends an event — must use `store_receipt_in_session` or
    `ingest_receipt` instead, so the blob and the event share a transaction.
    """
    with session_scope() as session:
        return store_receipt_in_session(session, blob, content_type)


def store_receipt_in_session(
    session: Session, blob: bytes, content_type: str
) -> tuple[str, str]:
    """`store_receipt` in the caller's transaction. Same pre/postconditions.

    Does not commit.
    """
    return ReceiptRepository(session).store(blob, content_type)


class ReceiptUpload(BaseModel):
    """The `POST /receipts` multipart form, as a value (CONTRACTS.md §6.4).

    The fields are that table verbatim, plus the two the seam needs and HTTP does not
    carry: `recorded_at`, because nothing below `api/` may read a clock (CLAUDE.md
    §4.4), and `event_id`, which is server-assigned when omitted (§6.1) and pinned only
    by tests that need a literal id.

    There is no OCR in this scope — §6.4 says so explicitly — so `amount_minor` is a
    required integer supplied by the user, and strict mode is what rejects the `19.99`
    a form field is one careless parse away from producing.
    """

    model_config = MONEY_MODEL_CONFIG

    blob: bytes
    content_type: str
    date: dt.date
    amount_minor: Minor
    category: str
    account_id: str
    recorded_at: UtcInstant
    merchant: str | None = None
    note: str | None = None
    event_id: UUID | None = None

    def to_event(self, sha256_hex: str) -> Event:
        """The canonical `ExpenseRecorded` this upload becomes.

        Postconditions:
            dedupe_key == f"receipt:{sha256_hex}"
            a pure function of the upload and the hash — no database, no clock

        A receipt is discretionary spending, so it is an `ExpenseRecorded` and nothing
        else. `account_id` decides whether it hits discretionary now or at statement
        payment; that is the projection's call under the account's `budget_timing`
        (PLAN.md §6.4), and ingestion neither knows nor asks.
        """
        return normalize_event(
            {
                "event_type": "ExpenseRecorded",
                "date": self.date,
                "amount_minor": self.amount_minor,
                "category": self.category,
                "account_id": self.account_id,
                "merchant": self.merchant,
                "note": self.note,
            },
            recorded_at=self.recorded_at,
            event_id=self.event_id,
            content_sha256=sha256_hex,
        )


class ReceiptIngestResult(BaseModel):
    """The outcome of one receipt upload. The four fields of `ReceiptUploadResponse`
    (CONTRACTS.md §6.4), which `api/` serializes."""

    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    blob_id: str
    content_sha256: str
    deduplicated: bool


def ingest_receipt(session: Session, upload: ReceiptUpload) -> ReceiptIngestResult:
    """Store the bytes and append the expense they document. Backs `POST /receipts`.

    Preconditions:
        upload.content_type is an accepted image or PDF type, else
        AppError(UNSUPPORTED_MEDIA_TYPE)

    Postconditions:
        returns (event_id, blob_id, content_sha256, deduplicated)
        re-uploading identical bytes reuses the blob, writes no event, and reports
        deduplicated=True with the EXISTING event_id — a 200 no-op, not an error
        never UPDATEs, never DELETEs

    Blob first, then event, and the order is forced: `events.receipt_blob_id` is a
    foreign key into `receipt_blobs`, so the reverse order cannot be written. The two
    statements share the caller's transaction, so a rejected event rolls the blob back
    with it rather than orphaning bytes.

    `deduplicated` is the *event's*, not the blob's. The two can disagree in one
    direction: a blob stored earlier by a bare `store_receipt` call is reused while the
    event is genuinely new, and reporting that as a dedupe would tell the caller nothing
    was written when a ledger row was. The event is the answer the caller asked for.
    """
    blob_id, sha256_hex = store_receipt_in_session(
        session, upload.blob, upload.content_type
    )
    event = upload.to_event(sha256_hex)
    event_id, deduplicated = EventRepository(session).append(
        event, receipt_blob_id=blob_id
    )
    return ReceiptIngestResult(
        event_id=event_id,
        blob_id=blob_id,
        content_sha256=sha256_hex,
        deduplicated=deduplicated,
    )
