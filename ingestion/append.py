"""Idempotent append (CONTRACTS.md §8.8), and the payload -> canonical event step.

Owned by `module/ingestion` (PLAN.md §13.2).

This module is the seam over `persistence.EventRepository`, not a second implementation
of it. `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` is spelled exactly once, in
`EventRepository.append` (Phase 2), and `append_event` delegates to it. Two answers to
idempotency in one codebase is one answer too many: the database decides, and it decides
in one place.

What this module *does* add is step 1 and step 2 of the ingestion flow in PLAN.md §3 —
"normalize to canonical event payload" and "compute dedupe_key". Those are ingestion's
own job and live nowhere else: `domain/events.py` computes a key from a payload it is
handed, `api/` speaks HTTP, and neither one turns a request body into a validated,
key-bearing `Event`. `normalize_event` is that turn.

No clock is read here. `recorded_at` is an explicit parameter on every entry point, for
the same reason `as_of_date` is (CLAUDE.md §4.4): CONTRACTS.md §6.3 names
`api.resolve_as_of` the only clock read in the codebase, so the instant an event was
recorded is decided at the HTTP boundary and threaded down, never sampled here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from core.types import MONEY_MODEL_CONFIG, AppError, ErrorCode, UtcInstant
from domain.events import Event, ExternalRef, compute_dedupe_key
from persistence.repositories import EventRepository

#: Validation through the discriminated union, keyed on `event_type`. The same adapter
#: `persistence.mapping` uses on the way out — an event that cannot be reconstructed
#: from its own fields must not be storable in the first place.
#:
#: `domain/events.py` pins `Field(strict=True)` on the money fields of the two event
#: classes carrying a `model_validator`, specifically because this adapter is the
#: ingestion boundary and a PEP 695 alias hoisted out of the model's config scope stops
#: being strict here. That comment is about this line.
_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

#: Fields the seam assigns rather than reads off the caller's payload. CONTRACTS.md
#: §6.1: "event_id/dedupe_key server-assigned if omitted". `recorded_at` is always the
#: parameter's, never the payload's — a caller-supplied "when I recorded this" would be
#: a clock read wearing a disguise.
_SERVER_ASSIGNED = frozenset({"event_id", "recorded_at", "dedupe_key"})


class AppendResult(BaseModel):
    """One event's outcome at the ingestion boundary.

    The three fields are exactly `AppendEventResponse` (CONTRACTS.md §6.1). This is not
    that DTO — serialization belongs to `api/` — it is the value `api/` builds it from,
    and it carries `dedupe_key` because the caller cannot recover the server-assigned
    key from `append_event`'s `(UUID, bool)` alone.
    """

    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    dedupe_key: str
    deduplicated: bool


def append_event(session: Session, event: Event) -> tuple[UUID, bool]:
    """Append idempotently.

    Implementation: INSERT ... ON CONFLICT (dedupe_key) DO NOTHING.

    Preconditions:
        event.dedupe_key is set and non-empty

    Postconditions:
        returns (event_id, deduplicated)
        deduplicated=True  -> nothing was written; event_id is the EXISTING row's
        deduplicated=False -> exactly one row was written
        never UPDATEs, never DELETEs
        appending the same event twice leaves the table and State unchanged

    Delegates to `EventRepository.append`, which is where the conflict clause lives.
    An empty `dedupe_key` is `AppError(VALIDATION_FAILED)` — raised there, not
    re-checked here, so there is one definition of "set and non-empty".

    Does not commit. The caller's transaction decides, so that storing a receipt blob
    and appending the event that references it commit together (CONTRACTS.md §6.4).
    """
    return EventRepository(session).append(event)


def append_events(session: Session, events: Iterable[Event]) -> tuple[AppendResult, ...]:
    """`append_event` over a sequence, in the order given. Backs `POST /events/batch`.

    Postconditions:
        one AppendResult per input event, positionally aligned
        already-present events report deduplicated=True and change nothing

    "Partial success" in CONTRACTS.md §6.1 means per-item *results*, not per-item
    transactions: this does not commit and does not swallow an `AppError`. A malformed
    item is malformed input and aborts the batch at `api/`'s transaction boundary; a
    duplicate item is not an error and simply reports itself as one.
    """
    repository = EventRepository(session)
    results: list[AppendResult] = []
    for event in events:
        event_id, deduplicated = repository.append(event)
        results.append(
            AppendResult(
                event_id=event_id,
                dedupe_key=event.dedupe_key,
                deduplicated=deduplicated,
            )
        )
    return tuple(results)


def normalize_event(
    payload: Mapping[str, object],
    *,
    recorded_at: UtcInstant,
    event_id: UUID | None = None,
    client_nonce: str | None = None,
    content_sha256: str | None = None,
) -> Event:
    """Turn an ingestion payload into a canonical, key-bearing `Event`.

    This is steps 1 and 2 of PLAN.md §3 and the reason `IngestionSource` can promise to
    yield `Event`s: every source — receipt upload, manual entry, a future aggregator —
    hands a payload through here and gets back the same shape, with the same key rule.

    Preconditions:
        payload names an `event_type` in the discriminated union and carries that
        type's fields

    Postconditions:
        `dedupe_key` is the payload's when it supplied a non-empty one, else
        `compute_dedupe_key`'s (precedence content_sha256 > external_ref > manual)
        `recorded_at` is the parameter's, always
        `event_id` is the parameter's, else the payload's, else a fresh UUID4
        deterministic in everything but a generated `event_id`: the same payload with
        the same nonce always yields the same `dedupe_key`

    Raises:
        AppError(VALIDATION_FAILED) for a payload that is not a valid event of its own
        declared type, with the pydantic errors flattened into `details`. Malformed
        input, never a warning (CLAUDE.md §6).
        AppError(PAYMENT_SPLIT_MISMATCH) / AppError(TRANSFER_SAME_ACCOUNT) propagate
        unwrapped from the model validators that raise them — those codes exist so the
        caller can tell those two cases apart from a generic rejection, and re-labelling
        them here would throw that away.
    """
    event_type = _event_type_of(payload)
    external_ref = _external_ref_of(payload)

    values: dict[str, Any] = {
        name: value for name, value in payload.items() if name not in _SERVER_ASSIGNED
    }
    if external_ref is None:
        values.pop("external_ref", None)
    else:
        # Constructed, not left as a dict: strict mode's treatment of a dict for a
        # nested model is not something to depend on at an ingestion boundary.
        values["external_ref"] = external_ref

    values["event_id"] = _resolve_event_id(payload, event_id)
    values["recorded_at"] = recorded_at
    values["dedupe_key"] = _resolve_dedupe_key(
        payload,
        event_type,
        external_ref=external_ref,
        client_nonce=client_nonce,
        content_sha256=content_sha256,
    )

    try:
        return _EVENT_ADAPTER.validate_python(values)
    except ValidationError as exc:
        raise _as_app_error(exc, event_type) from exc


# ------------------------------------------------------------------------ internals


def _event_type_of(payload: Mapping[str, object]) -> str:
    raw = payload.get("event_type")
    if not isinstance(raw, str) or not raw:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "payload must name a non-empty `event_type` from the event union",
            {"event_type": repr(raw)},
        )
    return raw


def _external_ref_of(payload: Mapping[str, object]) -> ExternalRef | None:
    """The payload's `external_ref` as a model, accepting one already built.

    It is read out before the key is computed because it *decides* the key
    (CONTRACTS.md §3.1: `ext:{provider}:{provider_txn_id}`), and a provider replaying a
    transaction has to land on the identical key or the replay stops being a no-op.
    """
    raw = payload.get("external_ref")
    if raw is None:
        return None
    if isinstance(raw, ExternalRef):
        return raw
    if isinstance(raw, Mapping):
        try:
            return ExternalRef.model_validate(dict(raw))
        except ValidationError as exc:
            raise _as_app_error(exc, "ExternalRef") from exc
    raise AppError(
        ErrorCode.VALIDATION_FAILED,
        "external_ref must be an object with `provider` and `provider_txn_id`",
        {"type": type(raw).__name__},
    )


def _resolve_event_id(payload: Mapping[str, object], supplied: UUID | None) -> UUID:
    """The event's id: the parameter's, else the payload's, else a fresh one.

    A generated id is not a determinism problem the way a clock read would be: it never
    participates in the dedupe key (`_NON_DISCRIMINATING_FIELDS` in `domain/events.py`
    excludes it precisely so that two attempts at the same event still collide), and on
    a duplicate the ledger returns the stored row's id, not this one.
    """
    if supplied is not None:
        return supplied
    raw = payload.get("event_id")
    if raw is None:
        return uuid4()
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError as exc:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "event_id is not a UUID",
                {"event_id": raw},
            ) from exc
    raise AppError(
        ErrorCode.VALIDATION_FAILED,
        "event_id must be a UUID",
        {"type": type(raw).__name__},
    )


def _resolve_dedupe_key(
    payload: Mapping[str, object],
    event_type: str,
    *,
    external_ref: ExternalRef | None,
    client_nonce: str | None,
    content_sha256: str | None,
) -> str:
    """The payload's key when it supplied one, else the computed one.

    A caller-supplied key is honoured rather than overwritten so that a source with a
    genuinely better natural key than the manual composite can use it. An empty string
    counts as absent — `EventRepository.append` rejects an empty key outright, and
    silently forwarding one to be rejected downstream is worse than filling it in.
    """
    raw = payload.get("dedupe_key")
    if raw is not None and not isinstance(raw, str):
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "dedupe_key must be a string",
            {"type": type(raw).__name__},
        )
    if raw:
        return raw
    return compute_dedupe_key(
        event_type,
        payload,
        content_sha256=content_sha256,
        external_ref=external_ref,
        client_nonce=client_nonce,
    )


def _as_app_error(exc: ValidationError, context: str) -> AppError:
    """Flatten a `ValidationError` into `AppError(VALIDATION_FAILED)`.

    CONTRACTS.md §7.1 maps "Pydantic rejection" to VALIDATION_FAILED / 422, and the
    ingestion seam is where a payload stops being a dict and becomes a model — so this
    is where the mapping is cheapest and where the per-field detail still exists. `api/`
    receives one exception type from this module and needs no second handler.

    The errors are flattened to plain strings rather than passed through: `details` is
    `dict[str, object]` and ends up in a JSON body, and a pydantic `ctx` can hold an
    arbitrary exception object that does not serialize.
    """
    return AppError(
        ErrorCode.VALIDATION_FAILED,
        f"{exc.error_count()} validation error(s) for {context}",
        {
            "context": context,
            "errors": [
                {
                    "loc": ".".join(str(part) for part in error["loc"]),
                    "msg": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors(include_url=False)
            ],
        },
    )
