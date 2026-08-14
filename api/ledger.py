"""The `GET /ledger` read model: one flat row per event (CONTRACTS.md §6.2).

Owned by `module/api` (PLAN.md §13.2).

This is a *view*, not a second projection. It reads the stored event stream directly
rather than `State`, because the spreadsheet view shows the ledger as entered —
including voided rows, with `is_voided: true` — and `State` is what the ledger *means*
after voids are filtered and obligations are folded. Two different questions.

Nothing here decides money. Every `amount_minor` is copied off the event unchanged; no
row is summed, netted, or signed differently from how it was recorded.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from collections.abc import Mapping, Sequence
from uuid import UUID

from api.dtos import LedgerOrigin, LedgerRow
from core.periods import PeriodResolver
from core.types import AppError, ErrorCode, PeriodId
from domain.events import (
    AccountOpeningBalance,
    Event,
    EventVoided,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
)
from persistence.repositories import LedgerCursor

#: Separator inside the encoded cursor. Neither an ISO-8601 date, an ISO-8601 instant,
#: nor a UUID can contain it, so splitting is unambiguous.
_CURSOR_SEPARATOR = "|"


def period_date(event: Event) -> dt.date:
    """The business date that decides `event`'s period.

    `ObligationRaised` is the one type whose period comes from `due_date` rather than
    `date` — CONTRACTS.md §3.2 states it on the field itself ("decides period
    membership, NOT `date`"). Every other type uses `date`.
    """
    if isinstance(event, ObligationRaised):
        return event.due_date
    return event.date


def to_ledger_row(
    event: Event,
    *,
    resolver: PeriodResolver,
    voided_by: Mapping[UUID, UUID],
) -> LedgerRow:
    """One event as a flat spreadsheet row.

    `voided_by` maps a voided event's id to the id of the `EventVoided` that killed it.
    An `EventVoided` row is itself shown, unvoided — the correction is part of the
    history the tabular view exists to display.
    """
    return LedgerRow(
        event_id=event.event_id,
        event_type=event.event_type,
        date=event.date,
        recorded_at=event.recorded_at,
        period_id=_period_id(event, resolver),
        amount_minor=_amount_minor(event),
        account_id=_account_id(event),
        counterparty=_counterparty(event),
        category=_category(event),
        is_voided=event.event_id in voided_by,
        voided_by_event_id=voided_by.get(event.event_id),
        note=event.note,
        origin=_origin(event.dedupe_key),
    )


def _origin(dedupe_key: str) -> LedgerOrigin:
    if dedupe_key.startswith("receipt:"):
        return LedgerOrigin.RECEIPT
    if dedupe_key.startswith("ext:"):
        return LedgerOrigin.EXTERNAL
    return LedgerOrigin.MANUAL


def build_voided_index(voids: Sequence[Event]) -> dict[UUID, UUID]:
    """`{target_event_id: voiding_event_id}` from the ledger's `EventVoided` rows.

    Built from the stored voids rather than from `State`, so a row is marked voided even
    when its target falls outside the requested page or the projection's window.
    """
    return {
        void.target_event_id: void.event_id
        for void in voids
        if isinstance(void, EventVoided)
    }


# --------------------------------------------------------------------------- cursor
# A keyset cursor on `(date, recorded_at, event_id)` — the canonical ledger order
# (CONTRACTS.md §3.1). Keyset rather than offset so that a page neither skips nor
# repeats a row when an event is appended mid-pagination, which for an append-only
# ledger being backfilled is the normal case rather than a race.


def encode_cursor(cursor: LedgerCursor) -> str:
    """The keyset triple as an opaque token."""
    ledger_date, recorded_at, event_id = cursor
    raw = _CURSOR_SEPARATOR.join(
        (ledger_date.isoformat(), recorded_at.isoformat(), str(event_id))
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> LedgerCursor:
    """Recover the keyset triple.

    Raises:
        AppError(VALIDATION_FAILED) for a token that is not one this API issued. A
        malformed cursor is input that could never be valid, so it is an error and not
        a silently-ignored "start from the beginning" — silently restarting would make a
        paginating client loop forever without ever reporting why.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ledger_date, recorded_at, event_id = raw.split(_CURSOR_SEPARATOR)
        return (
            dt.date.fromisoformat(ledger_date),
            dt.datetime.fromisoformat(recorded_at),
            UUID(event_id),
        )
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "cursor is not a token this API issued",
            {"cursor": cursor},
        ) from exc


# ------------------------------------------------------------------------ per-field
# Explicit `isinstance` branches rather than `getattr`. The union is closed and frozen
# (CONTRACTS.md §3.2), so exhaustiveness is checkable, and `getattr(event, "amount_minor",
# None)` would silently start returning a value for any future event type that happened
# to name a field the same way.


def _period_id(event: Event, resolver: PeriodResolver) -> PeriodId:
    return resolver.period_for(period_date(event))


def _amount_minor(event: Event) -> int | None:
    """The event's own amount, or None for the types that carry no money.

    `EventVoided` carries none: a void is not a reversing entry with an amount, it is a
    filter applied before the fold (PLAN.md §8.4).
    """
    match event:
        case (
            IncomeReceived()
            | GiftReceived()
            | ObligationRaised()
            | PaymentMade()
            | ExpenseRecorded()
            | SavingsDrawn()
            | TransferMade()
            | AccountOpeningBalance()
            | InterestCharged()
            | InterestEarned()
        ):
            return event.amount_minor
        case EventVoided():
            return None


def _account_id(event: Event) -> str | None:
    """The account the money touched, from this event's point of view.

    A `TransferMade` reports the account it left; the destination is the counterparty.
    `SavingsDrawn` reports none — it names no account, because the savings account it
    draws from is the one the projection resolves, not one the event picks.
    """
    match event:
        case (
            IncomeReceived()
            | GiftReceived()
            | PaymentMade()
            | ExpenseRecorded()
            | AccountOpeningBalance()
            | InterestCharged()
            | InterestEarned()
        ):
            return event.account_id
        case TransferMade():
            return event.from_account_id
        case ObligationRaised() | SavingsDrawn() | EventVoided():
            return None


def _counterparty(event: Event) -> str | None:
    """"source / payee / merchant, normalized" (CONTRACTS.md §6.2).

    Plus the destination account of a transfer, which is literally the counterparty of
    the account named in `account_id` and is the only other field on the union that
    answers "who was on the other side".
    """
    match event:
        case IncomeReceived() | GiftReceived():
            return event.source
        case ObligationRaised():
            return event.payee
        case ExpenseRecorded():
            return event.merchant
        case TransferMade():
            return event.to_account_id
        case (
            PaymentMade()
            | SavingsDrawn()
            | AccountOpeningBalance()
            | InterestCharged()
            | InterestEarned()
            | EventVoided()
        ):
            return None


def _category(event: Event) -> str | None:
    """The chart label, where the event carries one. No allocation semantics."""
    match event:
        case ObligationRaised() | ExpenseRecorded():
            return event.category
        case (
            IncomeReceived()
            | GiftReceived()
            | PaymentMade()
            | SavingsDrawn()
            | TransferMade()
            | AccountOpeningBalance()
            | InterestCharged()
            | InterestEarned()
            | EventVoided()
        ):
            return None
