"""`dedupe_key` is UNIQUE, and the ON CONFLICT path is genuinely a no-op.

CONTRACTS.md §8.8 makes `append_event` idempotent by construction: the second write of
an event that already exists must leave the table and `State` unchanged. Two things have
to be true for that, and both are checked here rather than assumed — the constraint
exists in the database (not only in the model), and the insert that hits it writes
nothing while still reporting the *existing* row's id.

CLAUDE.md §5.1 property 7 states the projection-level version of this. It lives in
`tests/properties/`, owned by `module/properties`; what follows is the storage-level
fact that property rests on.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.types import AppError, ErrorCode
from domain.events import Event, ExpenseRecorded, compute_dedupe_key
from persistence.mapping import event_to_values
from persistence.models import EventRow, table_for
from persistence.repositories import EventRepository

UTC = dt.timezone.utc


def uid(n: int) -> UUID:
    return UUID(int=n)


def _expense(event_id: int, dedupe_key: str, *, amount_minor: int = 4_599) -> Event:
    return ExpenseRecorded(
        event_id=uid(event_id),
        date=dt.date(2026, 5, 1),
        recorded_at=dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        dedupe_key=dedupe_key,
        amount_minor=amount_minor,
        category="coffee",
        account_id="checking",
    )


def test_appending_the_same_event_twice_writes_one_row(session: Session) -> None:
    """Second append: no row written, `deduplicated=True`, table unchanged."""
    repository = EventRepository(session)
    event = _expense(1, "manual:ExpenseRecorded:2026-05-01:4599:coffee")

    first_id, first_dedup = repository.append(event)
    before = repository.list_all()
    second_id, second_dedup = repository.append(event)
    after = repository.list_all()

    assert first_dedup is False
    assert second_dedup is True
    assert first_id == second_id == event.event_id
    assert repository.count() == 1
    assert after == before


def test_a_duplicate_returns_the_stored_event_id_not_the_submitted_one(
    session: Session,
) -> None:
    """The ledger's answer is the row already written.

    Two attempts at the same purchase carry different `event_id`s — a fresh UUID is
    generated per attempt — but one `dedupe_key`. `append` must report the id that is
    actually in the table, because that is the id the caller will use to void or
    reference the event. Returning the submitted id would hand back an id that names
    nothing.
    """
    repository = EventRepository(session)
    dedupe_key = "manual:ExpenseRecorded:2026-05-01:4599:coffee"
    stored_id, _ = repository.append(_expense(1, dedupe_key))

    resubmitted_id, deduplicated = repository.append(_expense(2, dedupe_key))

    assert deduplicated is True
    assert resubmitted_id == stored_id
    assert resubmitted_id != uid(2)
    assert repository.get(uid(2)) is None
    assert repository.count() == 1


def test_a_duplicate_does_not_overwrite_the_stored_row(session: Session) -> None:
    """DO NOTHING means nothing — not "update the row with the newer values".

    A resubmission carrying a different amount under the same key must not silently
    rewrite the ledger. An amount correction is void + re-raise (PLAN.md §8.4).
    """
    repository = EventRepository(session)
    dedupe_key = "manual:ExpenseRecorded:2026-05-01:4599:coffee"
    original = _expense(1, dedupe_key, amount_minor=4_599)
    repository.append(original)

    repository.append(_expense(2, dedupe_key, amount_minor=999_999))
    session.commit()

    assert repository.get_by_dedupe_key(dedupe_key) == original


def test_the_unique_constraint_exists_in_the_database(session: Session) -> None:
    """Not merely declared on the model — enforced by the engine.

    A raw INSERT that bypasses the repository still has to fail. This is what makes the
    idempotency guarantee survive a future code path nobody has written yet.
    """
    repository = EventRepository(session)
    dedupe_key = "manual:ExpenseRecorded:2026-05-01:4599:coffee"
    repository.append(_expense(1, dedupe_key))
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            insert(table_for(EventRow)).values(
                **event_to_values(_expense(2, dedupe_key))
            )
        )
    session.rollback()


def test_distinct_dedupe_keys_both_persist(session: Session) -> None:
    """The constraint discriminates; it does not just reject the second write."""
    repository = EventRepository(session)
    repository.append(_expense(1, "manual:ExpenseRecorded:2026-05-01:4599:a"))
    second_id, deduplicated = repository.append(
        _expense(2, "manual:ExpenseRecorded:2026-05-01:4599:b")
    )

    assert deduplicated is False
    assert second_id == uid(2)
    assert repository.count() == 2


def test_two_identical_coffees_collide_and_a_nonce_separates_them(
    session: Session,
) -> None:
    """CONTRACTS.md §3.1, stated literally: the manual key is collision-prone across
    genuinely identical entries, and the caller disambiguates with a nonce.

    This is the documented behavior and it is deliberate — "silently accepting
    accidental duplicates is the worse failure". The test pins both halves: the collision
    happens, and the escape hatch works.
    """
    repository = EventRepository(session)
    payload: dict[str, object] = {
        "date": dt.date(2026, 5, 1),
        "amount_minor": 450,
        "category": "coffee",
        "account_id": "checking",
    }
    colliding_key = compute_dedupe_key("ExpenseRecorded", payload)
    separated_key = compute_dedupe_key(
        "ExpenseRecorded", payload, client_nonce="second-cup"
    )
    assert colliding_key != separated_key

    repository.append(_expense(1, colliding_key, amount_minor=450))
    _, deduplicated = repository.append(_expense(2, colliding_key, amount_minor=450))
    assert deduplicated is True
    assert repository.count() == 1

    _, nonced_dedup = repository.append(_expense(3, separated_key, amount_minor=450))
    assert nonced_dedup is False
    assert repository.count() == 2


def test_an_empty_dedupe_key_is_rejected_before_it_reaches_the_table(
    session: Session,
) -> None:
    """The precondition in CONTRACTS.md §8.8 is checked, not assumed.

    An empty key would make every keyless event collide with every other one, turning
    the idempotency guarantee into silent data loss. Malformed input, so an error rather
    than a warning (CLAUDE.md §6).
    """
    repository = EventRepository(session)
    with pytest.raises(AppError) as raised:
        repository.append(_expense(1, ""))
    assert raised.value.code == ErrorCode.VALIDATION_FAILED
    assert repository.count() == 0


def test_the_conflicting_insert_writes_no_row_at_all(session: Session) -> None:
    """Belt and braces: count the rows in the table directly, not through the repository.

    `count()` goes through the same layer being tested; a raw `SELECT` does not.
    """
    repository = EventRepository(session)
    dedupe_key = "manual:ExpenseRecorded:2026-05-01:4599:coffee"
    repository.append(_expense(1, dedupe_key))
    repository.append(_expense(2, dedupe_key))
    session.commit()

    rows = session.execute(select(table_for(EventRow).c.event_id)).all()
    assert len(rows) == 1
