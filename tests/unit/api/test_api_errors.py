"""`to_error_response` and the handler wiring (CONTRACTS.md §7.1, §8.9).

The postcondition on `to_error_response` is *totality*: "every code has a mapping". A
test that checks the codes that exist today would pass forever without noticing the
fourteenth one, so the first test below enumerates `ErrorCode` itself and fails the
moment a member is added without a row.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from api.errors import to_error_response
from core.types import AppError, ErrorCode, ErrorResponse

#: CONTRACTS.md §7.1, transcribed independently of the implementation's own table. If
#: the two disagree, one of them is wrong and the test says which row.
EXPECTED_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.UNKNOWN_ACCOUNT: 422,
    ErrorCode.UNKNOWN_OBLIGATION: 422,
    ErrorCode.UNKNOWN_EVENT: 404,
    ErrorCode.ALREADY_VOIDED: 409,
    ErrorCode.CANNOT_VOID_A_VOID: 422,
    ErrorCode.POLICY_BPS_NOT_10000: 422,
    ErrorCode.OVERLAPPING_VERSIONS: 409,
    ErrorCode.EFFECTIVE_RANGE_INVALID: 422,
    ErrorCode.PAYMENT_SPLIT_MISMATCH: 422,
    ErrorCode.TRANSFER_SAME_ACCOUNT: 422,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.INTERNAL: 500,
}


def test_mapping_is_total_over_error_code() -> None:
    """Every member of the enum, not every member the test happened to list.

    This is the test that fails when someone adds `UNKNOWN_RECEIPT` to `ErrorCode` and
    forgets the HTTP row — which is a 500 in production and a one-line fix here.
    """
    assert set(EXPECTED_STATUS) == set(ErrorCode)
    for code in ErrorCode:
        status, _ = to_error_response(AppError(code))
        assert isinstance(status, int)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda code: code.value)
def test_each_code_maps_to_its_documented_status(code: ErrorCode) -> None:
    status, body = to_error_response(AppError(code))
    assert status == EXPECTED_STATUS[code]
    assert body.code == code


def test_body_carries_message_and_details() -> None:
    exc = AppError(
        ErrorCode.TRANSFER_SAME_ACCOUNT,
        "from_account_id and to_account_id must differ",
        {"account_id": "checking"},
    )
    status, body = to_error_response(exc)
    assert status == 422
    assert body == ErrorResponse(
        code=ErrorCode.TRANSFER_SAME_ACCOUNT,
        message="from_account_id and to_account_id must differ",
        details={"account_id": "checking"},
    )


def test_message_defaults_to_the_code() -> None:
    """`AppError(SOME_CODE)` is the one-argument shape CONTRACTS.md §8.1 spells."""
    _, body = to_error_response(AppError(ErrorCode.UNKNOWN_ACCOUNT))
    assert body.message == "UNKNOWN_ACCOUNT"
    assert body.details == {}


# ------------------------------------------------------------------ over the wire


def test_every_error_body_has_the_same_three_keys(client: TestClient) -> None:
    """One error shape, whichever layer rejected the request.

    A pydantic rejection, a routing miss and a deliberate `AppError` all come back as
    `{code, message, details}` — FastAPI's own `{"detail": ...}` never leaks.
    """
    responses = [
        client.post("/api/v1/events", json={"event": {"event_type": "Nope"}}),
        client.get("/api/v1/periods/not-a-period"),
        client.get("/api/v1/nothing-here"),
    ]
    for response in responses:
        body = response.json()
        assert set(body) == {"code", "message", "details"}
        assert body["code"] in set(ErrorCode)


def test_an_unknown_route_is_not_reported_as_a_missing_event(
    client: TestClient,
) -> None:
    """`UNKNOWN_EVENT` is about a ledger entity, never about a URL."""
    response = client.get("/api/v1/nothing-here")
    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value
