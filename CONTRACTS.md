# CONTRACTS.md — Frozen interfaces

Everything in this file is a contract. Implementation agents build against it; they do
not change it. If it is wrong or insufficient, stop and raise it — see `CLAUDE.md` §6.

Read `PLAN.md` §1 (the recognition principle) first. `CLAUDE.md` §2 governs money
representation and §4 lists the forbidden patterns.

---

## 1. Conventions

- **Imports.** `import datetime as dt` throughout. Never `from datetime import date` —
  models have a field named `date`, and the bare import collides with it in the class
  body.
- **Money** is `Minor` (`int`, minor units, signed). Every money field ends `_minor`.
- **Rates** are `Bps` (`int`, 1 bps = 0.01%). Every rate field ends `_bps`.
- **Business dates** are `dt.date` — no time, no zone. **Instants** are `UtcInstant` and
  end `_at`. `UtcInstant` is a `dt.datetime` that *enforces* what `CLAUDE.md` §4.5
  requires: a naive value is rejected, and any aware value is normalized to UTC. Never
  annotate an `_at` field as a bare `dt.datetime` — that accepts a naive datetime.
- Every model is `strict=True, frozen=True, extra="forbid"`.
- `Minor` fields accept negative values unless the docstring says otherwise. Sign is
  meaningful: negative allocation is a shortfall, negative balance is a liability.

```python
# core/types.py  -- all frozen declarations. See PLAN.md §13.3.
type Minor = int   # signed minor units (cents)
type Bps   = int   # basis points; 10_000 == 100%

MONEY_MODEL_CONFIG = ConfigDict(strict=True, frozen=True, extra="forbid")


def _require_utc(value: dt.datetime) -> dt.datetime:
    """Reject a naive datetime; normalize any aware one to UTC (CLAUDE.md §4.5)."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime: every instant must be timezone-aware and UTC")
    return value.astimezone(dt.timezone.utc)


type UtcInstant = Annotated[dt.datetime, AfterValidator(_require_utc)]
```

`UtcInstant` is transparent to the type checker — `mypy` sees a plain `dt.datetime`, so
it is passed to anything expecting one and no consumer changes. The validator is the
only logic permitted in this file besides `AppError.__init__`, and it is here for the
same reason: it is what makes a declared type enforce its own contract, and the three
modules that need it (`domain/events.py`, `domain/definitions.py`, `api/dtos.py`) sit on
three different branches, so it cannot live in any one of them.

**Types live in `core/types.py`; behavior lives elsewhere.** The aliases, the shared
model config, the enums in §2, and the id aliases are all declarations that nearly every
module imports. They land in Phase 0.5 and are frozen thereafter. `core/money.py`
contains only functions (`split_bps`, `allocate_period`), so no Phase-1 agent owns a type
that other agents depend on.

---

## 2. Enums and shared types

```python
# core/types.py
from enum import StrEnum

class AccountKind(StrEnum):
    CHECKING    = "CHECKING"
    SAVINGS     = "SAVINGS"
    CREDIT_CARD = "CREDIT_CARD"
    LOAN        = "LOAN"

class BudgetTiming(StrEnum):
    """When a credit-card purchase reduces discretionary. See PLAN.md §6.4."""
    AT_PURCHASE          = "AT_PURCHASE"
    AT_STATEMENT_PAYMENT = "AT_STATEMENT_PAYMENT"

class ObligationStatus(StrEnum):
    UNPAID          = "UNPAID"
    PARTIALLY_PAID  = "PARTIALLY_PAID"
    PAID            = "PAID"
    OVERPAID        = "OVERPAID"

class ObligationSource(StrEnum):
    EXPECTED = "EXPECTED"   # materialized from a FixedCost definition
    RAISED   = "RAISED"     # explicit ObligationRaised event

class Cadence(StrEnum):
    WEEKLY      = "WEEKLY"
    BIWEEKLY    = "BIWEEKLY"
    SEMIMONTHLY = "SEMIMONTHLY"
    MONTHLY     = "MONTHLY"
    QUARTERLY   = "QUARTERLY"
    ANNUAL      = "ANNUAL"

type PeriodId = str    # "YYYY-MM" under CalendarMonthResolver
type CycleId  = str    # f"{account_id}:{PeriodId}"
```

```python
# domain/events.py
class ExternalRef(BaseModel):
    """Provenance from an external ingestion source. Participates in the dedupe key.

    Reserved for future bank/card aggregation (PLAN.md §9). Nothing reads it today
    beyond dedupe; do not branch on it.
    """
    model_config = MONEY_MODEL_CONFIG
    provider: str
    provider_txn_id: str
```

---

## 3. Events

### 3.1 Base

```python
class EventBase(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    event_id: UUID
    date: dt.date              # business date; decides period membership
    recorded_at: UtcInstant    # tz-aware UTC (enforced); audit + tie-break only, never period membership
    dedupe_key: str            # natural key; UNIQUE in persistence
    external_ref: ExternalRef | None = None
    note: str | None = None
```

**Ledger ordering** is `(date, recorded_at, event_id)` — total and stable.

