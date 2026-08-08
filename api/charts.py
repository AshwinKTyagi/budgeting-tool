"""`GET /charts/series`: every chart variation from one shape (CONTRACTS.md §6.2).

Owned by `module/api` (PLAN.md §13.2).

**Every point is read off `State`.** Nothing here re-derives a budget number from the
event stream. That restraint is the whole design: CLAUDE.md §1 says an outflow affects
the budget exactly once, and a chart layer that computed "discretionary spent by
category" by summing `ExpenseRecorded` amounts would be recognizing those purchases a
second time — under `AT_STATEMENT_PAYMENT` it would be recognizing them at the wrong
moment as well (PLAN.md §6.4). `State.periods` already carries the answer the projection
decided; charts fold it, they do not recompute it.

That has a visible consequence, and it is deliberate: **a `group_by` is offered only
where `State` already carries the breakdown.**

| group_by | available for | source |
|---|---|---|
| `none` | every metric | the period / cycle row itself |
| `category`, `payee` | `fixed_due`, `fixed_paid`, `fixed_outstanding` | `State.obligations` |
| `account` | `account_balance`, `interest_charged`, `interest_earned` | `State.accounts`, `State.statement_cycles` |

Anything else is `VALIDATION_FAILED`, with a message naming what is available. Grouping
discretionary spend by category is the obvious missing chart; adding it means teaching
the *projection* to carry a per-category breakdown, which is the right place for it and
is not this module's to decide.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Final

from api.dtos import ChartGrain, ChartGroupBy, ChartMetric, ChartPoint
from core.types import AccountKind, AppError, ErrorCode, Minor
from domain.accounts import AccountBalance, StatementCycleSummary
from domain.projection import ObligationRow, PeriodSummary, State

#: The series name when nothing is grouped.
TOTAL_SERIES: Final[str] = "total"

_ONE_DAY: Final[dt.timedelta] = dt.timedelta(days=1)

_LIABILITY_KINDS: Final[frozenset[AccountKind]] = frozenset(
    {AccountKind.CREDIT_CARD, AccountKind.LOAN}
)

#: Metrics that are a field of `PeriodSummary`, read straight off it. Spelled as an
#: explicit accessor per metric rather than a field-name string plus `getattr`, so that
#: `mypy --strict` checks each one and a renamed field is a type error rather than an
#: `AttributeError` on the first request.
_PERIOD_VALUE: Final[dict[ChartMetric, Callable[[PeriodSummary], Minor]]] = {
    ChartMetric.INCOME: lambda period: period.income_minor,
    ChartMetric.ALLOCATABLE_INCOME: lambda period: period.allocatable_income_minor,
    ChartMetric.FIXED_DUE: lambda period: period.fixed_due_minor,
    ChartMetric.FIXED_PAID: lambda period: period.fixed_paid_minor,
    ChartMetric.FIXED_OUTSTANDING: lambda period: period.fixed_outstanding_minor,
    ChartMetric.SAVINGS_ALLOCATED: lambda period: period.savings_allocated_minor,
    ChartMetric.DISCRETIONARY_ALLOCATED: (
        lambda period: period.discretionary_allocated_minor
    ),
    ChartMetric.DISCRETIONARY_SPENT: lambda period: period.discretionary_spent_minor,
    ChartMetric.DISCRETIONARY_REMAINING: (
        lambda period: period.discretionary_remaining_minor
    ),
}

#: The obligation column each `fixed_*` metric groups by.
_OBLIGATION_VALUE: Final[dict[ChartMetric, Callable[[ObligationRow], Minor]]] = {
    ChartMetric.FIXED_DUE: lambda row: row.amount_minor,
    ChartMetric.FIXED_PAID: lambda row: row.paid_minor,
    ChartMetric.FIXED_OUTSTANDING: lambda row: row.remaining_minor,
}


def build_points(
    state: State,
    *,
    metric: ChartMetric,
    grain: ChartGrain,
    group_by: ChartGroupBy,
    from_date: dt.date | None,
    to_date: dt.date | None,
    state_at: Callable[[dt.date], State],
) -> tuple[ChartPoint, ...]:
    """Fold `state` into chart points.

    `state_at` re-projects at a given business date and backs the two *stock* metrics —
    a balance is a value at an instant, not a sum over a period, so there is no field on
    `PeriodSummary` to read and the honest answer is the projection's own answer at each
    bucket's close. Every other metric is a flow and needs no second pass.

    Postconditions:
        every `value_minor` is an int copied or summed from `State`; no float, no
        formatted string (CONTRACTS.md §6)
        points are ordered by `(bucket, series)` — total and stable, so two identical
        requests render identically

    Raises:
        AppError(VALIDATION_FAILED) for a metric / grain / group_by combination `State`
        cannot answer. See this module's docstring for the table.
    """
    if grain is ChartGrain.CYCLE:
        return _ordered(_cycle_points(state, metric, group_by, from_date, to_date))

    periods = tuple(
        period
        for period in state.periods
        if _overlaps(period.start_date, period.end_date_exclusive, from_date, to_date)
    )

    if metric in (ChartMetric.SAVINGS_BALANCE, ChartMetric.ACCOUNT_BALANCE):
        return _ordered(_balance_points(state, metric, group_by, periods, state_at))
    if metric in (ChartMetric.INTEREST_CHARGED, ChartMetric.INTEREST_EARNED):
        return _cycle_period_points(state, metric, group_by, periods)
    if group_by is not ChartGroupBy.NONE:
        return _ordered(_grouped_period_points(state, metric, group_by, periods))
    return _ordered(_period_points(metric, periods))


# --------------------------------------------------------------------------- flows


def _period_points(
    metric: ChartMetric, periods: Sequence[PeriodSummary]
) -> list[ChartPoint]:
    """One ungrouped point per period, read off `PeriodSummary`.

    `PERIOD` and `MONTH` grain coincide under `CalendarMonthResolver`, whose `PeriodId`
    *is* `"YYYY-MM"` (CONTRACTS.md §2). They are kept distinct in the enum because a
    paycheck-driven resolver would separate them, and a client asking for `month`
    deserves months from that resolver too.
    """
    value_of = _PERIOD_VALUE.get(metric)
    if value_of is None:
        raise _unsupported(
            metric,
            f"metric {metric.value!r} is not a per-period figure",
            {"period_metrics": sorted(m.value for m in _PERIOD_VALUE)},
        )
    return [
        ChartPoint(
            bucket=period.period_id, series=TOTAL_SERIES, value_minor=value_of(period)
        )
        for period in periods
    ]


def _grouped_period_points(
    state: State,
    metric: ChartMetric,
    group_by: ChartGroupBy,
    periods: Sequence[PeriodSummary],
) -> list[ChartPoint]:
    """`fixed_*` grouped by the obligation's own `category` or `payee`.

    `State.obligations` is the accrual view the projection already built — expected
    obligations expanded from `FixedCost` definitions, superseded where an
    `ObligationRaised` matched (PLAN.md §8.1). Summing it by period reproduces
    `fixed_due_minor` exactly, which is what makes this a breakdown of the period figure
    rather than a second opinion about it.
    """
    value_of = _OBLIGATION_VALUE.get(metric)
    if value_of is None or group_by not in (
        ChartGroupBy.CATEGORY,
        ChartGroupBy.PAYEE,
    ):
        raise _unsupported(
            metric,
            (
                f"metric {metric.value!r} cannot be grouped by {group_by.value!r} — "
                "State carries no such breakdown"
            ),
            {
                "groupable_metrics": sorted(m.value for m in _OBLIGATION_VALUE),
                "group_by": sorted(
                    (ChartGroupBy.CATEGORY.value, ChartGroupBy.PAYEE.value)
                ),
            },
        )

    wanted = {period.period_id for period in periods}
    series_of: Callable[[ObligationRow], str] = (
        (lambda row: row.category)
        if group_by is ChartGroupBy.CATEGORY
        else (lambda row: row.payee)
    )
    return _summed(
        (row.period_id, series_of(row), value_of(row))
        for row in state.obligations
        if row.period_id in wanted
    )


# --------------------------------------------------------------------------- stocks


def _balance_points(
    state: State,
    metric: ChartMetric,
    group_by: ChartGroupBy,
    periods: Sequence[PeriodSummary],
    state_at: Callable[[dt.date], State],
) -> list[ChartPoint]:
    """A balance per period, taken at each period's close.

    The close date is `end_date_exclusive - 1 day`, clamped to `state.as_of_date`: the
    in-progress period has not closed yet, and projecting past `as_of` would answer a
    question the caller did not ask. The clamp means the last bucket of an open period
    reports the balance as of today, which is the number a chart's rightmost point
    should show.
    """
    if metric is ChartMetric.SAVINGS_BALANCE and group_by is not ChartGroupBy.NONE:
        raise _unsupported(
            metric,
            "savings_balance is a single balance and cannot be grouped",
            {"group_by": [ChartGroupBy.NONE.value]},
        )
    if metric is ChartMetric.ACCOUNT_BALANCE and group_by not in (
        ChartGroupBy.NONE,
        ChartGroupBy.ACCOUNT,
    ):
        raise _unsupported(
            metric,
            f"account_balance cannot be grouped by {group_by.value!r}",
            {"group_by": [ChartGroupBy.NONE.value, ChartGroupBy.ACCOUNT.value]},
        )

    points: list[ChartPoint] = []
    for period in periods:
        close = min(period.end_date_exclusive - _ONE_DAY, state.as_of_date)
        at_close = state if close == state.as_of_date else state_at(close)
        if metric is ChartMetric.SAVINGS_BALANCE:
            points.append(
                ChartPoint(
                    bucket=period.period_id,
                    series=TOTAL_SERIES,
                    value_minor=at_close.savings.balance_minor,
                )
            )
            continue
        points.extend(_account_balance_points(period.period_id, at_close, group_by))
    return points


def _account_balance_points(
    bucket: str, at_close: State, group_by: ChartGroupBy
) -> list[ChartPoint]:
    """Signed account balances at one bucket.

    Ungrouped, the total is the **sum of the signed balances** — net worth, with
    liabilities negative (CONTRACTS.md §5.2). Summing `outstanding_minor` instead would
    add a liability to an asset as though both were positive, which is the exact shape
    of the double-count CLAUDE.md §1 warns about.
    """
    if group_by is ChartGroupBy.ACCOUNT:
        return [
            ChartPoint(
                bucket=bucket,
                series=account.account_id,
                value_minor=account.balance_minor,
            )
            for account in at_close.accounts
        ]
    return [
        ChartPoint(
            bucket=bucket,
            series=TOTAL_SERIES,
            value_minor=sum(
                account.balance_minor for account in at_close.accounts
            ),
        )
    ]


# -------------------------------------------------------------------------- cycles


def _cycle_points(
    state: State,
    metric: ChartMetric,
    group_by: ChartGroupBy,
    from_date: dt.date | None,
    to_date: dt.date | None,
) -> list[ChartPoint]:
    """`CYCLE` grain: one bucket per statement cycle, keyed by `cycle_id`.

    Only the three cycle-shaped metrics exist at this grain. A period figure has no
    cycle to sit in — statement cycles are per-account and need not align with periods
    at all (PLAN.md §7.4) — so asking for `income` by cycle is a question with no answer
    rather than a zero.
    """
    if metric not in (
        ChartMetric.INTEREST_CHARGED,
        ChartMetric.INTEREST_EARNED,
        ChartMetric.ACCOUNT_BALANCE,
    ):
        raise _unsupported(
            metric,
            f"metric {metric.value!r} has no statement-cycle grain",
            {
                "cycle_metrics": [
                    ChartMetric.INTEREST_CHARGED.value,
                    ChartMetric.INTEREST_EARNED.value,
                    ChartMetric.ACCOUNT_BALANCE.value,
                ]
            },
        )
    if group_by not in (ChartGroupBy.NONE, ChartGroupBy.ACCOUNT):
        raise _unsupported(
            metric,
            f"cycle-grain series cannot be grouped by {group_by.value!r}",
            {"group_by": [ChartGroupBy.NONE.value, ChartGroupBy.ACCOUNT.value]},
        )

    kinds = _kinds_by_account(state.accounts)
    cycles = [
        cycle
        for cycle in state.statement_cycles
        if _overlaps(cycle.start_date, cycle.end_date_exclusive, from_date, to_date)
        and _cycle_matches(metric, kinds.get(cycle.account_id))
    ]
    value_of = _cycle_value(metric)
    return _summed(
        (
            cycle.cycle_id,
            cycle.account_id if group_by is ChartGroupBy.ACCOUNT else TOTAL_SERIES,
            value_of(cycle),
        )
        for cycle in cycles
    )


def _cycle_period_points(
    state: State,
    metric: ChartMetric,
    group_by: ChartGroupBy,
    periods: Sequence[PeriodSummary],
) -> tuple[ChartPoint, ...]:
    """Interest at `PERIOD` / `MONTH` grain: cycles bucketed by their close date.

    A cycle belongs to the period containing its **close** — the day the statement was
    cut and the interest became real. Bucketing by the cycle's start would attribute a
    charge to the month the spending began rather than the month it was billed, and the
    two differ for every card whose close day is not the last of the month.
    """
    if group_by not in (ChartGroupBy.NONE, ChartGroupBy.ACCOUNT):
        raise _unsupported(
            metric,
            f"interest cannot be grouped by {group_by.value!r}",
            {"group_by": [ChartGroupBy.NONE.value, ChartGroupBy.ACCOUNT.value]},
        )

    kinds = _kinds_by_account(state.accounts)
    value_of = _cycle_value(metric)
    bounds = {
        period.period_id: (period.start_date, period.end_date_exclusive)
        for period in periods
    }
    triples: list[tuple[str, str, Minor]] = []
    for cycle in state.statement_cycles:
        if not _cycle_matches(metric, kinds.get(cycle.account_id)):
            continue
        close = cycle.end_date_exclusive - _ONE_DAY
        for period_id, (start, end_exclusive) in bounds.items():
            if start <= close < end_exclusive:
                series = (
                    cycle.account_id
                    if group_by is ChartGroupBy.ACCOUNT
                    else TOTAL_SERIES
                )
                triples.append((period_id, series, value_of(cycle)))
                break
    return _ordered(_summed(triples))


def _cycle_value(metric: ChartMetric) -> Callable[[StatementCycleSummary], Minor]:
    if metric is ChartMetric.ACCOUNT_BALANCE:
        return lambda cycle: cycle.close_balance_minor
    return lambda cycle: cycle.interest_minor


def _cycle_matches(metric: ChartMetric, kind: AccountKind | None) -> bool:
    """Whether a cycle on an account of `kind` contributes to `metric`.

    Interest charged is a liability's; interest earned is an asset's. The projection
    computes one figure per cycle either way — `StatementCycleSummary.interest_minor`
    does not carry a sign convention distinguishing them — so the account kind is what
    separates the two metrics, and an unknown account contributes to neither.
    """
    if kind is None:
        return False
    if metric is ChartMetric.INTEREST_CHARGED:
        return kind in _LIABILITY_KINDS
    if metric is ChartMetric.INTEREST_EARNED:
        return kind not in _LIABILITY_KINDS
    return True


def _kinds_by_account(accounts: Sequence[AccountBalance]) -> Mapping[str, AccountKind]:
    return {account.account_id: account.kind for account in accounts}


# ------------------------------------------------------------------------ internals


def _overlaps(
    start: dt.date,
    end_exclusive: dt.date,
    from_date: dt.date | None,
    to_date: dt.date | None,
) -> bool:
    """Whether `[start, end_exclusive)` intersects the inclusive window `[from, to]`.

    A bucket is included when any part of it falls in the window, so a request for a
    single day inside a month returns that month. Truncating instead would return an
    empty chart for a range narrower than one period, which reads as "no data".
    """
    if to_date is not None and start > to_date:
        return False
    if from_date is not None and end_exclusive <= from_date:
        return False
    return True


def _summed(triples: Iterable[tuple[str, str, Minor]]) -> list[ChartPoint]:
    """Collapse `(bucket, series, value)` triples, summing duplicates.

    Integer addition throughout — the values are `Minor` and stay `Minor`.
    """
    totals: dict[tuple[str, str], Minor] = {}
    for bucket, series, value in triples:
        totals[(bucket, series)] = totals.get((bucket, series), 0) + value
    return [
        ChartPoint(bucket=bucket, series=series, value_minor=value)
        for (bucket, series), value in totals.items()
    ]


def _ordered(points: Sequence[ChartPoint]) -> tuple[ChartPoint, ...]:
    """`(bucket, series)` order — total and stable, so a chart never reshuffles."""
    return tuple(sorted(points, key=lambda point: (point.bucket, point.series)))


def _unsupported(
    metric: ChartMetric, message: str, details: dict[str, object]
) -> AppError:
    return AppError(
        ErrorCode.VALIDATION_FAILED,
        message,
        {"metric": metric.value, **details},
    )
