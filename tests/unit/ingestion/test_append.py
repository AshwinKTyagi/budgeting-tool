"""`append_event` is idempotent, and `normalize_event` is what makes it so.

CONTRACTS.md §8.8 states the postcondition — "appending the same event twice leaves the
table and State unchanged" — and §7.1 states the consequence at the boundary: duplicate
ingestion is **not** an error, it is a 200 with `deduplicated: true`.

The storage-level fact those rest on is checked in `tests/unit/persistence/test_dedupe.py`
(the UNIQUE constraint and the ON CONFLICT clause). What is checked here is the seam:
that `ingestion` delegates to it rather than deciding for itself, that a payload arriving
without a key gets the key CONTRACTS.md §3.1 says it should, and that what lands in the
ledger reconstructs as the exact `domain.events.Event` it was built from.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import (
    Event,
    ExpenseRecorded,
    ExternalRef,
    IncomeReceived,
    PaymentMade,
    TransferMade,
    compute_dedupe_key,
)
from ingestion import append_event, append_events, normalize_event
from persistence.models import EventRow
from persistence.repositories import EventRepository

UTC = dt.timezone.utc
RECORDED_AT = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def uid(n: int) -> UUID:
    return UUID(int=n)


def coffee_payload(amount_minor: int = 4_599, **overrides: Any) -> dict[str, Any]:
    """A minimal, valid `ExpenseRecorded` payload as an HTTP body would carry it."""
    payload: dict[str, Any] = {
        "event_type": "ExpenseRecorded",
        "date": dt.date(2026, 5, 1),
        "amount_minor": amount_minor,
        "category": "coffee",
        "account_id": "checking",
    }
    payload.update(overrides)
    return payload


def coffee(amount_minor: int = 4_599, **overrides: Any) -> Event:
    return normalize_event(
        coffee_payload(amount_minor, **overrides), recorded_at=RECORDED_AT
    )


# ----------------------------------------------------------------------- idempotency


def test_appending_the_same_event_twice_writes_one_row(session: Session) -> None:
    """One row, `deduplicated=True`, and the id of the row that already existed.

    The second `event_id` is deliberately different from the first: `normalize_event`
    mints a fresh UUID per attempt, so the two events differ in the one field that is
    excluded from the dedupe key. That the ledger still returns the *stored* id is the
    whole postcondition — a caller that trusted its own id would be pointing at a row
    that was never written.
    """
    first = coffee()
    second = coffee()
    assert first.event_id != second.event_id
    assert first.dedupe_key == second.dedupe_key

    first_id, first_dedup = append_event(session, first)
    before = EventRepository(session).list_all()
    second_id, second_dedup = append_event(session, second)
    after = EventRepository(session).list_all()

    assert first_dedup is False
    assert second_dedup is True
    assert second_id == first_id
    assert second_id != second.event_id
    assert before == after
    assert len(after) == 1


def test_distinct_events_both_land(session: Session) -> None:
    """Different discriminating fields, different keys, two rows."""
    first = coffee(4_599)
    second = coffee(5_100)
    assert first.dedupe_key != second.dedupe_key

    first_id, first_dedup = append_event(session, first)
    second_id, second_dedup = append_event(session, second)

    stored = EventRepository(session).list_all()
    assert first_dedup is False
    assert second_dedup is False
    assert first_id != second_id
    assert len(stored) == 2
    assert {event.dedupe_key for event in stored} == {
        first.dedupe_key,
        second.dedupe_key,
    }


def test_a_client_nonce_separates_two_genuinely_identical_entries(
    session: Session,
) -> None:
    """CONTRACTS.md §3.1: the manual key is collision-prone *by design*.

    Two identical $4.50 coffees on the same day collide and the second is a no-op. The
    nonce is how the user says "there really were two", and it must produce a different
    key rather than a special case in the append path.
    """
    once = coffee()
    twice = normalize_event(
        coffee_payload(), recorded_at=RECORDED_AT, client_nonce="second-cup"
    )
    assert once.dedupe_key != twice.dedupe_key

    append_event(session, once)
    _, deduplicated = append_event(session, twice)

    assert deduplicated is False
    assert len(EventRepository(session).list_all()) == 2


def test_an_empty_dedupe_key_is_rejected(session: Session) -> None:
    """The precondition is "set and non-empty"; an empty key would defeat UNIQUE.

    Raised by the repository, not re-checked in `ingestion/` — one definition of the
    precondition, and this asserts the seam does not swallow or soften it.
    """
    keyless = ExpenseRecorded(
        event_id=uid(1),
        date=dt.date(2026, 5, 1),
        recorded_at=RECORDED_AT,
        dedupe_key="",
        amount_minor=4_599,
        category="coffee",
        account_id="checking",
    )

    with pytest.raises(AppError) as caught:
        append_event(session, keyless)

    assert caught.value.code == ErrorCode.VALIDATION_FAILED
    assert session.scalars(select(EventRow)).all() == []


def test_normalize_never_produces_an_empty_key() -> None:
    """Whatever the payload, the seam fills the key in rather than forwarding a blank.

    Includes the payload that supplies `dedupe_key: ""` explicitly: an empty string
    counts as absent, because forwarding one only to have it rejected downstream helps
    nobody.
    """
    for payload in (
        coffee_payload(),
        coffee_payload(dedupe_key=""),
        coffee_payload(amount_minor=0),
        coffee_payload(amount_minor=-1_500),
    ):
        event = normalize_event(payload, recorded_at=RECORDED_AT)
        assert event.dedupe_key != ""


# ------------------------------------------------------------------- key computation


def test_the_manual_key_is_the_documented_composite() -> None:
    """`manual:{event_type}:{date}:{amount_minor}:{sha256(discriminating_fields)}`.

    Asserted against `compute_dedupe_key` rather than a hardcoded digest: the key rule
    belongs to `domain/events.py` and this checks that ingestion *uses* it — a second
    spelling here is exactly the drift that would make a receipt re-uploadable.
    """
    payload = coffee_payload()
    event = normalize_event(payload, recorded_at=RECORDED_AT)

    assert event.dedupe_key == compute_dedupe_key("ExpenseRecorded", payload)
    assert event.dedupe_key.startswith("manual:ExpenseRecorded:2026-05-01:4599:")


def test_an_external_ref_takes_precedence_over_the_manual_composite() -> None:
    """A provider replaying a transaction must land on `ext:{provider}:{txn_id}`.

    PLAN.md §9: "A provider replaying the same transaction is already a no-op under the
    existing uniqueness constraint." That is only true if the ref decides the key, so it
    is read out of the payload before the key is computed.
    """
    ref = ExternalRef(provider="acme", provider_txn_id="txn-1")
    from_model = normalize_event(
        coffee_payload(external_ref=ref), recorded_at=RECORDED_AT
    )
    from_mapping = normalize_event(
        coffee_payload(external_ref={"provider": "acme", "provider_txn_id": "txn-1"}),
        recorded_at=RECORDED_AT,
    )

    assert from_model.dedupe_key == "ext:acme:txn-1"
    assert from_mapping.dedupe_key == "ext:acme:txn-1"
    assert from_model.external_ref == ref
    assert from_mapping.external_ref == ref


def test_a_supplied_key_is_honoured() -> None:
    """A source with a better natural key than the composite keeps it."""
    event = normalize_event(
        coffee_payload(dedupe_key="ext:mybank:0001"), recorded_at=RECORDED_AT
    )
    assert event.dedupe_key == "ext:mybank:0001"


def test_the_key_ignores_the_fields_that_cannot_discriminate() -> None:
    """`event_id` and `recorded_at` differ per attempt; the key must not.

    This is the trap `_NON_DISCRIMINATING_FIELDS` exists to close, checked from the
    ingestion side because ingestion is what supplies both values.
    """
    early = normalize_event(
        coffee_payload(),
        recorded_at=dt.datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        event_id=uid(1),
    )
    late = normalize_event(
        coffee_payload(),
        recorded_at=dt.datetime(2026, 5, 1, 23, 59, tzinfo=UTC),
        event_id=uid(2),
    )
    assert early.dedupe_key == late.dedupe_key


def test_recorded_at_is_the_parameters_never_the_payloads() -> None:
    """CLAUDE.md §4.4 / CONTRACTS.md §6.3: the instant is decided at the boundary.

    A payload that carries its own `recorded_at` does not get to set it — otherwise the
    one clock read in the codebase would have a second, unaudited source.
    """
    event = normalize_event(
        coffee_payload(recorded_at=dt.datetime(1999, 1, 1, tzinfo=UTC)),
        recorded_at=RECORDED_AT,
    )
    assert event.recorded_at == RECORDED_AT


def test_a_naive_recorded_at_is_rejected() -> None:
    """CLAUDE.md §4.5: there is no correct zone to assume, so none is assumed."""
    with pytest.raises(AppError) as caught:
        normalize_event(coffee_payload(), recorded_at=dt.datetime(2026, 5, 1, 12, 0))
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_a_non_utc_recorded_at_is_normalized_not_rejected() -> None:
    """An aware value converts; the instant is preserved exactly."""
    offset = dt.timezone(dt.timedelta(hours=-5))
    event = normalize_event(
        coffee_payload(), recorded_at=dt.datetime(2026, 5, 1, 7, 0, tzinfo=offset)
    )
    assert event.recorded_at == dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------ validation


def test_a_payload_without_an_event_type_is_rejected() -> None:
    with pytest.raises(AppError) as caught:
        normalize_event({"date": dt.date(2026, 5, 1)}, recorded_at=RECORDED_AT)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_an_unknown_event_type_is_rejected() -> None:
    with pytest.raises(AppError) as caught:
        normalize_event(
            {"event_type": "SomethingHappened", "date": dt.date(2026, 5, 1)},
            recorded_at=RECORDED_AT,
        )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_a_float_amount_is_rejected_at_the_seam() -> None:
    """CLAUDE.md §2.3: strict mode must reject `45.99`, not round it to `46`.

    The ingestion boundary validates through `TypeAdapter(Event)` — the exact path
    `domain/events.py` warns about, where a hoisted PEP 695 alias would leave the model
    config behind. This is the regression guard from the ingestion side.
    """
    with pytest.raises(AppError) as caught:
        normalize_event(
            coffee_payload(amount_minor=45.99),  # type: ignore[arg-type]
            recorded_at=RECORDED_AT,
        )
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_an_unknown_field_is_rejected() -> None:
    """`extra="forbid"`: a misspelled field is malformed input, not a silent drop."""
    with pytest.raises(AppError) as caught:
        normalize_event(coffee_payload(amount_cents=4_599), recorded_at=RECORDED_AT)
    assert caught.value.code == ErrorCode.VALIDATION_FAILED


def test_a_mismatched_payment_split_keeps_its_own_error_code() -> None:
    """PAYMENT_SPLIT_MISMATCH survives the seam unrelabelled.

    The code exists so the caller can tell this case apart from a generic rejection
    (CONTRACTS.md §7.1); flattening it into VALIDATION_FAILED would throw that away.
    """
    with pytest.raises(AppError) as caught:
        normalize_event(
            {
                "event_type": "PaymentMade",
                "date": dt.date(2026, 5, 1),
                "amount_minor": 118_000,
                "obligation_id": "loan-2026-05",
                "account_id": "checking",
                "principal_minor": 100_000,
                "interest_minor": 17_000,
            },
            recorded_at=RECORDED_AT,
        )
    assert caught.value.code == ErrorCode.PAYMENT_SPLIT_MISMATCH


def test_a_self_transfer_keeps_its_own_error_code() -> None:
    with pytest.raises(AppError) as caught:
        normalize_event(
            {
                "event_type": "TransferMade",
                "date": dt.date(2026, 5, 1),
                "amount_minor": 50_000,
                "from_account_id": "checking",
                "to_account_id": "checking",
            },
            recorded_at=RECORDED_AT,
        )
    assert caught.value.code == ErrorCode.TRANSFER_SAME_ACCOUNT


# ---------------------------------------------------------------------- round-trip


def test_an_ingested_event_round_trips_unchanged(session: Session) -> None:
    """What comes back out of the ledger is the model that went in. Not equivalent —
    equal.

    `project()` is a pure fold over these models, so a field the storage layer quietly
    altered would change every period after it. One event of each of four shapes,
    including the two that carry optional fields a simpler instance leaves NULL.
    """
    events: tuple[Event, ...] = (
        normalize_event(
            {
                "event_type": "IncomeReceived",
                "date": dt.date(2026, 5, 1),
                "amount_minor": 450_000,
                "source": "Employer",
                "account_id": "checking",
                "note": "May paycheck",
            },
            recorded_at=RECORDED_AT,
            event_id=uid(1),
        ),
        normalize_event(
            {
                "event_type": "PaymentMade",
                "date": dt.date(2026, 5, 2),
                "amount_minor": 118_000,
                "obligation_id": "loan-2026-05",
                "account_id": "checking",
                "principal_minor": 100_000,
                "interest_minor": 18_000,
            },
            recorded_at=RECORDED_AT,
            event_id=uid(2),
        ),
        normalize_event(
            coffee_payload(
                amount_minor=-1_500,
                merchant="Corner Store",
                external_ref={"provider": "acme", "provider_txn_id": "txn-9"},
            ),
            recorded_at=RECORDED_AT,
            event_id=uid(3),
        ),
        normalize_event(
            {
                "event_type": "EventVoided",
                "date": dt.date(2026, 5, 3),
                "target_event_id": uid(1),
                "reason": "entered twice",
            },
            recorded_at=RECORDED_AT,
            event_id=uid(4),
        ),
    )

    for event in events:
        event_id, deduplicated = append_event(session, event)
        assert deduplicated is False
        assert event_id == event.event_id

    repository = EventRepository(session)
    for event in events:
        assert repository.get(event.event_id) == event
        assert repository.get_by_dedupe_key(event.dedupe_key) == event

    assert set(repository.list_all()) == set(events)
    assert isinstance(events[0], IncomeReceived)
    assert isinstance(events[1], PaymentMade)


def test_a_transfer_round_trips_and_stays_budget_neutral_data(
    session: Session,
) -> None:
    """A `TransferMade` is stored like any other event; the seam adds no semantics.

    Ingestion never decides what an event *means* — PLAN.md §1's recognition principle
    is the projection's business. This is here so that stays true by test rather than by
    habit.
    """
    transfer = normalize_event(
        {
            "event_type": "TransferMade",
            "date": dt.date(2026, 5, 20),
            "amount_minor": 50_000,
            "from_account_id": "checking",
            "to_account_id": "visa",
        },
        recorded_at=RECORDED_AT,
        event_id=uid(11),
    )
    append_event(session, transfer)

    stored = EventRepository(session).get(uid(11))
    assert stored == transfer
    assert isinstance(stored, TransferMade)


# ---------------------------------------------------------------------------- batch


def test_a_batch_reports_per_item_results(session: Session) -> None:
    """`POST /events/batch` is per-item results over one transaction (§6.1).

    The duplicate in the middle is not an error and does not stop the items after it.
    """
    first = coffee(4_599)
    duplicate = coffee(4_599)
    third = coffee(5_100)
    assert first.dedupe_key == duplicate.dedupe_key

    results = append_events(session, (first, duplicate, third))

    assert [result.deduplicated for result in results] == [False, True, False]
    assert [result.dedupe_key for result in results] == [
        first.dedupe_key,
        duplicate.dedupe_key,
        third.dedupe_key,
    ]
    assert results[1].event_id == results[0].event_id
    assert len(EventRepository(session).list_all()) == 2


def test_a_batch_re_run_writes_nothing(session: Session) -> None:
    """CONTRACTS.md §8.8: re-ingestion leaves the table unchanged."""
    events = (coffee(4_599), coffee(5_100), coffee(1))

    first_run = append_events(session, events)
    before = EventRepository(session).list_all()
    second_run = append_events(session, events)
    after = EventRepository(session).list_all()

    assert all(result.deduplicated is False for result in first_run)
    assert all(result.deduplicated is True for result in second_run)
    assert [result.event_id for result in second_run] == [
        result.event_id for result in first_run
    ]
    assert before == after


def test_generated_event_ids_are_unique_across_attempts() -> None:
    """A generated id is fresh per call and never leaks between two normalizations."""
    ids = {normalize_event(coffee_payload(), recorded_at=RECORDED_AT).event_id
           for _ in range(16)}
    assert len(ids) == 16
    assert uuid4() not in ids