**`dedupe_key` construction**, by source:

| Source | Key |
|---|---|
| Receipt upload | `f"receipt:{sha256(file_bytes)}"` |
| External provider | `f"ext:{provider}:{provider_txn_id}"` |
| Manual entry | `f"manual:{event_type}:{date}:{amount_minor}:{sha256(discriminating_fields)}"` |

The manual key is deliberately collision-prone across genuinely identical entries — two
identical $4.50 coffees on the same day *will* collide and the second is a no-op. The
caller disambiguates with an explicit `note` or a client-supplied nonce appended to the
key. Silently accepting accidental duplicates is the worse failure.

### 3.2 Event types

```python
class IncomeReceived(EventBase):
    """External income. Contributes to allocatable_income."""
    event_type: Literal["IncomeReceived"] = "IncomeReceived"
    amount_minor: Minor            # > 0
    source: str
    account_id: str                # where it landed


class GiftReceived(EventBase):
    """Gift income. Folds into allocatable_income identically to IncomeReceived;
    the separate type exists only to label the source for reporting."""
    event_type: Literal["GiftReceived"] = "GiftReceived"
    amount_minor: Minor            # > 0
    source: str
    account_id: str


class ObligationRaised(EventBase):
    """An explicit bill. Supersedes the expected obligation materialized from the
    FixedCost with the same recurring_id in the same due-period (PLAN.md §8.1)."""
    event_type: Literal["ObligationRaised"] = "ObligationRaised"
    obligation_id: str
    due_date: dt.date              # decides period membership, NOT `date`
    amount_minor: Minor            # > 0
    payee: str
    category: str
    recurring_id: str | None = None


class PaymentMade(EventBase):
    """Payment against an obligation. Changes status only; never changes allocation
    (accrual basis, PLAN.md §6)."""
    event_type: Literal["PaymentMade"] = "PaymentMade"
    amount_minor: Minor            # > 0
    obligation_id: str
    account_id: str                # paid from
    principal_minor: Minor | None = None
    interest_minor: Minor | None = None
    # If either split field is set, both must be, and they must sum to amount_minor.


class ExpenseRecorded(EventBase):
    """Discretionary spending. Reduces discretionary_remaining, subject to the
    account's budget_timing when charged to a credit card (PLAN.md §6.4).
    `category` is a chart label with no allocation semantics."""
    event_type: Literal["ExpenseRecorded"] = "ExpenseRecorded"
    amount_minor: Minor            # negative permitted: a refund
    category: str
    account_id: str
    merchant: str | None = None


class SavingsDrawn(EventBase):
    """A deliberate top-up of discretionary from savings, beyond the automatic
    shortfall drain (PLAN.md §6.2). Exceeding the available balance raises a
    warning, never an error."""
    event_type: Literal["SavingsDrawn"] = "SavingsDrawn"
    amount_minor: Minor            # > 0
    reason: str


class TransferMade(EventBase):
    """Money between the user's own accounts. Budget-neutral by construction —
    this is the mechanism that prevents credit-card double-counting."""
    event_type: Literal["TransferMade"] = "TransferMade"
    amount_minor: Minor            # > 0; direction carried by the account fields
    from_account_id: str
    to_account_id: str


class AccountOpeningBalance(EventBase):
    """Signed opening balance. Covers both "opened checking with $500"
    (positive) and a loan disbursement (negative — a liability).
    Never contributes to allocatable_income."""
    event_type: Literal["AccountOpeningBalance"] = "AccountOpeningBalance"
    account_id: str
    amount_minor: Minor            # signed


class InterestCharged(EventBase):
    """Actual interest from a statement. Supersedes the projection's estimate for
    this cycle and pins it against cascade (PLAN.md §7.4)."""
    event_type: Literal["InterestCharged"] = "InterestCharged"
    account_id: str
    cycle_id: CycleId
    amount_minor: Minor            # > 0


class InterestEarned(EventBase):
    """Actual interest credited to an asset account. Not allocatable income."""
    event_type: Literal["InterestEarned"] = "InterestEarned"
    account_id: str
    cycle_id: CycleId
    amount_minor: Minor            # > 0


class EventVoided(EventBase):
    """The ONLY correction mechanism. The projection filters the target before
    folding. Amount corrections are void + re-raise (PLAN.md §8.4)."""
    event_type: Literal["EventVoided"] = "EventVoided"
    target_event_id: UUID
    reason: str


Event = Annotated[
    IncomeReceived | GiftReceived | ObligationRaised | PaymentMade
    | ExpenseRecorded | SavingsDrawn | TransferMade | AccountOpeningBalance
    | InterestCharged | InterestEarned | EventVoided,
    Field(discriminator="event_type"),
]
```

---

## 4. Definitions

All definitions are versioned. `effective_from` is **inclusive**, `effective_to` is
**exclusive** and nullable for open-ended. Versions of the same `entity_id` may not
overlap; enforced at write time.

