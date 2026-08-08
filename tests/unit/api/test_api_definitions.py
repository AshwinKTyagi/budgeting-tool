"""`GET` / `POST /definitions/{kind}` (CONTRACTS.md §6.2, §4).

Definitions are versioned and effective-dated, and nothing is ever edited: a change is a
new version with `effective_to` set on the prior one. Closing a version is the single
permitted `UPDATE` in the codebase (CLAUDE.md §4.3), and it is a separate call because
when the old version stops and when the new one starts are two decisions.

The last test in this file is the one that matters most: **a policy change effective
mid-period leaves the earlier period's numbers bit-identical.** That is CLAUDE.md §5.1
property 4, checked here at the HTTP boundary — because the endpoint that writes the new
version is the one a user reaches for when they want to change the split, and it must not
rewrite history when they do.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from core.types import ErrorCode
from tests.unit.api.conftest import CHECKING, SAVINGS, VISA, income

API = "/api/v1"


def account_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity_id": "brokerage",
        "effective_from": "2026-02-01",
        "effective_to": None,
        "name": "Brokerage",
        "kind": "SAVINGS",
        "apr_bps": 300,
        "statement_close_day": None,
        "payment_due_day": None,
    }
    body.update(overrides)
    return body


def policy_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity_id": "default",
        "effective_from": "2026-04-01",
        "effective_to": None,
        "savings_bps": 7000,
        "discretionary_bps": 3000,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- GET


def test_the_seeded_accounts_are_listed(client: TestClient) -> None:
    body = client.get(f"{API}/definitions/account?as_of=2026-03-15").json()
    assert body["kind"] == "account"
    assert body["as_of_date"] == "2026-03-15"
    assert body["include_history"] is False
    assert {version["entity_id"] for version in body["versions"]} == {
        CHECKING,
        SAVINGS,
        VISA,
    }


def test_subclass_fields_survive_serialization(client: TestClient) -> None:
    """Pydantic serializes to the *declared* type, so a `DefinitionBase`-typed response
    field would silently drop `apr_bps` and every other subclass field."""
    versions = client.get(f"{API}/definitions/account?as_of=2026-03-15").json()[
        "versions"
    ]
    card = [row for row in versions if row["entity_id"] == VISA][0]
    assert card["kind"] == "CREDIT_CARD"
    assert card["apr_bps"] == 2199
    assert card["statement_close_day"] == 15
    assert card["payment_due_day"] == 10
    assert card["budget_timing"] == "AT_PURCHASE"


def test_each_kind_is_routable(client: TestClient) -> None:
    for kind in ("recurring-income", "fixed-cost", "allocation-policy", "account"):
        assert client.get(f"{API}/definitions/{kind}").status_code == 200, kind


def test_an_unknown_kind_is_422(client: TestClient) -> None:
    response = client.get(f"{API}/definitions/nonsense")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_nothing_is_effective_before_the_seed_date(client: TestClient) -> None:
    """An account opened next month genuinely does not exist today. Not an error."""
    body = client.get(f"{API}/definitions/account?as_of=2025-06-01").json()
    assert body["versions"] == []


def test_a_fixed_cost_carries_its_money_as_an_integer(client: TestClient) -> None:
    body = client.get(f"{API}/definitions/fixed-cost").json()
    assert body["versions"][0]["amount_minor"] == 120_000
    assert isinstance(body["versions"][0]["amount_minor"], int)


# -------------------------------------------------------------------------- POST


def test_a_new_version_is_201_with_its_assigned_id(client: TestClient) -> None:
    response = client.post(
        f"{API}/definitions/account", json={"version": account_body()}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "account"
    assert body["entity_id"] == "brokerage"
    assert body["effective_from"] == "2026-02-01"
    assert body["effective_to"] is None
    assert body["closed_previous_version_id"] is None
    assert body["version_id"]


def test_a_new_version_is_immediately_readable(client: TestClient) -> None:
    created = client.post(
        f"{API}/definitions/account", json={"version": account_body()}
    ).json()
    versions = client.get(f"{API}/definitions/account?as_of=2026-03-01").json()[
        "versions"
    ]
    brokerage = [row for row in versions if row["entity_id"] == "brokerage"][0]
    assert brokerage["version_id"] == created["version_id"]
    assert brokerage["apr_bps"] == 300


def test_the_version_id_is_server_assigned(client: TestClient) -> None:
    """A client-chosen identifier would let two clients collide."""
    supplied = "11111111-1111-1111-1111-111111111111"
    created = client.post(
        f"{API}/definitions/account",
        json={"version": account_body(version_id=supplied)},
    ).json()
    assert created["version_id"] != supplied


def test_a_new_account_can_then_receive_events(client: TestClient) -> None:
    """The end-to-end reason this endpoint exists: `UNKNOWN_ACCOUNT` is checked at write
    time, so an account has to be definable before it can be spent from."""
    before = client.post(
        f"{API}/events",
        json={"event": income("2026-03-02", 1, account_id="brokerage")},
    )
    assert before.status_code == 422
    assert before.json()["code"] == ErrorCode.UNKNOWN_ACCOUNT.value

    client.post(f"{API}/definitions/account", json={"version": account_body()})
    after = client.post(
        f"{API}/events",
        json={"event": income("2026-03-02", 1, account_id="brokerage")},
    )
    assert after.status_code == 201


def test_an_overlapping_version_is_409(client: TestClient) -> None:
    """`add_version` deliberately does not auto-close the previous open version."""
    response = client.post(
        f"{API}/definitions/allocation-policy", json={"version": policy_body()}
    )
    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.OVERLAPPING_VERSIONS.value


def test_close_previous_then_add_supersedes_cleanly(client: TestClient) -> None:
    response = client.post(
        f"{API}/definitions/allocation-policy",
        json={"version": policy_body(), "close_previous_at": "2026-04-01"},
    )
    assert response.status_code == 201
    assert response.json()["closed_previous_version_id"] == (
        "00000000-0000-0000-0000-000000000068"
    )

    history = client.get(
        f"{API}/definitions/allocation-policy?include_history=true"
    ).json()["versions"]
    assert len(history) == 2
    assert [row["effective_from"] for row in history] == ["2026-01-01", "2026-04-01"]
    assert [row["effective_to"] for row in history] == ["2026-04-01", None]


def test_include_history_false_resolves_one_version_per_entity(
    client: TestClient,
) -> None:
    client.post(
        f"{API}/definitions/allocation-policy",
        json={"version": policy_body(), "close_previous_at": "2026-04-01"},
    )
    march = client.get(
        f"{API}/definitions/allocation-policy?as_of=2026-03-15"
    ).json()["versions"]
    april = client.get(
        f"{API}/definitions/allocation-policy?as_of=2026-04-15"
    ).json()["versions"]
    assert [row["savings_bps"] for row in march] == [5000]
    assert [row["savings_bps"] for row in april] == [7000]


def test_closing_an_entity_with_no_open_version_is_422(client: TestClient) -> None:
    """Proceeding would create the very overlap `close_previous_at` exists to avoid."""
    response = client.post(
        f"{API}/definitions/account",
        json={"version": account_body(), "close_previous_at": "2026-02-01"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_a_failed_insert_undoes_the_close(client: TestClient) -> None:
    """A close that succeeded before an insert that failed is rolled back with it."""
    response = client.post(
        f"{API}/definitions/allocation-policy",
        json={
            # effective_from before the close date: the close succeeds, then the insert
            # overlaps the now-closed version and is rejected.
            "version": policy_body(effective_from="2026-02-01"),
            "close_previous_at": "2026-04-01",
        },
    )
    assert response.status_code == 409
    history = client.get(
        f"{API}/definitions/allocation-policy?include_history=true"
    ).json()["versions"]
    assert len(history) == 1
    assert history[0]["effective_to"] is None


# ------------------------------------------------------- the model's own invariants


def test_a_policy_that_does_not_sum_to_10000_is_its_own_code(
    client: TestClient,
) -> None:
    response = client.post(
        f"{API}/definitions/allocation-policy",
        json={
            "version": policy_body(savings_bps=6000, discretionary_bps=3000),
            "close_previous_at": "2026-04-01",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.POLICY_BPS_NOT_10000.value


def test_an_inverted_effective_range_is_its_own_code(client: TestClient) -> None:
    response = client.post(
        f"{API}/definitions/account",
        json={
            "version": account_body(
                effective_from="2026-04-01", effective_to="2026-02-01"
            )
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.EFFECTIVE_RANGE_INVALID.value


def test_an_empty_effective_range_is_rejected(client: TestClient) -> None:
    """`effective_to` is exclusive, so equal bounds describe no days at all."""
    response = client.post(
        f"{API}/definitions/account",
        json={
            "version": account_body(
                effective_from="2026-02-01", effective_to="2026-02-01"
            )
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.EFFECTIVE_RANGE_INVALID.value


def test_a_float_amount_in_a_definition_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{API}/definitions/fixed-cost",
        json={
            "version": {
                "entity_id": "internet",
                "effective_from": "2026-02-01",
                "effective_to": None,
                "name": "Internet",
                "amount_minor": 59.99,
                "cadence": "MONTHLY",
                "due_day": 12,
                "payee": "ISP",
                "category": "utilities",
            }
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_a_wrong_kind_body_is_rejected(client: TestClient) -> None:
    """An account body posted to the fixed-cost route is not a fixed cost."""
    response = client.post(
        f"{API}/definitions/fixed-cost", json={"version": account_body()}
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_an_empty_post_body_is_422(client: TestClient) -> None:
    response = client.post(
        f"{API}/definitions/account",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


# --------------------------------------------- property 4, at the HTTP boundary


def test_a_policy_change_leaves_the_earlier_period_bit_identical(
    client: TestClient,
) -> None:
    """CLAUDE.md §5.1 property 4. The policy is resolved at PERIOD START, so a version
    effective 1 April cannot reach March — and March's row must come back byte-for-byte
    unchanged, `policy_version_id` included (PLAN.md §8.3)."""
    client.post(
        f"{API}/events/batch",
        json={
            "events": [
                {"event": income("2026-03-02", 450_001)},
                {"event": income("2026-04-02", 450_001)},
            ]
        },
    )
    before = client.get(f"{API}/state?as_of=2026-04-30").json()
    march_before = [p for p in before["periods"] if p["period_id"] == "2026-03"][0]

    created = client.post(
        f"{API}/definitions/allocation-policy",
        json={"version": policy_body(), "close_previous_at": "2026-04-01"},
    )
    assert created.status_code == 201

    after = client.get(f"{API}/state?as_of=2026-04-30").json()
    march_after = [p for p in after["periods"] if p["period_id"] == "2026-03"][0]
    april_after = [p for p in after["periods"] if p["period_id"] == "2026-04"][0]

    assert march_after == march_before
    assert april_after["savings_bps"] == 7000
    assert april_after["policy_version_id"] == created.json()["version_id"]


def test_a_mid_period_policy_applies_from_the_next_period(client: TestClient) -> None:
    """"A policy effective mid-period applies from the next period" (CONTRACTS.md §4)."""
    client.post(f"{API}/events", json={"event": income("2026-03-02", 450_001)})
    client.post(f"{API}/events", json={"event": income("2026-04-02", 450_001)})
    client.post(
        f"{API}/definitions/allocation-policy",
        json={
            "version": policy_body(effective_from="2026-03-15"),
            "close_previous_at": "2026-03-15",
        },
    )
    state = client.get(f"{API}/state?as_of=2026-04-30").json()
    by_period = {p["period_id"]: p for p in state["periods"]}
    assert by_period["2026-03"]["savings_bps"] == 5000
    assert by_period["2026-04"]["savings_bps"] == 7000
