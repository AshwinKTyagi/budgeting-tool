"""Read endpoints (CONTRACTS.md §6.2, §6.3).

Three things are being asserted here and they are worth naming separately:

* **`as_of` handling** — optional everywhere, today in `BUDGET_TZ` when omitted, verbatim
  when supplied *including a future date* (§6.3). The clock is pinned by monkeypatching
  `api.clock._now_utc`, which is the single call site the whole codebase reads a clock
  through.
* **The round trip** — append events, then read them back through `/state`, `/ledger` and
  `/charts/series` and check the three agree. They are three views of one fold, so a
  disagreement between them is a bug in this layer by construction.
* **No float, no formatted string, anywhere.** Every money value in every response body
  is a JSON integer. That is asserted structurally, over the whole body, rather than
  field by field.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from starlette.testclient import TestClient

from api import clock
from core.types import ErrorCode
from tests.unit.api.conftest import API, CHECKING, SAVINGS, VISA, expense, income

UTC = dt.timezone.utc

#: A ledger with two periods, an income that does not divide evenly, a card expense, a
#: rent payment against the *expected* obligation, and a budget-neutral transfer.
LEDGER: tuple[dict[str, object], ...] = (
    income("2026-03-02", 450_000),
    income("2026-04-02", 450_001),
    expense("2026-03-07", 4_599, account_id=VISA, merchant="Corner Store"),
    {
        "event_type": "PaymentMade",
        "date": "2026-03-01",
        "amount_minor": 120_000,
        "obligation_id": "expected:rent:2026-03",
        "account_id": CHECKING,
    },
    {
        "event_type": "TransferMade",
        "date": "2026-03-20",
        "amount_minor": 50_000,
        "from_account_id": CHECKING,
        "to_account_id": VISA,
    },
)


@pytest.fixture
def loaded(client: TestClient) -> TestClient:
    """The seeded application with `LEDGER` appended."""
    response = client.post(
        f"{API}/events/batch", json={"events": [{"event": e} for e in LEDGER]}
    )
    assert response.status_code == 200, response.text
    return client


def money_values(node: Any) -> list[Any]:
    """Every value under a `_minor` key, at any depth."""
    found: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.endswith("_minor") and value is not None:
                found.append(value)
            found.extend(money_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(money_values(item))
    return found


# ------------------------------------------------------------------ §6.3 as_of


def test_omitted_as_of_defaults_to_today_in_budget_tz(
    loaded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06:00 UTC on 8 August is still 7 August in Los Angeles. The zone decides."""
    monkeypatch.setattr(
        clock, "_now_utc", lambda: dt.datetime(2026, 8, 8, 6, 0, tzinfo=UTC)
    )
    assert loaded.get(f"{API}/state").json()["as_of_date"] == "2026-08-07"

    monkeypatch.setenv("BUDGET_TZ", "UTC")
    assert loaded.get(f"{API}/state").json()["as_of_date"] == "2026-08-08"


def test_supplied_as_of_is_used_verbatim(loaded: TestClient) -> None:
    assert (
        loaded.get(f"{API}/state?as_of=2026-03-15").json()["as_of_date"]
        == "2026-03-15"
    )


def test_a_future_as_of_is_a_valid_forecast_query(loaded: TestClient) -> None:
    """"including future dates, which are valid and produce a forecast-shaped `State`"."""
    state = loaded.get(f"{API}/state?as_of=2026-06-30").json()
    assert state["as_of_date"] == "2026-06-30"
    assert [period["period_id"] for period in state["periods"]] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
    ]
    # The rent FixedCost keeps being expanded into the future periods, unpaid.
    future = [
        row for row in state["obligations"] if row["period_id"] == "2026-06"
    ]
    assert [row["status"] for row in future] == ["UNPAID"]


def test_a_past_as_of_answers_a_historical_question(loaded: TestClient) -> None:
    """Time travel is free precisely because the fold reads no clock (PLAN.md §4.2)."""
    state = loaded.get(f"{API}/state?as_of=2026-03-31").json()
    assert [period["period_id"] for period in state["periods"]] == ["2026-03"]
    assert state["periods"][0]["income_minor"] == 450_000


def test_as_of_is_optional_on_every_read_endpoint(loaded: TestClient) -> None:
    for path in ("/state", "/periods", "/accounts", "/definitions/account"):
        assert loaded.get(f"{API}{path}").status_code == 200, path
        assert loaded.get(f"{API}{path}?as_of=2026-03-15").status_code == 200, path