```python
class DefinitionBase(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    version_id: UUID
    entity_id: str                 # stable logical identity across versions
    effective_from: dt.date        # inclusive
    effective_to: dt.date | None   # exclusive; None == open-ended
    recorded_at: UtcInstant


class RecurringIncome(DefinitionBase):
    """FORECAST ONLY. Never contributes to allocatable_income — only actual
    IncomeReceived / GiftReceived events do (PLAN.md §8.2)."""
    name: str
    amount_minor: Minor            # > 0
    cadence: Cadence
    anchor_day: int                # 1..31, clamped to month length
    account_id: str


class FixedCost(DefinitionBase):
    """Expanded by the projection into expected obligations. An ObligationRaised
    with the same recurring_id (== entity_id) in the same due-period supersedes
    the expected one (PLAN.md §8.1)."""
    name: str
    amount_minor: Minor            # > 0
    cadence: Cadence
    due_day: int                   # 1..31, clamped to month length
    payee: str
    category: str


class AllocationPolicy(DefinitionBase):
    """Resolved at PERIOD START. One policy governs a whole period; a policy
    effective mid-period applies from the next period. This is what makes closed
    periods immune to policy change (PLAN.md §8.3).

    Buckets are ordered; order breaks rounding ties (PLAN.md §5.1). Savings is
    declared first.
    """
    savings_bps: Bps
    discretionary_bps: Bps
    # INVARIANT: savings_bps + discretionary_bps == 10_000, validated on construction.


class Account(DefinitionBase):
    """entity_id is the account_id. APR is resolved at STATEMENT CYCLE START
    (PLAN.md §7.4)."""
    name: str
    kind: AccountKind
    apr_bps: Bps                          # 0 for non-interest-bearing
    statement_close_day: int | None       # CREDIT_CARD only; 1..31
    payment_due_day: int | None           # CREDIT_CARD only; 1..31
    budget_timing: BudgetTiming = BudgetTiming.AT_PURCHASE   # CREDIT_CARD only


class Definitions(BaseModel):
    """Immutable bundle passed to project(). Contains ALL versions, not just
    currently-effective ones — the projection resolves per period and per cycle."""
    model_config = MONEY_MODEL_CONFIG

    recurring_incomes: tuple[RecurringIncome, ...]
    fixed_costs: tuple[FixedCost, ...]
    allocation_policies: tuple[AllocationPolicy, ...]
    accounts: tuple[Account, ...]
```

**Seeded on first run:** one `CHECKING` and one `SAVINGS` account, and a default
`AllocationPolicy` of `savings_bps=5000, discretionary_bps=5000`.

---

## 5. Projection and State

### 5.1 Signature

```python
# domain/projection.py
def project(
    events: Sequence[Event],
    definitions: Definitions,
    as_of_date: dt.date,
    *,
    resolver: PeriodResolver | None = None,   # default CalendarMonthResolver()
) -> State: ...
```

Pure. No I/O, no clock, no database, no mutation, no logging. Same inputs → same
output, always. `resolver` is keyword-only with a default so the canonical three-argument
shape reads exactly as specified.

**Order of operations** (fixed; agents must not reorder):

1. Filter events voided by an `EventVoided`, and the `EventVoided` records themselves.
2. Sort by `(date, recorded_at, event_id)`.
3. Resolve periods from genesis (earliest event date) through `as_of_date`.
4. Expand `FixedCost` definitions into expected obligations; supersede by matching
   `ObligationRaised`.
5. Fold statement cycles **in order, per account**, carrying close balance and
   paid-in-full status forward (PLAN.md §7.4).
6. Fold per-period allocation, applying implied savings transfers at period close.
7. Assemble `State`.

### 5.2 State

