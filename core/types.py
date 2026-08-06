"""Frozen declarations. No logic ever lands here.

Phase 0.5 commits this file and it is frozen thereafter (PLAN.md §13.3). It is owned
by no branch, because nearly every module imports it — changing it is a contract
amendment (PLAN.md §13.5), not an edit.

Contents, and why each is here rather than somewhere else:

* CONTRACTS.md §1  -- `Minor`, `Bps`, `MONEY_MODEL_CONFIG`
* CONTRACTS.md §2  -- the shared enums and the `PeriodId` / `CycleId` id aliases
* CONTRACTS.md §7.1/§7.2 -- `ErrorCode`, `WarningCode`, `AppError`, `ErrorResponse`.
  These live here because `core/money.py` raises `AppError(POLICY_BPS_NOT_10000)` and
  `core/interest.py` raises `AppError(VALIDATION_FAILED)` (CONTRACTS.md §8.1, §8.3).
  `core/` may not import from `domain/` or `api/` (CLAUDE.md §3.1), and the two raising
  modules are owned by different Phase-1 branches, so the taxonomy cannot live in
  either one. This file is the only frozen, unowned home inside `core/`.
* `AllocationPolicyLike` -- see its docstring. Lets `core/money.py::allocate_period`
  take a policy without importing `domain/definitions.py`.

Every model in the project carries `MONEY_MODEL_CONFIG`: strict, frozen, extra
forbidden. Strict mode is what rejects `1.0` where a `Minor` is declared instead of
silently coercing it (CLAUDE.md §2.3).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------- §1
# Money and rates. PEP 695 aliases, deliberately not NewType: they document intent
# and read well in signatures without forbidding a raw int (CLAUDE.md §2.2). The
# `_minor` / `_bps` field-name suffixes are what actually survive into JSON, database
# columns, and log lines, so they are the redundancy that holds the line.

type Minor = int  # signed minor units (cents)
type Bps = int  # basis points; 10_000 == 100%

MONEY_MODEL_CONFIG = ConfigDict(strict=True, frozen=True, extra="forbid")


# --------------------------------------------------------------------------- §2
# Shared enums and identifier aliases.


class AccountKind(StrEnum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    CREDIT_CARD = "CREDIT_CARD"
    LOAN = "LOAN"


class BudgetTiming(StrEnum):
    """When a credit-card purchase reduces discretionary. See PLAN.md §6.4."""

    AT_PURCHASE = "AT_PURCHASE"
    AT_STATEMENT_PAYMENT = "AT_STATEMENT_PAYMENT"


class ObligationStatus(StrEnum):
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERPAID = "OVERPAID"


class ObligationSource(StrEnum):
    EXPECTED = "EXPECTED"  # materialized from a FixedCost definition
    RAISED = "RAISED"  # explicit ObligationRaised event


class Cadence(StrEnum):
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    SEMIMONTHLY = "SEMIMONTHLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


type PeriodId = str  # "YYYY-MM" under CalendarMonthResolver
type CycleId = str  # f"{account_id}:{PeriodId}"


# --------------------------------------------------------------------------- §7
# Errors and warnings. The distinction is load-bearing (CONTRACTS.md §7): errors are
# for input that could never be valid and are raised; warnings are for states that are
# surprising but legitimate and are DATA in State, never raised. Backdating means
# today's impossible state is tomorrow's ordinary one.


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    UNKNOWN_OBLIGATION = "UNKNOWN_OBLIGATION"
    UNKNOWN_EVENT = "UNKNOWN_EVENT"
    ALREADY_VOIDED = "ALREADY_VOIDED"
    CANNOT_VOID_A_VOID = "CANNOT_VOID_A_VOID"
    POLICY_BPS_NOT_10000 = "POLICY_BPS_NOT_10000"
    OVERLAPPING_VERSIONS = "OVERLAPPING_VERSIONS"
    EFFECTIVE_RANGE_INVALID = "EFFECTIVE_RANGE_INVALID"
    PAYMENT_SPLIT_MISMATCH = "PAYMENT_SPLIT_MISMATCH"
    TRANSFER_SAME_ACCOUNT = "TRANSFER_SAME_ACCOUNT"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    INTERNAL = "INTERNAL"


class WarningCode(StrEnum):
    SAVINGS_DRAW_EXCEEDS_BALANCE = "SAVINGS_DRAW_EXCEEDS_BALANCE"
    OBLIGATION_OVERPAID = "OBLIGATION_OVERPAID"
    PAYMENT_WITHOUT_OBLIGATION = "PAYMENT_WITHOUT_OBLIGATION"
    NEGATIVE_ALLOCATION = "NEGATIVE_ALLOCATION"
    ESTIMATED_INTEREST = "ESTIMATED_INTEREST"
    CHECKING_OVERDRAWN = "CHECKING_OVERDRAWN"
    OBLIGATION_PAST_DUE_UNPAID = "OBLIGATION_PAST_DUE_UNPAID"


class AppError(Exception):
    """Raised for input that could never be valid. Mapped to HTTP in `api/` by
    `to_error_response` (CONTRACTS.md §7.1).

    Never raised for a merely surprising state — those are `Warning` entries in
    `State` (CLAUDE.md §6). Duplicate ingestion is not an error either; it is a 200
    with `deduplicated: true`.

    The constructor exists so the documented raise sites typecheck: CONTRACTS.md §8.1
    and §8.3 both spell the call as `AppError(SOME_CODE)`, one positional argument.
    `message` defaults to the code's own value so that shape stays valid.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = code.value if message is None else message
        self.details: dict[str, object] = {} if details is None else details
        super().__init__(self.message)