def test_a_malformed_as_of_is_422(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/state?as_of=not-a-date")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


# ------------------------------------------------------------------------ /state


def test_state_carries_the_allocation_invariant(loaded: TestClient) -> None:
    """`fixed_due + savings_allocated + discretionary_allocated == allocatable_income`,
    exactly, for every period. Asserted here as well as in the projection's own suite
    because serialization is where an exact integer could stop being one."""
    for period in loaded.get(f"{API}/state?as_of=2026-04-30").json()["periods"]:
        assert (
            period["fixed_due_minor"]
            + period["savings_allocated_minor"]
            + period["discretionary_allocated_minor"]
            == period["allocatable_income_minor"]
        )


def test_the_odd_cent_lands_on_savings(loaded: TestClient) -> None:
    """April's income is 450_001 against a 50/50 policy, with 120_000 fixed off the top.

    The remainder 330_001 splits 165_001 / 165_000 — savings takes the leftover unit
    because it is declared first (PLAN.md §5.1). This is the worked example's tie-break,
    reaching the wire intact.
    """
    april = [
        period
        for period in loaded.get(f"{API}/state?as_of=2026-04-30").json()["periods"]
        if period["period_id"] == "2026-04"
    ][0]
    assert april["savings_allocated_minor"] == 165_001
    assert april["discretionary_allocated_minor"] == 165_000


def test_every_money_value_is_an_integer(loaded: TestClient) -> None:
    """"All money in responses is `_minor` integers — the API never emits a formatted
    currency string or a float" (CONTRACTS.md §6)."""
    for path in (
        "/state?as_of=2026-04-30",
        "/periods?as_of=2026-04-30",
        "/periods/2026-03?as_of=2026-04-30",
        "/accounts?as_of=2026-04-30",
        "/ledger",
        "/charts/series?metric=income&as_of=2026-04-30",
        "/definitions/fixed-cost",
    ):
        values = money_values(loaded.get(f"{API}{path}").json())
        assert values, path  # the assertion is worthless if it found nothing
        for value in values:
            assert isinstance(value, int), (path, value)
            assert not isinstance(value, bool), (path, value)


def test_warnings_are_data_and_never_an_error(client: TestClient) -> None:
    """A savings draw against an empty balance is a warning, not a 4xx (CLAUDE.md §6)."""
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

    state = client.get(f"{API}/state?as_of=2026-03-31")
    assert state.status_code == 200
    codes = {warning["code"] for warning in state.json()["warnings"]}
    assert "SAVINGS_DRAW_EXCEEDS_BALANCE" in codes


def test_state_of_an_empty_ledger_is_not_an_error(bare_client: TestClient) -> None:
    response = bare_client.get(f"{API}/state?as_of=2026-03-15")
    assert response.status_code == 200
    assert response.json()["current_period_id"] == "2026-03"


# ---------------------------------------------------------------------- /periods


def test_periods_are_filtered_by_the_window(loaded: TestClient) -> None:
    body = loaded.get(
        f"{API}/periods?as_of=2026-04-30&from=2026-03-05&to=2026-03-06"
    ).json()
    assert [period["period_id"] for period in body["periods"]] == ["2026-03"]
    assert body["as_of_date"] == "2026-04-30"


def test_periods_unfiltered_span_genesis_to_as_of(loaded: TestClient) -> None:
    body = loaded.get(f"{API}/periods?as_of=2026-04-30").json()
    assert [period["period_id"] for period in body["periods"]] == ["2026-03", "2026-04"]


def test_period_detail_scopes_obligations_to_the_period(loaded: TestClient) -> None:
    body = loaded.get(f"{API}/periods/2026-03?as_of=2026-04-30").json()
    assert body["period"]["period_id"] == "2026-03"
    assert [row["obligation_id"] for row in body["obligations"]] == [
        "expected:rent:2026-03"
    ]
    assert body["obligations"][0]["status"] == "PAID"


def test_a_period_outside_the_range_is_422_naming_what_exists(
    loaded: TestClient,
) -> None:
    """The §7.1 taxonomy's one not-found code is `UNKNOWN_EVENT`, which is about a stored
    ledger entity. A period is derived, not stored."""
    response = loaded.get(f"{API}/periods/2030-01?as_of=2026-04-30")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value
    assert response.json()["details"]["available"] == ["2026-03", "2026-04"]


def test_a_malformed_period_id_is_422(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/periods/not-a-period?as_of=2026-04-30")
    assert response.status_code == 422


# --------------------------------------------------------------------- /accounts


def test_accounts_report_signed_balances(loaded: TestClient) -> None:
    """`balance_minor` is signed; `outstanding_minor` is set for liabilities only."""
    body = loaded.get(f"{API}/accounts?as_of=2026-04-30").json()
    by_id = {row["account_id"]: row for row in body["accounts"]}
    assert by_id[CHECKING]["outstanding_minor"] is None
    assert by_id[SAVINGS]["outstanding_minor"] is None
    assert by_id[VISA]["outstanding_minor"] == abs(by_id[VISA]["balance_minor"])
    assert by_id[VISA]["kind"] == "CREDIT_CARD"


def test_accounts_move_with_as_of(loaded: TestClient) -> None:
    """The implied savings transfer lands at period close (PLAN.md §6.2), so savings is
    still empty on the last day of the period that allocated it."""
    march = loaded.get(f"{API}/accounts?as_of=2026-03-31").json()
    april = loaded.get(f"{API}/accounts?as_of=2026-04-30").json()
    savings_march = [
        row for row in march["accounts"] if row["account_id"] == SAVINGS
    ][0]
    savings_april = [
        row for row in april["accounts"] if row["account_id"] == SAVINGS
    ][0]
    assert savings_march["balance_minor"] == 0
    assert savings_april["balance_minor"] == 165_000


# ----------------------------------------------------------------------- /ledger


def test_ledger_is_newest_first(loaded: TestClient) -> None:
    rows = loaded.get(f"{API}/ledger").json()["rows"]
    dates = [row["date"] for row in rows]
    assert dates == sorted(dates, reverse=True)
    assert rows[0]["date"] == "2026-04-02"


def test_ledger_counts_the_filtered_set_not_the_page(loaded: TestClient) -> None:
    body = loaded.get(f"{API}/ledger?limit=2").json()
    assert len(body["rows"]) == 2
    assert body["total_count"] == len(LEDGER)
    assert body["next_cursor"] is not None


def test_the_cursor_walks_every_row_exactly_once(loaded: TestClient) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(len(LEDGER) + 2):  # a bounded loop: a bug must not hang the suite
        query = f"{API}/ledger?limit=2" + (
            "" if cursor is None else f"&cursor={cursor}"
        )
        body = loaded.get(query).json()
        seen.extend(row["event_id"] for row in body["rows"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(seen) == len(LEDGER)
    assert len(set(seen)) == len(LEDGER)


def test_the_last_page_has_no_cursor(loaded: TestClient) -> None:
    body = loaded.get(f"{API}/ledger?limit=100").json()
    assert body["next_cursor"] is None


def test_a_forged_cursor_is_422(loaded: TestClient) -> None:
    """Silently restarting would make a paginating client loop forever."""
    response = loaded.get(f"{API}/ledger?cursor=not-a-cursor")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_ledger_filters(loaded: TestClient) -> None:
    assert loaded.get(f"{API}/ledger?types=IncomeReceived").json()["total_count"] == 2
    assert loaded.get(f"{API}/ledger?category=groceries").json()["total_count"] == 1
    assert (
        loaded.get(f"{API}/ledger?from=2026-04-01").json()["total_count"] == 1
    )
    assert loaded.get(f"{API}/ledger?to=2026-03-31").json()["total_count"] == 4


def test_a_transfer_appears_under_both_accounts(loaded: TestClient) -> None:
    """`account_id` matches either end, so a transfer shows up on both sides."""
    from_side = loaded.get(f"{API}/ledger?account_id={CHECKING}&types=TransferMade")
    to_side = loaded.get(f"{API}/ledger?account_id={VISA}&types=TransferMade")
    assert from_side.json()["total_count"] == 1
    assert to_side.json()["total_count"] == 1


def test_ledger_row_shape(loaded: TestClient) -> None:
    row = [
        r
        for r in loaded.get(f"{API}/ledger").json()["rows"]
        if r["event_type"] == "ExpenseRecorded"
    ][0]
    assert row["amount_minor"] == 4_599
    assert row["account_id"] == VISA
    assert row["counterparty"] == "Corner Store"
    assert row["category"] == "groceries"
    assert row["period_id"] == "2026-03"
    assert row["is_voided"] is False
    assert row["voided_by_event_id"] is None
    assert row["origin"] == "manual"


def test_get_event_returns_the_canonical_payload(loaded: TestClient) -> None:
    row = [
        r
        for r in loaded.get(f"{API}/ledger").json()["rows"]
        if r["event_type"] == "ExpenseRecorded"
    ][0]
    body = loaded.get(f"{API}/events/{row['event_id']}").json()
    assert body["event_type"] == "ExpenseRecorded"
    assert body["amount_minor"] == 4_599
    assert body["merchant"] == "Corner Store"
    assert body["event_id"] == row["event_id"]


def test_get_event_unknown_is_404(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/events/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.UNKNOWN_EVENT.value


def test_get_event_malformed_id_is_422(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/events/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_an_obligation_row_takes_its_period_from_due_date(client: TestClient) -> None:
    """"`due_date` decides period membership, NOT `date`" (CONTRACTS.md §3.2)."""
    client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "ObligationRaised",
                "date": "2026-03-28",
                "obligation_id": "plumber",
                "due_date": "2026-04-05",
                "amount_minor": 4_500,
                "payee": "Plumber",
                "category": "maintenance",
            }
        },
    )
    row = client.get(f"{API}/ledger").json()["rows"][0]
    assert row["date"] == "2026-03-28"
    assert row["period_id"] == "2026-04"
    assert row["counterparty"] == "Plumber"
    assert row["account_id"] is None


def test_ledger_of_an_empty_database(bare_client: TestClient) -> None:
    body = bare_client.get(f"{API}/ledger").json()
    assert body == {"rows": [], "next_cursor": None, "total_count": 0}


# ---------------------------------------------------------------- /charts/series


def test_chart_series_agrees_with_state(loaded: TestClient) -> None:
    """Three views of one fold, so they cannot disagree without this layer being wrong."""
    state = loaded.get(f"{API}/state?as_of=2026-04-30").json()
    expected = {
        period["period_id"]: period["allocatable_income_minor"]
        for period in state["periods"]
    }
    series = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30&metric=allocatable_income"
    ).json()
    assert series["metric"] == "allocatable_income"
    assert series["grain"] == "period"
    assert series["group_by"] == "none"
    assert {
        point["bucket"]: point["value_minor"] for point in series["points"]
    } == expected


def test_month_grain_matches_period_grain_under_calendar_months(
    loaded: TestClient,
) -> None:
    """`PeriodId` *is* `"YYYY-MM"` under `CalendarMonthResolver` (CONTRACTS.md §2)."""
    base = f"{API}/charts/series?as_of=2026-04-30&metric=income"
    by_period = loaded.get(f"{base}&grain=period").json()["points"]
    by_month = loaded.get(f"{base}&grain=month").json()["points"]
    assert by_period == by_month


def test_grouping_fixed_due_by_payee_sums_to_the_period_figure(
    loaded: TestClient,
) -> None:
    """A breakdown of the period figure, not a second opinion about it."""
    state = loaded.get(f"{API}/state?as_of=2026-04-30").json()
    grouped = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30&metric=fixed_due&group_by=payee"
    ).json()["points"]
    per_bucket: dict[str, int] = {}
    for point in grouped:
        per_bucket[point["bucket"]] = (
            per_bucket.get(point["bucket"], 0) + point["value_minor"]
        )
    for period in state["periods"]:
        assert per_bucket[period["period_id"]] == period["fixed_due_minor"]
    assert {point["series"] for point in grouped} == {"Landlord"}


def test_savings_balance_is_taken_at_each_period_close(loaded: TestClient) -> None:
    points = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30&metric=savings_balance"
    ).json()["points"]
    assert points == [
        {"bucket": "2026-03", "series": "total", "value_minor": 0},
        {"bucket": "2026-04", "series": "total", "value_minor": 165_000},
    ]