```python
class PeriodSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    period_id: PeriodId
    start_date: dt.date                   # inclusive
    end_date_exclusive: dt.date
    is_closed: bool                       # end_date_exclusive <= as_of_date

    policy_version_id: UUID
    savings_bps: Bps
    discretionary_bps: Bps

    income_minor: Minor
    gifts_minor: Minor
    allocatable_income_minor: Minor       # income + gifts

    fixed_due_minor: Minor                # accrual: all obligations due this period
    fixed_paid_minor: Minor               # cash
    fixed_outstanding_minor: Minor        # fixed_due - fixed_paid

    savings_allocated_minor: Minor        # signed
    discretionary_allocated_minor: Minor  # signed
    savings_drawn_minor: Minor

    discretionary_spent_minor: Minor
    discretionary_remaining_minor: Minor
    # == discretionary_allocated + savings_drawn - discretionary_spent

    # INVARIANT (exact, always, including negative allocatable income):
    #   fixed_due_minor
    #     + savings_allocated_minor
    #     + discretionary_allocated_minor
    #     == allocatable_income_minor


class ObligationRow(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    obligation_id: str
    source: ObligationSource
    period_id: PeriodId
    due_date: dt.date
    payee: str
    category: str
    recurring_id: str | None

    amount_minor: Minor
    paid_minor: Minor
    remaining_minor: Minor                # amount - paid; negative when overpaid
    status: ObligationStatus


class AccountBalance(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    account_id: str
    name: str
    kind: AccountKind
    balance_minor: Minor                  # SIGNED; negative == liability
    outstanding_minor: Minor | None       # abs(balance) for liabilities; None for assets
    apr_bps: Bps
    cumulative_interest_minor: Minor


class StatementCycleSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    cycle_id: CycleId
    account_id: str
    start_date: dt.date
    end_date_exclusive: dt.date
    close_balance_minor: Minor
    interest_minor: Minor
    is_estimate: bool                     # False once an InterestCharged pins the cycle
    paid_in_full_by_due_date: bool
    grace_applied: bool                   # True == interest waived this cycle


class SavingsSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    balance_minor: Minor
    cumulative_allocated_minor: Minor
    cumulative_drawn_minor: Minor
    cumulative_interest_minor: Minor
    pending_allocation_minor: Minor       # in-progress period; not yet in balance

    # INVARIANT: balance_minor ==
    #   opening + cumulative_allocated - cumulative_drawn
    #   + cumulative_interest ± explicit transfers


class Warning(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    code: WarningCode
    message: str
    period_id: PeriodId | None = None
    event_id: UUID | None = None
    account_id: str | None = None


class State(BaseModel):
    """The complete answer at a point in time. Immutable; constructed once."""
    model_config = MONEY_MODEL_CONFIG

    as_of_date: dt.date
    current_period_id: PeriodId
    periods: tuple[PeriodSummary, ...]           # genesis .. as_of, ascending
    obligations: tuple[ObligationRow, ...]
    accounts: tuple[AccountBalance, ...]
    statement_cycles: tuple[StatementCycleSummary, ...]
    savings: SavingsSummary
    warnings: tuple[Warning, ...]
```

`periods` spans genesis through `as_of_date` rather than the current period alone, so
chart endpoints fold over `State` without a second projection pass.

---

## 6. REST API

Base path `/api/v1`. All money in responses is `_minor` integers — the API never emits a
formatted currency string or a float.

### 6.1 Ingestion

| Method | Path | Request | Response | Notes |
|---|---|---|---|---|
| `POST` | `/events` | `AppendEventRequest` | `201 AppendEventResponse` / `200` on dedupe | Idempotent |
| `POST` | `/events/batch` | `AppendBatchRequest` | `200 AppendBatchResponse` | Per-item results; partial success |
| `POST` | `/events/{event_id}/void` | `VoidRequest` | `201 AppendEventResponse` | Appends `EventVoided` |
| `POST` | `/receipts` | `multipart/form-data` | `201 ReceiptUploadResponse` / `200` on dedupe | See §6.4 |

```python
class AppendEventRequest(BaseModel):
    event: Event                     # discriminated union; event_id/dedupe_key
                                     # server-assigned if omitted
    client_nonce: str | None = None  # appended to dedupe_key to permit
                                     # genuinely-duplicate manual entries

class AppendEventResponse(BaseModel):
    event_id: UUID
    dedupe_key: str
    deduplicated: bool               # True == no-op, event already existed
```

**Re-uploading an identical receipt returns `200` with `deduplicated: true` and the
existing `event_id`.** It is not an error and must not be reported as one.

### 6.2 Read

| Method | Path | Query | Response |
|---|---|---|---|
| `GET` | `/state` | `as_of` (default: today in `BUDGET_TZ`) | `State` |
| `GET` | `/periods` | `from`, `to` | `PeriodListResponse` |
| `GET` | `/periods/{period_id}` | — | `PeriodDetailResponse` |
| `GET` | `/ledger` | `from`, `to`, `types[]`, `account_id`, `category`, `cursor`, `limit` | `LedgerPageResponse` |
| `GET` | `/charts/series` | `metric`, `grain`, `from`, `to`, `group_by` | `ChartSeriesResponse` |
| `GET` | `/accounts` | `as_of` | `AccountListResponse` |
| `GET` | `/definitions/{kind}` | `as_of`, `include_history` | `DefinitionListResponse` |
| `POST` | `/definitions/{kind}` | `NewDefinitionVersionRequest` | `201` |

`kind` ∈ `recurring-income | fixed-cost | allocation-policy | account`.

**`GET /ledger`** serves the spreadsheet view: one flat row per event, cursor-paginated,
newest first by `(date, recorded_at, event_id)`. Voided events are included with
`is_voided: true` — the tabular view shows history, it does not hide it.

```python
class LedgerRow(BaseModel):
    event_id: UUID
    event_type: str
    date: dt.date
    recorded_at: UtcInstant
    period_id: PeriodId
    amount_minor: Minor | None
    account_id: str | None
    counterparty: str | None      # source / payee / merchant, normalized
    category: str | None
    is_voided: bool
    voided_by_event_id: UUID | None
    note: str | None

class LedgerPageResponse(BaseModel):
    rows: tuple[LedgerRow, ...]
    next_cursor: str | None
    total_count: int
```

**`GET /charts/series`** serves every chart variation from one shape, so the frontend
adds charts without new endpoints:

