"""Wire JSON -> canonical model, strictly (CONTRACTS.md §6.1, §6.2).

Owned by `module/api` (PLAN.md §13.2).

**Why this module exists at all.** Every model in the project is `strict=True`
(CONTRACTS.md §1), and FastAPI validates a request body in pydantic's *python* mode —
against the already-parsed `dict`. In python mode, strict `dt.date` rejects the string
`"2026-03-05"` and strict `UUID` rejects the string form of a UUID, because in python
mode those types have a native representation and a string is not it. JSON has no such
representation, so pydantic's **json** mode accepts the string for exactly those types
and still rejects `19.99` where a `Minor` is declared.

So the strictness the money rules depend on and the deserialization the wire requires
both hold — but only if the body is validated from the raw bytes rather than from the
dict FastAPI parsed. That is what `parse_body` does, and it is the reason the write
endpoints read `Request` instead of declaring a body parameter.

Nothing here loosens a rule. `19.99` in an `amount_minor` is rejected here just as it
is at the ingestion boundary and in persistence, by the same `TypeAdapter(Event)`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request

from api.errors import validation_app_error
from core.types import AppError, ErrorCode
from domain.definitions import DefinitionBase
from domain.events import Event
from persistence.mapping import DEFINITION_MODEL_BY_KIND, DefinitionKind

#: The frozen event union. The same adapter `ingestion.append` and `persistence.mapping`
#: use — one definition of "is this a valid event", exercised on every boundary crossing.
_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

#: Fields the server assigns, never the client (CONTRACTS.md §6.1, and `_SERVER_ASSIGNED`
#: in `ingestion/append.py`, which this deliberately mirrors).
_SERVER_ASSIGNED: Final[frozenset[str]] = frozenset(
    {"event_id", "recorded_at", "dedupe_key"}
)

#: Stand-ins used only to make an inbound payload a *complete* event so the union can
#: validate it and coerce its JSON scalars. Every one of them is stripped again before
#: the payload reaches `normalize_event`, which assigns the real values. They are
#: constants rather than generated so that this function stays a pure function of its
#: input — the coercion pass must not be able to influence the dedupe key.
_PLACEHOLDER_EVENT_ID: Final[str] = "00000000-0000-0000-0000-000000000000"
_PLACEHOLDER_RECORDED_AT: Final[str] = "1970-01-01T00:00:00Z"


async def parse_body[T](request: Request, adapter: TypeAdapter[T], context: str) -> T:
    """Validate the raw request body against `adapter`, in JSON mode.

    Preconditions:
        none — an empty or unparseable body is a client error, not a precondition

    Postconditions:
        strict validation: a float where an int is declared is rejected
        JSON scalar forms for `dt.date`, `dt.datetime` and `UUID` are accepted, which
        is the only representation JSON has for them

    Raises:
        AppError(VALIDATION_FAILED) for an empty, unparseable, or invalid body.
    """
    raw = await request.body()
    if not raw.strip():
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            f"{context}: request body is empty",
            {"context": context},
        )
    try:
        return adapter.validate_json(raw)
    except ValidationError as exc:
        raise validation_app_error(exc, context) from exc


def canonical_event_payload(raw: Mapping[str, object]) -> dict[str, object]:
    """An inbound event object as a payload `ingestion.normalize_event` can consume.

    Preconditions:
        `raw` is a JSON object: every value is a JSON scalar, list, or object

    Postconditions:
        every value is the python type the event union declares — `dt.date` for a date,
        `int` for a `Minor`, never the string or float the wire carried
        the three server-assigned fields are absent, EXCEPT that a client-supplied
        `event_id` or `dedupe_key` is passed through verbatim for `normalize_event` to
        honour (its `_resolve_event_id` / `_resolve_dedupe_key` do exactly that)
        a pure function of `raw`: no clock, no database, no generated identifier

    Raises:
        AppError(VALIDATION_FAILED) if the object is not a valid event of its own
        declared type.
        AppError(PAYMENT_SPLIT_MISMATCH) / AppError(TRANSFER_SAME_ACCOUNT) unwrapped
        from the model validators that raise them, so the caller can tell those two
        cases apart from a generic rejection — the same propagation `normalize_event`
        documents.

    The round trip through `json.dumps` is what selects pydantic's json mode; see the
    module docstring for why python mode is not usable here. It costs one serialization
    per appended event and buys the only validation path in the codebase that is
    simultaneously strict about money and correct about dates.
    """
    body: dict[str, object] = {
        name: value for name, value in raw.items() if name not in _SERVER_ASSIGNED
    }
    body["event_id"] = _PLACEHOLDER_EVENT_ID
    body["recorded_at"] = _PLACEHOLDER_RECORDED_AT
    # Empty rather than absent: `dedupe_key` is a required `str`, and
    # `normalize_event._resolve_dedupe_key` treats an empty one as absent and computes
    # the natural key instead.
    body["dedupe_key"] = ""

    try:
        event = _EVENT_ADAPTER.validate_json(_encode(body, "Event"))
    except ValidationError as exc:
        raise validation_app_error(exc, "Event") from exc

    payload: dict[str, object] = event.model_dump()
    for name in _SERVER_ASSIGNED:
        payload.pop(name, None)
    # A client that did supply an id or a key keeps it. Both are passed on in the form
    # they arrived: `normalize_event` parses a string uuid and accepts a string key.
    for name in ("event_id", "dedupe_key"):
        if name in raw:
            payload[name] = raw[name]
    return payload


def canonical_definition_version(
    kind: DefinitionKind, raw: Mapping[str, object]
) -> DefinitionBase:
    """An inbound definition object as the concrete versioned model for `kind`.

    Preconditions:
        `raw` is a JSON object carrying `kind`'s own fields; `version_id` and
        `recorded_at` are ignored if present — both are server-assigned

    Postconditions:
        returns an instance of `DEFINITION_MODEL_BY_KIND[kind]` with the caller's
        `version_id` and `recorded_at`
        the model's own invariants have already run: `EFFECTIVE_RANGE_INVALID` and
        `POLICY_BPS_NOT_10000` are raised here, on construction, not later

    Raises:
        AppError(VALIDATION_FAILED) for a body that is not a valid version of `kind`.
        AppError(EFFECTIVE_RANGE_INVALID) / AppError(POLICY_BPS_NOT_10000) unwrapped
        from the model validators.
    """
    model_cls = DEFINITION_MODEL_BY_KIND[kind]
    adapter: TypeAdapter[DefinitionBase] = TypeAdapter(model_cls)
    encoded = _encode(dict(raw), model_cls.__name__)
    try:
        return adapter.validate_json(encoded)
    except ValidationError as exc:
        raise validation_app_error(exc, model_cls.__name__) from exc


def with_server_fields(
    raw: Mapping[str, object], *, version_id: str, recorded_at: str
) -> dict[str, object]:
    """`raw` with the two server-assigned definition fields overwritten.

    Separate from `canonical_definition_version` so the ids are assigned by the router,
    where a single request's identity decisions live together, rather than deep inside a
    validation helper where a test could not pin them.
    """
    body: dict[str, object] = {
        name: value
        for name, value in raw.items()
        if name not in ("version_id", "recorded_at")
    }
    body["version_id"] = version_id
    body["recorded_at"] = recorded_at
    return body


def _encode(body: Mapping[str, object], context: str) -> bytes:
    """`body` as JSON bytes, or `VALIDATION_FAILED`.

    A value that came from `json.loads` always re-encodes. One that did not — a python
    object smuggled in by a caller inside `api/` — is a programming error caught here
    rather than surfacing as a confusing pydantic message three frames down.
    """
    try:
        return json.dumps(body).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            f"{context}: payload is not a JSON object",
            {"context": context, "error": str(exc)},
        ) from exc