def test_account_balance_grouped_by_account_matches_accounts_at_as_of(
    loaded: TestClient,
) -> None:
    accounts = loaded.get(f"{API}/accounts?as_of=2026-04-30").json()["accounts"]
    expected = {row["account_id"]: row["balance_minor"] for row in accounts}
    points = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30"
        "&metric=account_balance&group_by=account&from=2026-04-01"
    ).json()["points"]
    assert {point["series"]: point["value_minor"] for point in points} == expected


def test_ungrouped_account_balance_sums_the_signed_balances(
    loaded: TestClient,
) -> None:
    """Net worth. Summing `outstanding_minor` instead would add a liability to an asset
    as though both were positive — the shape of the double-count CLAUDE.md §1 warns of."""
    accounts = loaded.get(f"{API}/accounts?as_of=2026-04-30").json()["accounts"]
    total = sum(row["balance_minor"] for row in accounts)
    points = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30&metric=account_balance&from=2026-04-01"
    ).json()["points"]
    assert points == [{"bucket": "2026-04", "series": "total", "value_minor": total}]


def test_interest_at_cycle_grain_buckets_by_cycle_id(loaded: TestClient) -> None:
    state = loaded.get(f"{API}/state?as_of=2026-04-30").json()
    card_cycles = {
        cycle["cycle_id"]: cycle["interest_minor"]
        for cycle in state["statement_cycles"]
        if cycle["account_id"] == VISA
    }
    points = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30"
        "&metric=interest_charged&grain=cycle&group_by=account"
    ).json()["points"]
    assert {point["bucket"]: point["value_minor"] for point in points} == card_cycles
    assert {point["series"] for point in points} == {VISA}


