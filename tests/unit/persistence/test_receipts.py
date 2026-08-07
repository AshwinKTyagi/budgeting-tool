"""Receipt blobs are content-addressed, and re-uploading the same bytes is a no-op.

CONTRACTS.md §8.8: "identical bytes always yield the identical sha256 and reuse the same
blob". §6.4 adds the consequence at the HTTP layer — re-uploading an identical receipt is
a `200` with `deduplicated: true`, not an error.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import ExpenseRecorded
from persistence.models import EventRow, ReceiptBlobRow
from persistence.repositories import (
    ACCEPTED_RECEIPT_CONTENT_TYPES,
    EventRepository,
    ReceiptRepository,
)

UTC = dt.timezone.utc
RECEIPT = b"%PDF-1.7 fake receipt bytes"


def uid(n: int) -> UUID:
    return UUID(int=n)


def test_storing_returns_the_sha256_of_the_bytes(session: Session) -> None:
    """The digest is of the content, not of anything else.

    `dedupe_key = f"receipt:{sha256}"` is built from this value, so a digest over
    (say) the bytes plus the filename would make the same receipt uploadable twice.
    """
    repository = ReceiptRepository(session)
    blob_id, sha256_hex = repository.store(RECEIPT, "application/pdf")
    session.commit()

    assert sha256_hex == hashlib.sha256(RECEIPT).hexdigest()
    assert len(sha256_hex) == 64
    assert blob_id != sha256_hex  # §6.4 carries both; they are not the same value


def test_identical_bytes_reuse_the_same_blob(session: Session) -> None:
    """One row, one blob_id, however many times it is uploaded."""
    repository = ReceiptRepository(session)
    first_id, first_sha = repository.store(RECEIPT, "application/pdf")
    second_id, second_sha = repository.store(RECEIPT, "application/pdf")
    session.commit()

    assert first_id == second_id
    assert first_sha == second_sha
    rows = session.execute(select(ReceiptBlobRow.blob_id)).all()
    assert len(rows) == 1


def test_a_re_upload_does_not_rewrite_the_stored_content_type(
    session: Session,
) -> None:
    """The bytes are the identity; a second upload does not `UPDATE` the row.

    Rewriting it would be a mutation of an existing row for no gain (CLAUDE.md §4.3).
    """
    repository = ReceiptRepository(session)
    blob_id, _ = repository.store(RECEIPT, "application/pdf")
    repository.store(RECEIPT, "image/png")
    session.commit()

    stored = repository.get(blob_id)
    assert stored is not None
    assert stored.content_type == "application/pdf"


def test_different_bytes_get_different_blobs(session: Session) -> None:
    repository = ReceiptRepository(session)
    first_id, first_sha = repository.store(RECEIPT, "application/pdf")
    second_id, second_sha = repository.store(RECEIPT + b" v2", "application/pdf")
    session.commit()

    assert first_id != second_id
    assert first_sha != second_sha
    assert len(session.execute(select(ReceiptBlobRow.blob_id)).all()) == 2


def test_the_bytes_come_back_unchanged(session: Session) -> None:
    """A receipt is evidence; a byte lost in storage is the evidence gone."""
    repository = ReceiptRepository(session)
    payload = bytes(range(256)) * 4
    blob_id, sha256_hex = repository.store(payload, "image/png")
    session.commit()
    session.expunge_all()

    stored = repository.get(blob_id)
    assert stored is not None
    assert stored.content == payload
    assert stored.byte_size == len(payload)
    assert hashlib.sha256(stored.content).hexdigest() == sha256_hex


def test_lookup_by_sha256(session: Session) -> None:
    """How the `receipt:{sha256}` dedupe key finds its blob."""
    repository = ReceiptRepository(session)
    blob_id, sha256_hex = repository.store(RECEIPT, "application/pdf")
    session.commit()

    found = repository.find_by_sha256(sha256_hex)
    assert found is not None
    assert found.blob_id == blob_id
    assert repository.find_by_sha256("0" * 64) is None


def test_an_unaccepted_content_type_is_refused(session: Session) -> None:
    """`UNSUPPORTED_MEDIA_TYPE`, 415 (CONTRACTS.md §7.1), and nothing is stored."""
    repository = ReceiptRepository(session)
    with pytest.raises(AppError) as raised:
        repository.store(RECEIPT, "text/html")
    assert raised.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert session.execute(select(ReceiptBlobRow.blob_id)).all() == []


def test_content_type_matching_ignores_case_and_parameters(session: Session) -> None:
    """`image/JPEG; charset=binary` is an accepted type spelled awkwardly.

    A browser-supplied header carries parameters. Rejecting on them would turn a valid
    upload into a 415 for a reason the user cannot act on.
    """
    repository = ReceiptRepository(session)
    blob_id, _ = repository.store(RECEIPT, "image/JPEG; charset=binary")
    session.commit()

    stored = repository.get(blob_id)
    assert stored is not None
    assert stored.content_type == "image/jpeg"


def test_every_accepted_type_is_actually_accepted(session: Session) -> None:
    """The advertised set and the enforced set are the same set."""
    repository = ReceiptRepository(session)
    for index, content_type in enumerate(sorted(ACCEPTED_RECEIPT_CONTENT_TYPES)):
        blob_id, _ = repository.store(f"bytes-{index}".encode(), content_type)
        assert blob_id != ""
    session.commit()
    rows = session.execute(select(ReceiptBlobRow.blob_id)).all()
    assert len(rows) == len(ACCEPTED_RECEIPT_CONTENT_TYPES)


def test_an_event_can_carry_its_receipt_provenance(session: Session) -> None:
    """The §6.4 flow: store the blob, then append the expense that references it.

    The reference is a real foreign key rather than a `receipt:` prefix parsed back off
    the dedupe key. Both exist; only one of them is checked by the database.
    """
    receipts = ReceiptRepository(session)
    events = EventRepository(session)
    blob_id, sha256_hex = receipts.store(RECEIPT, "application/pdf")

    expense = ExpenseRecorded(
        event_id=uid(1),
        date=dt.date(2026, 8, 1),
        recorded_at=dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        dedupe_key=f"receipt:{sha256_hex}",
        amount_minor=4_599,
        category="groceries",
        account_id="checking",
        merchant="Corner Store",
    )
    events.append(expense, receipt_blob_id=blob_id)
    session.commit()

    row = session.get(EventRow, uid(1))
    assert row is not None
    assert row.receipt_blob_id == blob_id
    # Provenance is outside the domain model, so it does not disturb the round-trip.
    assert events.get(uid(1)) == expense