```python
class ChartMetric(StrEnum):
    INCOME               = "income"
    ALLOCATABLE_INCOME   = "allocatable_income"
    FIXED_DUE            = "fixed_due"
    FIXED_PAID           = "fixed_paid"
    FIXED_OUTSTANDING    = "fixed_outstanding"
    SAVINGS_ALLOCATED    = "savings_allocated"
    SAVINGS_BALANCE      = "savings_balance"
    DISCRETIONARY_ALLOCATED = "discretionary_allocated"
    DISCRETIONARY_SPENT  = "discretionary_spent"
    DISCRETIONARY_REMAINING = "discretionary_remaining"
    ACCOUNT_BALANCE      = "account_balance"
    INTEREST_CHARGED     = "interest_charged"
    INTEREST_EARNED      = "interest_earned"

class ChartGrain(StrEnum):
    PERIOD = "period"
    MONTH  = "month"
    CYCLE  = "cycle"

class ChartGroupBy(StrEnum):
    NONE     = "none"
    CATEGORY = "category"
    ACCOUNT  = "account"
    PAYEE    = "payee"

class ChartPoint(BaseModel):
    bucket: str                   # period_id, "YYYY-MM", or cycle_id
    series: str                   # group_by value, or "total"
    value_minor: Minor

class ChartSeriesResponse(BaseModel):
    metric: ChartMetric
    grain: ChartGrain
    group_by: ChartGroupBy
    points: tuple[ChartPoint, ...]
```

### 6.3 `as_of` handling

`as_of` is optional on every read endpoint. When omitted, `api/` resolves "now" in
`BUDGET_TZ` to a `dt.date`. **This is the only place in the codebase that reads a clock**
(`CLAUDE.md` §4.4). When supplied, it is used verbatim — including future dates, which
are valid and produce a forecast-shaped `State`.

### 6.4 Receipt upload

`POST /receipts`, `multipart/form-data`:

| Field | Type | Notes |
|---|---|---|
| `file` | binary | The receipt image or PDF |
| `date` | `dt.date` | Business date |
| `amount_minor` | `int` | Required — no OCR in this scope |
| `category` | `str` | |
| `account_id` | `str` | |
| `merchant` | `str \| None` | |

The handler computes `sha256(file_bytes)`, sets
`dedupe_key = f"receipt:{sha256}"`, stores the blob, and appends an `ExpenseRecorded`
carrying a reference to it. Re-uploading identical bytes is a `200` no-op.

```python
class ReceiptUploadResponse(BaseModel):
    event_id: UUID
    blob_id: str
    content_sha256: str
    deduplicated: bool
```

---

## 7. Errors and warnings

The distinction is load-bearing:

- **Errors** are for input that could never be valid. They are raised and mapped to HTTP.
- **Warnings** are for states that are surprising but legitimate. They are **data in
  `State`**, never raised. Backdating means today's impossible state is tomorrow's
  ordinary one — a savings draw that looks overdrawn now may be fine once an earlier
  income event arrives.

### 7.1 Error taxonomy

```python
class ErrorCode(StrEnum):
    VALIDATION_FAILED         = "VALIDATION_FAILED"
    UNKNOWN_ACCOUNT           = "UNKNOWN_ACCOUNT"
    UNKNOWN_OBLIGATION        = "UNKNOWN_OBLIGATION"
    UNKNOWN_EVENT             = "UNKNOWN_EVENT"
    ALREADY_VOIDED            = "ALREADY_VOIDED"
    CANNOT_VOID_A_VOID        = "CANNOT_VOID_A_VOID"
    POLICY_BPS_NOT_10000      = "POLICY_BPS_NOT_10000"
    OVERLAPPING_VERSIONS      = "OVERLAPPING_VERSIONS"
    EFFECTIVE_RANGE_INVALID   = "EFFECTIVE_RANGE_INVALID"
    PAYMENT_SPLIT_MISMATCH    = "PAYMENT_SPLIT_MISMATCH"
    TRANSFER_SAME_ACCOUNT     = "TRANSFER_SAME_ACCOUNT"
    UNSUPPORTED_MEDIA_TYPE    = "UNSUPPORTED_MEDIA_TYPE"
    INTERNAL                  = "INTERNAL"

class AppError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, object]

class ErrorResponse(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, object]
```

| Code | HTTP | Raised when |
|---|---|---|
| `VALIDATION_FAILED` | 422 | Pydantic rejection; float where `Minor` expected |
| `UNKNOWN_ACCOUNT` | 422 | `account_id` matches no `Account` definition |
| `UNKNOWN_OBLIGATION` | 422 | `PaymentMade.obligation_id` unknown **at write time** |
| `UNKNOWN_EVENT` | 404 | Void target does not exist |
| `ALREADY_VOIDED` | 409 | Void target already voided |
| `CANNOT_VOID_A_VOID` | 422 | Target is itself an `EventVoided` |
| `POLICY_BPS_NOT_10000` | 422 | `savings_bps + discretionary_bps != 10_000` |
| `OVERLAPPING_VERSIONS` | 409 | New version overlaps an existing one for that `entity_id` |
| `EFFECTIVE_RANGE_INVALID` | 422 | `effective_to <= effective_from` |
| `PAYMENT_SPLIT_MISMATCH` | 422 | `principal + interest != amount`, or only one set |
| `TRANSFER_SAME_ACCOUNT` | 422 | `from_account_id == to_account_id` |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | Receipt is not an accepted image/PDF type |
| `INTERNAL` | 500 | Everything else |

