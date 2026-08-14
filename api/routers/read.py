"""Read endpoints (CONTRACTS.md §6.2).

Owned by `module/api` (PLAN.md §13.2).

Every one of these answers from a `State` folded by `api.deps.load_state`. There is no
cached state and no materialized balance column: a backdated receipt entered today
changes the answer for every period after it, automatically, because nothing was ever
stored that could go stale (PLAN.md §3).

`as_of` is optional on all of them and resolves through `api.deps.get_as_of` — the one
place a clock is read. A supplied date is used verbatim, including a future one, which
produces a forecast-shaped `State` (§6.3).

`State.warnings` travels out as data. No warning is ever inspected here and no warning
ever becomes an HTTP error (CONTRACTS.md §7).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Annotated

from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.orm import Session

from api.charts import build_points
from api.deps import AsOfDep, ResolverDep, SessionDep, load_state
from api.dtos import (
    AccountListResponse,
    ChartGrain,
    ChartGroupBy,
    ChartMetric,
    ChartSeriesResponse,
    LedgerPageResponse,
    PeriodDetailResponse,
    PeriodListResponse,
)
from api.ledger import build_voided_index, decode_cursor, encode_cursor, to_ledger_row
from core.types import AppError, ErrorCode, PeriodId
from domain.events import Event
from domain.projection import State
from persistence.repositories import EventRepository

router = APIRouter(tags=["read"])

#: Ceiling on one `GET /ledger` page. A cursor-paginated endpoint with no ceiling is an
#: endpoint that returns the whole ledger to a client that typed a large number.
_MAX_LEDGER_LIMIT = 500
_DEFAULT_LEDGER_LIMIT = 100

FromQuery = Annotated[
    dt.date | None, Query(alias="from", description="Inclusive lower bound")
]
ToQuery = Annotated[
    dt.date | None, Query(alias="to", description="Inclusive upper bound")
]


@router.get("/state", response_model=State, summary="The complete answer at a date")
def read_state(session: SessionDep, as_of_date: AsOfDep) -> State:
    """`State` verbatim — periods from genesis, obligations, balances, cycles, savings,
    and the warnings, which are data and not errors."""
    return load_state(session, as_of_date)


@router.get("/periods", response_model=PeriodListResponse, summary="Period summaries")
def read_periods(
    session: SessionDep,
    as_of_date: AsOfDep,
    from_date: FromQuery = None,
    to_date: ToQuery = None,
) -> PeriodListResponse:
    """The `PeriodSummary` rows overlapping `[from, to]`.

    The bounds are business dates rather than period ids, matching `/ledger` and
    `/charts/series`, and a period is included when any part of it falls in the window
    — a one-day range returns the month containing that day rather than nothing.
    """
    state = load_state(session, as_of_date)
    return PeriodListResponse(
        as_of_date=state.as_of_date,
        periods=tuple(
            period
            for period in state.periods
            if (to_date is None or period.start_date <= to_date)
            and (from_date is None or period.end_date_exclusive > from_date)
        ),
    )


@router.get(
    "/periods/{period_id}",
    response_model=PeriodDetailResponse,
    summary="One period, with its obligations and warnings",
)
def read_period(
    period_id: PeriodId, session: SessionDep, as_of_date: AsOfDep
) -> PeriodDetailResponse:
    """One period of `State`, with the rows scoped to it.

    A period outside `[genesis, as_of]` is `VALIDATION_FAILED` rather than a 404. The
    §7.1 taxonomy has one not-found code and it is `UNKNOWN_EVENT`, which is about a
    ledger entity; a period is not stored, it is derived, and asking for one the fold
    never produced is a request for a figure that does not exist rather than for a
    missing row. The available range is named in `details` so the caller can correct it.
    """
    state = load_state(session, as_of_date)
    for period in state.periods:
        if period.period_id == period_id:
            return PeriodDetailResponse(
                as_of_date=state.as_of_date,
                period=period,
                obligations=tuple(
                    row for row in state.obligations if row.period_id == period_id
                ),
                warnings=tuple(
                    warning
                    for warning in state.warnings
                    if warning.period_id == period_id
                ),
            )
    raise AppError(
        ErrorCode.VALIDATION_FAILED,
        f"period {period_id!r} is outside the projected range",
        {
            "period_id": period_id,
            "available": [period.period_id for period in state.periods],
            "as_of_date": state.as_of_date.isoformat(),
        },
    )


@router.get(
    "/ledger", response_model=LedgerPageResponse, summary="The spreadsheet view"
)
def read_ledger(
    session: SessionDep,
    resolver: ResolverDep,
    from_date: FromQuery = None,
    to_date: ToQuery = None,
    types: Annotated[
        list[str] | None, Query(description="Event types to include")
    ] = None,
    account_id: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query(description="Opaque keyset cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LEDGER_LIMIT)] = _DEFAULT_LEDGER_LIMIT,
) -> LedgerPageResponse:
    """One flat row per event, cursor-paginated, newest first (CONTRACTS.md §6.2).

    Voided events are **included**, with `is_voided: true` and the id of the
    `EventVoided` that killed them. The tabular view shows history; it does not hide it.

    This is the one read that does not build a `State`. The rows are the ledger as
    entered, and `State` is what the ledger means after voids are filtered and
    obligations folded — a different question, answered by every other endpoint here.

    `total_count` counts the filtered set, not the page, so a client can render "showing
    100 of 4,312" without walking the cursor.
    """
    repository = EventRepository(session)
    page = repository.list_events(
        from_date=from_date,
        to_date=to_date,
        event_types=types,
        account_id=account_id,
        category=category,
        newest_first=True,
        after=None if cursor is None else decode_cursor(cursor),
        # One extra row is what tells a full page apart from the last page, without a
        # second COUNT and without ever returning a cursor that yields nothing.
        limit=limit + 1,
    )
    rows = page[:limit]
    voided_by = build_voided_index(repository.list_events(event_types=["EventVoided"]))
    next_cursor = (
        encode_cursor((rows[-1].date, rows[-1].recorded_at, rows[-1].event_id))
        if len(page) > limit and rows
        else None
    )
    matching = repository.list_events(
        from_date=from_date,
        to_date=to_date,
        event_types=types,
        account_id=account_id,
        category=category,
    )
    return LedgerPageResponse(
        rows=tuple(
            to_ledger_row(event, resolver=resolver, voided_by=voided_by)
            for event in rows
        ),
        next_cursor=next_cursor,
        total_count=len(matching),
    )


@router.get("/events/{event_id}", response_model=Event, summary="One stored event")
def read_event(event_id: str, session: SessionDep) -> Event:
    """The canonical event, not a ledger row — used to clone on correction.

    A malformed id is `VALIDATION_FAILED` rather than a routing 404: the caller named
    a specific event and deserves to be told the id was unreadable.
    """
    try:
        parsed = UUID(event_id)
    except ValueError as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "event_id is not a UUID",
            {"event_id": event_id},
        ) from exc
    event = EventRepository(session).get(parsed)
    if event is None:
        raise AppError(
            ErrorCode.UNKNOWN_EVENT,
            f"no event {event_id}",
            {"event_id": event_id},
        )
    return event


@router.get(
    "/charts/series",
    response_model=ChartSeriesResponse,
    summary="Every chart variation from one shape",
)
def read_chart_series(
    session: SessionDep,
    as_of_date: AsOfDep,
    metric: Annotated[ChartMetric, Query()],
    grain: Annotated[ChartGrain, Query()] = ChartGrain.PERIOD,
    group_by: Annotated[ChartGroupBy, Query()] = ChartGroupBy.NONE,
    from_date: FromQuery = None,
    to_date: ToQuery = None,
) -> ChartSeriesResponse:
    """Fold `State` into `(bucket, series, value_minor)` points.

    Every value is read off the projection, never recomputed from events — see
    `api/charts.py` for why that restraint is what keeps the recognition principle from
    being applied twice. An unsupported metric / grain / group_by combination is
    `VALIDATION_FAILED` with the supported ones named in `details`.
    """
    state = load_state(session, as_of_date)
    return ChartSeriesResponse(
        metric=metric,
        grain=grain,
        group_by=group_by,
        points=build_points(
            state,
            metric=metric,
            grain=grain,
            group_by=group_by,
            from_date=from_date,
            to_date=to_date,
            state_at=_state_at(session),
        ),
    )


@router.get(
    "/accounts", response_model=AccountListResponse, summary="Balances at a date"
)
def read_accounts(session: SessionDep, as_of_date: AsOfDep) -> AccountListResponse:
    """Signed balances, with `outstanding_minor` set for liabilities (§5.2)."""
    state = load_state(session, as_of_date)
    return AccountListResponse(
        as_of_date=state.as_of_date, accounts=state.accounts
    )


def _state_at(session: Session) -> Callable[[dt.date], State]:
    """A re-projection at an arbitrary business date, for the stock metrics.

    A balance is a value at an instant and has no field on `PeriodSummary` to read, so
    the honest answer for "savings balance at each month's close" is the projection's
    own answer at each of those dates. Time travel is free precisely because the fold
    reads no clock (PLAN.md §4.2), so this costs one extra fold per bucket and nothing
    else — no cache to invalidate and no second definition of a balance.
    """

    def at(date: dt.date) -> State:
        return load_state(session, date)

    return at
