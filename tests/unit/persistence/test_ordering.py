"""Reads come back in the canonical ledger order, regardless of write order.

CONTRACTS.md §3.1: the ledger order is `(date, recorded_at, event_id)` — total and
stable. `project()` sorts its input anyway (step 2 of the fold), so this is not what
makes the projection order-independent. It is what makes `GET /ledger` pageable: a
keyset cursor is only correct over a total order, and an order that fell back on
insertion order would skip or repeat rows the moment a backdated event arrived
mid-pagination.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy.orm import Session

from domain.events import Event, ExpenseRecorded, IncomeReceived, TransferMade
from persistence.repositories import EventRepository

UTC = dt.timezone.utc


def uid(n: int) -> UUID:
    return UUID(int=n)


def _expense(
    event_id: int,
    date: dt.date,
    recorded_at: dt.datetime,
    *,
    category: str = "misc",
    account_id: str = "checking",
) -> Event:
    return ExpenseRecorded(
        event_id=uid(event_id),
        date=date,
        recorded_at=recorded_at,
        dedupe_key=f"manual:ExpenseRecorded:{date.isoformat()}:100:{event_id}",
        amount_minor=100,
        category=category,
        account_id=account_id,
    )


def _key(event: Event) -> tuple[dt.date, dt.datetime, UUID]:
    return (event.date, event.recorded_at, event.event_id)


def test_reads_are_ordered_by_date_then_recorded_at_then_event_id(
    session: Session,
) -> None:
    """Written in scrambled order, read back in canonical order.

    Every tier of the sort key is exercised: two different dates, two instants within one
    date, and two ids at one instant.
    """
    repository = EventRepository(session)
    scrambled = [
        _expense(5, dt.date(2026, 7, 2), dt.datetime(2026, 7, 2, 9, 0, tzinfo=UTC)),
        _expense(2, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)),
        _expense(4, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)),
        _expense(1, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 8, 0, tzinfo=UTC)),
        _expense(3, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)),
    ]
    for event in scrambled:
        repository.append(event)
    session.commit()

    ordered = repository.list_all()
    assert [event.event_id for event in ordered] == [uid(n) for n in (1, 2, 3, 4, 5)]
    assert list(ordered) == sorted(scrambled, key=_key)


def test_the_order_is_independent_of_insertion_order(session: Session) -> None:
    """Same set, two arrival orders, identical read.

    A backdated event is the normal case in this system, not the exception (PLAN.md §3),
    so "the order rows were written in" can never be the order they are read in.
    """
    repository = EventRepository(session)
    events = [
        _expense(n, dt.date(2026, 7, n), dt.datetime(2026, 7, n, 9, 0, tzinfo=UTC))
        for n in (1, 2, 3, 4, 5)
    ]
    for event in reversed(events):
        repository.append(event)
    session.commit()

    assert list(repository.list_all()) == events


def test_newest_first_is_the_exact_reverse(session: Session) -> None:
    """`GET /ledger` is newest-first (CONTRACTS.md §6.2); the tie-breaks reverse too."""
    repository = EventRepository(session)
    for n in (1, 2, 3):
        repository.append(
            _expense(n, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 9, 0, tzinfo=UTC))
        )
    session.commit()

    ascending = repository.list_events()
    descending = repository.list_events(newest_first=True)
    assert list(descending) == list(reversed(ascending))


def test_a_keyset_cursor_pages_without_gaps_or_repeats(session: Session) -> None:
    """Walk the whole ledger two rows at a time and land on every row exactly once.

    Keyset rather than OFFSET because an event appended mid-pagination shifts every
    OFFSET after it — and appending mid-pagination is what this application does.
    """
    repository = EventRepository(session)
    for n in range(1, 8):
        repository.append(
            _expense(n, dt.date(2026, 7, n), dt.datetime(2026, 7, n, 9, 0, tzinfo=UTC))
        )
    session.commit()

    collected: list[Event] = []
    cursor: tuple[dt.date, dt.datetime, UUID] | None = None
    while True:
        page = repository.list_events(after=cursor, limit=2)
        if not page:
            break
        collected.extend(page)
        cursor = _key(page[-1])

    assert [event.event_id for event in collected] == [uid(n) for n in range(1, 8)]
    assert len(collected) == len({event.event_id for event in collected})


def test_a_descending_cursor_walks_backwards(session: Session) -> None:
    """The cursor comparison follows `newest_first`, or the first page repeats forever."""
    repository = EventRepository(session)
    for n in range(1, 6):
        repository.append(
            _expense(n, dt.date(2026, 7, n), dt.datetime(2026, 7, n, 9, 0, tzinfo=UTC))
        )
    session.commit()

    first_page = repository.list_events(newest_first=True, limit=2)
    second_page = repository.list_events(
        newest_first=True, limit=2, after=_key(first_page[-1])
    )
    assert [event.event_id for event in first_page] == [uid(5), uid(4)]
    assert [event.event_id for event in second_page] == [uid(3), uid(2)]


def test_date_window_filters_on_the_business_date(session: Session) -> None:
    """`from_date`/`to_date` are inclusive and filter `date`, never `recorded_at`.

    Period membership is a business-date question (CONTRACTS.md §3.1). An event recorded
    in July for a June date belongs to June, and a June window has to contain it.
    """
    repository = EventRepository(session)
    repository.append(
        _expense(1, dt.date(2026, 6, 30), dt.datetime(2026, 7, 5, 9, 0, tzinfo=UTC))
    )
    repository.append(
        _expense(2, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 9, 0, tzinfo=UTC))
    )
    session.commit()

    june = repository.list_events(
        from_date=dt.date(2026, 6, 1), to_date=dt.date(2026, 6, 30)
    )
    assert [event.event_id for event in june] == [uid(1)]


def test_account_filter_matches_both_ends_of_a_transfer(session: Session) -> None:
    """A transfer belongs to both accounts it touches.

    `TransferMade` carries no `account_id` — direction lives in `from_account_id` and
    `to_account_id` — so an account filter that only looked at `account_id` would make
    every transfer invisible in an account view, which is precisely the event type that
    must stay visible (PLAN.md §1: it is the mechanism preventing double-counting).
    """
    repository = EventRepository(session)
    repository.append(
        TransferMade(
            event_id=uid(1),
            date=dt.date(2026, 7, 1),
            recorded_at=dt.datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            dedupe_key="manual:TransferMade:2026-07-01:50000:t",
            amount_minor=50_000,
            from_account_id="checking",
            to_account_id="visa",
        )
    )
    repository.append(
        _expense(2, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    )
    session.commit()

    assert {event.event_id for event in repository.list_events(account_id="visa")} == {
        uid(1)
    }
    assert {
        event.event_id for event in repository.list_events(account_id="checking")
    } == {uid(1), uid(2)}


def test_type_and_category_filters(session: Session) -> None:
    """The two remaining `GET /ledger` filters."""
    repository = EventRepository(session)
    repository.append(
        _expense(1, dt.date(2026, 7, 1), dt.datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
                 category="groceries")
    )
    repository.append(
        IncomeReceived(
            event_id=uid(2),
            date=dt.date(2026, 7, 1),
            recorded_at=dt.datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            dedupe_key="manual:IncomeReceived:2026-07-01:1000:i",
            amount_minor=1_000,
            source="Employer",
            account_id="checking",
        )
    )
    session.commit()

    by_type = repository.list_events(event_types=["IncomeReceived"])
    assert [event.event_id for event in by_type] == [uid(2)]

    by_category = repository.list_events(category="groceries")
    assert [event.event_id for event in by_category] == [uid(1)]


def test_voided_events_are_included_in_reads(session: Session, events: tuple[Event, ...]) -> None:
    """Storage does not filter. The projection does.

    Filtering voided events is step 1 of the fold (CONTRACTS.md §5.1), and `GET /ledger`
    shows them with `is_voided: true` — "the tabular view shows history, it does not hide
    it" (§6.2). A repository that hid them would make both impossible.
    """
    repository = EventRepository(session)
    for event in events:
        repository.append(event)
    session.commit()

    stored = repository.list_all()
    assert len(stored) == len(events)
    assert any(event.event_type == "EventVoided" for event in stored)
    # uid(9) is the target of the EventVoided in the sample ledger.
    assert any(event.event_id == uid(9) for event in stored)


def test_find_void_for_locates_the_voiding_record(
    session: Session, events: tuple[Event, ...]
) -> None:
    """The write-time `ALREADY_VOIDED` check (CONTRACTS.md §7.1) is an O(1) lookup."""
    repository = EventRepository(session)
    for event in events:
        repository.append(event)
    session.commit()

    void = repository.find_void_for(uid(9))
    assert void is not None
    assert void.target_event_id == uid(9)
    assert void.reason == "entered twice"
    assert repository.find_void_for(uid(1)) is None


def test_exists_answers_the_unknown_event_check(
    session: Session, events: tuple[Event, ...]
) -> None:
    """`UNKNOWN_EVENT` is a 404 on a void whose target does not exist."""
    repository = EventRepository(session)
    for event in events:
        repository.append(event)
    session.commit()

    assert repository.exists(uid(1)) is True
    assert repository.exists(uid(9999)) is False
