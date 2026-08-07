"""The `IngestionSource` seam, and the claim it exists to make.

PLAN.md §9: a future provider is another implementation of `fetch`, "producing the same
events as a receipt upload", and **nothing in `core/` or `domain/` changes** when one is
added. That is only worth anything if the two shipped implementations really do produce
the same shape and really do flow through one append path — so both are asserted here
rather than left as a design intention.

`fetch` reads no clock and touches no database (CLAUDE.md §4.4): every source below is a
pure function of the values it was constructed with, which is what lets a test pin an
event id and an instant and compare for equality.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import Event, ExpenseRecorded, IncomeReceived
from ingestion import (
    IngestionSource,
    ManualEntry,
    ManualEntrySource,
    ReceiptUpload,
    ReceiptUploadSource,
    content_sha256,
    ingest,
    ingest_receipt,
)
from persistence.repositories import EventRepository

UTC = dt.timezone.utc
RECORDED_AT = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
EPOCH = dt.date(2000, 1, 1)
RECEIPT = b"%PDF-1.7 fake receipt bytes"


def uid(n: int) -> UUID:
    return UUID(int=n)


def coffee_payload(
    amount_minor: int = 4_599, date: dt.date = dt.date(2026, 5, 1), **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "ExpenseRecorded",
        "date": date,
        "amount_minor": amount_minor,
        "category": "coffee",
        "account_id": "checking",
    }
    payload.update(extra)
    return payload


def entry(
    n: int, amount_minor: int = 4_599, date: dt.date = dt.date(2026, 5, 1)
) -> ManualEntry:
    return ManualEntry(
        payload=coffee_payload(amount_minor, date),
        recorded_at=RECORDED_AT,
        event_id=uid(n),
    )


def an_upload(
    n: int, blob: bytes = RECEIPT, date: dt.date = dt.date(2026, 5, 1)
) -> ReceiptUpload:
    return ReceiptUpload(
        blob=blob,
        content_type="application/pdf",
        date=date,
        amount_minor=4_599,
        category="groceries",
        account_id="visa",
        recorded_at=RECORDED_AT,
        event_id=uid(n),
    )


# -------------------------------------------------------------------- the protocol


def test_both_shipped_sources_satisfy_the_protocol() -> None:
    """Structural, checked by assignment: mypy proves it statically and this proves the
    runtime objects have the method, which is what a provider will be judged against."""
    sources: tuple[IngestionSource, ...] = (
        ManualEntrySource(),
        ReceiptUploadSource(),
    )
    for source in sources:
        assert source.fetch(EPOCH) == ()


# ------------------------------------------------------------------- manual entries


def test_manual_entries_become_canonical_keyed_events() -> None:
    source = ManualEntrySource((entry(1, 4_599), entry(2, 5_100)))
    fetched = source.fetch(EPOCH)

    assert len(fetched) == 2
    assert all(event.dedupe_key.startswith("manual:ExpenseRecorded:") for event in fetched)
    assert all(isinstance(event, ExpenseRecorded) for event in fetched)
    assert [event.event_id for event in fetched] == [uid(1), uid(2)]


def test_since_filters_on_the_business_date() -> None:
    """Inclusive of `since` itself. Period membership is a business-date question
    (CONTRACTS.md §3.1), so `recorded_at` is not consulted."""
    early = entry(1, date=dt.date(2026, 4, 30))
    boundary = entry(2, date=dt.date(2026, 5, 1))
    late = entry(3, date=dt.date(2026, 5, 2))
    source = ManualEntrySource((early, boundary, late))

    fetched = source.fetch(dt.date(2026, 5, 1))
    assert [event.event_id for event in fetched] == [uid(2), uid(3)]


def test_fetch_is_pure_and_repeatable() -> None:
    """Same source, same call, same events — ids included, because they were pinned.

    An unpinned entry mints a fresh `event_id` per fetch by design; the dedupe key does
    not move with it, which is the property the next test checks.
    """
    source = ManualEntrySource((entry(1), entry(2)))
    assert source.fetch(EPOCH) == source.fetch(EPOCH)


def test_an_unpinned_entry_keeps_one_key_across_fetches() -> None:
    unpinned = ManualEntry(payload=coffee_payload(), recorded_at=RECORDED_AT)
    source = ManualEntrySource((unpinned,))

    first = source.fetch(EPOCH)[0]
    second = source.fetch(EPOCH)[0]

    assert first.event_id != second.event_id
    assert first.dedupe_key == second.dedupe_key


def test_fetch_returns_canonical_ledger_order() -> None:
    """`(date, recorded_at, event_id)` — total and stable."""
    source = ManualEntrySource(
        (
            entry(3, date=dt.date(2026, 5, 3)),
            entry(1, date=dt.date(2026, 5, 1)),
            entry(2, date=dt.date(2026, 5, 2)),
        )
    )
    fetched = source.fetch(EPOCH)
    assert [event.date for event in fetched] == [
        dt.date(2026, 5, 1),
        dt.date(2026, 5, 2),
        dt.date(2026, 5, 3),
    ]


def test_a_malformed_entry_raises_at_fetch() -> None:
    """Malformed input is an error, raised where the payload becomes a model."""
    source = ManualEntrySource(
        (ManualEntry(payload={"event_type": "Nope"}, recorded_at=RECORDED_AT),)
    )
    with pytest.raises(AppError) as caught:
        source.fetch(EPOCH)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


# ------------------------------------------------------------------ the equivalence


def test_a_receipt_source_yields_exactly_what_the_receipt_path_appends(
    session: Session,
) -> None:
    """The seam's actual claim, as an equality.

    `ReceiptUploadSource.fetch` is the read-side view of an upload and `ingest_receipt`
    is the write path; if the events they produce ever diverged, "a provider produces
    the same events as a receipt upload" would be false and the protocol would be
    decoration.
    """
    upload = an_upload(1)
    from_source = ReceiptUploadSource((upload,)).fetch(EPOCH)

    result = ingest_receipt(session, upload)
    appended = EventRepository(session).get(result.event_id)

    assert len(from_source) == 1
    assert from_source[0] == appended
    assert from_source[0].dedupe_key == f"receipt:{content_sha256(RECEIPT)}"


def test_a_receipt_source_hashes_without_a_database() -> None:
    fetched = ReceiptUploadSource((an_upload(1),)).fetch(EPOCH)
    assert fetched[0].dedupe_key == f"receipt:{content_sha256(RECEIPT)}"


def test_receipt_uploads_are_filtered_by_since_too() -> None:
    source = ReceiptUploadSource(
        (
            an_upload(1, b"first", dt.date(2026, 4, 30)),
            an_upload(2, b"second", dt.date(2026, 5, 5)),
        )
    )
    fetched = source.fetch(dt.date(2026, 5, 1))
    assert [event.event_id for event in fetched] == [uid(2)]


# ---------------------------------------------------------------------- ingest()


def test_ingest_appends_everything_a_source_produced(session: Session) -> None:
    source = ManualEntrySource((entry(1, 4_599), entry(2, 5_100), entry(3, 1)))

    results = ingest(session, source, EPOCH)

    assert len(results) == 3
    assert all(result.deduplicated is False for result in results)
    assert len(EventRepository(session).list_all()) == 3


def test_ingesting_the_same_source_twice_writes_nothing_the_second_time(
    session: Session,
) -> None:
    """CONTRACTS.md §8.8, at the level a provider poll actually runs at: re-running a
    fetch over an unchanged window is the normal case, not an error case."""
    source = ManualEntrySource((entry(1, 4_599), entry(2, 5_100)))

    first_run = ingest(session, source, EPOCH)
    before = EventRepository(session).list_all()
    second_run = ingest(session, source, EPOCH)
    after = EventRepository(session).list_all()

    assert all(result.deduplicated is False for result in first_run)
    assert all(result.deduplicated is True for result in second_run)
    assert [result.event_id for result in second_run] == [
        result.event_id for result in first_run
    ]
    assert before == after


def test_ingest_carries_the_key_back_to_the_caller(session: Session) -> None:
    """`append_event` alone returns `(UUID, bool)`, from which a server-assigned key
    cannot be recovered — `AppendResult` is why `api/` can fill in
    `AppendEventResponse.dedupe_key` (CONTRACTS.md §6.1)."""
    source = ManualEntrySource((entry(1),))
    fetched = source.fetch(EPOCH)

    results = ingest(session, source, EPOCH)

    assert results[0].dedupe_key == fetched[0].dedupe_key


def test_two_sources_converge_on_one_ledger(session: Session) -> None:
    """A manual entry and a receipt describing different purchases both land, and the
    ledger does not care which produced which."""
    ingest(session, ManualEntrySource((entry(1),)), EPOCH)
    ingest_receipt(session, an_upload(2))

    stored = EventRepository(session).list_all()
    assert len(stored) == 2
    assert {event.dedupe_key.split(":")[0] for event in stored} == {"manual", "receipt"}


def test_an_income_event_ingests_through_the_same_seam(session: Session) -> None:
    """Nothing about the seam is expense-specific; the union is the only contract."""
    payday = ManualEntry(
        payload={
            "event_type": "IncomeReceived",
            "date": dt.date(2026, 5, 1),
            "amount_minor": 450_000,
            "source": "Employer",
            "account_id": "checking",
        },
        recorded_at=RECORDED_AT,
        event_id=uid(7),
    )
    results = ingest(session, ManualEntrySource((payday,)), EPOCH)

    stored: Event | None = EventRepository(session).get(results[0].event_id)
    assert isinstance(stored, IncomeReceived)
    assert stored.amount_minor == 450_000
