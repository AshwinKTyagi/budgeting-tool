"""Request/response DTOs (CONTRACTS.md §6).

All money in responses is `_minor` integers — the API never emits a formatted currency
string or a float (CONTRACTS.md §6).

The first half of this module transcribes the DTOs CONTRACTS.md §6 defines with fields.
Several response types appear in the §6.2 endpoint table with no field definition:
`AppendBatchRequest`, `AppendBatchResponse`, `VoidRequest`, `PeriodListResponse`,
`PeriodDetailResponse`, `AccountListResponse`, `DefinitionListResponse`, and
`NewDefinitionVersionRequest`. Their shapes were unspecified, so Phase 0.5 left them for
`module/api` to define inside its own owned subtree rather than invent them (CLAUDE.md
§6). They are defined at the bottom of this file, under "§6 — shapes the table names but
does not define". Each one reuses a `domain/` model wherever the domain already says the
right thing; none restates a money field that `State` already carries.

**Two shapes here are deliberately un-typed at the field level** — `InboundEventRequest.
event` and `NewDefinitionVersionRequest.version` are `dict[str, object]`. Both are
canonicalized one line later, strictly, against the frozen model they are a wire form of
(`api/payloads.py`). See `InboundEventRequest` for why that is the honest spelling rather
than a weakening.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from core.types import MONEY_MODEL_CONFIG, Minor, PeriodId, UtcInstant
from domain.accounts import AccountBalance
from domain.definitions import Account, AllocationPolicy, FixedCost, RecurringIncome
from domain.events import Event
from domain.projection import ObligationRow, PeriodSummary, Warning as StateWarning
from persistence.mapping import DefinitionKind


# ------------------------------------------------------------------------- §6.1
# Ingestion. Idempotent: re-uploading an identical receipt returns 200 with
# `deduplicated: true` and the existing event_id. It is not an error and must not be
# reported as one.


class AppendEventRequest(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event: Event  # discriminated union; event_id/dedupe_key
    # server-assigned if omitted
    client_nonce: str | None = None  # appended to dedupe_key to permit
    # genuinely-duplicate manual entries

    # NOTE for module/api: `EventBase` declares `event_id` and `dedupe_key` as
    # required, so "server-assigned if omitted" cannot be expressed by this field
    # as-is. Resolving that is an api-layer concern (an inbound model that omits both,
    # then constructs the canonical Event) and does not change the frozen event
    # contract. Raise it if you conclude otherwise.


class AppendEventResponse(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    dedupe_key: str
    deduplicated: bool  # True == no-op, event already existed


# ------------------------------------------------------------------------- §6.4


class ReceiptUploadResponse(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    blob_id: str
    content_sha256: str
    deduplicated: bool


# ------------------------------------------------------------------------- §6.2
# The spreadsheet view: one flat row per event, cursor-paginated, newest first by
# (date, recorded_at, event_id). Voided events are INCLUDED with is_voided=true — the
# tabular view shows history, it does not hide it.


class LedgerRow(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    event_type: str
    date: dt.date
    recorded_at: UtcInstant
    period_id: PeriodId
    amount_minor: Minor | None
    account_id: str | None
    counterparty: str | None  # source / payee / merchant, normalized
    category: str | None
    is_voided: bool
    voided_by_event_id: UUID | None
    note: str | None


class LedgerPageResponse(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    rows: tuple[LedgerRow, ...]
    next_cursor: str | None
    total_count: int


# Charts: every variation is served from one shape, so the frontend adds charts
# without new endpoints.


class ChartMetric(StrEnum):
    INCOME = "income"
    ALLOCATABLE_INCOME = "allocatable_income"
    FIXED_DUE = "fixed_due"
    FIXED_PAID = "fixed_paid"
    FIXED_OUTSTANDING = "fixed_outstanding"
    SAVINGS_ALLOCATED = "savings_allocated"
    SAVINGS_BALANCE = "savings_balance"
    DISCRETIONARY_ALLOCATED = "discretionary_allocated"
    DISCRETIONARY_SPENT = "discretionary_spent"
    DISCRETIONARY_REMAINING = "discretionary_remaining"
    ACCOUNT_BALANCE = "account_balance"
    INTEREST_CHARGED = "interest_charged"
    INTEREST_EARNED = "interest_earned"


class ChartGrain(StrEnum):
    PERIOD = "period"
    MONTH = "month"
    CYCLE = "cycle"


class ChartGroupBy(StrEnum):
    NONE = "none"
    CATEGORY = "category"
    ACCOUNT = "account"
    PAYEE = "payee"


class ChartPoint(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    bucket: str  # period_id, "YYYY-MM", or cycle_id
    series: str  # group_by value, or "total"
    value_minor: Minor


class ChartSeriesResponse(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    metric: ChartMetric
    grain: ChartGrain
    group_by: ChartGroupBy
    points: tuple[ChartPoint, ...]


# ==========================================================================
# §6 — shapes the table names but does not define.
#
# Defined here, in `module/api`'s own subtree, per the note at the top of this file.
# The rule followed throughout: reuse the `domain/` model rather than restate its
# fields. `PeriodListResponse` carries `PeriodSummary`, not a copy of its nineteen
# money fields; `AccountListResponse` carries `AccountBalance`. A DTO that restated
# them would be a second place for the `_minor` suffix rule to be got right, and the
# projection is the one that has to be authoritative.
#
# Every one of them repeats `as_of_date`. It is not redundant: `as_of` is optional on
# every read endpoint (§6.3) and defaults to today in `BUDGET_TZ`, so a client that
# omitted it cannot otherwise know which date the answer describes.


# ------------------------------------------------------------------------- §6.1


class InboundEventRequest(BaseModel):
    """`AppendEventRequest` as it actually arrives on the wire.

    CONTRACTS.md §6.1 spells the request `event: Event` and annotates it
    "event_id/dedupe_key server-assigned if omitted". `EventBase` declares both as
    **required** (§3.1), so the canonical `Event` type cannot express a body that omits
    them, and Phase 0.5 flagged exactly this on `AppendEventRequest` above: resolving it
    is an api-layer concern and does not change the frozen event contract.

    This is that resolution. `event` is the event's own JSON object with the three
    server-assigned fields optional — `event_id`, `recorded_at` and `dedupe_key` — and
    `api.payloads.canonical_event_payload` immediately validates it against the frozen
    discriminated union, in **strict** mode, before `ingestion.normalize_event` turns it
    into a canonical `Event`. Nothing is loosened by the `dict[str, object]` annotation:
    a float where a `Minor` is declared is rejected one call later, by the same
    `TypeAdapter(Event)` the ingestion boundary and the persistence layer both use.

    The alternative — mirroring all eleven event classes here with two fields made
    optional — would put a second copy of the event contract in `api/`, which is the
    drift CLAUDE.md §6 exists to prevent. `AppendEventRequest` above stays as the
    contract's own spelling and describes a strict subset of what this accepts: a client
    that supplies `event_id` and `dedupe_key` is honoured, the server only fills in what
    is missing.
    """

    model_config = MONEY_MODEL_CONFIG

    event: dict[str, object]
    client_nonce: str | None = None  # folded into the dedupe key so a genuinely
    # duplicate manual entry survives (§3.1)


class AppendBatchRequest(BaseModel):
    """`POST /events/batch`. The single-event body, repeated.

    Each item carries its own `client_nonce` rather than the batch carrying one, because
    the nonce disambiguates *one* entry from an identical one — a batch-wide nonce would
    let two identical items inside the same batch collide with each other, which is the
    case it exists to fix.
    """

    model_config = MONEY_MODEL_CONFIG

    events: tuple[InboundEventRequest, ...]


class AppendBatchResponse(BaseModel):
    """Per-item results, positionally aligned with the request (§6.1).

    "Partial success" is per-item *results*, not per-item transactions — a duplicate
    reports `deduplicated: true` and is not an error, while a malformed item is
    malformed input and fails the whole batch. That reading is `ingestion.append_events`'
    own, stated in its docstring, and is followed here rather than re-decided.
    """

    model_config = MONEY_MODEL_CONFIG

    results: tuple[AppendEventResponse, ...]


class VoidRequest(BaseModel):
    """`POST /events/{event_id}/void`. Appends an `EventVoided` (§6.1).

    The target is the path parameter, so the body carries only what the ledger cannot
    derive. `date` is the void's own business date and defaults to today in `BUDGET_TZ`
    — voiding is an act with a date of its own, and it is deliberately not the target's
    date: the correction happened when it happened (PLAN.md §8.4).
    """

    model_config = MONEY_MODEL_CONFIG

    reason: str
    date: dt.date | None = None
    client_nonce: str | None = None


# ------------------------------------------------------------------------- §6.2


class PeriodListResponse(BaseModel):
    """`GET /periods`. The `PeriodSummary` rows of `State` in the requested window."""

    model_config = MONEY_MODEL_CONFIG

    as_of_date: dt.date
    periods: tuple[PeriodSummary, ...]


class PeriodDetailResponse(BaseModel):
    """`GET /periods/{period_id}`. One period, with what belongs to it.

    Obligations and warnings are filtered to the period rather than summarized: the
    period view is where a user asks "what is due, and what looks wrong", and both
    answers already exist on `State`, scoped by `period_id`.
    """

    model_config = MONEY_MODEL_CONFIG

    as_of_date: dt.date
    period: PeriodSummary
    obligations: tuple[ObligationRow, ...]
    warnings: tuple[StateWarning, ...]


class AccountListResponse(BaseModel):
    """`GET /accounts`. Balances as of the resolved date.

    `AccountBalance.balance_minor` is signed and `outstanding_minor` is the absolute
    amount for liabilities (CONTRACTS.md §5.2). Both are passed through untouched — the
    API does not choose a display sign any more than it chooses a currency symbol.
    """

    model_config = MONEY_MODEL_CONFIG

    as_of_date: dt.date
    accounts: tuple[AccountBalance, ...]


class DefinitionListResponse(BaseModel):
    """`GET /definitions/{kind}`.

    `versions` is the concrete union rather than `DefinitionBase`, because pydantic
    serializes to the *declared* type and a base-typed field would silently drop
    `apr_bps`, `cadence` and every other subclass field from the response body.

    With `include_history=false` this is the single version effective at `as_of_date`
    per `entity_id`; with `include_history=true` it is every version, which is what a
    caller needs to see when a period's numbers were fixed by a policy that is no longer
    in force (PLAN.md §8.3).
    """

    model_config = MONEY_MODEL_CONFIG

    kind: DefinitionKind
    as_of_date: dt.date
    include_history: bool
    versions: tuple[RecurringIncome | FixedCost | AllocationPolicy | Account, ...]


class NewDefinitionVersionRequest(BaseModel):
    """`POST /definitions/{kind}`. One new version of one definition.

    `version` is the definition's own JSON object; `version_id` and `recorded_at` are
    server-assigned, for the same reason and by the same mechanism as on an event, and
    the object is validated strictly against `DEFINITION_MODEL_BY_KIND[kind]` before
    anything is written (`api/payloads.py`).

    `close_previous_at` is separate and optional because superseding is **two**
    decisions — when the old version stops and when the new one starts.
    `DefinitionRepository.add_version` deliberately refuses to infer the first from the
    second ("guessing is how a one-day gap or a silent overwrite gets in"), so the
    caller states it or the previous version stays open and `OVERLAPPING_VERSIONS`
    rejects the write.
    """

    model_config = MONEY_MODEL_CONFIG

    version: dict[str, object]
    close_previous_at: dt.date | None = None


class NewDefinitionVersionResponse(BaseModel):
    """`201` from `POST /definitions/{kind}`.

    The §6.2 table names no body for this row. Returning the assigned `version_id` is
    the minimum that makes the endpoint usable: the id is server-generated, so without
    it a client cannot later close the version it just created.
    """

    model_config = MONEY_MODEL_CONFIG

    kind: DefinitionKind
    version_id: UUID
    entity_id: str
    effective_from: dt.date
    effective_to: dt.date | None
    closed_previous_version_id: UUID | None
