"""Event models, the discriminated union, and dedupe-key computation.

Owned by `module/domain-events` (PLAN.md §13.2). Pure: no I/O, no clock, no DB.

Event classes are past tense because the ledger records what happened, not what should
(CLAUDE.md §3.2). `PaymentMade`, never `MakePayment` or `Payment`.

Read PLAN.md §1 before touching anything here. The recurring failure mode is
double-counting: recording a card purchase as an expense *and* the card statement
payment as a fixed cost. `TransferMade` exists specifically to make the payment side
non-budgetary.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.types import MONEY_MODEL_CONFIG, CycleId, Minor, UtcInstant


class ExternalRef(BaseModel):
    """Provenance from an external ingestion source. Participates in the dedupe key.

    Reserved for future bank/card aggregation (PLAN.md §9). Nothing reads it today
    beyond dedupe; do not branch on it.
    """

    model_config = MONEY_MODEL_CONFIG
    provider: str
    provider_txn_id: str


class EventBase(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    date: dt.date  # business date; decides period membership
    recorded_at: UtcInstant  # tz-aware UTC (enforced); audit + tie-break only, never period membership
    dedupe_key: str  # natural key; UNIQUE in persistence
    external_ref: ExternalRef | None = None
    note: str | None = None


class IncomeReceived(EventBase):
    """External income. Contributes to allocatable_income."""

    event_type: Literal["IncomeReceived"] = "IncomeReceived"
    amount_minor: Minor  # > 0
    source: str
    account_id: str  # where it landed


class GiftReceived(EventBase):
    """Gift income. Folds into allocatable_income identically to IncomeReceived;
    the separate type exists only to label the source for reporting."""

    event_type: Literal["GiftReceived"] = "GiftReceived"
    amount_minor: Minor  # > 0
    source: str
    account_id: str


class ObligationRaised(EventBase):
    """An explicit bill. Supersedes the expected obligation materialized from the
    FixedCost with the same recurring_id in the same due-period (PLAN.md §8.1)."""

    event_type: Literal["ObligationRaised"] = "ObligationRaised"
    obligation_id: str
    due_date: dt.date  # decides period membership, NOT `date`
    amount_minor: Minor  # > 0
    payee: str
    category: str
    recurring_id: str | None = None


class PaymentMade(EventBase):
    """Payment against an obligation. Changes status only; never changes allocation
    (accrual basis, PLAN.md §6)."""

    event_type: Literal["PaymentMade"] = "PaymentMade"
    amount_minor: Minor  # > 0
    obligation_id: str
    account_id: str  # paid from
    principal_minor: Minor | None = None
    interest_minor: Minor | None = None
    # If either split field is set, both must be, and they must sum to amount_minor.


class ExpenseRecorded(EventBase):
    """Discretionary spending. Reduces discretionary_remaining, subject to the
    account's budget_timing when charged to a credit card (PLAN.md §6.4).
    `category` is a chart label with no allocation semantics."""

    event_type: Literal["ExpenseRecorded"] = "ExpenseRecorded"
    amount_minor: Minor  # negative permitted: a refund
    category: str
    account_id: str
    merchant: str | None = None


class SavingsDrawn(EventBase):
    """A deliberate top-up of discretionary from savings, beyond the automatic
    shortfall drain (PLAN.md §6.2). Exceeding the available balance raises a
    warning, never an error."""

    event_type: Literal["SavingsDrawn"] = "SavingsDrawn"
    amount_minor: Minor  # > 0
    reason: str


class TransferMade(EventBase):
    """Money between the user's own accounts. Budget-neutral by construction —
    this is the mechanism that prevents credit-card double-counting."""

    event_type: Literal["TransferMade"] = "TransferMade"
    amount_minor: Minor  # > 0; direction carried by the account fields
    from_account_id: str
    to_account_id: str


class AccountOpeningBalance(EventBase):
    """Signed opening balance. Covers both "opened checking with $500"
    (positive) and a loan disbursement (negative — a liability).
    Never contributes to allocatable_income."""

    event_type: Literal["AccountOpeningBalance"] = "AccountOpeningBalance"
    account_id: str
    amount_minor: Minor  # signed


class InterestCharged(EventBase):
    """Actual interest from a statement. Supersedes the projection's estimate for
    this cycle and pins it against cascade (PLAN.md §7.4)."""

    event_type: Literal["InterestCharged"] = "InterestCharged"
    account_id: str
    cycle_id: CycleId
    amount_minor: Minor  # > 0


class InterestEarned(EventBase):
    """Actual interest credited to an asset account. Not allocatable income."""

    event_type: Literal["InterestEarned"] = "InterestEarned"
    account_id: str
    cycle_id: CycleId
    amount_minor: Minor  # > 0


class EventVoided(EventBase):
    """The ONLY correction mechanism. The projection filters the target before
    folding. Amount corrections are void + re-raise (PLAN.md §8.4)."""

    event_type: Literal["EventVoided"] = "EventVoided"
    target_event_id: UUID
    reason: str


Event = Annotated[
    IncomeReceived
    | GiftReceived
    | ObligationRaised
    | PaymentMade
    | ExpenseRecorded
    | SavingsDrawn
    | TransferMade
    | AccountOpeningBalance
    | InterestCharged
    | InterestEarned
    | EventVoided,
    Field(discriminator="event_type"),
]


def compute_dedupe_key(
    event_type: str,
    payload: Mapping[str, object],
    *,
    content_sha256: str | None = None,
    external_ref: ExternalRef | None = None,
    client_nonce: str | None = None,
) -> str:
    """Derive the natural dedupe key (§3.1).

    Precedence: content_sha256 > external_ref > manual composite.

    Preconditions:
        payload contains every discriminating field for event_type

    Postconditions:
        deterministic — identical inputs yield an identical key across processes
        (no dict-ordering or hash-randomization dependence)
        the same receipt bytes always yield the same key
        client_nonce, when given, is folded in so a deliberate duplicate survives
    """
    raise NotImplementedError


def is_voided(event: Event, voids: Mapping[UUID, EventVoided]) -> bool:
    """Postcondition: True iff an EventVoided targets event.event_id."""
    raise NotImplementedError
