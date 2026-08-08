"""Definition endpoints (CONTRACTS.md §6.2, last two rows).

Owned by `module/api` (PLAN.md §13.2).

`kind` is a `DefinitionKind`, whose values *are* the path segments
(`recurring-income | fixed-cost | allocation-policy | account`). `persistence.mapping`
spells them that way deliberately so this router needs no second translation table —
which is where the two would drift.

**Definitions are versioned; nothing is ever edited.** A change is a new version with
`effective_to` set on the prior one, and closing a version is the only `UPDATE` in the
codebase (CLAUDE.md §4.3). Both steps happen here, in that order, because
`DefinitionRepository.add_version` refuses to infer the close from the insert: when the
old version stops and when the new one starts are two decisions, and guessing the first
from the second is how a one-day gap or a silent overwrite gets in.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, status
from pydantic import TypeAdapter
from starlette.requests import Request

from api.clock import now_utc
from api.deps import AsOfDep, SessionDep
from api.dtos import (
    DefinitionListResponse,
    NewDefinitionVersionRequest,
    NewDefinitionVersionResponse,
)
from api.payloads import canonical_definition_version, parse_body, with_server_fields
from core.types import AppError, ErrorCode
from domain.definitions import (
    Account,
    AllocationPolicy,
    DefinitionBase,
    FixedCost,
    RecurringIncome,
    resolve_version,
)
from persistence.mapping import DefinitionKind, DefinitionVersion
from persistence.repositories import DefinitionRepository

router = APIRouter(tags=["definitions"])

_NEW_VERSION_REQUEST: TypeAdapter[NewDefinitionVersionRequest] = TypeAdapter(
    NewDefinitionVersionRequest
)


@router.get(
    "/definitions/{kind}",
    response_model=DefinitionListResponse,
    summary="Definition versions of one kind",
)
def read_definitions(
    kind: DefinitionKind,
    session: SessionDep,
    as_of_date: AsOfDep,
    include_history: Annotated[
        bool, Query(description="All versions, not just the effective one")
    ] = False,
) -> DefinitionListResponse:
    """The versions of `kind`, effective at `as_of_date` or all of them.

    With `include_history=false` this is at most one version per `entity_id` — the one
    effective at `as_of_date`, resolved by `domain.definitions.resolve_version` rather
    than by a `WHERE` clause here, so "effective at" has one definition in the codebase
    and the half-open `[effective_from, effective_to)` convention is stated once.

    An entity with no version effective at that date is simply absent. That is not an
    error: an account opened next month genuinely does not exist today, and a caller
    time-travelling to before it opened is asking a well-formed question.
    """
    versions = DefinitionRepository(session).list_versions(kind)
    selected = versions if include_history else _effective(versions, as_of_date)
    return DefinitionListResponse(
        kind=kind,
        as_of_date=as_of_date,
        include_history=include_history,
        versions=tuple(_as_concrete(version) for version in selected),
    )


@router.post(
    "/definitions/{kind}",
    status_code=status.HTTP_201_CREATED,
    response_model=NewDefinitionVersionResponse,
    summary="Append a new version of a definition",
)
async def create_definition_version(
    kind: DefinitionKind, request: Request, session: SessionDep
) -> NewDefinitionVersionResponse:
    """Append a version, optionally closing the entity's open one first.

    `version_id` and `recorded_at` are server-assigned, exactly as `event_id` and
    `recorded_at` are on an event: an identifier the client chose would let two clients
    collide, and a `recorded_at` the client chose would be a clock read wearing a
    disguise (`ingestion/append.py` makes the same argument at greater length).

    The rejections, all from layers below this one:

    * `EFFECTIVE_RANGE_INVALID` (422) — `effective_to <= effective_from`, raised by
      `DefinitionBase`'s own validator on construction.
    * `POLICY_BPS_NOT_10000` (422) — raised by `AllocationPolicy`'s validator.
    * `OVERLAPPING_VERSIONS` (409) — raised by `add_version`, which checks with
      `domain.definitions.validate_no_overlap` so overlap means one thing everywhere.

    All three leave nothing written: the request's session is rolled back by
    `api.deps.get_session`, so a close that succeeded before an insert that failed is
    undone with it.
    """
    body = await parse_body(request, _NEW_VERSION_REQUEST, "NewDefinitionVersionRequest")
    version = _as_concrete(
        canonical_definition_version(
            kind,
            with_server_fields(
                body.version,
                version_id=str(uuid4()),
                recorded_at=now_utc().isoformat(),
            ),
        )
    )

    repository = DefinitionRepository(session)
    closed = (
        None
        if body.close_previous_at is None
        else _close_open_version(
            repository, kind, version.entity_id, body.close_previous_at
        )
    )
    repository.add_version(version)
    return NewDefinitionVersionResponse(
        kind=kind,
        version_id=version.version_id,
        entity_id=version.entity_id,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        closed_previous_version_id=closed,
    )


# ------------------------------------------------------------------------ internals


def _close_open_version(
    repository: DefinitionRepository,
    kind: DefinitionKind,
    entity_id: str,
    effective_to: dt.date,
) -> UUID:
    """Close the entity's single open-ended version.

    At most one can exist — a partial unique index in the schema enforces it — so "the
    open one" is unambiguous. Closing an entity that has none is `VALIDATION_FAILED`
    rather than a silent no-op: the caller asked for a supersession that cannot happen,
    and proceeding would create the very overlap `close_previous_at` exists to avoid.
    """
    open_versions = [
        version
        for version in repository.list_versions(kind, entity_id=entity_id)
        if version.effective_to is None
    ]
    if not open_versions:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            f"{entity_id!r} has no open-ended version to close",
            {"kind": kind.value, "entity_id": entity_id},
        )
    version_id = open_versions[0].version_id
    repository.close_version(kind, version_id, effective_to)
    return version_id


def _effective(
    versions: tuple[DefinitionBase, ...], at: dt.date
) -> tuple[DefinitionBase, ...]:
    """One version per `entity_id`: the one effective at `at`, where there is one."""
    entity_ids: list[str] = []
    for version in versions:
        if version.entity_id not in entity_ids:
            entity_ids.append(version.entity_id)
    resolved = (resolve_version(versions, entity_id, at) for entity_id in entity_ids)
    return tuple(version for version in resolved if version is not None)


def _as_concrete(version: DefinitionBase) -> DefinitionVersion:
    """Narrow `DefinitionBase` to the concrete union the response and repository take.

    `list_versions` is kind-dispatched and returns the base type because its caller
    routed on a string, and both `DefinitionListResponse.versions` and
    `add_version` are typed against the concrete union — pydantic serializes to the
    *declared* type, so a base-typed response field would silently drop `apr_bps` and
    every other subclass field. This is the one place the two meet.
    """
    if isinstance(version, (RecurringIncome, FixedCost, AllocationPolicy, Account)):
        return version
    raise AppError(  # pragma: no cover - the four kinds are the whole union
        ErrorCode.INTERNAL,
        "definition version is not one of the four concrete kinds",
        {"type": type(version).__name__},
    )
