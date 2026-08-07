"""Instants survive SQLite as timezone-aware UTC.

SQLite is the trap. Its `DATETIME` storage format has no offset field, so SQLAlchemy's
default `DateTime(timezone=True)` on this backend accepts an aware value, writes the
wall-clock digits, and hands back a **naive** datetime. Nothing raises. The value looks
right, compares wrong against every other instant in the system, and violates CLAUDE.md
§4.5 the moment it is read.

`persistence.base.UtcDateTime` is the fix and this module is the proof. These tests
would fail against a plain `DateTime(timezone=True)` column — that is their whole
purpose, so do not "simplify" the column type without running them.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from domain.events import Event, ExpenseRecorded, IncomeReceived
from persistence.mapping import event_to_values
from persistence.models import EventRow, table_for
from persistence.repositories import EventRepository

UTC = dt.timezone.utc
TOKYO = dt.timezone(dt.timedelta(hours=9))
CHICAGO = dt.timezone(dt.timedelta(hours=-5))


def uid(n: int) -> UUID:
    return UUID(int=n)


def _income(event_id: int, recorded_at: dt.datetime) -> Event:
    return IncomeReceived(
        event_id=uid(event_id),
        date=dt.date(2026, 6, 1),
        recorded_at=recorded_at,
        dedupe_key=f"manual:IncomeReceived:2026-06-01:1000:{event_id}",
        amount_minor=1_000,
        source="Employer",
        account_id="checking",
    )


def test_a_utc_instant_comes_back_aware(session: Session) -> None:
    """The failure mode in one assertion: `tzinfo` is not None after a round-trip."""
    repository = EventRepository(session)
    recorded_at = dt.datetime(2026, 6, 1, 14, 30, 15, 123456, tzinfo=UTC)
    repository.append(_income(1, recorded_at))
    session.commit()
    session.expunge_all()

    loaded = repository.get(uid(1))
    assert loaded is not None
    assert loaded.recorded_at.tzinfo is not None
    assert loaded.recorded_at.utcoffset() == dt.timedelta(0)
    assert loaded.recorded_at == recorded_at


def test_microseconds_survive(session: Session) -> None:
    """Truncation to the second would be a silent reordering of two same-second events.

    `recorded_at` is the tie-break in the canonical ledger order, so losing sub-second
    precision does not lose an audit detail — it makes the order non-deterministic.
    """
    repository = EventRepository(session)
    recorded_at = dt.datetime(2026, 6, 1, 14, 30, 15, 999999, tzinfo=UTC)
    repository.append(_income(1, recorded_at))
    session.commit()
    session.expunge_all()

    loaded = repository.get(uid(1))
    assert loaded is not None
    assert loaded.recorded_at.microsecond == 999999
    assert loaded.recorded_at == recorded_at


def test_a_non_utc_offset_is_normalized_to_utc_preserving_the_instant(
    session: Session,
) -> None:
    """Same instant, expressed in Tokyo; stored and returned as UTC.

    `UtcInstant` already normalizes at the model boundary, so this is the *second* line
    of defence — it holds for a value that reaches the column without passing a Pydantic
    model, which is exactly what a repository-level or migration-level write does.
    """
    repository = EventRepository(session)
    tokyo_instant = dt.datetime(2026, 6, 1, 23, 30, tzinfo=TOKYO)
    utc_equivalent = dt.datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
    assert tokyo_instant == utc_equivalent

    repository.append(_income(1, tokyo_instant))
    session.commit()
    session.expunge_all()

    loaded = repository.get(uid(1))
    assert loaded is not None
    assert loaded.recorded_at == utc_equivalent
    assert loaded.recorded_at.utcoffset() == dt.timedelta(0)
    assert loaded.recorded_at.hour == 14


def test_the_column_itself_normalizes_a_non_utc_value(session: Session) -> None:
    """Write straight to the column, bypassing the model, and read straight back.

    This is the assertion that isolates the column type: no `UtcInstant` runs anywhere
    in this test, so anything the value keeps or loses is the column's doing.
    """
    chicago_instant = dt.datetime(2026, 6, 1, 9, 30, tzinfo=CHICAGO)
    values = event_to_values(_income(1, dt.datetime(2026, 6, 1, 14, 30, tzinfo=UTC)))
    values["recorded_at"] = chicago_instant
    session.execute(insert(table_for(EventRow)).values(**values))
    session.commit()

    stored = session.execute(
        select(table_for(EventRow).c.recorded_at).where(
            table_for(EventRow).c.event_id == uid(1)
        )
    ).scalar_one()
    assert stored == dt.datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
    assert stored.tzinfo is not None


def test_a_naive_datetime_is_rejected_at_the_column(session: Session) -> None:
    """There is no correct zone to assume, so the write fails rather than guessing.

    CLAUDE.md §4.5. A naive value silently interpreted as local time is how a March
    boundary bug gets in — an event lands in the wrong period and every subsequent
    period's allocation shifts with it.
    """
    values = event_to_values(_income(1, dt.datetime(2026, 6, 1, 14, 30, tzinfo=UTC)))
    values["recorded_at"] = dt.datetime(2026, 6, 1, 14, 30)  # naive

    with pytest.raises(StatementError):
        session.execute(insert(table_for(EventRow)).values(**values))
    session.rollback()


def test_the_stored_representation_is_utc_digits(engine: Engine) -> None:
    """What SQLite actually holds, read as raw text.

    Storing the local wall clock and relying on a zone attached at read time would make
    ordering by `recorded_at` in SQL disagree with ordering in Python. Both the digits
    and the retrieved value are UTC, so `ORDER BY recorded_at` is correct in the
    database — which matters, because that is how `ix_events_ledger_order` is used.
    """
    factory_session = Session(engine)
    repository = EventRepository(factory_session)
    repository.append(_income(1, dt.datetime(2026, 6, 1, 23, 30, tzinfo=TOKYO)))
    factory_session.commit()

    raw = factory_session.execute(
        text("SELECT recorded_at FROM events WHERE event_id = :event_id"),
        {"event_id": uid(1).hex},
    ).scalar_one()
    assert "14:30:00" in str(raw)
    factory_session.close()


def test_instants_in_different_zones_order_by_instant(session: Session) -> None:
    """The ledger order is by instant, not by wall clock.

    Two events recorded a minute apart in different zones must come back in the order
    they happened. If the column stored local digits, the Tokyo event would sort nine
    hours later than it occurred and the tie-break would be wrong.
    """
    repository = EventRepository(session)
    earlier = dt.datetime(2026, 6, 1, 23, 30, tzinfo=TOKYO)  # 14:30 UTC
    later = dt.datetime(2026, 6, 1, 9, 31, tzinfo=CHICAGO)  # 14:31 UTC
    assert earlier < later

    repository.append(_income(2, later))
    repository.append(_income(1, earlier))
    session.commit()

    ordered = repository.list_all()
    assert [event.recorded_at for event in ordered] == [
        dt.datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        dt.datetime(2026, 6, 1, 14, 31, tzinfo=UTC),
    ]


def test_a_business_date_carries_no_time_component(session: Session) -> None:
    """`date` is a `dt.date`, and comes back as one.

    CLAUDE.md §4.5: business dates have no time component at all — that is the type, not
    a convention. A `date` widened to a `datetime` on the way back would put an event's
    period membership at the mercy of a zone conversion.
    """
    repository = EventRepository(session)
    repository.append(
        ExpenseRecorded(
            event_id=uid(1),
            date=dt.date(2026, 6, 30),
            recorded_at=dt.datetime(2026, 7, 1, 2, 0, tzinfo=UTC),
            dedupe_key="manual:ExpenseRecorded:2026-06-30:1000:x",
            amount_minor=1_000,
            category="misc",
            account_id="checking",
        )
    )
    session.commit()
    session.expunge_all()

    loaded = repository.get(uid(1))
    assert loaded is not None
    assert type(loaded.date) is dt.date
    assert loaded.date == dt.date(2026, 6, 30)
