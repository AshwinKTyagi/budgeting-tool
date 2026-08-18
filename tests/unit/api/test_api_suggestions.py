"""The confirmation inbox (PLAN.md §8.5).

Owned by `module/api` (PLAN.md §13.2).

The property under nearly every test here is the suppression rule: an occurrence is
offered iff no event carries its `dedupe_key`, so confirming, editing, and rejecting
each retire it exactly once and permanently.

No tolerance anywhere; every assertion is `==` (CLAUDE.md §4.6).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine
from starlette.testclient import TestClient

from core.types import Cadence
from domain.definitions import RecurringIncome
from persistence.engine import create_session_factory
from persistence.repositories import DefinitionRepository

from tests.unit.api.conftest import API, CHECKING, GENESIS, SEEDED_AT, uid

#: Far enough past the seeded GENESIS that the monthly rent has come due three times.
AS_OF = "2026-03-31"


def _add_salary(
    engine: Engine,
    *,
    cadence: Cadence = Cadence.MONTHLY,
    anchor_day: int = 1,
    amount_minor: int = 240_000,
    effective_from: dt.date = GENESIS,
    effective_to: dt.date | None = None,
    entity_id: str = "salary",
    version: int = 900,
) -> None:
    """Append a `RecurringIncome` version straight to the definition tables."""
    factory = create_session_factory(engine)
    with factory() as session:
        DefinitionRepository(session).add_version(
            RecurringIncome(
                version_id=uid(version),
                entity_id=entity_id,
                effective_from=effective_from,
                effective_to=effective_to,
                recorded_at=SEEDED_AT,
                name="Salary",
                amount_minor=amount_minor,
                cadence=cadence,
                anchor_day=anchor_day,
                account_id=CHECKING,
            )
        )
        session.commit()


def _anchor(client: TestClient, date: str = "2026-01-01") -> None:
    """Pull genesis back to `date` by opening checking with a zero balance.

    Genesis follows the ledger, not the forecast (`domain/projection.py::_genesis`), so
    on an empty ledger only the current period exists — and an expected obligation
    cannot be offered for a period the projection never built.
    """
    response = client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "AccountOpeningBalance",
                "date": date,
                "account_id": CHECKING,
                "amount_minor": 0,
            },
            "client_nonce": None,
        },
    )
    assert response.status_code == 201


def _suggestions(client: TestClient, as_of: str = AS_OF) -> list[dict[str, object]]:
    response = client.get(f"{API}/suggestions", params={"as_of": as_of})
    assert response.status_code == 200
    body = response.json()
    assert body["as_of_date"] == as_of
    result: list[dict[str, object]] = body["suggestions"]
    return result


def _by_kind(rows: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [row for row in rows if row["kind"] == kind]


def _income_minor(client: TestClient, period_id: str, as_of: str = AS_OF) -> int:
    response = client.get(f"{API}/periods/{period_id}", params={"as_of": as_of})
    assert response.status_code == 200
    value: int = response.json()["period"]["allocatable_income_minor"]
    return value


def _fixed_due_minor(client: TestClient, period_id: str, as_of: str = AS_OF) -> int:
    response = client.get(f"{API}/periods/{period_id}", params={"as_of": as_of})
    assert response.status_code == 200
    value: int = response.json()["period"]["fixed_due_minor"]
    return value


# ------------------------------------------------------------------------- listing


def test_a_seeded_fixed_cost_is_offered_once_per_due_month(client: TestClient) -> None:
    """The rent FixedCost is monthly from GENESIS, so by 2026-03-31 three have come
    due — and every one of them is a forecast the user has not yet answered for."""
    _anchor(client)

    bills = _by_kind(_suggestions(client), "bill")

    assert [row["date"] for row in bills] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert [row["suggestion_id"] for row in bills] == [
        "expected:bill:rent:2026-01",
        "expected:bill:rent:2026-02",
        "expected:bill:rent:2026-03",
    ]
    assert {row["event_type"] for row in bills} == {"ObligationRaised"}
    assert {row["amount_minor"] for row in bills} == {120_000}


def test_nothing_dated_after_as_of_is_ever_offered(
    client: TestClient, engine: Engine
) -> None:
    """"an unreceived paycheck cannot be spent" (PLAN.md §8.2) — so it is not offered
    either. Confirming a future occurrence is the one way this inbox could invent money
    that does not exist."""
    _add_salary(engine)

    rows = _suggestions(client, as_of="2026-02-15")

    assert [row["date"] for row in _by_kind(rows, "income")] == [
        "2026-01-01",
        "2026-02-01",
    ]
    assert all(str(row["date"]) <= "2026-02-15" for row in rows)


def test_an_empty_ledger_still_offers_a_backdated_paycheck(
    bare_client: TestClient, engine: Engine
) -> None:
    """Genesis follows the ledger, not the forecast — so on an empty ledger it is
    today. The inbox must not inherit that, or setting up a salary and looking at it a
    month later would show nothing at all."""
    _add_salary(engine, effective_from=dt.date(2026, 1, 1))

    rows = _by_kind(_suggestions(bare_client), "income")

    assert [row["date"] for row in rows] == ["2026-01-01", "2026-02-01", "2026-03-01"]


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        (Cadence.WEEKLY, 13),
        (Cadence.BIWEEKLY, 7),
        (Cadence.SEMIMONTHLY, 6),
        (Cadence.MONTHLY, 3),
        (Cadence.QUARTERLY, 1),
        (Cadence.ANNUAL, 1),
    ],
)
def test_every_cadence_is_offered_at_its_own_rate(
    bare_client: TestClient, engine: Engine, cadence: Cadence, expected: int
) -> None:
    """Weekly, biweekly and semimonthly all land more than once in a month — the case
    a period-shaped expansion cannot express, and the reason income expands by date."""
    _add_salary(engine, cadence=cadence)

    assert len(_by_kind(_suggestions(bare_client), "income")) == expected


def test_effective_to_ends_the_series(bare_client: TestClient, engine: Engine) -> None:
    """Exclusive end: a job closed on 2026-03-01 pays through February and not on the
    first of March."""
    _add_salary(engine, effective_to=dt.date(2026, 3, 1))

    rows = _by_kind(_suggestions(bare_client), "income")

    assert [row["date"] for row in rows] == ["2026-01-01", "2026-02-01"]


def test_a_later_version_does_not_rewrite_earlier_occurrences(
    bare_client: TestClient, engine: Engine
) -> None:
    """A raise is a new version, not an edit. January's paycheck keeps January's
    amount — which is the whole reason cadence changes are versioned rather than
    applied in place."""
    _add_salary(engine, effective_to=dt.date(2026, 2, 1), amount_minor=240_000)
    _add_salary(
        engine,
        effective_from=dt.date(2026, 2, 1),
        amount_minor=300_000,
        version=901,
    )

    rows = _by_kind(_suggestions(bare_client), "income")

    assert [(row["date"], row["amount_minor"]) for row in rows] == [
        ("2026-01-01", 240_000),
        ("2026-02-01", 300_000),
        ("2026-03-01", 300_000),
    ]


# ------------------------------------------------------------------------- confirm


def test_confirming_income_appends_it_and_retires_the_suggestion(client: TestClient, engine: Engine) -> None:
    _add_salary(engine)
    _anchor(client)
    before = _income_minor(client, "2026-01")

    response = client.post(
        f"{API}/suggestions/expected:income:salary:2026-01-01/confirm",
        params={"as_of": AS_OF},
    )

    assert response.status_code == 201
    assert response.json()["dedupe_key"] == "expected:income:salary:2026-01-01"
    assert response.json()["deduplicated"] is False
    assert _income_minor(client, "2026-01") == before + 240_000
    assert "expected:income:salary:2026-01-01" not in {
        row["suggestion_id"] for row in _suggestions(client)
    }


def test_a_confirmed_row_reports_its_origin_as_expected(client: TestClient, engine: Engine) -> None:
    """`origin` is read off the dedupe-key prefix, so a confirmed occurrence is
    distinguishable from something typed in by hand without storing a flag."""
    _add_salary(engine)
    _anchor(client)
    client.post(
        f"{API}/suggestions/expected:income:salary:2026-01-01/confirm",
        params={"as_of": AS_OF},
    )

    rows = client.get(f"{API}/ledger").json()["rows"]
    confirmed = [row for row in rows if row["event_type"] == "IncomeReceived"]

    assert [row["origin"] for row in confirmed] == ["expected"]
    assert [row["origin"] for row in rows if row["event_type"] != "IncomeReceived"] == [
        "manual"
    ]


def test_editing_on_confirm_keeps_the_occurrence_key(client: TestClient, engine: Engine) -> None:
    """A corrected paycheck is the same paycheck. If an edit moved the key, the
    occurrence would be offered again and the user would enter it twice."""
    _add_salary(engine)
    _anchor(client)

    response = client.post(
        f"{API}/suggestions/expected:income:salary:2026-02-01/confirm",
        params={"as_of": AS_OF},
        json={"amount_minor": 111_111, "date": "2026-02-03", "counterparty": "NewCo"},
    )

    assert response.status_code == 201
    assert response.json()["dedupe_key"] == "expected:income:salary:2026-02-01"
    assert _income_minor(client, "2026-02") == 111_111
    assert "expected:income:salary:2026-02-01" not in {
        row["suggestion_id"] for row in _suggestions(client)
    }
    row = client.get(f"{API}/ledger").json()["rows"][0]
    assert row["date"] == "2026-02-03"
    assert row["counterparty"] == "NewCo"


def test_confirming_a_bill_supersedes_rather_than_sums(client: TestClient) -> None:
    """The recognition-principle failure in its obligation-shaped form: the bill would
    reserve money twice (PLAN.md §8.1). `fixed_due_minor` must not move."""
    _anchor(client)
    before = _fixed_due_minor(client, "2026-02")

    response = client.post(
        f"{API}/suggestions/expected:bill:rent:2026-02/confirm",
        params={"as_of": AS_OF},
    )

    assert response.status_code == 201
    assert _fixed_due_minor(client, "2026-02") == before == 120_000
    rows = [
        row
        for row in client.get(f"{API}/periods/2026-02", params={"as_of": AS_OF}).json()[
            "obligations"
        ]
    ]
    assert [row["source"] for row in rows] == ["RAISED"]
    assert [row["obligation_id"] for row in rows] == ["expected:rent:2026-02"]


def test_confirming_twice_is_idempotent(client: TestClient, engine: Engine) -> None:
    """The second attempt cannot resolve the suggestion at all — it is suppressed — so
    it is a 404 rather than a second row."""
    _add_salary(engine)
    _anchor(client)
    path = f"{API}/suggestions/expected:income:salary:2026-01-01/confirm"

    assert client.post(path, params={"as_of": AS_OF}).status_code == 201
    second = client.post(path, params={"as_of": AS_OF})

    assert second.status_code == 404
    assert second.json()["code"] == "UNKNOWN_EVENT"
    assert _income_minor(client, "2026-01") == 240_000


# -------------------------------------------------------------------------- reject


def test_rejecting_appends_then_voids_and_retires_the_suggestion(client: TestClient, engine: Engine) -> None:
    _add_salary(engine)
    _anchor(client)

    response = client.post(
        f"{API}/suggestions/expected:income:salary:2026-01-01/reject",
        params={"as_of": AS_OF},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["dedupe_key"] == "expected:income:salary:2026-01-01"
    assert body["event_id"] != body["voided_by_event_id"]
    assert _income_minor(client, "2026-01") == 0
    assert "expected:income:salary:2026-01-01" not in {
        row["suggestion_id"] for row in _suggestions(client)
    }


def test_a_rejected_row_stays_visible_with_a_note_saying_why(client: TestClient, engine: Engine) -> None:
    """A rejection is history, not a deletion. The note is what tells a later reader
    the paycheck never arrived, rather than that a figure was corrected."""
    _add_salary(engine)
    _anchor(client)
    client.post(
        f"{API}/suggestions/expected:income:salary:2026-01-01/reject",
        params={"as_of": AS_OF},
    )

    rows = client.get(f"{API}/ledger").json()["rows"]
    income_rows = [row for row in rows if row["event_type"] == "IncomeReceived"]

    assert len(income_rows) == 1
    assert income_rows[0]["is_voided"] is True
    assert "Rejected by user" in str(income_rows[0]["note"])


def test_rejecting_a_bill_leaves_it_reserving_money(client: TestClient) -> None:
    """Dismiss-only, deliberately. Rejecting a bill must not be a one-click way to
    inflate discretionary — the error direction PLAN.md §8.1 exists to prevent."""
    _anchor(client)
    before = _fixed_due_minor(client, "2026-02")

    response = client.post(
        f"{API}/suggestions/expected:bill:rent:2026-02/reject",
        params={"as_of": AS_OF},
    )

    assert response.status_code == 201
    assert _fixed_due_minor(client, "2026-02") == before == 120_000
    assert "expected:bill:rent:2026-02" not in {
        row["suggestion_id"] for row in _suggestions(client)
    }


def test_rejecting_accepts_a_reason(client: TestClient, engine: Engine) -> None:
    _add_salary(engine)
    _anchor(client)

    response = client.post(
        f"{API}/suggestions/expected:income:salary:2026-01-01/reject",
        params={"as_of": AS_OF},
        json={"reason": "left that job"},
    )

    assert response.status_code == 201
    voids = [
        row
        for row in client.get(f"{API}/ledger").json()["rows"]
        if row["event_type"] == "EventVoided"
    ]
    assert len(voids) == 1
    detail = client.get(f"{API}/events/{voids[0]['event_id']}").json()
    assert detail["reason"] == "left that job"


# --------------------------------------------------------------------------- errors


def test_an_unknown_suggestion_id_is_a_404(client: TestClient) -> None:
    response = client.post(
        f"{API}/suggestions/expected:income:nobody:2026-01-01/confirm",
        params={"as_of": AS_OF},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "UNKNOWN_EVENT"


def test_a_malformed_confirm_body_is_a_422(client: TestClient) -> None:
    """Strict mode: a float where a `Minor` is declared is rejected, not rounded."""
    response = client.post(
        f"{API}/suggestions/expected:bill:rent:2026-01/confirm",
        params={"as_of": AS_OF},
        json={"amount_minor": 1.5},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_reading_suggestions_writes_nothing(client: TestClient) -> None:
    """Opening the app must not append. The inbox is a read; only the buttons write."""
    before = client.get(f"{API}/ledger").json()["total_count"]

    _suggestions(client)
    _suggestions(client)

    assert client.get(f"{API}/ledger").json()["total_count"] == before == 0
