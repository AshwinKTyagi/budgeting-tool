"""Ingestion endpoints (CONTRACTS.md §6.1, §6.4).

Owned by `module/api` (PLAN.md §13.2).

**Duplicate ingestion is a 200, never an error.** CONTRACTS.md §6.1 says it twice and
§7.1 says it again: re-uploading an identical receipt "returns `200` with
`deduplicated: true` and the existing `event_id`. It is not an error and must not be
reported as one." The status code is the only difference between the two outcomes — the
body shape is identical, so a client that ignores the distinction still gets a correct
`event_id`.

Nothing here writes SQL. Every append goes through `ingestion/`, which goes through
`persistence.EventRepository.append`, where `INSERT ... ON CONFLICT DO NOTHING` is
spelled exactly once. The database decides idempotency; this layer reports the decision.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from pydantic import TypeAdapter
from sqlalchemy.orm import Session
from starlette.requests import Request

from api.clock import budget_tz, now_utc, resolve_as_of
from api.deps import SessionDep
from api.dtos import (
    AppendBatchRequest,
    AppendBatchResponse,
    AppendEventResponse,
    InboundEventRequest,
    ReceiptUploadResponse,
    VoidRequest,
)
from api.payloads import canonical_event_payload, parse_body
from api.references import check_references
from core.types import AppError, ErrorCode
from domain.events import Event, EventVoided
from ingestion.append import append_event, normalize_event
from ingestion.receipts import ReceiptUpload, ingest_receipt
from persistence.repositories import EventRepository

router = APIRouter(tags=["ingestion"])

_APPEND_REQUEST: TypeAdapter[InboundEventRequest] = TypeAdapter(InboundEventRequest)
_BATCH_REQUEST: TypeAdapter[AppendBatchRequest] = TypeAdapter(AppendBatchRequest)
_VOID_REQUEST: TypeAdapter[VoidRequest] = TypeAdapter(VoidRequest)


@router.post(
    "/events",
    status_code=status.HTTP_201_CREATED,
    response_model=AppendEventResponse,
    summary="Append one event, idempotently",
)
async def append_one(
    request: Request, response: Response, session: SessionDep
) -> AppendEventResponse:
    """`201` on a write, `200` when the `dedupe_key` already existed.

    The body is read raw and validated in pydantic's json mode; see
    `api/payloads.py` for why a strict model cannot be a FastAPI body parameter.
    """
    body = await parse_body(request, _APPEND_REQUEST, "AppendEventRequest")
    event = _canonical_event(body)
    check_references(session, (event,), as_of_date=_today())
    result = _append(session, event)
    if result.deduplicated:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/events/batch",
    status_code=status.HTTP_200_OK,
    response_model=AppendBatchResponse,
    summary="Append many events, idempotently",
)
async def append_batch(request: Request, session: SessionDep) -> AppendBatchResponse:
    """Per-item results, positionally aligned with the request.

    Always `200`: a batch is a mixture of writes and no-ops by nature, so there is no
    single status that describes it. The per-item `deduplicated` flag is the answer.

    A malformed item fails the whole batch — see `AppendBatchResponse` for why that is
    "partial success" as `ingestion.append_events` reads it. The request's session is
    rolled back by `api.deps.get_session`, so a rejected batch writes nothing.
    """
    body = await parse_body(request, _BATCH_REQUEST, "AppendBatchRequest")
    events = tuple(_canonical_event(item) for item in body.events)
    check_references(session, events, as_of_date=_today())
    return AppendBatchResponse(
        results=tuple(_append(session, event) for event in events)
    )


@router.post(
    "/events/{event_id}/void",
    status_code=status.HTTP_201_CREATED,
    response_model=AppendEventResponse,
    summary="Void an event by appending an EventVoided",
)
async def void_event(
    event_id: str, request: Request, session: SessionDep
) -> AppendEventResponse:
    """The only correction mechanism (PLAN.md §8.4). Appends, never deletes.

    Three rejections, checked in this order because each one presupposes the last:

    * the target does not exist -> `UNKNOWN_EVENT`, 404
    * the target is itself an `EventVoided` -> `CANNOT_VOID_A_VOID`, 422
    * the target is already voided -> `ALREADY_VOIDED`, 409

    Void-a-void before already-voided: an `EventVoided` can never be a valid target
    whatever else is true of it, so reporting 409 for one would name the wrong problem.
    """
    body = await parse_body(request, _VOID_REQUEST, "VoidRequest")
    target_id = _parse_event_id(event_id)
    repository = EventRepository(session)

    target = repository.get(target_id)
    if target is None:
        raise AppError(
            ErrorCode.UNKNOWN_EVENT,
            f"no event {event_id}",
            {"event_id": event_id},
        )
    if isinstance(target, EventVoided):
        raise AppError(
            ErrorCode.CANNOT_VOID_A_VOID,
            "an EventVoided cannot itself be voided",
            {"event_id": event_id},
        )
    existing_void = repository.find_void_for(target_id)
    if existing_void is not None:
        raise AppError(
            ErrorCode.ALREADY_VOIDED,
            f"event {event_id} was already voided",
            {
                "event_id": event_id,
                "voided_by_event_id": str(existing_void.event_id),
            },
        )

    void = normalize_event(
        {
            "event_type": "EventVoided",
            "date": body.date if body.date is not None else _today(),
            "target_event_id": target_id,
            "reason": body.reason,
        },
        recorded_at=now_utc(),
        client_nonce=body.client_nonce,
    )
    return _append(session, void)


@router.post(
    "/receipts",
    status_code=status.HTTP_201_CREATED,
    response_model=ReceiptUploadResponse,
    summary="Upload a receipt and append the expense it documents",
)
async def upload_receipt(
    response: Response,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="The receipt image or PDF")],
    date: Annotated[dt.date, Form(description="Business date")],
    amount_minor: Annotated[
        int, Form(description="Required — there is no OCR in this scope")
    ],
    category: Annotated[str, Form()],
    account_id: Annotated[str, Form()],
    merchant: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> ReceiptUploadResponse:
    """`201` on a write, `200` when these exact bytes were uploaded before (§6.4).

    The blob and the `ExpenseRecorded` that references it are written in the request's
    single transaction — `events.receipt_blob_id` is a foreign key, so a rolled-back
    event must take its blob with it. `ingest_receipt` does both in the caller's
    session for exactly that reason.

    A receipt is discretionary spending and therefore an `ExpenseRecorded`. Whether it
    reduces discretionary now or at statement payment is the account's `budget_timing`
    and the projection's decision (PLAN.md §6.4); this endpoint neither knows nor asks.
    """
    blob = await file.read()
    upload = ReceiptUpload(
        blob=blob,
        # A missing Content-Type is not a defaultable field: guessing `image/jpeg` for
        # unlabelled bytes is how a mislabelled blob gets stored. An empty string is not
        # an accepted type, so `ReceiptRepository.store` answers with the 415 the table
        # calls for.
        content_type=file.content_type or "",
        date=date,
        amount_minor=amount_minor,
        category=category,
        account_id=account_id,
        recorded_at=now_utc(),
        merchant=merchant,
        note=note,
    )
    _check_receipt_account(session, upload)
    result = ingest_receipt(session, upload)
    if result.deduplicated:
        response.status_code = status.HTTP_200_OK
    return ReceiptUploadResponse(
        event_id=result.event_id,
        blob_id=result.blob_id,
        content_sha256=result.content_sha256,
        deduplicated=result.deduplicated,
    )


# ------------------------------------------------------------------------ internals


def _canonical_event(item: InboundEventRequest) -> Event:
    """The wire object as a canonical, key-bearing `Event`.

    Two steps, both someone else's: `canonical_event_payload` coerces JSON scalars
    against the frozen union, and `ingestion.normalize_event` assigns `event_id`,
    `recorded_at` and `dedupe_key`. `recorded_at` is stamped here, at the HTTP boundary,
    because that is the one place in the codebase allowed to read a clock.
    """
    return normalize_event(
        canonical_event_payload(item.event),
        recorded_at=now_utc(),
        client_nonce=item.client_nonce,
    )


def _append(session: Session, event: Event) -> AppendEventResponse:
    """Append and report. The `event_id` on a duplicate is the STORED row's.

    Two attempts at the same event carry different `event_id`s by construction — a
    fresh UUID per attempt — and the ledger's answer is the one already written, so a
    client that retries a timed-out request gets back the id it would have got the
    first time.
    """
    event_id, deduplicated = append_event(session, event)
    return AppendEventResponse(
        event_id=event_id,
        dedupe_key=event.dedupe_key,
        deduplicated=deduplicated,
    )


def _check_receipt_account(session: Session, upload: ReceiptUpload) -> None:
    """`UNKNOWN_ACCOUNT` for a receipt, before any bytes are stored.

    Checked against the event the upload will become rather than against the raw form
    field, so the receipt path and the `POST /events` path agree by construction about
    what "the account this event names" means.
    """
    check_references(
        session,
        (upload.to_event("0" * 64),),
        as_of_date=_today(),
    )


def _parse_event_id(event_id: str) -> UUID:
    """The path parameter as a UUID.

    A path parameter rather than a body field, so a malformed one is this endpoint's
    `VALIDATION_FAILED` and not a routing 404 — the caller asked for a specific event
    and deserves to be told the id was unreadable.
    """
    try:
        return UUID(event_id)
    except ValueError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "event_id is not a UUID",
            {"event_id": event_id},
        ) from exc


def _today() -> dt.date:
    """Today in `BUDGET_TZ`, for the endpoints that need a default business date."""
    return resolve_as_of(None, budget_tz())