def test_interest_charged_and_earned_are_separated_by_account_kind(
    loaded: TestClient,
) -> None:
    """One figure per cycle carries no sign convention distinguishing the two, so the
    account kind is what separates a charge from a credit."""
    charged = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30"
        "&metric=interest_charged&grain=cycle&group_by=account"
    ).json()["points"]
    earned = loaded.get(
        f"{API}/charts/series?as_of=2026-04-30"
        "&metric=interest_earned&grain=cycle&group_by=account"
    ).json()["points"]
    assert {point["series"] for point in charged} == {VISA}
    assert VISA not in {point["series"] for point in earned}


def test_a_recorded_interest_charge_reaches_the_chart(client: TestClient) -> None:
    client.post(
        f"{API}/events/batch",
        json={
            "events": [
                {"event": expense("2026-03-07", 120_000, account_id=VISA)},
                {
                    "event": {
                        "event_type": "InterestCharged",
                        "date": "2026-04-15",
                        "account_id": VISA,
                        "cycle_id": "visa:2026-04",
                        "amount_minor": 2_241,
                    }
                },
            ]
        },
    )
    points = client.get(
        f"{API}/charts/series?as_of=2026-04-30&metric=interest_charged&grain=cycle"
    ).json()["points"]
    assert {"bucket": "visa:2026-04", "series": "total", "value_minor": 2_241} in points


