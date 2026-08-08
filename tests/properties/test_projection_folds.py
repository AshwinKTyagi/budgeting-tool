"""Projection invariants: CLAUDE.md §5.1 properties 5, 6 and 8.

Owned by `module/properties` (PLAN.md §13.2). Property 7 needs a database and lives in
`test_idempotent_ingestion.py`; properties 9-12 need a card and live in
`test_interest_and_cycles.py`.

  5  determinism            -> `test_property_5_project_is_deterministic`
  6  ingestion-order        -> `test_property_6_shuffling_arrival_order_changes_nothing`
  8  void equivalence       -> `test_property_8_void_equivalence`

Property 6 is about *arrival* order and nothing more. Shuffling the sequence handed to
`project()` cannot change the answer, because the fold sorts first. That is not immunity
to backdating: a backdated event changes the past, correctly and deterministically, and
property 9 is where that is pinned down (PLAN.md §6.3).
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from domain.events import Event
from domain.projection import State, project

from tests.properties.strategies import (
    AS_OF,
    DEFAULT_DEFINITIONS,
    LEDGER_SETTINGS,
    business_dates,
    ledgers,
    void,
    without_voided,
)


# ------------------------------------------------------------------------ property 5


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_5_project_is_deterministic(events: tuple[Event, ...]) -> None:
    """Property 5. `project(e, d, t) == project(e, d, t)`. Two calls, identical result.

    A projection that read a clock, memoised into a module-level dict, or mutated any
    input would fail this. Comparing whole `State` models rather than a summary figure
    is deliberate: hidden state usually shows up somewhere other than the number the
    test author was thinking about.
    """
    first = project(events, DEFAULT_DEFINITIONS, AS_OF)
    second = project(events, DEFAULT_DEFINITIONS, AS_OF)

    assert first == second


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_5a_project_does_not_touch_its_inputs(
    events: tuple[Event, ...],
) -> None:
    """Property 5, the other half of purity: no observable side effect on the arguments.

    Determinism across two calls would still hold if `project()` mutated a *copy*. This
    asserts the ledger it was handed, and the definitions bundle, come back unchanged —
    which is what makes the second call's inputs the same inputs as the first's.
    """
    before_events = tuple(event.model_copy(deep=True) for event in events)
    before_definitions = DEFAULT_DEFINITIONS.model_copy(deep=True)

    project(events, DEFAULT_DEFINITIONS, AS_OF)

    assert events == before_events
    assert DEFAULT_DEFINITIONS == before_definitions


# ------------------------------------------------------------------------ property 6


@given(events=ledgers(), data=st.data())
@LEDGER_SETTINGS
def test_property_6_shuffling_arrival_order_changes_nothing(
    events: tuple[Event, ...], data: st.DataObject
) -> None:
    """Property 6. Shuffling the arrival order of the same event set yields an
    identical `State` (CLAUDE.md §5.1).

    Every event carries a distinct `(date, recorded_at, event_id)`, so the canonical
    order is total and the shuffle can only be undone one way. This is what lets
    ingestion accept events in whatever order they turn up — a receipt photographed
    today for a purchase made last month is the normal case, not an exception.
    """
    shuffled = tuple(data.draw(st.permutations(events)))

    assert project(shuffled, DEFAULT_DEFINITIONS, AS_OF) == project(
        events, DEFAULT_DEFINITIONS, AS_OF
    )


@given(events=ledgers(), data=st.data())
@LEDGER_SETTINGS
def test_property_6a_reversing_arrival_order_changes_nothing(
    events: tuple[Event, ...], data: st.DataObject
) -> None:
    """Property 6, with the one permutation a random shuffle rarely reaches.

    Exact reversal is the arrangement most likely to expose an accumulator that depends
    on the order it was fed, and a uniform permutation of eight events picks it roughly
    once in forty thousand draws. `data` is unused beyond keeping the signature uniform
    with its sibling above.
    """
    del data
    reversed_events = tuple(reversed(events))

    assert project(reversed_events, DEFAULT_DEFINITIONS, AS_OF) == project(
        events, DEFAULT_DEFINITIONS, AS_OF
    )


# ------------------------------------------------------------------------ property 8


@given(events=ledgers())
@LEDGER_SETTINGS
def test_property_8_void_equivalence(events: tuple[Event, ...]) -> None:
    """Property 8. Folding `events` equals folding `events` with the voided events and
    their `EventVoided` records both removed (CLAUDE.md §5.1).

    `ledgers()` generates voids itself, targeting an earlier non-void, non-anchor event,
    so this runs against ledgers where the void's target is any event type at all. The
    anchor is excluded there only to keep genesis fixed for the generator's other
    consumers; `test_property_8a_...` below voids it explicitly.

    One universal void is what buys this single property. Typed adjustment events would
    need one invariant each (PLAN.md §8.4).
    """
    assert project(events, DEFAULT_DEFINITIONS, AS_OF) == project(
        without_voided(events), DEFAULT_DEFINITIONS, AS_OF
    )


@given(
    events=ledgers(include_voids=False),
    index=st.integers(min_value=0, max_value=32),
    void_date=business_dates(),
)
@LEDGER_SETTINGS
def test_property_8a_voiding_any_single_event_equals_never_having_it(
    events: tuple[Event, ...], index: int, void_date: dt.date
) -> None:
    """Property 8, constructed rather than generated.

    `ledgers(include_voids=False)` gives a ledger with nothing voided; a void is then
    aimed at one event chosen from it. The generated form above can only void what the
    generator happened to produce a void for — this form voids *every* event in turn as
    Hypothesis explores `index`, including the anchor, whose removal moves genesis.

    The void's own business date is drawn independently and must not matter: a void is
    filtered before the fold, so when it was recorded cannot change what remains
    (CONTRACTS.md §5.1 step 1).
    """
    target = events[index % len(events)]
    voided = (*events, void(950_000, void_date, target))
    without = tuple(event for event in events if event.event_id != target.event_id)

    assert project(voided, DEFAULT_DEFINITIONS, AS_OF) == project(
        without, DEFAULT_DEFINITIONS, AS_OF
    )


@given(events=ledgers())
@LEDGER_SETTINGS
def test_anomalies_are_data_and_never_raised(events: tuple[Event, ...]) -> None:
    """CONTRACTS.md §7. No generated ledger, however pathological, makes `project()`
    raise — a savings draw against nothing, an overpaid obligation, a payment naming an
    obligation that was voided out from under it. Each is a `Warning` in `State`.

    This is not one of the fifteen, but every one of the fifteen assumes it: a property
    whose subject raises before it can be checked is a property that never runs.
    """
    state = project(events, DEFAULT_DEFINITIONS, AS_OF)

    assert isinstance(state, State)
    assert state.current_period_id in tuple(
        period.period_id for period in state.periods
    )
