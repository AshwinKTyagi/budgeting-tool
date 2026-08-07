"""Unit tests for `domain/events.py` (CONTRACTS.md §3, §8.4).

Owned by `module/domain-events` (PLAN.md §13.2).

No tolerance anywhere: integer arithmetic is exact and every assertion is `==`
(CLAUDE.md §4.6). No clock reads: every date and instant below is an explicit literal
(CLAUDE.md §4.4).

The Hypothesis strategies here are deliberately module-local. The shared
`tests/properties/strategies.py` and the fifteen named invariants of CLAUDE.md §5.1
belong to `module/properties`; nothing here is a substitute for them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from core.types import AppError, ErrorCode
from domain.events import (
    AccountOpeningBalance,
    Event,
    EventBase,
    EventVoided,
    ExpenseRecorded,
    ExternalRef,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    ObligationRaised,
    PaymentMade,
    SavingsDrawn,
    TransferMade,
    compute_dedupe_key,
    is_voided,
)

# --------------------------------------------------------------------------- fixtures
# Literals, not fixtures: a module-level constant cannot drift between tests and cannot
# read a clock.

EVENT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
VOID_EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")

DATE = dt.date(2026, 3, 31)
DUE_DATE = dt.date(2026, 4, 15)
RECORDED_AT = dt.datetime(2026, 3, 31, 12, 0, 0, tzinfo=dt.timezone.utc)

_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Event)


def _income() -> IncomeReceived:
    return IncomeReceived(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="manual:IncomeReceived:2026-03-31:250000:deadbeef",
        amount_minor=250_000,
        source="employer",
        account_id="checking",
    )


ALL_EVENTS: tuple[EventBase, ...] = (
    _income(),
    GiftReceived(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-gift",
        amount_minor=5_000,
        source="grandmother",
        account_id="checking",
    ),
    ObligationRaised(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-obligation",
        obligation_id="ob-1",
        due_date=DUE_DATE,
        amount_minor=120_000,
        payee="landlord",
        category="rent",
        recurring_id="fc-rent",
    ),
    PaymentMade(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-payment",
        amount_minor=120_000,
        obligation_id="ob-1",
        account_id="checking",
        principal_minor=118_000,
        interest_minor=2_000,
    ),
    ExpenseRecorded(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-expense",
        amount_minor=-450,
        category="coffee",
        account_id="card",
        merchant="the cafe",
    ),
    SavingsDrawn(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-draw",
        amount_minor=30_000,
        reason="car repair",
    ),
    TransferMade(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-transfer",
        amount_minor=75_000,
        from_account_id="checking",
        to_account_id="card",
    ),
    AccountOpeningBalance(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-opening",
        account_id="loan",
        amount_minor=-1_500_000,
    ),
    InterestCharged(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-interest-charged",
        account_id="card",
        cycle_id="card:2026-03",
        amount_minor=2_241,
    ),
    InterestEarned(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-interest-earned",
        account_id="savings",
        cycle_id="savings:2026-03",
        amount_minor=1_849,
    ),
    EventVoided(
        event_id=VOID_EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k-void",
        target_event_id=EVENT_ID,
        reason="entered twice",
    ),
)

EXPECTED_TAGS = (
    "IncomeReceived",
    "GiftReceived",
    "ObligationRaised",
    "PaymentMade",
    "ExpenseRecorded",
    "SavingsDrawn",
    "TransferMade",
    "AccountOpeningBalance",
    "InterestCharged",
    "InterestEarned",
    "EventVoided",
)


# ------------------------------------------------------------------ construction
# CONTRACTS.md §9: every event type in §3.2 appears in the Event union.


def test_every_contract_event_type_is_covered() -> None:
    tags = tuple(getattr(event, "event_type") for event in ALL_EVENTS)
    assert tags == EXPECTED_TAGS
    assert len(ALL_EVENTS) == 11


@pytest.mark.parametrize("event", ALL_EVENTS, ids=EXPECTED_TAGS)
def test_event_round_trips_through_the_union(event: EventBase) -> None:
    """Dump and re-parse: the discriminated union reproduces the exact instance."""
    parsed = _EVENT_ADAPTER.validate_python(event.model_dump())
    assert parsed == event
    assert type(parsed) is type(event)


@pytest.mark.parametrize("event", ALL_EVENTS, ids=EXPECTED_TAGS)
def test_event_round_trips_through_json(event: EventBase) -> None:
    parsed = _EVENT_ADAPTER.validate_json(event.model_dump_json())
    assert parsed == event


@pytest.mark.parametrize("event", ALL_EVENTS, ids=EXPECTED_TAGS)
def test_every_at_field_is_utc(event: EventBase) -> None:
    """CLAUDE.md §4.5: every `_at` field is aware and UTC once validated."""
    at_fields = [name for name in type(event).model_fields if name.endswith("_at")]
    assert at_fields == ["recorded_at"]
    assert event.recorded_at.tzinfo == dt.timezone.utc


# -------------------------------------------------------------------- strict mode
# CLAUDE.md §2.3 rule 1. This is the single most important behaviour in the file:
# without strict mode a float reaching the boundary is silently rounded, which is
# exactly the failure integer minor units exist to prevent.


def test_strict_mode_rejects_a_fractional_float_for_a_minor_field() -> None:
    with pytest.raises(ValidationError):
        IncomeReceived(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=RECORDED_AT,
            dedupe_key="k",
            amount_minor=19.99,  # type: ignore[arg-type]
            source="employer",
            account_id="checking",
        )


def test_strict_mode_rejects_an_integral_float_for_a_minor_field() -> None:
    """`1999.0` is the dangerous one: it round-trips through `int()` losslessly, so
    only strict mode stands between a float pipeline and the ledger."""
    with pytest.raises(ValidationError):
        IncomeReceived(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=RECORDED_AT,
            dedupe_key="k",
            amount_minor=1999.0,  # type: ignore[arg-type]
            source="employer",
            account_id="checking",
        )


@pytest.mark.parametrize("bad_amount", [19.99, 1999.0, 0.0, -0.5])
def test_union_parsing_rejects_every_float_amount(bad_amount: float) -> None:
    payload = _income().model_dump()
    payload["amount_minor"] = bad_amount
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


MONEY_FIELD_CASES: tuple[tuple[str, str], ...] = tuple(
    (str(getattr(event, "event_type")), name)
    for event in ALL_EVENTS
    for name in type(event).model_fields
    if name.endswith("_minor")
)


@pytest.mark.parametrize(
    ("tag", "field"),
    MONEY_FIELD_CASES,
    ids=[f"{tag}.{field}" for tag, field in MONEY_FIELD_CASES],
)
@pytest.mark.parametrize("bad_amount", [19.99, 1999.0, -0.5])
def test_strict_mode_holds_through_the_union_for_every_money_field(
    tag: str, field: str, bad_amount: float
) -> None:
    """The regression guard for a real strict-mode leak. Read this before relaxing it.

    `Minor` is a PEP 695 alias, so pydantic hoists it into a shared schema definition
    once a model mentions it more than once. A `model_validator(mode="after")` wraps
    the model schema, putting that hoisted definition outside the model's config
    scope — and `strict=True` then stops applying whenever the model is validated
    through an ENCLOSING adapter instead of directly.

    That is not a corner case: `AppendEventRequest.event` is the `Event` union itself
    (CONTRACTS.md §6.1), so the enclosing adapter is the ingestion boundary. Before the
    `Field(strict=True)` pin in `domain/events.py`,
    `TypeAdapter(Event).validate_python` accepted `principal_minor=118_000.0` and
    silently coerced it to `118_000`, while `PaymentMade.model_validate` on the same
    payload rejected it correctly. Both paths must reject.

    Asserting per event type rather than only on `PaymentMade` is deliberate: the trap
    is triggered by a field count and a decorator, both of which can change.
    """
    source = next(e for e in ALL_EVENTS if getattr(e, "event_type") == tag)

    via_union = source.model_dump()
    via_union[field] = bad_amount
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(via_union)

    via_model = source.model_dump()
    via_model[field] = bad_amount
    with pytest.raises(ValidationError):
        type(source).model_validate(via_model)


def test_strict_mode_still_accepts_every_money_field_as_an_int() -> None:
    """The pin above must not have made a valid integer unparseable."""
    for event in ALL_EVENTS:
        parsed = _EVENT_ADAPTER.validate_python(event.model_dump())
        for name in type(event).model_fields:
            if name.endswith("_minor"):
                value = getattr(parsed, name)
                assert value is None or type(value) is int


def test_strict_mode_rejects_a_numeric_string_for_a_minor_field() -> None:
    payload = _income().model_dump()
    payload["amount_minor"] = "250000"
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


def test_strict_mode_rejects_a_datetime_for_a_business_date() -> None:
    """Business dates are `dt.date` with no time component — that is the type, not a
    convention (CLAUDE.md §4.5)."""
    payload = _income().model_dump()
    payload["date"] = dt.datetime(2026, 3, 31, 12, tzinfo=dt.timezone.utc)
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


def test_strict_mode_rejects_a_string_uuid() -> None:
    payload = _income().model_dump()
    payload["event_id"] = str(EVENT_ID)
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


# ------------------------------------------------------------------ model config


def test_models_are_frozen() -> None:
    """Note there is no `type: ignore` here on purpose: `mypy --strict` with the
    pydantic plugin does NOT reject this assignment, so `frozen=True` in
    MONEY_MODEL_CONFIG is the only thing standing between the projection and a
    mutated event (CLAUDE.md §4.2)."""
    event = _income()
    with pytest.raises(ValidationError):
        event.amount_minor = 1
    assert event.amount_minor == 250_000


def test_external_ref_is_frozen() -> None:
    ref = ExternalRef(provider="acme", provider_txn_id="txn-1")
    with pytest.raises(ValidationError):
        ref.provider = "other"
    assert ref.provider == "acme"


def test_extra_fields_are_forbidden() -> None:
    payload = _income().model_dump()
    payload["surprise_minor"] = 1
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


def test_every_money_field_carries_the_minor_suffix() -> None:
    """CLAUDE.md §2.2: a money field without `_minor` is a review failure."""
    money_fields = {
        "amount_minor",
        "principal_minor",
        "interest_minor",
    }
    for event in ALL_EVENTS:
        for name, field in type(event).model_fields.items():
            annotation = repr(field.annotation)
            looks_like_money = name in money_fields
            if looks_like_money:
                assert name.endswith("_minor"), (type(event).__name__, name)
            assert "float" not in annotation
            assert "Decimal" not in annotation


# ------------------------------------------------------------- discriminated union


@pytest.mark.parametrize("tag", EXPECTED_TAGS)
def test_union_dispatches_on_the_tag(tag: str) -> None:
    source = next(e for e in ALL_EVENTS if getattr(e, "event_type") == tag)
    parsed = _EVENT_ADAPTER.validate_python(source.model_dump())
    assert getattr(parsed, "event_type") == tag


def test_union_rejects_an_unknown_tag() -> None:
    payload = _income().model_dump()
    payload["event_type"] = "MoneyVanished"
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


def test_union_rejects_a_missing_tag() -> None:
    payload = _income().model_dump()
    del payload["event_type"]
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


def test_union_rejects_a_tag_that_does_not_match_the_body() -> None:
    """A `SavingsDrawn` tag over an `IncomeReceived` body has no `reason`."""
    payload = _income().model_dump()
    payload["event_type"] = "SavingsDrawn"
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)


# ---------------------------------------------------------------------- UtcInstant


def test_naive_recorded_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IncomeReceived(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=dt.datetime(2026, 3, 31, 12),
            dedupe_key="k",
            amount_minor=250_000,
            source="employer",
            account_id="checking",
        )


def test_aware_non_utc_recorded_at_is_normalized_preserving_the_instant() -> None:
    offset = dt.timezone(dt.timedelta(hours=5, minutes=30))
    local = dt.datetime(2026, 3, 31, 17, 30, 0, tzinfo=offset)
    event = IncomeReceived(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=local,
        dedupe_key="k",
        amount_minor=250_000,
        source="employer",
        account_id="checking",
    )
    assert event.recorded_at.tzinfo == dt.timezone.utc
    assert event.recorded_at == dt.datetime(2026, 3, 31, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert event.recorded_at == local  # same instant, exactly


@given(
    offset_minutes=st.integers(min_value=-14 * 60, max_value=14 * 60),
    naive=st.datetimes(
        min_value=dt.datetime(2000, 1, 1), max_value=dt.datetime(2100, 1, 1)
    ),
)
def test_normalization_never_moves_the_instant(
    offset_minutes: int, naive: dt.datetime
) -> None:
    aware = naive.replace(tzinfo=dt.timezone(dt.timedelta(minutes=offset_minutes)))
    event = IncomeReceived(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=aware,
        dedupe_key="k",
        amount_minor=1,
        source="employer",
        account_id="checking",
    )
    assert event.recorded_at == aware
    assert event.recorded_at.tzinfo == dt.timezone.utc


def test_ledger_ordering_is_by_instant_not_by_wall_clock() -> None:
    """CONTRACTS.md §3.1: `(date, recorded_at, event_id)` is total and stable, and
    aware datetimes compare by instant regardless of offset."""
    east = dt.timezone(dt.timedelta(hours=9))
    earlier = IncomeReceived(
        event_id=OTHER_EVENT_ID,
        date=DATE,
        # 23:00+09:00 == 14:00Z, which is LATER than 13:00Z despite the bigger clock
        recorded_at=dt.datetime(2026, 3, 31, 23, 0, tzinfo=east),
        dedupe_key="k1",
        amount_minor=1,
        source="a",
        account_id="checking",
    )
    later = IncomeReceived(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=dt.datetime(2026, 3, 31, 13, 0, tzinfo=dt.timezone.utc),
        dedupe_key="k2",
        amount_minor=1,
        source="b",
        account_id="checking",
    )
    ordered = sorted(
        [earlier, later], key=lambda e: (e.date, e.recorded_at, e.event_id)
    )
    assert [e.dedupe_key for e in ordered] == ["k2", "k1"]


# ------------------------------------------------------- model-level input errors
# CONTRACTS.md §7.1. These are errors, not warnings: input that could never be valid.


def test_payment_split_must_reconcile() -> None:
    with pytest.raises(AppError) as excinfo:
        PaymentMade(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=RECORDED_AT,
            dedupe_key="k",
            amount_minor=120_000,
            obligation_id="ob-1",
            account_id="checking",
            principal_minor=118_000,
            interest_minor=1_999,
        )
    assert excinfo.value.code == ErrorCode.PAYMENT_SPLIT_MISMATCH


@pytest.mark.parametrize(
    ("principal_minor", "interest_minor"),
    [(118_000, None), (None, 2_000)],
)
def test_payment_split_must_be_both_or_neither(
    principal_minor: int | None, interest_minor: int | None
) -> None:
    with pytest.raises(AppError) as excinfo:
        PaymentMade(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=RECORDED_AT,
            dedupe_key="k",
            amount_minor=120_000,
            obligation_id="ob-1",
            account_id="checking",
            principal_minor=principal_minor,
            interest_minor=interest_minor,
        )
    assert excinfo.value.code == ErrorCode.PAYMENT_SPLIT_MISMATCH


def test_payment_without_a_split_is_valid() -> None:
    payment = PaymentMade(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k",
        amount_minor=120_000,
        obligation_id="ob-1",
        account_id="checking",
    )
    assert payment.principal_minor is None
    assert payment.interest_minor is None


def test_transfer_to_the_same_account_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        TransferMade(
            event_id=EVENT_ID,
            date=DATE,
            recorded_at=RECORDED_AT,
            dedupe_key="k",
            amount_minor=75_000,
            from_account_id="checking",
            to_account_id="checking",
        )
    assert excinfo.value.code == ErrorCode.TRANSFER_SAME_ACCOUNT


def test_a_refund_is_a_negative_expense_and_is_permitted() -> None:
    """CONTRACTS.md §3.2: negative permitted on ExpenseRecorded, a refund."""
    expense = ExpenseRecorded(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k",
        amount_minor=-450,
        category="coffee",
        account_id="card",
    )
    assert expense.amount_minor == -450


def test_a_loan_disbursement_is_a_negative_opening_balance() -> None:
    opening = AccountOpeningBalance(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k",
        account_id="loan",
        amount_minor=-1_500_000,
    )
    assert opening.amount_minor == -1_500_000


# --------------------------------------------------------------------- dedupe keys

MANUAL_PAYLOAD: dict[str, object] = {
    "date": dt.date(2026, 3, 31),
    "amount_minor": 450,
    "category": "coffee",
    "account_id": "card",
    "merchant": "the cafe",
}


def test_receipt_key_shape() -> None:
    digest = "a" * 64
    key = compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD, content_sha256=digest
    )
    assert key == f"receipt:{digest}"


def test_identical_receipt_bytes_always_yield_the_same_key() -> None:
    digest = "9f2c" + "0" * 60
    first = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, content_sha256=digest)
    second = compute_dedupe_key(
        "ExpenseRecorded",
        {"date": dt.date(2027, 1, 1), "amount_minor": 999, "note": "different"},
        content_sha256=digest,
    )
    assert first == second


def test_receipt_digest_case_is_normalized() -> None:
    lower = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, content_sha256="ab" * 32)
    upper = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, content_sha256="AB" * 32)
    assert lower == upper


def test_external_key_shape() -> None:
    ref = ExternalRef(provider="acme", provider_txn_id="txn-42")
    key = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, external_ref=ref)
    assert key == "ext:acme:txn-42"


def test_precedence_is_content_then_external_then_manual() -> None:
    ref = ExternalRef(provider="acme", provider_txn_id="txn-42")
    both = compute_dedupe_key(
        "ExpenseRecorded",
        MANUAL_PAYLOAD,
        content_sha256="cd" * 32,
        external_ref=ref,
    )
    assert both == "receipt:" + "cd" * 32

    external_only = compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD, external_ref=ref
    )
    assert external_only == "ext:acme:txn-42"

    manual = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD)
    assert manual.startswith("manual:ExpenseRecorded:2026-03-31:450:")


def test_manual_key_shape_and_digest_length() -> None:
    key = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD)
    head, _, digest = key.rpartition(":")
    assert head == "manual:ExpenseRecorded:2026-03-31:450"
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_manual_key_tolerates_an_event_without_an_amount() -> None:
    """`EventVoided` has no `amount_minor`; the component renders empty and the
    digest still discriminates."""
    payload: dict[str, object] = {
        "date": DATE,
        "target_event_id": EVENT_ID,
        "reason": "entered twice",
    }
    key = compute_dedupe_key("EventVoided", payload)
    assert key.startswith("manual:EventVoided:2026-03-31::")

    other = compute_dedupe_key(
        "EventVoided",
        {"date": DATE, "target_event_id": OTHER_EVENT_ID, "reason": "entered twice"},
    )
    assert key != other


def test_manual_key_is_deterministic() -> None:
    assert compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD) == compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD
    )


def test_manual_key_is_stable_across_field_order() -> None:
    reversed_payload: dict[str, object] = dict(reversed(list(MANUAL_PAYLOAD.items())))
    assert list(reversed_payload) != list(MANUAL_PAYLOAD)
    assert compute_dedupe_key("ExpenseRecorded", reversed_payload) == compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", dt.date(2026, 4, 1)),
        ("amount_minor", 451),
        ("category", "groceries"),
        ("account_id", "checking"),
        ("merchant", "another cafe"),
        ("note", "a note that disambiguates"),
    ],
)
def test_manual_key_changes_when_any_discriminating_field_changes(
    field: str, value: object
) -> None:
    changed = dict(MANUAL_PAYLOAD)
    changed[field] = value
    assert compute_dedupe_key("ExpenseRecorded", changed) != compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD
    )


def test_manual_key_changes_with_the_event_type() -> None:
    assert compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD) != compute_dedupe_key(
        "SavingsDrawn", MANUAL_PAYLOAD
    )


@pytest.mark.parametrize("field", ["event_id", "recorded_at", "dedupe_key", "event_type"])
def test_manual_key_ignores_non_discriminating_fields(field: str) -> None:
    """The trap this closes: the obvious caller passes `event.model_dump()`, whose
    freshly-generated `event_id` would make every key unique and dedupe a no-op."""
    values: dict[str, object] = {
        "event_id": OTHER_EVENT_ID,
        "recorded_at": dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc),
        "dedupe_key": "some-previous-key",
        "event_type": "ExpenseRecorded",
    }
    polluted = dict(MANUAL_PAYLOAD)
    polluted[field] = values[field]
    assert compute_dedupe_key("ExpenseRecorded", polluted) == compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD
    )


def test_manual_key_treats_an_explicit_none_as_absent() -> None:
    """A full `model_dump()` materializes every optional as `None`; a hand-built
    payload omits it. They describe the same event and must share a key."""
    with_none = dict(MANUAL_PAYLOAD)
    with_none["note"] = None
    with_none["external_ref"] = None
    assert compute_dedupe_key("ExpenseRecorded", with_none) == compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD
    )


def test_manual_key_distinguishes_a_string_from_the_equal_number() -> None:
    """The canonical encoding is type-tagged, so `"450"` and `450` are not one key."""
    as_text = dict(MANUAL_PAYLOAD)
    as_text["category"] = "450"
    as_int = dict(MANUAL_PAYLOAD)
    as_int["category"] = 450
    assert compute_dedupe_key("ExpenseRecorded", as_text) != compute_dedupe_key(
        "ExpenseRecorded", as_int
    )


def test_manual_key_distinguishes_true_from_one() -> None:
    truthy = dict(MANUAL_PAYLOAD)
    truthy["flagged"] = True
    numeric = dict(MANUAL_PAYLOAD)
    numeric["flagged"] = 1
    assert compute_dedupe_key("ExpenseRecorded", truthy) != compute_dedupe_key(
        "ExpenseRecorded", numeric
    )


def test_manual_key_does_not_confuse_key_and_value_boundaries() -> None:
    """`{"a": "b:c"}` and `{"a:b": "c"}` must not collapse into one digest."""
    first: dict[str, object] = {"a": "b:c"}
    second: dict[str, object] = {"a:b": "c"}
    assert compute_dedupe_key("ExpenseRecorded", first) != compute_dedupe_key(
        "ExpenseRecorded", second
    )


def test_nested_payload_values_are_encoded() -> None:
    nested_a: dict[str, object] = dict(MANUAL_PAYLOAD)
    nested_a["tags"] = ["a", "b"]
    nested_b: dict[str, object] = dict(MANUAL_PAYLOAD)
    nested_b["tags"] = ["b", "a"]
    assert compute_dedupe_key("ExpenseRecorded", nested_a) != compute_dedupe_key(
        "ExpenseRecorded", nested_b
    )


def test_a_model_valued_payload_field_is_encoded() -> None:
    with_ref: dict[str, object] = dict(MANUAL_PAYLOAD)
    with_ref["external_ref"] = ExternalRef(provider="acme", provider_txn_id="txn-1")
    as_dict: dict[str, object] = dict(MANUAL_PAYLOAD)
    as_dict["external_ref"] = {"provider": "acme", "provider_txn_id": "txn-1"}
    # The model and its dump describe the same value, but are tagged distinctly, so
    # only the determinism guarantee is asserted here.
    assert compute_dedupe_key("ExpenseRecorded", with_ref) == compute_dedupe_key(
        "ExpenseRecorded", dict(with_ref)
    )
    assert compute_dedupe_key("ExpenseRecorded", as_dict) == compute_dedupe_key(
        "ExpenseRecorded", dict(as_dict)
    )


def test_an_unencodable_payload_value_is_a_validation_error() -> None:
    """Falling back to `str()` would tie the key to a `__repr__` nobody promised to
    keep stable — including one containing a memory address."""
    payload: dict[str, object] = dict(MANUAL_PAYLOAD)
    payload["mystery"] = object()
    with pytest.raises(AppError) as excinfo:
        compute_dedupe_key("ExpenseRecorded", payload)
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


# ---------------------------------------------------------------------- the nonce


def test_nonce_makes_a_deliberate_duplicate_survive() -> None:
    plain = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD)
    nonced = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, client_nonce="n1")
    assert nonced != plain
    assert nonced == f"{plain}:nonce:n1"


def test_distinct_nonces_give_distinct_keys() -> None:
    first = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, client_nonce="n1")
    second = compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, client_nonce="n2")
    assert first != second


def test_an_empty_nonce_is_absent() -> None:
    assert compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD, client_nonce=""
    ) == compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD)


def test_a_nonce_never_splits_one_receipt_into_two_keys() -> None:
    digest = "ef" * 32
    assert compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD, content_sha256=digest, client_nonce="n1"
    ) == compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, content_sha256=digest)


def test_a_nonce_never_splits_one_external_transaction_into_two_keys() -> None:
    ref = ExternalRef(provider="acme", provider_txn_id="txn-42")
    assert compute_dedupe_key(
        "ExpenseRecorded", MANUAL_PAYLOAD, external_ref=ref, client_nonce="n1"
    ) == compute_dedupe_key("ExpenseRecorded", MANUAL_PAYLOAD, external_ref=ref)


# ------------------------------------------------------------------------- voiding
# PLAN.md §8.4: EventVoided is the ONLY correction mechanism.


def _void_of(target: UUID) -> EventVoided:
    return EventVoided(
        event_id=VOID_EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key=f"manual:EventVoided:2026-03-31::{target}",
        target_event_id=target,
        reason="entered twice",
    )


def test_event_voided_references_another_event() -> None:
    income = _income()
    void = _void_of(income.event_id)
    assert void.target_event_id == income.event_id
    assert void.event_id != income.event_id


def test_is_voided_finds_the_target() -> None:
    income = _income()
    void = _void_of(income.event_id)
    assert is_voided(income, {void.target_event_id: void}) is True


def test_is_voided_is_false_for_an_untargeted_event() -> None:
    income = _income()
    void = _void_of(OTHER_EVENT_ID)
    assert is_voided(income, {void.target_event_id: void}) is False


def test_is_voided_is_false_for_an_empty_index() -> None:
    assert is_voided(_income(), {}) is False


def test_is_voided_ignores_a_miskeyed_index_entry() -> None:
    """A void filed under the wrong key reads as "not voided" rather than silently
    voiding the wrong event."""
    income = _income()
    void = _void_of(OTHER_EVENT_ID)
    assert is_voided(income, {income.event_id: void}) is False


def test_a_void_can_itself_be_looked_up() -> None:
    """`is_voided` is total over the union — it accepts an `EventVoided` too. Whether
    voiding a void is permitted is a write-time question (CANNOT_VOID_A_VOID)."""
    void = _void_of(EVENT_ID)
    assert is_voided(void, {}) is False


# ------------------------------------------------------------- module-local properties
# Not a substitute for tests/properties/ — those belong to module/properties and cover
# the fifteen named invariants of CLAUDE.md §5.1.

_PAYLOAD_KEYS = st.sampled_from(
    ["date", "amount_minor", "category", "account_id", "merchant", "note", "payee"]
)
_PAYLOAD_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**15), max_value=10**15),
    st.text(max_size=32),
    st.dates(),
    st.uuids(),
)
_PAYLOADS = st.dictionaries(_PAYLOAD_KEYS, _PAYLOAD_VALUES, max_size=7)


@given(payload=_PAYLOADS)
def test_property_dedupe_key_is_deterministic(payload: dict[str, object]) -> None:
    assert compute_dedupe_key("ExpenseRecorded", payload) == compute_dedupe_key(
        "ExpenseRecorded", payload
    )


@given(payload=_PAYLOADS, rotation=st.integers(min_value=0, max_value=6))
def test_property_dedupe_key_ignores_field_order(
    payload: dict[str, object], rotation: int
) -> None:
    items = list(payload.items())
    if items:
        cut = rotation % len(items)
        rotated = dict(items[cut:] + items[:cut])
    else:
        rotated = {}
    assert compute_dedupe_key("ExpenseRecorded", rotated) == compute_dedupe_key(
        "ExpenseRecorded", payload
    )


@given(payload=_PAYLOADS)
def test_property_dedupe_key_ignores_audit_fields(payload: dict[str, object]) -> None:
    polluted: dict[str, object] = dict(payload)
    polluted["event_id"] = UUID("44444444-4444-4444-8444-444444444444")
    polluted["recorded_at"] = dt.datetime(2001, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    polluted["dedupe_key"] = "stale"
    assert compute_dedupe_key("ExpenseRecorded", polluted) == compute_dedupe_key(
        "ExpenseRecorded", payload
    )


@given(
    payload=_PAYLOADS,
    left=st.integers(min_value=-(10**9), max_value=10**9),
    right=st.integers(min_value=-(10**9), max_value=10**9),
)
def test_property_distinct_categories_give_distinct_keys(
    payload: dict[str, object], left: int, right: int
) -> None:
    if left == right:
        return
    first: dict[str, object] = dict(payload)
    first["category"] = left
    second: dict[str, object] = dict(payload)
    second["category"] = right
    assert compute_dedupe_key("ExpenseRecorded", first) != compute_dedupe_key(
        "ExpenseRecorded", second
    )


@given(payload=_PAYLOADS, nonce=st.text(min_size=1, max_size=16))
def test_property_a_nonce_always_yields_a_fresh_key(
    payload: dict[str, object], nonce: str
) -> None:
    plain = compute_dedupe_key("ExpenseRecorded", payload)
    nonced = compute_dedupe_key("ExpenseRecorded", payload, client_nonce=nonce)
    assert nonced == f"{plain}:nonce:{nonce}"
    assert nonced != plain


@given(amount_minor=st.integers(min_value=-(10**15), max_value=10**15))
def test_property_every_integer_amount_survives_construction(amount_minor: int) -> None:
    expense = ExpenseRecorded(
        event_id=EVENT_ID,
        date=DATE,
        recorded_at=RECORDED_AT,
        dedupe_key="k",
        amount_minor=amount_minor,
        category="coffee",
        account_id="card",
    )
    assert expense.amount_minor == amount_minor
    assert type(expense.amount_minor) is int
