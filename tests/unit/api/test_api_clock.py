"""`resolve_as_of` — the only clock read in the codebase (CONTRACTS.md §8.9).

Two branches and one hazard. The branches are "supplied" and "omitted". The hazard is
that "today" is a function of the *zone*, not of UTC, and the offset that separates them
changes twice a year — so a naive implementation is correct for ten months and wrong for
the two hours either side of a DST transition, plus every evening in between.

Every test pins the instant by monkeypatching `api.clock._now_utc`. That is the whole
reason it exists as a named private function rather than an inline `dt.datetime.now`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from api import clock
from api.clock import BUDGET_TZ_DEFAULT, budget_tz, resolve_as_of

UTC = dt.timezone.utc
LA = "America/Los_Angeles"


def pin(monkeypatch: pytest.MonkeyPatch, instant: dt.datetime) -> None:
    """Freeze the one clock read at `instant`."""
    monkeypatch.setattr(clock, "_now_utc", lambda: instant)


# ----------------------------------------------------------------- supplied verbatim


def test_supplied_as_of_is_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    pin(monkeypatch, dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
    assert resolve_as_of(dt.date(2026, 3, 31), LA) == dt.date(2026, 3, 31)


def test_a_future_as_of_is_not_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future date is a forecast query and a valid one (CONTRACTS.md §6.3)."""
    pin(monkeypatch, dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
    assert resolve_as_of(dt.date(2099, 12, 31), LA) == dt.date(2099, 12, 31)


def test_a_supplied_as_of_ignores_the_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verbatim means verbatim: the zone is not consulted at all on this branch."""
    pin(monkeypatch, dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
    supplied = dt.date(2026, 3, 31)
    assert resolve_as_of(supplied, "Pacific/Kiritimati") == supplied
    assert resolve_as_of(supplied, "Pacific/Midway") == supplied


# ------------------------------------------------------------------ omitted == today


def test_omitted_as_of_is_today_in_the_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """23:00 in Los Angeles is already tomorrow in UTC. The zone wins."""
    pin(monkeypatch, dt.datetime(2026, 8, 8, 6, 0, tzinfo=UTC))  # 23:00 PDT on the 7th
    assert resolve_as_of(None, LA) == dt.date(2026, 8, 7)
    assert resolve_as_of(None, "UTC") == dt.date(2026, 8, 8)


def test_omitted_as_of_at_local_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    pin(monkeypatch, dt.datetime(2026, 8, 7, 7, 0, tzinfo=UTC))  # 00:00 PDT
    assert resolve_as_of(None, LA) == dt.date(2026, 8, 7)


# ------------------------------------------------------------------------------ DST
# 2026 transitions in America/Los_Angeles: forward on 8 March (PST -08:00 -> PDT
# -07:00), back on 1 November (PDT -> PST). The instant 07:00 UTC lands on a different
# local *date* on either side of each one, which is exactly the boundary a date-valued
# clock read has to get right.


def test_spring_forward_changes_the_local_date_at_a_fixed_utc_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin(monkeypatch, dt.datetime(2026, 3, 8, 7, 0, tzinfo=UTC))  # 23:00 PST, 7 March
    assert resolve_as_of(None, LA) == dt.date(2026, 3, 7)

    pin(monkeypatch, dt.datetime(2026, 3, 9, 7, 0, tzinfo=UTC))  # 00:00 PDT, 9 March
    assert resolve_as_of(None, LA) == dt.date(2026, 3, 9)


def test_either_side_of_the_spring_forward_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:59 PST and 03:00 PDT are one minute apart and the same business date."""
    pin(monkeypatch, dt.datetime(2026, 3, 8, 9, 59, tzinfo=UTC))
    assert resolve_as_of(None, LA) == dt.date(2026, 3, 8)

    pin(monkeypatch, dt.datetime(2026, 3, 8, 10, 0, tzinfo=UTC))
    assert resolve_as_of(None, LA) == dt.date(2026, 3, 8)


def test_fall_back_repeats_an_hour_without_repeating_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:30 happens twice on 1 November. Both are 1 November."""
    pin(monkeypatch, dt.datetime(2026, 11, 1, 8, 30, tzinfo=UTC))  # 01:30 PDT
    assert resolve_as_of(None, LA) == dt.date(2026, 11, 1)

    pin(monkeypatch, dt.datetime(2026, 11, 1, 9, 30, tzinfo=UTC))  # 01:30 PST
    assert resolve_as_of(None, LA) == dt.date(2026, 11, 1)

    pin(monkeypatch, dt.datetime(2026, 11, 2, 7, 0, tzinfo=UTC))  # 23:00 PST, 1 Nov
    assert resolve_as_of(None, LA) == dt.date(2026, 11, 1)


# ---------------------------------------------------------------------- BUDGET_TZ


def test_budget_tz_defaults_to_los_angeles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUDGET_TZ", raising=False)
    assert budget_tz() == BUDGET_TZ_DEFAULT
    assert BUDGET_TZ_DEFAULT == "America/Los_Angeles"


def test_budget_tz_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUDGET_TZ", "Europe/Berlin")
    assert budget_tz() == "Europe/Berlin"


def test_an_empty_budget_tz_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset variable and an empty one are the same unconfigured deployment."""
    monkeypatch.setenv("BUDGET_TZ", "")
    assert budget_tz() == BUDGET_TZ_DEFAULT


def test_now_utc_is_aware_and_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """`recorded_at` must satisfy `UtcInstant`, which rejects a naive value outright."""
    pin(monkeypatch, dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
    assert clock.now_utc() == dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    assert clock.now_utc().tzinfo is not None
