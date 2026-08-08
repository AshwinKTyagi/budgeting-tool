"""Request-scoped dependencies: the session, the resolved `as_of`, and the projection.

Owned by `module/api` (PLAN.md §13.2).

Two things are decided here once so that no router decides them twice:

* **The transaction boundary is the request.** `persistence/` deliberately commits
  nowhere — "Transactions belong to the caller" — precisely so that `POST /receipts`
  can write a blob and the event referencing it atomically. The caller is this
  dependency.
* **Every read recomputes from genesis.** `load_state` reads the whole ledger and every
  definition version and folds them (PLAN.md §3). There is no cache, and the one that
  would be correct — keyed on `(max(recorded_at), as_of_date)` — is explicitly deferred.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from api.clock import budget_tz, resolve_as_of
from core.periods import CalendarMonthResolver, PeriodResolver
from domain.projection import State, project
from persistence.engine import get_session_factory
from persistence.repositories import DefinitionRepository, EventRepository

#: The only resolver built (PLAN.md §4.1). Stateless, so one instance is shared; a
#: paycheck-driven resolver would be selected here and nowhere else.
_RESOLVER: PeriodResolver = CalendarMonthResolver()


def get_resolver() -> PeriodResolver:
    """The period resolver for this request."""
    return _RESOLVER


def get_session() -> Iterator[Session]:
    """A session whose transaction is the request.

    Commits on a clean return, rolls back on any exception — including an `AppError`
    raised by a repository, which must leave nothing written. The rollback happens
    inside the dependency's own teardown, before the exception reaches the handler that
    turns it into an HTTP body, so a 409 or a 422 can never leave a half-written batch
    behind.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_as_of(
    as_of: Annotated[
        dt.date | None,
        Query(
            description=(
                "Business date the answer describes. Omitted means today in BUDGET_TZ; "
                "a future date is a valid forecast query (CONTRACTS.md §6.3)."
            )
        ),
    ] = None,
) -> dt.date:
    """The resolved `as_of_date` for a read endpoint.

    A dependency rather than a per-router line, so that "the clock is read in exactly
    one place" survives the addition of the next read endpoint.
    """
    return resolve_as_of(as_of, budget_tz())


SessionDep = Annotated[Session, Depends(get_session)]
AsOfDep = Annotated[dt.date, Depends(get_as_of)]
ResolverDep = Annotated[PeriodResolver, Depends(get_resolver)]


def load_state(session: Session, as_of_date: dt.date) -> State:
    """Fold the whole ledger and every definition version into `State`.

    `list_all()` includes voided events and their `EventVoided` records — filtering them
    is step 1 of the fold, not a storage concern. `load_definitions()` returns ALL
    versions, not the currently-effective ones, because the projection resolves per
    period and per statement cycle (PLAN.md §8.3).

    Warnings in the returned `State` are data. They are never inspected here and never
    become an HTTP error (CONTRACTS.md §7).
    """
    events = EventRepository(session).list_all()
    definitions = DefinitionRepository(session).load_definitions()
    return project(events, definitions, as_of_date, resolver=_RESOLVER)
