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
import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from core.types import (
    MONEY_MODEL_CONFIG,
    AppError,
    CycleId,
    ErrorCode,
    Minor,
    UtcInstant,
)


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


# ------------------------------------------------------------------ strict pinning
# `Annotated[Minor, Field(strict=True)]` appears below on the money fields of the two
# event classes that carry a `model_validator`. It is not decoration and must not be
# simplified back to a bare `Minor`.
#
# `Minor` is a PEP 695 alias, so pydantic represents it as a schema DEFINITION and
# refers to it by ref once a model mentions it more than once. A
# `model_validator(mode="after")` wraps the model schema, and the hoisted definition
# then sits OUTSIDE the model's own config scope -- so `strict=True` from
# MONEY_MODEL_CONFIG stops reaching it whenever the model is validated through an
# enclosing adapter rather than directly.
#
# That enclosing adapter is not hypothetical: it is the ingestion boundary.
# `AppendEventRequest.event` is the `Event` union itself (CONTRACTS.md §6.1), so every
# event that arrives over HTTP is validated exactly that way.
#
# Observed, not theorised: without the pin,
# `TypeAdapter(Event).validate_python({... "principal_minor": 118_000.0 ...})`
# ACCEPTED the float and silently coerced it to `118_000`, while
# `PaymentMade.model_validate` on the identical payload correctly rejected it. A float
# reaching the boundary and being silently rounded is the precise failure CLAUDE.md
# §2.3 exists to prevent.
#
# `test_strict_mode_holds_through_the_union_for_every_money_field` is the regression
# guard, and it covers every event type rather than only these two.


class PaymentMade(EventBase):
    """Payment against an obligation. Changes status only; never changes allocation
    (accrual basis, PLAN.md §6)."""

    event_type: Literal["PaymentMade"] = "PaymentMade"
    amount_minor: Annotated[Minor, Field(strict=True)]  # > 0
    obligation_id: str
    account_id: str  # paid from
    principal_minor: Annotated[Minor, Field(strict=True)] | None = None
    interest_minor: Annotated[Minor, Field(strict=True)] | None = None
    # If either split field is set, both must be, and they must sum to amount_minor.

    @model_validator(mode="after")
    def _check_split(self) -> PaymentMade:
        """Enforce the split rule stated above.

        CONTRACTS.md §7.1 assigns this its own error code, PAYMENT_SPLIT_MISMATCH,
        raised when "principal + interest != amount, or only one set". The check lives
        on the model rather than in `api/` because `AppendEventRequest.event` is the
        `Event` union itself (§6.1) — model validation is the only point every write
        path passes through, so anywhere else would leave the code unreachable.

        A split that does not reconcile is malformed input, not a surprising state, so
        it is an error and not a `Warning` (CLAUDE.md §6).
        """
        principal_minor = self.principal_minor
        interest_minor = self.interest_minor

        if principal_minor is None and interest_minor is None:
            return self

        if principal_minor is None or interest_minor is None:
            raise AppError(
                ErrorCode.PAYMENT_SPLIT_MISMATCH,
                "principal_minor and interest_minor must both be set or both omitted",
                {
                    "principal_minor": principal_minor,
                    "interest_minor": interest_minor,
                    "amount_minor": self.amount_minor,
                },
            )

        if principal_minor + interest_minor != self.amount_minor:
            raise AppError(
                ErrorCode.PAYMENT_SPLIT_MISMATCH,
                "principal_minor + interest_minor must equal amount_minor",
                {
                    "principal_minor": principal_minor,
                    "interest_minor": interest_minor,
                    "amount_minor": self.amount_minor,
                },
            )

        return self


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

    # Strict pinned for the same reason as PaymentMade above: this class carries a
    # model_validator. One `Minor` field is not enough to trigger the hoist today, but
    # relying on that is relying on a field count nobody promised to keep.
    event_type: Literal["TransferMade"] = "TransferMade"
    amount_minor: Annotated[Minor, Field(strict=True)]  # > 0; direction carried by the account fields
    from_account_id: str
    to_account_id: str

    @model_validator(mode="after")
    def _check_distinct_accounts(self) -> TransferMade:
        """A transfer to the account it came from is input that could never be valid.

        CONTRACTS.md §7.1: TRANSFER_SAME_ACCOUNT, 422. Same reasoning as
        `PaymentMade._check_split` for why the check lives on the model.
        """
        if self.from_account_id == self.to_account_id:
            raise AppError(
                ErrorCode.TRANSFER_SAME_ACCOUNT,
                "from_account_id and to_account_id must differ",
                {"account_id": self.from_account_id},
            )
        return self


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


# --------------------------------------------------------------------------- §3.1
# Dedupe keys.
#
# `dedupe_key` is UNIQUE in persistence and `append_event` is
# INSERT ... ON CONFLICT DO NOTHING (CONTRACTS.md §8.8), so the key IS the idempotency
# boundary: two payloads that produce the same key are the same event and the second
# write is a no-op. Everything below exists to make that mapping total, injective where
# it must be, and identical across processes and Python versions.

#: Fields that can never discriminate one event from another and are therefore excluded
#: from the manual composite's digest.
#:
#: * `event_id` is freshly generated per attempt — including it would make every key
#:   unique and defeat dedupe entirely. This is the trap the exclusion exists to close,
#:   because the obvious caller passes `event.model_dump()`.
#: * `recorded_at` is "audit + tie-break only" (§3.1); the same purchase entered twice
#:   has two different `recorded_at` values and one identity.
#: * `dedupe_key` is the value being derived. Feeding it back in is circular.
#: * `event_type` already appears verbatim in the key, so hashing it again only makes
#:   the key depend on whether the caller happened to include it in `payload`.
_NON_DISCRIMINATING_FIELDS = frozenset(
    {"event_id", "recorded_at", "dedupe_key", "event_type"}
)


