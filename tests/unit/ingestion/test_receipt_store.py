"""Receipts are content-addressed, and re-uploading identical bytes is a 200 no-op.

CONTRACTS.md §8.8: "identical bytes always yield the identical sha256 and reuse the same
blob". §6.4 adds the consequence: `POST /receipts` with bytes already seen returns 200
with `deduplicated: true` and the existing `event_id`, and §7.1 says in as many words
that this "is not an error and must not be reported as one".

The blob-level facts are also checked in `tests/unit/persistence/test_receipts.py`. What
is checked here is the two-row path that only exists at this layer: bytes and the
`ExpenseRecorded` that references them, written in the order the foreign key forces and
committed together.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import ExpenseRecorded
from ingestion import (
    ReceiptUpload,
    content_sha256,
    ingest_receipt,
    store_receipt,
    store_receipt_in_session,
)
from persistence.engine import create_session_factory
from persistence.models import EventRow, ReceiptBlobRow
from persistence.repositories import EventRepository

UTC = dt.timezone.utc
RECORDED_AT = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
RECEIPT = b"%PDF-1.7 fake receipt bytes"
OTHER = b"%PDF-1.7 a different receipt"


def uid(n: int) -> UUID:
    return UUID(int=n)


def upload(
    blob: bytes = RECEIPT,
    *,
    content_type: str = "application/pdf",
    amount_minor: int = 4_599,
    date: dt.date = dt.date(2026, 5, 1),
    event_id: UUID | None = None,
) -> ReceiptUpload:
    return ReceiptUpload(
        blob=blob,
        content_type=content_type,
        date=date,
        amount_minor=amount_minor,
        category="groceries",
        account_id="visa",
        recorded_at=RECORDED_AT,
        merchant="Corner Store",
        event_id=event_id,
    )


# ------------------------------------------------------------------ content address


def test_the_hash_is_of_the_content_and_nothing_else() -> None:
    """`dedupe_key = f"receipt:{sha256}"` is built from this, so a digest over the
    bytes *plus* a filename would make the same receipt uploadable twice."""
    assert content_sha256(RECEIPT) == hashlib.sha256(RECEIPT).hexdigest()
    assert len(content_sha256(RECEIPT)) == 64
    assert content_sha256(RECEIPT) != content_sha256(OTHER)
    assert content_sha256(b"") == hashlib.sha256(b"").hexdigest()


def test_identical_bytes_yield_the_same_sha_and_the_same_blob(
    session: Session,
) -> None:
    """Reuse is the UNIQUE constraint, not an application convention."""
    first_blob, first_sha = store_receipt_in_session(session, RECEIPT, "application/pdf")
    second_blob, second_sha = store_receipt_in_session(session, RECEIPT, "image/png")

    assert first_sha == second_sha == content_sha256(RECEIPT)
    assert first_blob == second_blob
    assert len(session.scalars(select(ReceiptBlobRow)).all()) == 1


def test_different_bytes_get_different_blobs(session: Session) -> None:
    first_blob, first_sha = store_receipt_in_session(session, RECEIPT, "application/pdf")
    second_blob, second_sha = store_receipt_in_session(session, OTHER, "application/pdf")

    assert first_sha != second_sha
    assert first_blob != second_blob
    assert len(session.scalars(select(ReceiptBlobRow)).all()) == 2


def test_the_content_type_of_the_first_upload_is_kept(session: Session) -> None:
    """The bytes are the identity; rewriting the row would be an UPDATE for no gain."""
    blob_id, _ = store_receipt_in_session(session, RECEIPT, "application/pdf")
    store_receipt_in_session(session, RECEIPT, "image/png")

    row = session.get(ReceiptBlobRow, blob_id)
    assert row is not None
    assert row.content_type == "application/pdf"


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/zip", "application/octet-stream", "", "image"],
)
def test_an_unsupported_media_type_is_rejected(
    session: Session, content_type: str
) -> None:
    """CONTRACTS.md §7.1: UNSUPPORTED_MEDIA_TYPE, HTTP 415. Nothing is written."""
    with pytest.raises(AppError) as caught:
        store_receipt_in_session(session, RECEIPT, content_type)

    assert caught.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert session.scalars(select(ReceiptBlobRow)).all() == []


def test_a_content_type_with_parameters_is_accepted(session: Session) -> None:
    """`multipart/form-data` routinely delivers `image/jpeg; charset=binary`."""
    blob_id, _ = store_receipt_in_session(session, RECEIPT, "image/jpeg; charset=binary")
    row = session.get(ReceiptBlobRow, blob_id)
    assert row is not None
    assert row.content_type == "image/jpeg"


# --------------------------------------------------------------------- the session
# `store_receipt` takes no Session (CONTRACTS.md §8.8) and reaches the database through
# `persistence.engine.session_scope()`, which commits. These two tests are the ones that
# actually exercise that resolution.


def test_store_receipt_opens_and_commits_its_own_transaction(
    process_engine: Engine,
) -> None:
    """Committed, so a *separate* session can see it."""
    blob_id, sha256_hex = store_receipt(RECEIPT, "application/pdf")

    factory = create_session_factory(process_engine)
    with factory() as observer:
        row = observer.get(ReceiptBlobRow, blob_id)
        assert row is not None
        assert row.content_sha256 == sha256_hex
        assert row.content == RECEIPT
        assert row.byte_size == len(RECEIPT)


def test_store_receipt_is_idempotent_across_transactions(
    process_engine: Engine,
) -> None:
    first_blob, first_sha = store_receipt(RECEIPT, "application/pdf")
    second_blob, second_sha = store_receipt(RECEIPT, "application/pdf")

    assert (first_blob, first_sha) == (second_blob, second_sha)
    factory = create_session_factory(process_engine)
    with factory() as observer:
        assert len(observer.scalars(select(ReceiptBlobRow)).all()) == 1


# -------------------------------------------------------------------- the two rows


def test_uploading_writes_the_blob_and_the_expense(session: Session) -> None:
    """§6.4: store the blob, append an `ExpenseRecorded` carrying a reference to it."""
    result = ingest_receipt(session, upload(event_id=uid(1)))

    assert result.deduplicated is False
    assert result.event_id == uid(1)
    assert result.content_sha256 == content_sha256(RECEIPT)
    assert result.blob_id != result.content_sha256  # §6.4 carries both; distinct values

    event = EventRepository(session).get(result.event_id)
    assert isinstance(event, ExpenseRecorded)
    assert event.dedupe_key == f"receipt:{content_sha256(RECEIPT)}"
    assert event.amount_minor == 4_599
    assert event.category == "groceries"
    assert event.account_id == "visa"
    assert event.merchant == "Corner Store"
    assert event.date == dt.date(2026, 5, 1)
    assert event.recorded_at == RECORDED_AT

    row = session.get(EventRow, result.event_id)
    assert row is not None
    assert row.receipt_blob_id == result.blob_id


def test_re_uploading_identical_bytes_is_a_no_op(session: Session) -> None:
    """One blob, one event, `deduplicated=True`, and the EXISTING event id.

    Not an error (CONTRACTS.md §7.1), and the ledger is byte-for-byte unchanged.
    """
    first = ingest_receipt(session, upload(event_id=uid(1)))
    before = EventRepository(session).list_all()
    second = ingest_receipt(session, upload(event_id=uid(2)))
    after = EventRepository(session).list_all()

    assert second.deduplicated is True
    assert second.event_id == first.event_id == uid(1)
    assert second.blob_id == first.blob_id
    assert second.content_sha256 == first.content_sha256
    assert before == after
    assert len(after) == 1
    assert len(session.scalars(select(ReceiptBlobRow)).all()) == 1


def test_the_same_bytes_with_different_form_fields_still_dedupe(
    session: Session,
) -> None:
    """The bytes are the identity. §3.1 gives receipts a content-hash key, so the same
    photo submitted with a corrected amount is the same event and the second submission
    changes nothing.

    That is the documented behaviour and not an oversight: a correction is
    `EventVoided` plus a re-raise (PLAN.md §8.4), never a second write that silently
    wins.
    """
    ingest_receipt(session, upload(amount_minor=4_599, event_id=uid(1)))
    second = ingest_receipt(session, upload(amount_minor=9_999, event_id=uid(2)))

    stored = EventRepository(session).list_all()
    assert second.deduplicated is True
    assert len(stored) == 1
    event = stored[0]
    assert isinstance(event, ExpenseRecorded)
    assert event.amount_minor == 4_599


def test_different_receipts_are_different_events(session: Session) -> None:
    first = ingest_receipt(session, upload(RECEIPT, event_id=uid(1)))
    second = ingest_receipt(session, upload(OTHER, event_id=uid(2)))

    assert second.deduplicated is False
    assert first.event_id != second.event_id
    assert first.blob_id != second.blob_id
    assert len(EventRepository(session).list_all()) == 2


def test_an_unsupported_upload_writes_neither_row(session: Session) -> None:
    """The media check runs before anything is written, so 415 leaves no trace."""
    with pytest.raises(AppError) as caught:
        ingest_receipt(session, upload(content_type="text/plain"))

    assert caught.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert session.scalars(select(ReceiptBlobRow)).all() == []
    assert session.scalars(select(EventRow)).all() == []


def test_a_float_amount_never_reaches_an_upload() -> None:
    """CLAUDE.md §2.3: there is no OCR here, so `amount_minor` is user input — the one
    place a form parse could hand `45.99` to a money field. Strict mode refuses it."""
    with pytest.raises(Exception) as caught:
        ReceiptUpload(
            blob=RECEIPT,
            content_type="application/pdf",
            date=dt.date(2026, 5, 1),
            amount_minor=45.99,  # type: ignore[arg-type]
            category="groceries",
            account_id="visa",
            recorded_at=RECORDED_AT,
        )
    assert "amount_minor" in str(caught.value)


def test_the_event_an_upload_becomes_is_a_pure_function_of_its_bytes() -> None:
    """`to_event` touches no database and no clock, so the seam can show the same event
    the ingest path writes without writing anything."""
    pinned = upload(event_id=uid(1))
    once = pinned.to_event(content_sha256(RECEIPT))
    twice = pinned.to_event(content_sha256(RECEIPT))
    assert once == twice
    assert once.dedupe_key == f"receipt:{content_sha256(RECEIPT)}"
