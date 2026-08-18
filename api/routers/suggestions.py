"""Forecast occurrences awaiting confirmation (PLAN.md §8.5, CONTRACTS.md §6.5).

Owned by `module/api` (PLAN.md §13.2).

Three endpoints over one derived list. `api/suggestions.py` decides what is pending and
what each confirmation would append; this module does the I/O and nothing else.

**Events stay explicit user actions.** Nothing here runs on a timer, and no read writes.
A suggestion becomes a row only when the user confirms or rejects it, which is the
whole reason this is an inbox rather than the background scheduler PLAN.md §8.1
rejected: no clock-dependent writer, and a definition edit never has to backfill or
delete a row it already wrote.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, status
from pydantic import TypeAdapter
from sqlalchemy.orm import Session
from starlette.requests import Request

from api.clock import budget_tz, now_utc, resolve_as_of
from api.deps import AsOfDep, SessionDep, load_state
from api.dtos import (
    AppendEventResponse,
    Suggestion,
    SuggestionConfirmRequest,
    SuggestionListResponse,
    SuggestionRejectRequest,
    SuggestionRejectResponse,
)
from api.payloads import parse_body
from api.references import check_references
from api.suggestions import (
    REJECTED_NOTE,
    REJECTED_REASON,
    build_suggestions,
    find_suggestion,
    suggestion_payload,
)
from domain.events import Event
from ingestion.append import append_event, normalize_event
from persistence.repositories import DefinitionRepository, EventRepository

router = APIRouter(tags=["suggestions"])

_CONFIRM_REQUEST: TypeAdapter[SuggestionConfirmRequest] = TypeAdapter(
    SuggestionConfirmRequest
)
_REJECT_REQUEST: TypeAdapter[SuggestionRejectRequest] = TypeAdapter(
    SuggestionRejectRequest
)

#: Every suggestion id shares this prefix, so one indexed prefix read answers "which
#: occurrences have already been dealt with" for all three kinds at once.
_KEY_PREFIX = "expected:"


@router.get(
    "/suggestions",
    response_model=SuggestionListResponse,
    summary="Forecast occurrences that have come due",
)
def read_suggestions(session: SessionDep, as_of_date: AsOfDep) -> SuggestionListResponse:
    """Bills, paychecks, and statement interest that are due and unconfirmed.

    A pure read: it appends nothing, so opening the app never writes. Ascending by
    date, oldest first — the opposite of `/ledger`, because this is a worklist and the
    thing you have been putting off longest belongs at the top.
    """
    return SuggestionListResponse(
        as_of_date=as_of_date,
        suggestions=_pending(session, as_of_date),
    )


@router.post(
    "/suggestions/{suggestion_id}/confirm",
    status_code=status.HTTP_201_CREATED,
    response_model=AppendEventResponse,
    summary="Accept a forecast occurrence, appending the real event",
)
async def confirm_suggestion(
    suggestion_id: str,
    request: Request,
    session: SessionDep,
    as_of_date: AsOfDep,
) -> AppendEventResponse:
    """Append the event the occurrence stands for, with optional edits.

    The body may be empty — confirming an unedited occurrence carries nothing to say.

    `dedupe_key` is the suggestion's own id regardless of what was edited, so a
    corrected amount is still the same paycheck and the occurrence never comes back.
    """
    edits = await _optional_body(request, _CONFIRM_REQUEST, SuggestionConfirmRequest())
    suggestion = find_suggestion(_pending(session, as_of_date), suggestion_id)
    event = _normalize(suggestion_payload(suggestion, edits), edits.client_nonce)
    check_references(session, (event,), as_of_date=_today())
    event_id, deduplicated = append_event(session, event)
    return AppendEventResponse(
        event_id=event_id,
        dedupe_key=event.dedupe_key,
        deduplicated=deduplicated,
    )


@router.post(
    "/suggestions/{suggestion_id}/reject",
    status_code=status.HTTP_201_CREATED,
    response_model=SuggestionRejectResponse,
    summary="Record that a forecast occurrence did not happen",
)
async def reject_suggestion(
    suggestion_id: str,
    request: Request,
    session: SessionDep,
    as_of_date: AsOfDep,
) -> SuggestionRejectResponse:
    """Append the occurrence, noted as rejected, then void it.

    Two rows rather than a deletion, because this codebase has no deletes (CLAUDE.md
    §4.3) and because a rejection is worth keeping: the note on the event and the
    reason on the `EventVoided` are what tell a later reader that the paycheck never
    arrived, rather than that a figure was corrected.

    The voided row still carries the occurrence's `dedupe_key`, which is what stops it
    being offered again. Its effect on the budget is nil — `project()` filters voided
    events before it folds — so rejecting a bill leaves that bill's expected obligation
    reserving money exactly as before, and rejecting a paycheck leaves it unspendable.
    """
    body = await _optional_body(request, _REJECT_REQUEST, SuggestionRejectRequest())
    suggestion = find_suggestion(_pending(session, as_of_date), suggestion_id)
    event = _normalize(
        suggestion_payload(suggestion, SuggestionConfirmRequest(), note=REJECTED_NOTE),
        body.client_nonce,
    )
    check_references(session, (event,), as_of_date=_today())
    event_id, _deduplicated = append_event(session, event)
    void = _normalize(
        {
            "event_type": "EventVoided",
            "date": _today(),
            "target_event_id": event_id,
            "reason": REJECTED_REASON if body.reason is None else body.reason,
        },
        body.client_nonce,
    )
    void_id, _ = append_event(session, void)
    return SuggestionRejectResponse(
        event_id=event_id,
        voided_by_event_id=void_id,
        dedupe_key=event.dedupe_key,
    )


# --------------------------------------------------------------------------- helpers


def _pending(session: Session, as_of_date: dt.date) -> tuple[Suggestion, ...]:
    """The unsuppressed occurrences at `as_of_date`.

    Rebuilt per request rather than cached, for the same reason every other read
    recomputes from genesis (`api/deps.py`): a backdated event changes what is pending,
    and a cache keyed on anything cheaper than the whole ledger would be wrong.
    """
    return build_suggestions(
        load_state(session, as_of_date),
        DefinitionRepository(session).load_definitions(),
        suppressed=EventRepository(session).list_dedupe_keys(_KEY_PREFIX),
    )


def _normalize(payload: object, client_nonce: str | None) -> Event:
    """Canonicalize a payload this module built.

    Not `canonical_event_payload`: that turns *wire* values into python ones, and these
    payloads are assembled from `State` and so already carry `dt.date` and `int`.
    """
    assert isinstance(payload, dict)
    return normalize_event(payload, recorded_at=now_utc(), client_nonce=client_nonce)


async def _optional_body[T](
    request: Request, adapter: TypeAdapter[T], default: T
) -> T:
    """The parsed body, or `default` when the request carried none.

    Both endpoints are meaningful with no body at all — confirming as-forecast, or
    rejecting without prose — and `parse_body` rejects an empty body outright, which is
    right everywhere else in this API. Starlette caches the body, so the delegation
    below re-reads nothing.
    """
    if not (await request.body()).strip():
        return default
    return await parse_body(request, adapter, type(default).__name__)


def _today() -> dt.date:
    """Today in `BUDGET_TZ`, for the reference check and the void's own date."""
    return resolve_as_of(None, budget_tz())