**Duplicate ingestion is not an error.** It returns `200` with `deduplicated: true`.

`UNKNOWN_OBLIGATION` is checked **only at write time**, against obligations known then. A
payment can still end up orphaned later if its obligation is voided — that surfaces as
the `PAYMENT_WITHOUT_OBLIGATION` warning, not an error, because the ledger is
append-only and the projection must survive it.

### 7.2 Warning taxonomy

```python
class WarningCode(StrEnum):
    SAVINGS_DRAW_EXCEEDS_BALANCE = "SAVINGS_DRAW_EXCEEDS_BALANCE"
    OBLIGATION_OVERPAID          = "OBLIGATION_OVERPAID"
    PAYMENT_WITHOUT_OBLIGATION   = "PAYMENT_WITHOUT_OBLIGATION"
    NEGATIVE_ALLOCATION          = "NEGATIVE_ALLOCATION"
    ESTIMATED_INTEREST           = "ESTIMATED_INTEREST"
    CHECKING_OVERDRAWN           = "CHECKING_OVERDRAWN"
    OBLIGATION_PAST_DUE_UNPAID   = "OBLIGATION_PAST_DUE_UNPAID"
```

None of these ever prevents ingestion or fails a projection.

---

## 8. Stubs

Signatures only. Bodies `raise NotImplementedError`. Docstrings state pre/postconditions
and **are** the specification — implement against them, not against what a caller happens
to pass.

### 8.1 `core/money.py`

```python
def split_bps(
    total_minor: Minor,
    buckets: Sequence[tuple[str, Bps]],
) -> dict[str, Minor]:
    """Split `total_minor` across `buckets` by basis points, exactly.

    Algorithm (PLAN.md §5.1): work on abs(total); floor-divide each bucket;
    distribute the leftover one minor unit at a time in descending fractional
    remainder, ties broken by position in `buckets`; reapply sign(total).

    Preconditions:
        sum(bps for _, bps in buckets) == 10_000
        every bps >= 0
        bucket names are unique
        buckets is non-empty and ORDERED — order is significant for tie-breaking

    Postconditions:
        sum(result.values()) == total_minor            EXACTLY, always
        result.keys() == {name for name, _ in buckets}
        split_bps(-t, b) == {k: -v for k, v in split_bps(t, b).items()}
        every value is int; no float is produced at any point

    Raises:
        AppError(POLICY_BPS_NOT_10000) if the bps precondition fails.
    """
    raise NotImplementedError


def allocate_period(
    allocatable_income_minor: Minor,
    fixed_due_minor: Minor,
    policy: AllocationPolicy,
) -> dict[str, Minor]:
    """Apply the allocation rule: fixed off the top, remainder split by policy.

    Preconditions:
        policy.savings_bps + policy.discretionary_bps == 10_000

    Postconditions:
        returns keys {"savings", "discretionary"}
        fixed_due_minor + result["savings"] + result["discretionary"]
            == allocatable_income_minor        EXACTLY

        Holds when the remainder is negative (income below fixed costs), in which
        case both shares are negative. Nothing is clamped (PLAN.md §6.1).
    """
    raise NotImplementedError
```

### 8.2 `core/periods.py`

```python
class PeriodResolver(Protocol):
    """Maps dates to periods. CalendarMonthResolver is the only implementation
    built; paycheck-driven is future work. Nothing outside this module may assume
    months (PLAN.md §4.1)."""

    def period_for(self, d: dt.date) -> PeriodId: ...
    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        """Returns (start inclusive, end exclusive)."""
    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        """Ascending, inclusive of the periods containing both endpoints."""


class CalendarMonthResolver:
    """Half-open calendar months: [first day, first day of next month).
    PeriodId is "YYYY-MM".

    Postconditions:
        period_for is total over dt.date — every date maps to exactly one period
        bounds(period_for(d)) always contains d
        periods_between is ascending with no gaps
    """

    def period_for(self, d: dt.date) -> PeriodId:
        raise NotImplementedError

    def bounds(self, period_id: PeriodId) -> tuple[dt.date, dt.date]:
        raise NotImplementedError

    def periods_between(self, start: dt.date, end: dt.date) -> Sequence[PeriodId]:
        raise NotImplementedError


def clamp_day_to_month(year: int, month: int, day: int) -> dt.date:
    """Clamp `day` to the last valid day of the month.

    Preconditions:  1 <= day <= 31
    Postconditions: clamp_day_to_month(2026, 2, 31) == dt.date(2026, 2, 28)
                    result is always a valid date in (year, month)
    """
    raise NotImplementedError
```

### 8.3 `core/interest.py`

