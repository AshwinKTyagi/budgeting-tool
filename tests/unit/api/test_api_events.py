"""`POST /events` and `/events/batch` (CONTRACTS.md §6.1).

The load-bearing assertion in this file is the 201-vs-200 pair. CONTRACTS.md says it
three times — §6.1 twice and §7.1 once — because "duplicate ingestion is an error" is
the reflex it exists to prevent, and it is the reflex that makes a re-uploaded receipt
look like a failure to a user who did nothing wrong.
"""

from __future__ import annotations

from tests.unit.api.conftest import API, CHECKING, SAVINGS, VISA, expense, income

from starlette.testclient import TestClient

from core.types import ErrorCode


def float_expense(amount: float) -> dict[str, object]:
    """An `ExpenseRecorded` body whose amount is a float, which is the bug under test.

    Spelled out rather than routed through `conftest.expense`, whose `amount_minor` is
    typed `int` — as it should be. A test that needs a float in a money field has to say
    so explicitly, and `mypy --strict` is what makes that true of the helpers too.
    """
    return {
        "event_type": "ExpenseRecorded",
        "date": "2026-03-02",
        "amount_minor": amount,
        "category": "groceries",
        "account_id": CHECKING,
    }


# ------------------------------------------------------------------ 201 then 200


def test_first_append_is_201_and_second_is_200(client: TestClient) -> None:
    """The same manual entry twice: one write, one no-op, one `event_id`."""
    first = client.post(f"{API}/events", json={"event": income("2026-03-02", 450_000)})
    assert first.status_code == 201
    assert first.json()["deduplicated"] is False

    second = client.post(f"{API}/events", json={"event": income("2026-03-02", 450_000)})
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True

    # The id returned on the duplicate is the STORED row's, not the second attempt's.
    assert second.json()["event_id"] == first.json()["event_id"]
    assert second.json()["dedupe_key"] == first.json()["dedupe_key"]


def test_a_duplicate_writes_nothing(client: TestClient) -> None:
    client.post(f"{API}/events", json={"event": income("2026-03-02", 450_000)})
    client.post(f"{API}/events", json={"event": income("2026-03-02", 450_000)})
    page = client.get(f"{API}/ledger").json()
    assert page["total_count"] == 1


def test_client_nonce_makes_a_genuine_duplicate_survive(client: TestClient) -> None:
    """Two identical $4.50 coffees on the same day collide by design (§3.1).

    The nonce is how the user says "no, there really were two".
    """
    body = expense("2026-03-02", 450)
    first = client.post(f"{API}/events", json={"event": body})
    second = client.post(f"{API}/events", json={"event": body, "client_nonce": "second"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["dedupe_key"] != second.json()["dedupe_key"]
    assert client.get(f"{API}/ledger").json()["total_count"] == 2


def test_server_assigns_event_id_and_dedupe_key(client: TestClient) -> None:
    """The body omits both; §6.1 says the server fills them in."""
    response = client.post(f"{API}/events", json={"event": income("2026-03-02", 1)})
    body = response.json()
    assert body["event_id"]
    assert body["dedupe_key"].startswith("manual:IncomeReceived:2026-03-02:1:")


def test_a_client_supplied_dedupe_key_is_honoured(client: TestClient) -> None:
    response = client.post(
        f"{API}/events",
        json={"event": income("2026-03-02", 1, dedupe_key="ext:acme:txn-1")},
    )
    assert response.json()["dedupe_key"] == "ext:acme:txn-1"


def test_an_external_ref_decides_the_key(client: TestClient) -> None:
    """A provider replaying a transaction must land on the identical key (§3.1)."""
    body = income(
        "2026-03-02",
        450_000,
        external_ref={"provider": "acme", "provider_txn_id": "txn-1"},
    )
    first = client.post(f"{API}/events", json={"event": body})
    assert first.json()["dedupe_key"] == "ext:acme:txn-1"
    assert client.post(f"{API}/events", json={"event": body}).status_code == 200


# ------------------------------------------------------------------ strictness
# The API takes JSON, and JSON has no native date. Pydantic's json mode accepts the
# string form for `dt.date` and `UUID` — the only representation JSON has for them —
# while still rejecting a float where a `Minor` is declared. Both halves are asserted
# here, because a fix for either one that broke the other would look like it worked.


def test_a_json_date_string_is_accepted(client: TestClient) -> None:
    response = client.post(f"{API}/events", json={"event": income("2026-03-02", 1)})
    assert response.status_code == 201
    row = client.get(f"{API}/ledger").json()["rows"][0]
    assert row["date"] == "2026-03-02"


def test_a_float_amount_is_rejected(client: TestClient) -> None:
    """`19.99` where a `Minor` is declared. Strict mode, not a rounding decision."""
    response = client.post(
        f"{API}/events", json={"event": float_expense(19.99)}
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_a_whole_float_amount_is_still_rejected(client: TestClient) -> None:
    """`1999.0` divides evenly and is still a float. There is no tolerance anywhere."""
    response = client.post(
        f"{API}/events", json={"event": float_expense(1999.0)}
    )
    assert response.status_code == 422


def test_an_unknown_event_type_is_rejected(client: TestClient) -> None:
    response = client.post(f"{API}/events", json={"event": {"event_type": "Nope"}})
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_an_empty_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{API}/events", content=b"", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_an_extra_field_is_rejected(client: TestClient) -> None:
    """`extra="forbid"` on every model (CONTRACTS.md §1)."""
    response = client.post(
        f"{API}/events", json={"event": income("2026-03-02", 1, colour="blue")}
    )
    assert response.status_code == 422


# ------------------------------------------------------ the specific §7.1 codes


def test_a_transfer_to_itself_is_transfer_same_account(client: TestClient) -> None:
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "TransferMade",
                "date": "2026-03-02",
                "amount_minor": 1_000,
                "from_account_id": CHECKING,
                "to_account_id": CHECKING,
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.TRANSFER_SAME_ACCOUNT.value


def test_a_mismatched_payment_split_is_its_own_code(client: TestClient) -> None:
    """`PAYMENT_SPLIT_MISMATCH` exists so this is distinguishable from a generic 422."""
    client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "ObligationRaised",
                "date": "2026-03-01",
                "obligation_id": "loan-2026-03",
                "due_date": "2026-03-25",
                "amount_minor": 118_000,
                "payee": "Bank",
                "category": "debt",
            }
        },
    )
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "PaymentMade",
                "date": "2026-03-25",
                "amount_minor": 118_000,
                "obligation_id": "loan-2026-03",
                "account_id": CHECKING,
                "principal_minor": 100_000,
                "interest_minor": 17_000,
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.PAYMENT_SPLIT_MISMATCH.value


