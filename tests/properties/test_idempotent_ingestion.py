"""Idempotent re-ingestion: CLAUDE.md §5.1 property 7.

Owned by `module/properties` (PLAN.md §13.2).

   7  idempotent re-ingestion -> `test_property_7_re_appending_a_dedupe_key_changes_nothing`

This is the one property in the suite that cannot be checked against a pure function.
"Appending an event whose `dedupe_key` already exists leaves `State` unchanged" is a
claim about two things at once — that the database refuses the second write, and that the
projection over what is stored is therefore the same fold — and the first half is
`INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` (CONTRACTS.md §8.8). A mocked session
would decide idempotency in application code, which is precisely the implementation
`ingestion/` is forbidden to have, so every example here runs against real SQLite.

The engine is built by a plain function rather than a `pytest` fixture. A
function-scoped fixture is set up once and reused across every generated example, which
would let one example's rows leak into the next — and a dedupe assertion that passes
because the row was already there from a previous example is a dedupe assertion that
passes for the wrong reason.
"""

from __future__ import annotations

from uuid import UUID

from hypothesis import given
from sqlalchemy import Engine

from domain.events import Event
from domain.projection import State, project
from ingestion.append import append_event
from persistence.base import Base
from persistence.engine import create_db_engine, create_session_factory
from persistence.repositories import EventRepository

from tests.properties.strategies import (
    AS_OF,
    DATABASE_SETTINGS,
    DEFAULT_DEFINITIONS,
    ledgers,
    uid,
)

#: Where the re-appended copies' `event_id`s come from. Distinct from every index
#: `strategies.py` uses, so a copy can never accidentally carry an id that is already in
#: the ledger — which would make the second write a no-op for the wrong reason.
_COPY_INDEX_BASE = 990_000


def _fresh_engine() -> Engine:
    """A new in-memory database with the schema applied.

    `create_all` from the models rather than `alembic upgrade head`, which would cost a
    subprocess per generated example. That the two produce the same schema is asserted
    in `tests/unit/persistence/test_migrations.py`, so the shortcut cannot hide a
    migration that has drifted.
    """
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _with_new_event_id(event: Event, index: int) -> Event:
    """The same event under a different `event_id`, carrying the identical `dedupe_key`.

    This is the shape a genuine re-ingestion has. `event_id` is freshly generated per
    attempt — `domain/events.py` excludes it from the dedupe payload for exactly that
    reason — so two attempts at the same event differ in their id and agree in their key.
    Re-appending the *same object* is the easier case and is checked separately below.

    `model_copy` deliberately skips re-validation: the source event is already valid and
    only its id changes.
    """
    copied = event.model_copy(update={"event_id": uid(_COPY_INDEX_BASE + index)})
    assert copied.dedupe_key == event.dedupe_key
    return copied


def _stored(session_events: tuple[Event, ...]) -> tuple[tuple[str, UUID], ...]:
    """The ledger reduced to `(dedupe_key, event_id)` pairs, in canonical order.

    Comparing this alongside the full event tuple makes a failure legible: if a second
    write did land, the pair list shows which key gained a row rather than only that two
    large tuples differ.
    """
    return tuple((event.dedupe_key, event.event_id) for event in session_events)


