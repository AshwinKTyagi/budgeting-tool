"""Forecast occurrences awaiting confirmation (PLAN.md §8.5).

Owned by `module/api` (PLAN.md §13.2).

This is a *view*, like `api/ledger.py`, and it is pure: every function here is a
function of its arguments, with no session, no clock, and no writes. The router does
the I/O and hands the results in.

Three forecasts already exist in the system and none of them is a ledger row:

* an **expected obligation**, which `project()` materializes from a `FixedCost`
* an **estimated interest** figure, which the statement-cycle fold computes
* a **forecast paycheck**, which `expand_recurring_incomes` derives from a
  `RecurringIncome`

Confirming one appends the real event it stands for. Nothing here decides money: every
`amount_minor` is copied off the forecast unchanged, and the only figure a confirmation
can change is one the user typed.

**The suppression rule.** A suggestion is offered iff no event carries its
`suggestion_id` as a `dedupe_key`. Confirm, edit-then-confirm, and reject all append a
row bearing that key, so each suppresses the occurrence permanently against the UNIQUE
index that already exists on that column. There is no dismissal table and no new state
— which is why rejecting is append-then-void rather than a delete, and why a rejected
occurrence stays visible in the ledger as the history it is.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping, Sequence

from api.dtos import Suggestion, SuggestionConfirmRequest, SuggestionKind
from core.types import AppError, ErrorCode, ObligationSource, ObligationStatus
from domain.accounts import StatementCycleSummary
from domain.definitions import Definitions, ExpectedIncome, expand_recurring_incomes
from domain.projection import ObligationRow, State

#: Every suggestion id starts here, which is also what `api/ledger.py::_origin` reads
#: to label a confirmed row `expected`.
_PREFIX = "expected"

_ONE_DAY = dt.timedelta(days=1)

#: The default `note` on a rejected occurrence and the `reason` on the `EventVoided`
#: that kills it. Both are recorded so the ledger distinguishes "I corrected this" from
#: "this never happened" -- the two look identical otherwise, and only one of them is a
#: correction.
REJECTED_NOTE = "Rejected by user: this forecast occurrence did not happen."
REJECTED_REASON = "rejected by user: forecast occurrence did not happen"


def build_suggestions(
    state: State,
    definitions: Definitions,
    *,
    suppressed: Collection[str],
) -> tuple[Suggestion, ...]:
    """Every unsuppressed forecast occurrence at or before `state.as_of_date`.

    Postconditions:
        every row's date is <= state.as_of_date -- nothing future is ever offered
        no row's suggestion_id is in `suppressed`
        sorted by (date, suggestion_id); a total key, so the order is stable
        no I/O, no clock, no writes

    Future occurrences are withheld on purpose. `project()` already refuses to count an
    event dated after `as_of_date`, and offering a paycheck before it has arrived would
    invite confirming money that does not exist -- the one error direction PLAN.md §8.2
    exists to prevent.
    """
    rows = (
        *_income_suggestions(definitions, state.as_of_date),
        *_bill_suggestions(state.obligations, state.as_of_date),
        *_interest_suggestions(state.statement_cycles, state.as_of_date),
    )
    return tuple(
        sorted(
            (row for row in rows if row.suggestion_id not in suppressed),
            key=lambda row: (row.date, row.suggestion_id),
        )
    )


def _income_suggestions(
    definitions: Definitions, as_of_date: dt.date
) -> Sequence[Suggestion]:
    """Forecast paychecks, from every `RecurringIncome` version.

    The window opens at `dt.date.min` rather than at the projection's genesis. Genesis
    follows the ledger ("periods follow the ledger, not the forecast",
    `domain/projection.py::_genesis`), so on an empty ledger it is today — and a salary
    effective four weeks ago would produce nothing to confirm, which is precisely the
    case this feature exists for. `expand_recurring_incomes` floors each version at its
    own `effective_from` regardless, so the open lower bound adds no occurrence that the
    definition did not already declare.
    """
    return tuple(
        _income_suggestion(row)
        for row in expand_recurring_incomes(
            definitions.recurring_incomes, dt.date.min, as_of_date
        )
    )


def _income_suggestion(row: ExpectedIncome) -> Suggestion:
    return Suggestion(
        suggestion_id=row.income_id,
        kind=SuggestionKind.INCOME,
        event_type="IncomeReceived",
        entity_id=row.entity_id,
        date=row.date,
        amount_minor=row.amount_minor,
        description=row.name,
        account_id=row.account_id,
        counterparty=row.name,
        category=None,
    )


def _bill_suggestions(
    obligations: Sequence[ObligationRow], as_of_date: dt.date
) -> Sequence[Suggestion]:
    """Expected obligations that have come due and nothing has been paid against.

    `PARTIALLY_PAID` and `PAID` are excluded: paying a bill is already an answer about
    whether it happened, and re-asking would invite a second obligation for money
    already settled.

    An expected obligation always carries a `recurring_id` (it is the `FixedCost`'s
    `entity_id`); the guard is for the type, not for a case that occurs.
    """
    return tuple(
        _bill_suggestion(row)
        for row in obligations
        if row.source is ObligationSource.EXPECTED
        and row.status is ObligationStatus.UNPAID
        and row.due_date <= as_of_date
        and row.recurring_id is not None
    )


def _bill_suggestion(row: ObligationRow) -> Suggestion:
    return Suggestion(
        suggestion_id=f"{_PREFIX}:bill:{row.recurring_id}:{row.period_id}",
        kind=SuggestionKind.BILL,
        event_type="ObligationRaised",
        entity_id=str(row.recurring_id),
        date=row.due_date,
        amount_minor=row.amount_minor,
        description=row.payee,
        account_id=None,
        counterparty=row.payee,
        category=row.category,
    )


def _interest_suggestions(
    cycles: Sequence[StatementCycleSummary], as_of_date: dt.date
) -> Sequence[Suggestion]:
    """Closed statement cycles still carrying an estimate.

    A cycle whose interest is zero is not offered — there is nothing to confirm, and a
    grace-period cycle would otherwise ask the user to affirm a charge that was waived.
    `is_estimate` goes False the moment an `InterestCharged` pins the cycle, so a
    confirmed cycle drops out here for the same reason it stops being an estimate.
    """
    return tuple(
        _interest_suggestion(cycle)
        for cycle in cycles
        if cycle.is_estimate
        and cycle.interest_minor != 0
        and cycle.end_date_exclusive <= as_of_date
    )


def _interest_suggestion(cycle: StatementCycleSummary) -> Suggestion:
    # The cycle's own close date, not the exclusive bound: an interest charge lands on
    # the last day of the cycle it belongs to, and the bound is the next cycle's first.
    close_date = cycle.end_date_exclusive - _ONE_DAY
    return Suggestion(
        suggestion_id=f"{_PREFIX}:interest:{cycle.cycle_id}",
        kind=SuggestionKind.INTEREST,
        event_type="InterestCharged",
        entity_id=cycle.account_id,
        date=close_date,
        amount_minor=cycle.interest_minor,
        description=f"Interest on {cycle.account_id}",
        account_id=cycle.account_id,
        counterparty=None,
        category=None,
    )


def find_suggestion(
    suggestions: Sequence[Suggestion], suggestion_id: str
) -> Suggestion:
    """The suggestion with that id.

    Raises:
        AppError(UNKNOWN_EVENT) when nothing matches. A suggestion is derived, so an id
        that resolved a moment ago legitimately stops resolving once it is confirmed,
        rejected, or its definition is closed. That is a 404 about a ledger entity, not
        a malformed request — the id itself was well-formed.
    """
    for suggestion in suggestions:
        if suggestion.suggestion_id == suggestion_id:
            return suggestion
    raise AppError(
        ErrorCode.UNKNOWN_EVENT,
        "no pending suggestion with that id; it may already be confirmed or rejected",
        {"suggestion_id": suggestion_id},
    )


def suggestion_payload(
    suggestion: Suggestion,
    edits: SuggestionConfirmRequest,
    *,
    note: str | None = None,
) -> Mapping[str, object]:
    """The canonical event payload a confirmation appends.

    Postconditions:
        payload["dedupe_key"] == suggestion.suggestion_id, whatever the edits say
        payload names every field the event type declares

    The key is pinned deliberately. `normalize_event` honours a supplied `dedupe_key`,
    and pinning it is what makes an edited confirmation still suppress its occurrence:
    change the amount or the date and it is the same paycheck, corrected, not a second
    one. It also means a rejected occurrence can never be re-offered, since the
    rejection wrote a row under that key too.
    """
    date = suggestion.date if edits.date is None else edits.date
    amount_minor = (
        suggestion.amount_minor if edits.amount_minor is None else edits.amount_minor
    )
    counterparty = (
        suggestion.counterparty if edits.counterparty is None else edits.counterparty
    )
    account_id = (
        suggestion.account_id if edits.account_id is None else edits.account_id
    )
    category = suggestion.category if edits.category is None else edits.category
    common: dict[str, object] = {
        "date": date,
        "amount_minor": amount_minor,
        "dedupe_key": suggestion.suggestion_id,
        "note": note if edits.note is None else edits.note,
    }
    match suggestion.kind:
        case SuggestionKind.INCOME:
            return {
                **common,
                "event_type": "IncomeReceived",
                "source": counterparty,
                "account_id": account_id,
                "recurring_id": suggestion.entity_id,
            }
        case SuggestionKind.BILL:
            # `obligation_id` is the expected row's own id, so a payment already made
            # against the expected obligation stays attached to the raised one. The
            # period comes from `due_date`, so both dates move together when edited.
            return {
                **common,
                "event_type": "ObligationRaised",
                "obligation_id": f"{_PREFIX}:{suggestion.entity_id}:{_period_of(suggestion)}",
                "due_date": date,
                "payee": counterparty,
                "category": category,
                "recurring_id": suggestion.entity_id,
            }
        case SuggestionKind.INTEREST:
            return {
                **common,
                "event_type": "InterestCharged",
                "account_id": account_id,
                "cycle_id": _cycle_of(suggestion),
            }


def _period_of(suggestion: Suggestion) -> str:
    """The period id encoded in a bill suggestion's own id."""
    return suggestion.suggestion_id.rsplit(":", 1)[-1]


def _cycle_of(suggestion: Suggestion) -> str:
    """The cycle id encoded in an interest suggestion's own id.

    Split once from the left past the two fixed segments rather than from the right: a
    `CycleId` is `f"{account_id}:{PeriodId}"` and so contains a colon of its own.
    """
    return suggestion.suggestion_id.split(":", 2)[-1]
