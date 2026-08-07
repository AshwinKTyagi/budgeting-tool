"""The round-trip is exact, for every event type.

`row_to_event(store(event)) == event`, with no normalization step in between. This is
the property the rest of persistence rests on: `project()` is a pure fold over these
models, so a field the database quietly altered would change the answer for every period
after it and there would be nothing in the ledger to show why.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy.orm import Session

from domain.events import Event, ExpenseRecorded, ExternalRef, PaymentMade
from persistence.mapping import (
    EVENT_CLASS_BY_TYPE,
    EVENT_SCALAR_FIELDS,
    event_to_values,
)
from persistence.models import EventRow, table_for
from persistence.repositories import EventRepository

UTC = dt.timezone.utc


def uid(n: int) -> UUID:
    return UUID(int=n)


def test_every_event_type_round_trips_exactly(session: Session, any_event: Event) -> None:
    """Store one event, read it back, and require equality — not equivalence.

    Pydantic model equality compares every field, so this covers the discriminator, the
    optional fields left at None, the nested `ExternalRef`, the signed amounts, and the
    instant. `type(...) is type(...)` is asserted separately because two different event
    classes could in principle compare equal on a shared field set.
    """
    repository = EventRepository(session)
    event_id, deduplicated = repository.append(any_event)
    assert deduplicated is False
    assert event_id == any_event.event_id

    loaded = repository.get(any_event.event_id)
    assert loaded == any_event
    assert type(loaded) is type(any_event)


def test_round_trip_survives_a_commit_and_a_new_session(
    session: Session, events: tuple[Event, ...]
) -> None:
    """The identity map is not what makes the round-trip work.

    Reading back inside the session that wrote could be served from memory without ever
    touching a column. Committing and opening a second session forces the values through
    the storage layer in both directions.
    """
    repository = EventRepository(session)
    for event in events:
        repository.append(event)
    session.commit()
    session.expunge_all()

    reloaded = repository.list_all()
    assert set(reloaded) == set(events)
    assert len(reloaded) == len(events)


def test_external_ref_round_trips_both_ways(session: Session) -> None:
    """A set `external_ref` comes back whole; an absent one comes back as None.

    The two are stored in the same pair of columns, so "both NULL" and "both set" are
    the only two states the table admits (a CHECK constraint enforces it) and both have
    to reconstruct correctly.
    """
    repository = EventRepository(session)
    with_ref = ExpenseRecorded(
        event_id=uid(101),
        date=dt.date(2026, 4, 1),
        recorded_at=dt.datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        dedupe_key="ext:acme:txn-99",
        external_ref=ExternalRef(provider="acme", provider_txn_id="txn-99"),
        amount_minor=1_000,
        category="misc",
        account_id="checking",
    )
    without_ref = ExpenseRecorded(
        event_id=uid(102),
        date=dt.date(2026, 4, 1),
        recorded_at=dt.datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        dedupe_key="manual:ExpenseRecorded:2026-04-01:1000:zzz",
        amount_minor=1_000,
        category="misc",
        account_id="checking",
    )
    repository.append(with_ref)
    repository.append(without_ref)
    session.commit()

    assert repository.get(uid(101)) == with_ref
    loaded_without = repository.get(uid(102))
    assert loaded_without == without_ref
    assert loaded_without is not None
    assert loaded_without.external_ref is None


def test_payment_split_round_trips_set_and_unset(session: Session) -> None:
    """`principal_minor`/`interest_minor` are both-or-neither, and both shapes persist.

    A split that came back half-populated would fail `PaymentMade._check_split` on
    reconstruction rather than load a wrong number — which is the right failure, and
    this test is what confirms the right one never happens.
    """
    repository = EventRepository(session)
    split = PaymentMade(
        event_id=uid(103),
        date=dt.date(2026, 4, 2),
        recorded_at=dt.datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
        dedupe_key="manual:PaymentMade:2026-04-02:118000:split",
        amount_minor=118_000,
        obligation_id="loan-2026-04",
        account_id="checking",
        principal_minor=100_000,
        interest_minor=18_000,
    )
    unsplit = PaymentMade(
        event_id=uid(104),
        date=dt.date(2026, 4, 2),
        recorded_at=dt.datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
        dedupe_key="manual:PaymentMade:2026-04-02:118000:unsplit",
        amount_minor=118_000,
        obligation_id="loan-2026-04",
        account_id="checking",
    )
    repository.append(split)
    repository.append(unsplit)
    session.commit()

    loaded_split = repository.get(uid(103))
    assert loaded_split == split
    assert isinstance(loaded_split, PaymentMade)
    assert loaded_split.principal_minor == 100_000
    assert loaded_split.interest_minor == 18_000

    loaded_unsplit = repository.get(uid(104))
    assert loaded_unsplit == unsplit
    assert isinstance(loaded_unsplit, PaymentMade)
    assert loaded_unsplit.principal_minor is None
    assert loaded_unsplit.interest_minor is None


def test_money_is_stored_as_an_integer(session: Session) -> None:
    """The value in the column is an `int`, not a float that happens to print right.

    CLAUDE.md §2.1: money is `int` minor units, "not even transiently". A column typed
    REAL would round-trip 4599 without complaint and lose a cent somewhere around
    2^53; reading the raw column is the only way to see which one is stored.
    """
    repository = EventRepository(session)
    event = ExpenseRecorded(
        event_id=uid(105),
        date=dt.date(2026, 4, 3),
        recorded_at=dt.datetime(2026, 4, 3, 12, 0, tzinfo=UTC),
        dedupe_key="manual:ExpenseRecorded:2026-04-03:4599:int",
        amount_minor=4_599,
        category="groceries",
        account_id="checking",
    )
    repository.append(event)
    session.commit()

    row = session.get(EventRow, uid(105))
    assert row is not None
    assert type(row.amount_minor) is int
    assert row.amount_minor == 4_599


def test_negative_and_large_amounts_survive(session: Session) -> None:
    """Signed and large magnitudes, exactly.

    `BIGINT` rather than `INTEGER` because a signed 32-bit column tops out at about
    $21.5M in cents — an amount a single user will not reach, and precisely the kind of
    limit that is discovered by an overflow rather than by a test.
    """
    repository = EventRepository(session)
    extremes = (-9_223_372_036_854_775_808 + 1, -1, 0, 1, 9_223_372_036_854_775_807)
    for index, amount_minor in enumerate(extremes):
        repository.append(
            ExpenseRecorded(
                event_id=uid(200 + index),
                date=dt.date(2026, 4, 4),
                recorded_at=dt.datetime(2026, 4, 4, 12, 0, tzinfo=UTC),
                dedupe_key=f"manual:ExpenseRecorded:2026-04-04:{amount_minor}:big",
                amount_minor=amount_minor,
                category="misc",
                account_id="checking",
            )
        )
    session.commit()
    session.expunge_all()

    for index, amount_minor in enumerate(extremes):
        loaded = repository.get(uid(200 + index))
        assert isinstance(loaded, ExpenseRecorded)
        assert loaded.amount_minor == amount_minor


def test_every_event_field_has_a_column() -> None:
    """No field of any event type can be silently unmapped.

    `EVENT_SCALAR_FIELDS` is derived from the union at import time, so this test fails
    the moment an event type gains a field without a column — which would otherwise
    manifest as that field quietly resetting to its default on every read.
    """
    column_keys = {column.key for column in table_for(EventRow).columns}
    mapped = {
        EventRow.__mapper__.columns[field].key for field in EVENT_SCALAR_FIELDS
    }
    assert mapped <= column_keys
    assert len(mapped) == len(EVENT_SCALAR_FIELDS)


def test_every_event_class_is_reachable_by_its_discriminator() -> None:
    """The stored `event_type` string maps back to exactly one class, for all eleven."""
    assert len(EVENT_CLASS_BY_TYPE) == 11
    for event_type, event_cls in EVENT_CLASS_BY_TYPE.items():
        assert event_cls.model_fields["event_type"].default == event_type


def test_values_payload_names_every_column_for_every_type(
    events: tuple[Event, ...],
) -> None:
    """Each INSERT names the same columns regardless of type.

    Fields the event's own type does not declare are written as explicit NULLs. If a
    type contributed a shorter column list, a future default or a reused statement cache
    could leave a stale value in the gap.
    """
    payloads = [event_to_values(event) for event in events]
    key_sets = {frozenset(payload) for payload in payloads}
    assert len(key_sets) == 1
    assert key_sets.pop() == {column.name for column in table_for(EventRow).columns}
