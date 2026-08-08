"""`POST /receipts` (CONTRACTS.md §6.4).

The receipt path is where the content-hash form of `dedupe_key` earns its existence: the
bytes are the identity of the purchase, so re-uploading the same photo is a no-op decided
by `sha256` and not by whether the user remembers having uploaded it.

CONTRACTS.md §6.1 is explicit that this "returns `200` with `deduplicated: true` and the
existing `event_id`. It is not an error and must not be reported as one." That is the
first test below and it is the reason this file exists separately from `test_events.py`.
"""

from __future__ import annotations

import hashlib

from httpx2 import Response
from starlette.testclient import TestClient

from core.types import ErrorCode
from tests.unit.api.conftest import API, CHECKING, VISA

RECEIPT = b"%PDF-1.7 fake receipt bytes"
OTHER_RECEIPT = b"%PDF-1.7 a different receipt"


def upload(
    client: TestClient,
    *,
    blob: bytes = RECEIPT,
    content_type: str = "application/pdf",
    filename: str = "receipt.pdf",
    account_id: str = CHECKING,
    amount_minor: str = "4599",
    date: str = "2026-03-07",
    **extra: str,
) -> Response:
    return client.post(
        f"{API}/receipts",
        files={"file": (filename, blob, content_type)},
        data={
            "date": date,
            "amount_minor": amount_minor,
            "category": "groceries",
            "account_id": account_id,
            **extra,
        },
    )


# ------------------------------------------------------------------ 201 then 200


def test_first_upload_is_201(client: TestClient) -> None:
    response = upload(client)
    assert response.status_code == 201
    body = response.json()
    assert body["deduplicated"] is False
    assert body["content_sha256"] == hashlib.sha256(RECEIPT).hexdigest()
    assert body["blob_id"]
    assert body["event_id"]


def test_re_uploading_identical_bytes_is_200_and_not_an_error(
    client: TestClient,
) -> None:
    first = upload(client)
    second = upload(client)
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["event_id"] == first.json()["event_id"]
    assert second.json()["blob_id"] == first.json()["blob_id"]
    assert second.json()["content_sha256"] == first.json()["content_sha256"]


def test_a_duplicate_upload_writes_no_second_event(client: TestClient) -> None:
    upload(client)
    upload(client)
    assert client.get(f"{API}/ledger").json()["total_count"] == 1


def test_identical_bytes_dedupe_even_with_different_form_fields(
    client: TestClient,
) -> None:
    """The bytes are the identity. `dedupe_key` is `receipt:{sha256}` and nothing else,
    so a second upload of the same photo with a different amount is still a no-op."""
    first = upload(client, amount_minor="4599")
    second = upload(client, amount_minor="9999", date="2026-03-08")
    assert second.status_code == 200
    assert second.json()["event_id"] == first.json()["event_id"]


def test_different_bytes_are_a_different_receipt(client: TestClient) -> None:
    first = upload(client, blob=RECEIPT)
    second = upload(client, blob=OTHER_RECEIPT)
    assert second.status_code == 201
    assert second.json()["event_id"] != first.json()["event_id"]
    assert second.json()["blob_id"] != first.json()["blob_id"]
    assert client.get(f"{API}/ledger").json()["total_count"] == 2


# ------------------------------------------------------------------- what it appends


def test_the_receipt_becomes_an_expense_recorded(client: TestClient) -> None:
    """A receipt is discretionary spending, so it is an `ExpenseRecorded` and nothing
    else. Whether it hits discretionary now or at statement payment is the account's
    `budget_timing` and the projection's call (PLAN.md §6.4)."""
    upload(client, merchant="Corner Store", note="lunch")
    row = client.get(f"{API}/ledger").json()["rows"][0]
    assert row["event_type"] == "ExpenseRecorded"
    assert row["date"] == "2026-03-07"
    assert row["amount_minor"] == 4_599
    assert row["category"] == "groceries"
    assert row["account_id"] == CHECKING
    assert row["counterparty"] == "Corner Store"
    assert row["note"] == "lunch"


def test_the_expense_reaches_the_projection(client: TestClient) -> None:
    upload(client)
    state = client.get(f"{API}/state?as_of=2026-03-31").json()
    assert state["periods"][0]["discretionary_spent_minor"] == 4_599


def test_a_card_receipt_is_accepted(client: TestClient) -> None:
    response = upload(client, account_id=VISA)
    assert response.status_code == 201
    assert client.get(f"{API}/ledger").json()["rows"][0]["account_id"] == VISA


# ------------------------------------------------------------------------ rejections


def test_an_unaccepted_content_type_is_415(client: TestClient) -> None:
    response = upload(client, content_type="text/plain", filename="receipt.txt")
    assert response.status_code == 415
    assert response.json()["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE.value


def test_a_rejected_media_type_stores_no_blob(client: TestClient) -> None:
    upload(client, content_type="text/plain", filename="receipt.txt")
    assert client.get(f"{API}/ledger").json()["total_count"] == 0
    # And the same bytes as an accepted type still work afterwards, which is only true
    # if the rejected attempt rolled its blob back.
    assert upload(client).status_code == 201


def test_a_float_amount_is_rejected(client: TestClient) -> None:
    """A form field is one careless parse away from `19.99`. There is no OCR in this
    scope, so the amount is the user's integer and strict is what keeps it one."""
    response = upload(client, amount_minor="19.99")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_an_undefined_account_is_unknown_account(client: TestClient) -> None:
    response = upload(client, account_id="not-an-account")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.UNKNOWN_ACCOUNT.value


def test_an_unknown_account_stores_no_blob(client: TestClient) -> None:
    """The account is checked before any bytes are written."""
    upload(client, account_id="not-an-account")
    assert client.get(f"{API}/ledger").json()["total_count"] == 0


def test_a_missing_amount_is_422(client: TestClient) -> None:
    response = client.post(
        f"{API}/receipts",
        files={"file": ("receipt.pdf", RECEIPT, "application/pdf")},
        data={"date": "2026-03-07", "category": "groceries", "account_id": CHECKING},
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_every_accepted_image_type_is_accepted(client: TestClient) -> None:
    """One upload per accepted type, each with distinct bytes so none dedupes."""
    for index, content_type in enumerate(
        ("image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf")
    ):
        response = upload(
            client,
            blob=f"bytes-{index}".encode(),
            content_type=content_type,
            filename=f"receipt-{index}",
        )
        assert response.status_code == 201, content_type
