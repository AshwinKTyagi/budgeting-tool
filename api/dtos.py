"""Request/response DTOs (CONTRACTS.md §6).

All money in responses is `_minor` integers — the API never emits a formatted currency
string or a float (CONTRACTS.md §6).

This module transcribes only the DTOs CONTRACTS.md §6 defines with fields. Several
response types appear in the §6.2 endpoint table with no field definition:
`AppendBatchRequest`, `AppendBatchResponse`, `VoidRequest`, `PeriodListResponse`,
`PeriodDetailResponse`, `AccountListResponse`, `DefinitionListResponse`, and
`NewDefinitionVersionRequest`. Their shapes are unspecified, so they are left for
`module/api` to define inside its own owned subtree rather than invented here — Phase 0.5
must not manufacture contract surface (CLAUDE.md §6).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from core.types import MONEY_MODEL_CONFIG, Minor, PeriodId, UtcInstant
from domain.events import Event


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
