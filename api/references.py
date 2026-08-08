"""Write-time reference checks: `UNKNOWN_ACCOUNT` and `UNKNOWN_OBLIGATION`.

Owned by `module/api` (PLAN.md §13.2).

These are the two rows of the CONTRACTS.md §7.1 table that no other layer can raise.
`domain/` cannot: `project()` is pure and folds whatever it is handed, and an event
referencing a since-voided obligation must keep folding rather than blowing up.
`persistence/` cannot: there is no foreign key from `events.account_id` to `accounts`,
deliberately, because the events table is immutable and an account definition arriving
after a backdated event must not be prevented from arriving.

So the check lives exactly where §7.1 says it lives — **at write time**, against what
is known then:

> `UNKNOWN_OBLIGATION` is checked **only at write time**, against obligations known
> then. A payment can still end up orphaned later if its obligation is voided — that
> surfaces as the `PAYMENT_WITHOUT_OBLIGATION` warning, not an error, because the
> ledger is append-only and the projection must survive it.

That asymmetry is the point and it is not a gap. Rejecting a typo'd `account_id` at the
moment a human types it is cheap; refusing to *fold* a ledger that already contains one
would make the tool unusable the first time a definition is superseded.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import (
    AccountOpeningBalance,
    Event,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    TransferMade,
)
from domain.projection import project
from persistence.repositories import DefinitionRepository, EventRepository


def check_references(
    session: Session, events: Sequence[Event], *, as_of_date: dt.date
) -> None:
    """Reject events naming an account or obligation the ledger does not know.

    Preconditions:
        `events` are canonical and validated; this checks references, not shape

    Postconditions:
        returns None, or raises before anything is written
        never mutates, never writes, never warns — a reference that is merely
        surprising is the projection's business (CONTRACTS.md §7)

    Raises:
        AppError(UNKNOWN_ACCOUNT) — 422 — an account_id matching no `Account` version.
        AppError(UNKNOWN_OBLIGATION) — 422 — a `PaymentMade.obligation_id` unknown now.

    `events` is checked as a set, so an `ObligationRaised` earlier in the same batch
    satisfies a `PaymentMade` later in it: the batch commits as one transaction, so
    "known at write time" means known once the batch has been written, not known before
    it started.

    The projection is only run when a payment is actually present, and once for the
    whole call. Obligations are not all events: an expected obligation is *materialized*
    from a `FixedCost` definition and never appears in the ledger at all (PLAN.md §8.1),
    so the only complete answer to "does this obligation exist" is the fold's own, and
    re-deriving it here with a second copy of the expansion rule is exactly the drift
    CLAUDE.md §6 forbids.
    """
    _check_accounts(session, events)
    _check_obligations(session, events, as_of_date=as_of_date)


def _check_accounts(session: Session, events: Sequence[Event]) -> None:
    referenced = sorted(
        {account_id for event in events for account_id in _account_ids(event)}
    )
    if not referenced:
        return
    repository = DefinitionRepository(session)
    for account_id in referenced:
        if not repository.list_accounts(entity_id=account_id):
            raise AppError(
                ErrorCode.UNKNOWN_ACCOUNT,
                f"no Account definition for {account_id!r}",
                {"account_id": account_id},
            )


def _check_obligations(
    session: Session, events: Sequence[Event], *, as_of_date: dt.date
) -> None:
    payments = [event for event in events if isinstance(event, PaymentMade)]
    if not payments:
        return

    known = _projected_obligation_ids(session, events, as_of_date)
    for payment in payments:
        if payment.obligation_id not in known:
            raise AppError(
                ErrorCode.UNKNOWN_OBLIGATION,
                f"no obligation {payment.obligation_id!r} is known at write time",
                {
                    "obligation_id": payment.obligation_id,
                    "event_id": str(payment.event_id),
                },
            )


def _projected_obligation_ids(
    session: Session, events: Sequence[Event], as_of_date: dt.date
) -> frozenset[str]:
    """Every obligation the ledger will know about once `events` are written.

    Two details, both of which the naive version gets wrong:

    * **The incoming events are folded in.** An `ObligationRaised` earlier in the same
      batch has to satisfy a `PaymentMade` later in it, and — less obviously — an event
      dated before every stored one moves *genesis* backwards, which is what causes the
      expected obligations for the intervening periods to be materialized at all. On an
      empty ledger, projecting without the incoming payment produces no periods and
      therefore no expected rent, and a perfectly good first payment would be rejected.
    * **The horizon is the latest date in play**, not `as_of_date`. Paying a bill that
      falls due next month is ordinary, and an obligation expected in a future period
      would otherwise not exist yet.
    """
    horizon = max([as_of_date, *(event.date for event in events)])
    state = project(
        (*EventRepository(session).list_all(), *events),
        DefinitionRepository(session).load_definitions(),
        horizon,
    )
    return frozenset(row.obligation_id for row in state.obligations)


def _account_ids(event: Event) -> tuple[str, ...]:
    """Every account this event names. A transfer names two."""
    match event:
        case (
            IncomeReceived()
            | GiftReceived()
            | ExpenseRecorded()
            | PaymentMade()
            | AccountOpeningBalance()
            | InterestCharged()
            | InterestEarned()
        ):
            return (event.account_id,)
        case TransferMade():
            return (event.from_account_id, event.to_account_id)
        case _:
            return ()