class ErrorResponse(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    code: ErrorCode
    message: str
    details: dict[str, object]


# --------------------------------------------------------------------- structural
# Two `core/` signatures in CONTRACTS.md §8 name a definition model from `domain/`:
# `allocate_period(policy: AllocationPolicy)` and `build_statement_cycles(account:
# Account)`. `core/` may not import `domain/` (CLAUDE.md §3.1), so each is annotated
# against a structural view declared here instead. The concrete models satisfy these
# by shape, so no call site changes and no stub moves out of the file CONTRACTS.md
# assigns it to. Both are kept deliberately narrow: only the fields the function reads.


class AllocationPolicyLike(Protocol):
    """The two fields `core/money.py::allocate_period` actually reads off a policy.

    `domain/definitions.py::AllocationPolicy` satisfies this structurally, so call
    sites pass one unchanged and nothing about the contract's intent moves. The
    protocol exists because `allocate_period` lives in `core/money.py` (PLAN.md §10,
    §13.2) while `AllocationPolicy` is a definition model in `domain/` (CONTRACTS.md
    §4) — and `core/` may not import `domain/` (CLAUDE.md §3.1). Annotating against
    the shape rather than the class is what keeps that boundary intact.

    INVARIANT, guaranteed by the concrete model and assumed here:
        savings_bps + discretionary_bps == 10_000
    """

    @property
    def savings_bps(self) -> Bps: ...

    @property
    def discretionary_bps(self) -> Bps: ...


class StatementCycleAccountLike(Protocol):
    """The three fields `core/interest.py::build_statement_cycles` reads off an account.

    `domain/definitions.py::Account` satisfies this structurally. `entity_id` is the
    account_id and is what `cycle_id` is built from; `kind` and `statement_close_day`
    carry the CREDIT_CARD precondition documented on the function.

    Note this view exposes no `apr_bps`. Cycle *enumeration* does not need a rate — the
    APR is resolved per cycle at the cycle's START date, by the caller that folds them
    (PLAN.md §7.4).
    """

    @property
    def entity_id(self) -> str: ...

    @property
    def kind(self) -> AccountKind: ...

    @property
    def statement_close_day(self) -> int | None: ...