def test_an_undefined_account_is_unknown_account(client: TestClient) -> None:
    response = client.post(
        f"{API}/events",
        json={"event": income("2026-03-02", 1, account_id="not-an-account")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.UNKNOWN_ACCOUNT.value
    assert response.json()["details"]["account_id"] == "not-an-account"


def test_a_transfer_checks_both_ends(client: TestClient) -> None:
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "TransferMade",
                "date": "2026-03-02",
                "amount_minor": 1_000,
                "from_account_id": CHECKING,
                "to_account_id": "nowhere",
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.UNKNOWN_ACCOUNT.value


def test_a_payment_against_nothing_is_unknown_obligation(client: TestClient) -> None:
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "PaymentMade",
                "date": "2026-03-05",
                "amount_minor": 1_000,
                "obligation_id": "no-such-bill",
                "account_id": CHECKING,
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.UNKNOWN_OBLIGATION.value


def test_a_payment_against_an_expected_obligation_is_accepted(
    client: TestClient,
) -> None:
    """The rent `FixedCost` is never an event; the projection materializes it.

    So "known at write time" has to mean known to the *fold*, not present in the events
    table — which is why the check runs a projection rather than a `SELECT`.
    """
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "PaymentMade",
                "date": "2026-03-01",
                "amount_minor": 120_000,
                "obligation_id": "expected:rent:2026-03",
                "account_id": CHECKING,
            }
        },
    )
    assert response.status_code == 201


def test_a_rejected_event_writes_nothing(client: TestClient) -> None:
    """The session rolls back, so a 422 never leaves a row behind."""
    client.post(f"{API}/events", json={"event": income("2026-03-02", 1)})
    client.post(
        f"{API}/events",
        json={"event": income("2026-03-03", 1, account_id="not-an-account")},
    )
    assert client.get(f"{API}/ledger").json()["total_count"] == 1


# ----------------------------------------------------------------------- batch


def test_batch_returns_one_result_per_item(client: TestClient) -> None:
    response = client.post(
        f"{API}/events/batch",
        json={
            "events": [
                {"event": income("2026-03-02", 450_000)},
                {"event": expense("2026-03-03", 4_599)},
                {"event": expense("2026-03-04", 1_250, account_id=VISA)},
            ]
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 3
    assert [row["deduplicated"] for row in results] == [False, False, False]


def test_replaying_a_batch_is_a_no_op(client: TestClient) -> None:
    body = {
        "events": [
            {"event": income("2026-03-02", 450_000)},
            {"event": expense("2026-03-03", 4_599)},
        ]
    }
    client.post(f"{API}/events/batch", json=body)
    replay = client.post(f"{API}/events/batch", json=body)
    assert [row["deduplicated"] for row in replay.json()["results"]] == [True, True]
    assert client.get(f"{API}/ledger").json()["total_count"] == 2


def test_a_malformed_item_fails_the_whole_batch(client: TestClient) -> None:
    """Partial success is per-item *results*, not per-item transactions.

    That reading is `ingestion.append_events`' own, and following it means a batch is
    all-or-nothing on the write side: the good item is rolled back with the bad one.
    """
    response = client.post(
        f"{API}/events/batch",
        json={
            "events": [
                {"event": income("2026-03-02", 450_000)},
                {"event": float_expense(19.99)},
            ]
        },
    )
    assert response.status_code == 422
    assert client.get(f"{API}/ledger").json()["total_count"] == 0


def test_a_batch_satisfies_its_own_forward_reference(client: TestClient) -> None:
    """An obligation raised in the batch makes a payment later in it valid.

    The batch is one transaction, so "known at write time" means known once the batch
    has been written.
    """
    response = client.post(
        f"{API}/events/batch",
        json={
            "events": [
                {
                    "event": {
                        "event_type": "ObligationRaised",
                        "date": "2026-03-01",
                        "obligation_id": "plumber",
                        "due_date": "2026-03-20",
                        "amount_minor": 4_500,
                        "payee": "Plumber",
                        "category": "maintenance",
                    }
                },
                {
                    "event": {
                        "event_type": "PaymentMade",
                        "date": "2026-03-20",
                        "amount_minor": 4_500,
                        "obligation_id": "plumber",
                        "account_id": CHECKING,
                    }
                },
            ]
        },
    )
    assert response.status_code == 200


def test_savings_drawn_needs_no_account(client: TestClient) -> None:
    """`SavingsDrawn` names no account; the projection resolves the savings one."""
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "SavingsDrawn",
                "date": "2026-03-12",
                "amount_minor": 25_000,
                "reason": "car repair",
            }
        },
    )
    assert response.status_code == 201
    assert SAVINGS  # the seeded account exists; the event just does not name it