def _canonical(value: object) -> str:
    """Serialize `value` to a stable, type-tagged string.

    Deterministic across processes and runs: mapping keys are sorted, `hash()` is never
    consulted, and no `repr` of a container is relied upon. Each branch carries a type
    tag so the encoding is injective across types — without it the string `"5"` and the
    integer `5` would hash identically, and a caller could collide two genuinely
    different events.

    Raises:
        AppError(VALIDATION_FAILED) for a type with no defined encoding. Silently
        falling back to `str()` would make the key depend on a `__repr__` nobody
        promised to keep stable, which is the one thing this function must not do.
    """
    if value is None:
        return "n"
    # bool before int: `isinstance(True, int)` is True.
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, str):
        # StrEnum members land here and encode as their value, which is what persists.
        return "s:" + json.dumps(str(value), ensure_ascii=True)
    if isinstance(value, bytes):
        return "y:" + value.hex()
    # datetime before date: datetime is a subclass of date.
    if isinstance(value, dt.datetime):
        return f"t:{value.isoformat()}"
    if isinstance(value, dt.date):
        return f"d:{value.isoformat()}"
    if isinstance(value, UUID):
        return f"u:{value}"
    if isinstance(value, BaseModel):
        return "m:" + _canonical(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return "{" + ",".join(_canonical_pairs(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, (set, frozenset)):
        # Iteration order of a set is not stable, so sort the encoded members.
        return "<" + ",".join(sorted(_canonical(item) for item in value)) + ">"

    raise AppError(
        ErrorCode.VALIDATION_FAILED,
        f"no canonical encoding for {type(value).__name__} in a dedupe payload",
        {"type": type(value).__name__},
    )


def _canonical_pairs(mapping: Mapping[object, object]) -> list[str]:
    """Encode a mapping's entries, key-sorted, with `None` values dropped.

    Sorting is what makes the key independent of field order, so
    `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` are one key.

    Dropping `None` makes an explicitly-null field and an absent field the same, which
    is the semantics dedupe wants: a caller passing `event.model_dump()` (which
    materializes every optional as `None`) and a caller passing only the fields it set
    describe the same event and must produce the same key.
    """
    return sorted(
        json.dumps(str(key), ensure_ascii=True) + ":" + _canonical(item)
        for key, item in mapping.items()
        if item is not None
    )


def _component(value: object) -> str:
    """Render the plaintext `{date}` / `{amount_minor}` components of the manual key.

    These are spelled out in §3.1 so a key stays greppable in a database. The digest
    beside them is what actually carries the discrimination, so an absent component
    renders empty rather than raising — `EventVoided` has no `amount_minor`.
    """
    if value is None:
        return ""
    if isinstance(value, dt.date):  # covers dt.datetime
        return value.isoformat()
    return str(value)


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

    Implementation notes, where §3.1 left the shape open:

    * The three key forms are exactly the §3.1 table:
      `receipt:{sha256}`, `ext:{provider}:{provider_txn_id}`, and
      `manual:{event_type}:{date}:{amount_minor}:{sha256(discriminating_fields)}`.
    * `discriminating_fields` is the whole of `payload` minus
      `_NON_DISCRIMINATING_FIELDS` and minus `None` values, canonically encoded by
      `_canonical`. `note` deliberately participates: §3.1 names an explicit `note` as
      one of the two ways a caller disambiguates a genuine duplicate.
    * `content_sha256` is lowercased. `hashlib.hexdigest()` is already lowercase, so
      this is a no-op for real callers and only guards a hand-built uppercase digest
      from splitting one receipt into two keys.
    * `client_nonce` is folded into the **manual** form only. §6.1 describes it as
      permitting "genuinely-duplicate manual entries", and folding it into the receipt
      form would contradict the stronger postcondition above — the same receipt bytes
      must always yield the same key. An external provider's `provider_txn_id` is
      already unique by construction, so a nonce there has nothing to disambiguate.
      An empty nonce is treated as absent.
    """
    if content_sha256 is not None:
        return f"receipt:{content_sha256.lower()}"

    if external_ref is not None:
        return f"ext:{external_ref.provider}:{external_ref.provider_txn_id}"

    discriminating = {
        name: value
        for name, value in payload.items()
        if name not in _NON_DISCRIMINATING_FIELDS
    }
    digest = hashlib.sha256(
        _canonical(discriminating).encode("utf-8")
    ).hexdigest()

    date_component = _component(payload.get("date"))
    amount_component = _component(payload.get("amount_minor"))
    key = f"manual:{event_type}:{date_component}:{amount_component}:{digest}"

    if client_nonce:
        return f"{key}:nonce:{client_nonce}"
    return key


def is_voided(event: Event, voids: Mapping[UUID, EventVoided]) -> bool:
    """Postcondition: True iff an EventVoided targets event.event_id.

    `voids` is the void index, **keyed by `EventVoided.target_event_id`** — the id of
    the event being voided, not the id of the `EventVoided` record itself. That keying
    is what makes this an O(1) lookup, which matters because the projection filters
    every event through it on every read (PLAN.md §3: every read recomputes from
    genesis). The target is re-checked rather than trusted so a mis-keyed index reads
    as "not voided" instead of silently voiding the wrong event.
    """
    void = voids.get(event.event_id)
    return void is not None and void.target_event_id == event.event_id