```python
def interest_for_cycle(
    outstanding_minor: Minor,
    apr_bps: Bps,
    cycle_days: int,
) -> Minor:
    """Integer interest, floor division, actual/365, no intra-cycle compounding.

        outstanding_minor * apr_bps * cycle_days // (10_000 * 365)

    Preconditions:
        outstanding_minor >= 0   -- an ABSOLUTE amount, never a signed balance
        apr_bps >= 0
        cycle_days > 0

        What the caller passes, by account kind:
          liability (CREDIT_CARD, LOAN) -> AccountBalance.outstanding_minor
          asset (CHECKING, SAVINGS)     -> AccountBalance.balance_minor, which is
                                           non-negative in the normal case
        An overdrawn asset account (balance_minor < 0) accrues no interest; the
        caller skips it or passes 0. It must NOT pass the negative balance.

        Passing a signed `balance_minor` for a liability is a bug: it is NEGATIVE
        for liabilities (§5.2), and raw floor division on a negative operand
        rounds toward -inf, producing a larger-magnitude charge than the balance
        warrants.

        This does NOT use the abs-then-reapply-sign discipline of split_bps, and
        the difference is deliberate. split_bps must accept signed input because
        negative allocatable income is a real state (PLAN.md §6.1). A negative
        card balance is a credit, which earns no interest rather than negative
        interest — so the correct treatment is a precondition, not a sign flip.

    Postconditions:
        result is int; no float anywhere in the computation
        result >= 0
        multiplication happens before division (CLAUDE.md §2.1)
        outstanding_minor == 0 or apr_bps == 0  =>  result == 0
        worked example: (120_000, 2199, 31) -> 2241        (PLAN.md §7.2)
                        (500_000,  450, 30) -> 1849

    Raises:
        AppError(VALIDATION_FAILED) if outstanding_minor < 0. Never silently
        clamps -- a negative here means the caller used the wrong field.
    """
    raise NotImplementedError


def build_statement_cycles(
    account: Account,
    genesis: dt.date,
    as_of_date: dt.date,
    resolver: PeriodResolver,
) -> Sequence[tuple[CycleId, dt.date, dt.date]]:
    """Enumerate an account's statement cycles as (cycle_id, start, end_exclusive).

    Preconditions:
        account.kind == CREDIT_CARD implies statement_close_day is not None
        genesis <= as_of_date

    Postconditions:
        ascending, contiguous, non-overlapping
        cycle_id == f"{account.entity_id}:{period_id}"
        no cycle starts after as_of_date
    """
    raise NotImplementedError
```

### 8.4 `domain/events.py`

```python
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
```

### 8.5 `domain/definitions.py`

```python
def resolve_version[T: DefinitionBase](
    versions: Sequence[T],
    entity_id: str,
    at: dt.date,
) -> T | None:
    """The version of `entity_id` effective at `at`.

    Preconditions:
        versions of a given entity_id do not overlap

    Postconditions:
        returns v where v.effective_from <= at < (v.effective_to or +inf)
        None when no version is effective
        at most one version can match — overlap is a write-time error
    """
    raise NotImplementedError


def validate_no_overlap(versions: Sequence[DefinitionBase]) -> None:
    """Raise AppError(OVERLAPPING_VERSIONS) if two versions of the same entity_id
    have intersecting [effective_from, effective_to) ranges.

    Postcondition: returns None, or raises. Never mutates.
    """
    raise NotImplementedError


class ExpectedObligation(BaseModel):
    """An obligation materialized from a FixedCost, before the projection turns it
    into an ObligationRow.

    This type exists to keep the dependency graph acyclic: `domain/definitions.py`
    must not import from `domain/projection.py`, so it cannot return ObligationRow.
    The projection converts ExpectedObligation -> ObligationRow when assembling State.
    """
    model_config = MONEY_MODEL_CONFIG

    obligation_id: str
    period_id: PeriodId
    due_date: dt.date
    amount_minor: Minor
    payee: str
    category: str
    recurring_id: str | None
    source: ObligationSource


def expand_fixed_costs(
    fixed_costs: Sequence[FixedCost],
    period_id: PeriodId,
    resolver: PeriodResolver,
) -> Sequence[ExpectedObligation]:
    """Materialize expected obligations for `period_id` (PLAN.md §8.1).

    Preconditions:
        the FixedCost version is resolved at the PERIOD START date

    Postconditions:
        every row has source == EXPECTED
        obligation_id is deterministic: f"expected:{entity_id}:{period_id}"
        due_date is clamp_day_to_month(period, due_day)
        no I/O, no clock
    """
    raise NotImplementedError


def supersede_expected(
    expected: Sequence[ExpectedObligation],
    raised: Sequence[ObligationRaised],
    resolver: PeriodResolver,
) -> Sequence[ExpectedObligation]:
    """Replace expected obligations with matching explicit ones.

    Match key: (recurring_id, period of due_date).

    Postconditions:
        an expected row with a match is REPLACED, not summed
        a raised event with no expected match is included as source == RAISED
        result contains no duplicate (recurring_id, period) pairs
    """
    raise NotImplementedError
```

### 8.6 `domain/accounts.py`

