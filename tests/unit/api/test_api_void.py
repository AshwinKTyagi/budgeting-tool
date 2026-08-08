"""`POST /events/{event_id}/void` (CONTRACTS.md §6.1, §7.1, PLAN.md §8.4).

Voiding is the only correction mechanism in the codebase — there is no `DELETE` and no
`UPDATE` against the events table (CLAUDE.md §4.3). So this endpoint appends, and the
three ways it can refuse are each their own code:

    UNKNOWN_EVENT       404   the target does not exist
    ALREADY_VOIDED      409   the target is already voided
    CANNOT_VOID_A_VOID  422   the target is itself an EventVoided

The order they are checked in is asserted below, because a void-of-a-void that is
already voided must report the reason that is true of it unconditionally.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx2 import Response
from starlette.testclient import TestClient

from api import clock
from core.types import ErrorCode
from tests.unit.api.conftest import API, income

UTC = dt.timezone.utc


def append_income(client: TestClient, date: str = "2026-03-02") -> str:
    response = client.post(f"{API}/events", json={"event": income(date, 450_000)})
    assert response.status_code == 201
    event_id: str = response.json()["event_id"]
    return event_id


def void(client: TestClient, event_id: str, **body: object) -> Response:
    return client.post(
        f"{API}/events/{event_id}/void", json={"reason": "entered twice", **body}
    )


# ------------------------------------------------------------------------- 201


def test_voiding_appends_an_event_voided(client: TestClient) -> None:
    target = append_income(client)
    response = void(client, target)
    assert response.status_code == 201
    assert response.json()["deduplicated"] is False

    rows = client.get(f"{API}/ledger").json()["rows"]
    assert len(rows) == 2
    by_type = {row["event_type"]: row for row in rows}
    assert by_type["EventVoided"]["amount_minor"] is None
    assert by_type["IncomeReceived"]["is_voided"] is True
    assert (
        by_type["IncomeReceived"]["voided_by_event_id"]
        == by_type["EventVoided"]["event_id"]
    )


def test_the_ledger_shows_a_voided_row_and_state_does_not_count_it(
    client: TestClient,
) -> None:
    """"Voided events are included with `is_voided: true` — the tabular view shows
    history, it does not hide it" (§6.2). The projection filters them before folding."""
    target = append_income(client)
    before = client.get(f"{API}/state?as_of=2026-03-31").json()
    assert before["periods"][0]["income_minor"] == 450_000

    void(client, target)
    after = client.get(f"{API}/state?as_of=2026-03-31").json()
    assert after["periods"][0]["income_minor"] == 0
    assert client.get(f"{API}/ledger").json()["total_count"] == 2


def test_the_void_carries_its_own_business_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction happened when it happened, not when the target did (PLAN.md §8.4)."""
    target = append_income(client, "2026-03-02")
    monkeypatch.setattr(
        clock, "_now_utc", lambda: dt.datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
    )
    void(client, target)
    voided = [
        row
        for row in client.get(f"{API}/ledger").json()["rows"]
        if row["event_type"] == "EventVoided"
    ]
    assert voided[0]["date"] == "2026-08-08"


def test_a_supplied_void_date_is_used(client: TestClient) -> None:
    target = append_income(client)
    void(client, target, date="2026-03-09")
    voided = [
        row
        for row in client.get(f"{API}/ledger").json()["rows"]
        if row["event_type"] == "EventVoided"
    ]
    assert voided[0]["date"] == "2026-03-09"


# ------------------------------------------------------------------------- 404


def test_voiding_an_unknown_event_is_404(client: TestClient) -> None:
    response = void(client, "00000000-0000-0000-0000-0000000000ff")
    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.UNKNOWN_EVENT.value


def test_an_unknown_target_writes_nothing(client: TestClient) -> None:
    void(client, "00000000-0000-0000-0000-0000000000ff")
    assert client.get(f"{API}/ledger").json()["total_count"] == 0


# ------------------------------------------------------------------------- 409


def test_voiding_twice_is_409(client: TestClient) -> None:
    target = append_income(client)
    assert void(client, target).status_code == 201

    second = void(client, target, reason="again")
    assert second.status_code == 409
    assert second.json()["code"] == ErrorCode.ALREADY_VOIDED.value
    assert second.json()["details"]["event_id"] == target


def test_the_409_names_the_void_that_already_exists(client: TestClient) -> None:
    target = append_income(client)
    first = void(client, target)
    second = void(client, target, reason="again")
    assert (
        second.json()["details"]["voided_by_event_id"] == first.json()["event_id"]
    )


def test_a_rejected_second_void_writes_nothing(client: TestClient) -> None:
    target = append_income(client)
    void(client, target)
    void(client, target, reason="again")
    assert client.get(f"{API}/ledger").json()["total_count"] == 2


# ------------------------------------------------------------------------- 422


def test_voiding_a_void_is_422(client: TestClient) -> None:
    target = append_income(client)
    void_id: str = void(client, target).json()["event_id"]

    response = void(client, void_id, reason="undo the undo")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.CANNOT_VOID_A_VOID.value


def test_cannot_void_a_void_outranks_already_voided(client: TestClient) -> None:
    """An `EventVoided` is never a valid target, whatever else is true of it.

    Reporting 409 here would name a condition that is beside the point: the caller's
    mistake is not "you already did this", it is "corrections are not themselves
    correctable — append a new event instead" (PLAN.md §8.4).
    """
    target = append_income(client)
    void_id: str = void(client, target).json()["event_id"]
    # A second void aimed at the same EventVoided: still CANNOT_VOID_A_VOID.
    void(client, void_id, reason="first attempt")
    response = void(client, void_id, reason="second attempt")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.CANNOT_VOID_A_VOID.value


def test_a_malformed_event_id_is_422(client: TestClient) -> None:
    """A path parameter that is not a UUID is this endpoint's problem, not a routing
    miss — the caller asked for a specific event and deserves to be told why not."""
    response = void(client, "not-a-uuid")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_a_body_without_a_reason_is_422(client: TestClient) -> None:
    target = append_income(client)
    response = client.post(f"{API}/events/{target}/void", json={})
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_an_extra_body_field_is_422(client: TestClient) -> None:
    target = append_income(client)
    response = client.post(
        f"{API}/events/{target}/void", json={"reason": "x", "colour": "blue"}
    )
    assert response.status_code == 422