def test_an_unsupported_grain_is_422(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/charts/series?metric=income&grain=cycle")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value
    assert "interest_charged" in response.json()["details"]["cycle_metrics"]


def test_an_unsupported_group_by_is_422_and_says_what_is_available(
    loaded: TestClient,
) -> None:
    """Grouping discretionary spend by category is the obvious missing chart. It is
    refused rather than guessed: deriving it here means re-deciding recognition outside
    the projection, which is the bug CLAUDE.md §1 exists to prevent."""
    response = loaded.get(
        f"{API}/charts/series?metric=discretionary_spent&group_by=category"
    )
    assert response.status_code == 422
    assert response.json()["details"]["groupable_metrics"] == [
        "fixed_due",
        "fixed_outstanding",
        "fixed_paid",
    ]


def test_savings_balance_cannot_be_grouped(loaded: TestClient) -> None:
    response = loaded.get(
        f"{API}/charts/series?metric=savings_balance&group_by=account"
    )
    assert response.status_code == 422


def test_an_unknown_metric_is_422(loaded: TestClient) -> None:
    response = loaded.get(f"{API}/charts/series?metric=vibes")
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.VALIDATION_FAILED.value


def test_a_missing_metric_is_422(loaded: TestClient) -> None:
    assert loaded.get(f"{API}/charts/series").status_code == 422


def test_chart_points_are_ordered_deterministically(loaded: TestClient) -> None:
    """Two identical requests render identically."""
    query = (
        f"{API}/charts/series?as_of=2026-04-30"
        "&metric=account_balance&group_by=account"
    )
    first = loaded.get(query).json()["points"]
    assert first == loaded.get(query).json()["points"]
    assert first == sorted(
        first, key=lambda point: (point["bucket"], point["series"])
    )


# ---------------------------------------------------------------- transfer neutrality


def test_a_transfer_leaves_discretionary_unchanged(client: TestClient) -> None:
    """Property 15, at the HTTP boundary: "inserting any `TransferMade` between own
    accounts leaves every period's `discretionary_remaining` unchanged"."""
    client.post(f"{API}/events", json={"event": income("2026-03-02", 450_000)})
    before = client.get(f"{API}/state?as_of=2026-03-31").json()

    client.post(
        f"{API}/events",
        json={
            "event": {
                "event_type": "TransferMade",
                "date": "2026-03-20",
                "amount_minor": 50_000,
                "from_account_id": CHECKING,
                "to_account_id": SAVINGS,
            }
        },
    )
    after = client.get(f"{API}/state?as_of=2026-03-31").json()

    assert [p["discretionary_remaining_minor"] for p in before["periods"]] == [
        p["discretionary_remaining_minor"] for p in after["periods"]
    ]