```python
def derive_obligation_status(
    amount_minor: Minor,
    paid_minor: Minor,
) -> ObligationStatus:
    """Postconditions:
        paid == 0            -> UNPAID
        0 < paid < amount    -> PARTIALLY_PAID
        paid == amount       -> PAID
        paid > amount        -> OVERPAID   (permitted; raises a warning, not an error)
    """
    raise NotImplementedError


def fold_account_balances(
    events: Sequence[Event],
    accounts: Sequence[Account],
    implied_transfers: Sequence[tuple[dt.date, str, str, Minor]],
    as_of_date: dt.date,
) -> Sequence[AccountBalance]:
    """Fold every balance-affecting event plus the projection's implied savings
    transfers (PLAN.md §6.2).

    Preconditions:
        events are sorted by (date, recorded_at, event_id)
        events are already void-filtered

    Postconditions:
        balance_minor is SIGNED; negative == liability
        outstanding_minor == abs(balance) for liability kinds, else None
        pure — no mutation of inputs, no accumulator reassignment across the fold
    """
    raise NotImplementedError


def fold_statement_cycles(
    account: Account,
    account_versions: Sequence[Account],
    events: Sequence[Event],
    cycles: Sequence[tuple[CycleId, dt.date, dt.date]],
) -> Sequence[StatementCycleSummary]:
    """Fold cycles IN ORDER, carrying close balance and paid-in-full status forward.

    Cycles cannot be computed in isolation (PLAN.md §7.4).

    Preconditions:
        cycles ascending, contiguous, non-overlapping
        events sorted and void-filtered

    Postconditions:
        APR resolved at each cycle's START date
        grace_applied is True iff the PREVIOUS cycle was paid in full by its due
            date; when True, interest_minor == 0
        an InterestCharged for a cycle PINS it: interest_minor is the recorded
            amount and is_estimate is False
        a pinned cycle's figure is independent of any backdated event within it
        interest is identical under both BudgetTiming modes — timing affects only
            budget recognition, never computation (PLAN.md §6.4)
    """
    raise NotImplementedError
```

### 8.7 `domain/projection.py`

```python
def project(
    events: Sequence[Event],
    definitions: Definitions,
    as_of_date: dt.date,
    *,
    resolver: PeriodResolver | None = None,
) -> State:
    """Fold events and definitions into State. PURE.

    Preconditions:
        events may arrive in ANY order, including backdated
        definitions contains ALL versions; resolution happens inside
        as_of_date may be past, present, or future

    Postconditions:
        for every period:
            fixed_due + savings_allocated + discretionary_allocated
                == allocatable_income                       EXACTLY
        savings.balance == opening + Σallocated - Σdrawn + Σinterest
                           ± explicit transfers              EXACTLY
        project(e, d, t) == project(e, d, t)                 always
        shuffling the order of `events` yields an identical State
        no I/O, no clock read, no DB access, no mutation, no logging
        the returned State and everything reachable from it is frozen
        anomalies surface as State.warnings, never as raised exceptions

    Order of operations is fixed — see §5.1. Do not reorder.
    """
    raise NotImplementedError
```

### 8.8 `ingestion/`

```python
class IngestionSource(Protocol):
    """A producer of canonical events. Receipt upload and manual entry implement
    this today; a bank/card aggregator would implement it unchanged (PLAN.md §9).
    """
    def fetch(self, since: dt.date) -> Sequence[Event]: ...


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
    """
    raise NotImplementedError


def store_receipt(blob: bytes, content_type: str) -> tuple[str, str]:
    """Persist a receipt blob.

    Preconditions:
        content_type is an accepted image or PDF type, else
        AppError(UNSUPPORTED_MEDIA_TYPE)

    Postconditions:
        returns (blob_id, sha256_hex)
        identical bytes always yield the identical sha256 and reuse the same blob
    """
    raise NotImplementedError
```

### 8.9 `api/`

```python
def resolve_as_of(as_of: dt.date | None, tz: str) -> dt.date:
    """Resolve the effective as_of_date.

    THE ONLY CLOCK READ IN THE CODEBASE (CLAUDE.md §4.4).

    Preconditions:
        tz is a valid IANA zone name

    Postconditions:
        as_of is not None -> returned verbatim, INCLUDING future dates
        as_of is None     -> today in `tz`
        never called from core/ or domain/
    """
    raise NotImplementedError


def to_error_response(exc: AppError) -> tuple[int, ErrorResponse]:
    """Map an AppError to (http_status, body) per the table in §7.1.

    Postcondition: total over ErrorCode — every code has a mapping.
    """
    raise NotImplementedError
```

---

## 9. Frozen surface checklist

Before Phase 0.5, confirm:

- [ ] Every event type in §3.2 appears in the `Event` union.
- [ ] Every `_at` field is annotated `UtcInstant`, never a bare `dt.datetime` (§1).
- [ ] Every `ErrorCode` has a row in the §7.1 HTTP mapping.
- [ ] Every module in `PLAN.md` §10 has at least one stub here.
- [ ] Every stub docstring states preconditions **and** postconditions.
- [ ] No stub signature references a type not defined in this file.
- [ ] `mypy --strict` passes over the stub commit.