@given(events=ledgers(max_events=6))
@DATABASE_SETTINGS
def test_property_7_re_appending_a_dedupe_key_changes_nothing(
    events: tuple[Event, ...],
) -> None:
    """Property 7. Appending an event whose `dedupe_key` already exists leaves `State`
    unchanged (CLAUDE.md §5.1).

    The whole ledger is appended, projected, then appended a second time under fresh
    `event_id`s and projected again. Four things are asserted, and the first two are what
    stop the last two from passing vacuously:

    * every first append reports `deduplicated=False` — so the generated keys really are
      distinct and the ledger really was written, rather than the second pass agreeing
      with a first pass that silently collapsed;
    * every second append reports `deduplicated=True` **and returns the stored row's
      id**, not the id it was handed. Two attempts at one event carry different ids by
      construction and the ledger's answer is the one already written
      (CONTRACTS.md §8.8);
    * the stored rows are byte-for-byte what they were;
    * `State` is identical.

    Re-uploading an identical receipt is a `200` with `deduplicated: true`, not an error,
    and must never be reported as one (CONTRACTS.md §6.1).
    """
    engine = _fresh_engine()
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            repository = EventRepository(session)

            written_ids = []
            for event in events:
                event_id, deduplicated = append_event(session, event)
                assert deduplicated is False
                assert event_id == event.event_id
                written_ids.append(event_id)
            session.commit()

            first_rows = repository.list_all()
            first_state = project(first_rows, DEFAULT_DEFINITIONS, AS_OF)
            assert len(first_rows) == len(events)
            assert repository.count() == len(events)

            for index, event in enumerate(events):
                event_id, deduplicated = append_event(
                    session, _with_new_event_id(event, index)
                )
                assert deduplicated is True
                assert event_id == written_ids[index]
            session.commit()

            second_rows = repository.list_all()
            second_state = project(second_rows, DEFAULT_DEFINITIONS, AS_OF)

            assert repository.count() == len(events)
            assert _stored(second_rows) == _stored(first_rows)
            assert second_rows == first_rows
            assert second_state == first_state
    finally:
        engine.dispose()


@given(events=ledgers(max_events=6))
@DATABASE_SETTINGS
def test_property_7a_appending_the_very_same_event_twice_is_a_no_op(
    events: tuple[Event, ...],
) -> None:
    """Property 7 in the contract's own words: "appending the same event twice leaves the
    table and `State` unchanged" (CONTRACTS.md §8.8).

    The same object, not a copy — the case a retried HTTP request produces when the
    client resends a body the server already assigned ids to. It is the easier of the two
    because the returned `event_id` is trivially the stored one; it is here because the
    contract states it, and because a conflict clause keyed on the wrong column would
    pass the copy test and fail this one.
    """
    engine = _fresh_engine()
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            repository = EventRepository(session)

            for event in events:
                append_event(session, event)
            session.commit()
            before = repository.list_all()
            before_state = project(before, DEFAULT_DEFINITIONS, AS_OF)

            for event in events:
                event_id, deduplicated = append_event(session, event)
                assert deduplicated is True
                assert event_id == event.event_id
            session.commit()

            assert repository.list_all() == before
            assert project(repository.list_all(), DEFAULT_DEFINITIONS, AS_OF) == (
                before_state
            )
    finally:
        engine.dispose()


@given(events=ledgers(max_events=6))
@DATABASE_SETTINGS
def test_property_7b_a_round_trip_through_the_ledger_preserves_the_projection(
    events: tuple[Event, ...],
) -> None:
    """Property 7's other half: storage must not change the answer.

    `project()` is a pure fold over the events it is handed, and every read recomputes
    from genesis over whatever the ledger returns (PLAN.md §3). So the `State` folded
    from the in-memory ledger and the `State` folded from the same ledger after a
    write-and-read-back must be the same value — a dropped timezone, a coerced amount or
    a lost optional field would show up here and nowhere in the pure properties.

    This is what makes the rest of the suite's conclusions apply to the running system
    rather than only to hand-built tuples.
    """
    engine = _fresh_engine()
    try:
        factory = create_session_factory(engine)
        with factory() as session:
            for event in events:
                append_event(session, event)
            session.commit()
            round_tripped = EventRepository(session).list_all()
    finally:
        engine.dispose()

    in_memory = project(events, DEFAULT_DEFINITIONS, AS_OF)
    from_ledger = project(round_tripped, DEFAULT_DEFINITIONS, AS_OF)

    assert isinstance(from_ledger, State)
    assert from_ledger == in_memory
