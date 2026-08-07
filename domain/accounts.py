"""Balance folding, statement-cycle folding, and obligation status derivation.

Owned by `module/domain-accounts` (PLAN.md §13.2). Pure: no I/O, no clock, no DB.

`AccountBalance` and `StatementCycleSummary` are declared here rather than in
`domain/projection.py`, even though CONTRACTS.md presents them in §5 alongside `State`.
They have to be: `fold_account_balances` and `fold_statement_cycles` return them, and
`domain/projection.py` imports those functions — so declaring them there would make
`accounts -> projection -> accounts`, and a cycle is a build failure (CLAUDE.md §3.1).
This is the same reasoning that puts `ExpectedObligation` in `domain/definitions.py`
(CONTRACTS.md §8.5). No field or signature changes; only the file.

Statement cycles CANNOT be computed in isolation. They fold strictly in order, each
carrying forward the closing balance and whether the statement was paid in full by its
due date, because that decides the next cycle's grace (PLAN.md §7.4).

Sign convention, applied uniformly to every balance in this module
------------------------------------------------------------------
A *balance* is SIGNED and negative means liability (CONTRACTS.md §5.2). An
*outstanding* is the absolute amount owed. `AccountBalance.balance_minor` and
`StatementCycleSummary.close_balance_minor` are both balances and both signed, so a
card carrying debt closes its statement at a negative number; `outstanding_minor` is
the non-negative face of the same figure. `core/interest.py::interest_for_cycle`
refuses a negative input precisely so that handing it a liability's signed balance is
impossible rather than merely wrong, so everything here converts through
`_accrual_base_minor` first.

Recognition vs. movement (PLAN.md §1)
-------------------------------------
Nothing in this module touches discretionary. Balances move when money moves;
recognition is the projection's business. That split is why `SavingsDrawn` has no
effect here (see `fold_account_balances`) and why `budget_timing` is never read: the
mode changes when a card purchase reduces discretionary, never what the card owes and
never a single figure computed below (PLAN.md §6.4).
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import Sequence
from typing import NamedTuple

from pydantic import BaseModel

from core.interest import interest_for_cycle
from core.periods import clamp_day_to_month
from core.types import (
    MONEY_MODEL_CONFIG,
    AccountKind,
    Bps,
    CycleId,
    Minor,
    ObligationStatus,
)
from domain.definitions import Account, resolve_version
from domain.events import (
    AccountOpeningBalance,
    Event,
    ExpenseRecorded,
    GiftReceived,
    IncomeReceived,
    InterestCharged,
    InterestEarned,
    PaymentMade,
    TransferMade,
)


class AccountBalance(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    account_id: str
    name: str
    kind: AccountKind
    balance_minor: Minor  # SIGNED; negative == liability
    outstanding_minor: Minor | None  # abs(balance) for liabilities; None for assets
    apr_bps: Bps
    cumulative_interest_minor: Minor


class StatementCycleSummary(BaseModel):
    model_config = MONEY_MODEL_CONFIG

    cycle_id: CycleId
    account_id: str
    start_date: dt.date
    end_date_exclusive: dt.date
    close_balance_minor: Minor
    interest_minor: Minor
    is_estimate: bool  # False once an InterestCharged pins the cycle
    paid_in_full_by_due_date: bool
    grace_applied: bool  # True == interest waived this cycle


#: The kinds whose balance is negative when money is owed. Everything else is an asset:
#: its balance is positive when it holds money, and it reports no `outstanding_minor`.
_LIABILITY_KINDS = frozenset({AccountKind.CREDIT_CARD, AccountKind.LOAN})

#: Business dates are half-open everywhere in this codebase, so "the day after" is the
#: only date arithmetic below.
_ONE_DAY = dt.timedelta(days=1)

#: The largest day-of-month `clamp_day_to_month` accepts. Asking it for day 31 of any
#: month is how this module obtains that month's last day without doing calendar
#: arithmetic of its own (PLAN.md §4.1: nothing outside `core/periods.py` may assume
#: months).
_LAST_POSSIBLE_DAY = 31


def derive_obligation_status(
    amount_minor: Minor,
    paid_minor: Minor,
) -> ObligationStatus:
    """Postconditions:
        paid == 0            -> UNPAID
        0 < paid < amount    -> PARTIALLY_PAID
        paid == amount       -> PAID
        paid > amount        -> OVERPAID   (permitted; raises a warning, not an error)

    Total over every pair of ints, including the two the contract does not name:

    * `paid < 0` reads as UNPAID. A negative total paid is a net refund, which is not
      a payment; calling it PARTIALLY_PAID would claim progress that has not happened.
    * `amount == 0` with `paid == 0` reads as UNPAID rather than PAID, because the
      contract lists `paid == 0 -> UNPAID` first and a zero-amount obligation has had
      nothing paid against it. `paid > 0` against a zero amount is still OVERPAID.

    OVERPAID is a legitimate state, never an error: the projection emits
    `OBLIGATION_OVERPAID` as a `Warning` beside it (CONTRACTS.md §7.2). Backdating
    means today's impossible state is tomorrow's ordinary one, so this function
    classifies and never rejects.
    """
    if paid_minor <= 0:
        return ObligationStatus.UNPAID
    if paid_minor > amount_minor:
        return ObligationStatus.OVERPAID
    if paid_minor == amount_minor:
        return ObligationStatus.PAID
    return ObligationStatus.PARTIALLY_PAID


def _account_delta(event: Event, account_id: str) -> Minor:
    """The signed effect of `event` on `account_id`'s balance; 0 if it does not touch it.

    One function decides every balance movement in this module, so `fold_account_balances`
    and `fold_statement_cycles` cannot drift apart on what a card purchase does.

    Which events move a balance, and why the rest do not:

    * `IncomeReceived`, `GiftReceived`, `InterestEarned` credit the named account.
    * `AccountOpeningBalance` is already signed, so it is added as-is — that is what
      lets one event type cover both "opened checking with $500" and a loan
      disbursement (a negative, i.e. a liability).
    * `ExpenseRecorded` and `PaymentMade` debit the account they were paid from. On a
      card that debit *is* the liability growing, because a liability's balance is
      negative. A negative `ExpenseRecorded` is a refund and correctly credits.
    * `InterestCharged` debits: interest owed is more owed.
    * `TransferMade` moves both sides, which is exactly why paying a card bill is
      budget-neutral (PLAN.md §1). A transfer to and from one account is impossible by
      construction — `TransferMade` rejects it (TRANSFER_SAME_ACCOUNT).
    * `ObligationRaised` is an accrual, not a movement: raising a bill does not move
      money, paying it does.
    * `SavingsDrawn` names no account, and that is not an omission — it is a
      *budgetary* top-up of discretionary from savings (PLAN.md §6.2). When cash
      actually moves between the two accounts, the ledger carries a `TransferMade` for
      it, and folding the draw here as well would debit savings twice for one
      movement. `SavingsSummary.balance_minor` is the projection's budget-side view
      and counts draws by its own documented formula (CONTRACTS.md §5.2).
    * `EventVoided` is filtered before the fold and never reaches here (precondition).
    """
    if isinstance(
        event, (IncomeReceived, GiftReceived, AccountOpeningBalance, InterestEarned)
    ):
        return event.amount_minor if event.account_id == account_id else 0
    if isinstance(event, (ExpenseRecorded, PaymentMade, InterestCharged)):
        return -event.amount_minor if event.account_id == account_id else 0
    if isinstance(event, TransferMade):
        if event.from_account_id == account_id:
            return -event.amount_minor
        if event.to_account_id == account_id:
            return event.amount_minor
    return 0


def _recorded_interest(event: Event, account_id: str) -> Minor:
    """The interest `event` records against `account_id`, as a magnitude; else 0.

    Charged and earned both count positively: `cumulative_interest_minor` answers "how
    much interest has this account seen", and its sign would otherwise duplicate the
    account kind. The balance effect of the same event carries the direction and is
    `_account_delta`'s job.
    """
    if isinstance(event, (InterestCharged, InterestEarned)):
        return event.amount_minor if event.account_id == account_id else 0
    return 0


def _implied_transfer_delta(
    transfer: tuple[dt.date, str, str, Minor],
    account_id: str,
) -> Minor:
    """The signed effect of one implied savings transfer on `account_id`.

    The tuple is `(date, from_account_id, to_account_id, amount_minor)` and its amount
    is signed: a *negative* period allocation reverses the direction, draining savings
    back to checking during a shortfall (PLAN.md §6.2). Negating for the `from` side
    and adding for the `to` side handles both directions without a special case, which
    is why no branch here looks at the sign.
    """
    _date, from_account_id, to_account_id, amount_minor = transfer
    if from_account_id == account_id:
        return -amount_minor
    if to_account_id == account_id:
        return amount_minor
    return 0


def _latest_version(versions: Sequence[Account], entity_id: str) -> Account:
    """The last-starting version of `entity_id`. Precondition: at least one exists.

    Keyed by `(effective_from, recorded_at, version_id)` — the same total order
    `domain/definitions.py` uses for versions — so the answer does not depend on the
    order the caller's rows arrived in.
    """
    return max(
        (v for v in versions if v.entity_id == entity_id),
        key=lambda v: (v.effective_from, v.recorded_at, str(v.version_id)),
    )


def fold_account_balances(
    events: Sequence[Event],
    accounts: Sequence[Account],
    implied_transfers: Sequence[tuple[dt.date, str, str, Minor]],
    as_of_date: dt.date,
) -> Sequence[AccountBalance]:
    """Fold every balance-affecting event plus the projection's implied savings
    transfers (PLAN.md §6.2).

    Preconditions:
        events are sorted by (date, recorded_at, event_id)
        events are already void-filtered

    Postconditions:
        balance_minor is SIGNED; negative == liability
        outstanding_minor == abs(balance) for liability kinds, else None
        pure — no mutation of inputs, no accumulator reassignment across the fold

    Decisions taken where the contract is silent:

    * **`accounts` is the whole version history**, exactly as `Definitions.accounts`
      holds it (CONTRACTS.md §4). One row comes back per distinct `entity_id`, sorted
      by `account_id` so the result is independent of input order.
    * **Descriptive fields are resolved at `as_of_date`** — `name`, `kind`, `apr_bps`
      are read off the version effective then, because the balance is being reported
      as of that date. An account whose versions have all been closed out still
      reports, using its last version; dropping it would make money vanish from the
      answer.
    * **Events dated after `as_of_date` are excluded**, as are implied transfers, so a
      future-dated event does not appear in today's balance. This is the only thing
      `as_of_date` is used for beyond version resolution, and it is what makes
      `fold_account_balances(..., t)` answer as of `t` rather than as of the ledger's
      end.
    * **Events naming an unknown account are ignored.** An unknown `account_id` is a
      write-time error (`UNKNOWN_ACCOUNT`, CONTRACTS.md §7.1); by projection time the
      only way to see one is a definition that was never written, and inventing a row
      for it would put an account in `State` that no definition describes.
    * **`cumulative_interest_minor` counts recorded interest events only** — the
      `InterestCharged` / `InterestEarned` actually in the ledger, as magnitudes. It
      does not include the projection's *estimates*: this function is not given the
      statement cycles, and a figure that mixed recorded actuals with estimates would
      be reconcilable against nothing.

    The fold is expressed as a sum per account over the events rather than as a running
    accumulator, so there is no accumulator to reassign and no partially-built row to
    mutate (CLAUDE.md §4.2).
    """
    in_window = tuple(event for event in events if event.date <= as_of_date)
    transfers_in_window = tuple(
        transfer for transfer in implied_transfers if transfer[0] <= as_of_date
    )
    account_ids = sorted({account.entity_id for account in accounts})

    return tuple(
        _build_balance(account_id, accounts, in_window, transfers_in_window, as_of_date)
        for account_id in account_ids
    )


def _build_balance(
    account_id: str,
    accounts: Sequence[Account],
    events: Sequence[Event],
    implied_transfers: Sequence[tuple[dt.date, str, str, Minor]],
    as_of_date: dt.date,
) -> AccountBalance:
    """One account's row. `events` and `implied_transfers` are already window-filtered."""
    resolved = resolve_version(accounts, account_id, as_of_date)
    version = resolved if resolved is not None else _latest_version(accounts, account_id)

    balance_minor = sum(
        _account_delta(event, account_id) for event in events
    ) + sum(
        _implied_transfer_delta(transfer, account_id)
        for transfer in implied_transfers
    )
    is_liability = version.kind in _LIABILITY_KINDS

    return AccountBalance(
        account_id=account_id,
        name=version.name,
        kind=version.kind,
        balance_minor=balance_minor,
        outstanding_minor=abs(balance_minor) if is_liability else None,
        apr_bps=version.apr_bps,
        cumulative_interest_minor=sum(
            _recorded_interest(event, account_id) for event in events
        ),
    )


def _accrual_base_minor(kind: AccountKind, balance_minor: Minor) -> Minor:
    """The non-negative amount interest accrues on, given a SIGNED balance.

    A liability accrues on what is owed, which is the negation of its balance; an asset
    accrues on what it holds. Either way the answer is clamped at zero, because a card
    in credit earns no negative interest and an overdrawn checking account accrues
    none — PLAN.md §7.1 states both, and `interest_for_cycle` refuses a negative input
    rather than sign-mirroring it.
    """
    owed_minor = -balance_minor if kind in _LIABILITY_KINDS else balance_minor
    return owed_minor if owed_minor > 0 else 0


def _interest_balance_delta(kind: AccountKind, interest_minor: Minor) -> Minor:
    """The effect of `interest_minor` on a SIGNED balance of an account of `kind`.

    Interest on a liability is more owed, so the balance falls; interest on an asset is
    credited to it, so the balance rises.
    """
    return -interest_minor if kind in _LIABILITY_KINDS else interest_minor


def _payment_due_date(close_date: dt.date, payment_due_day: int | None) -> dt.date | None:
    """The date the statement closing on `close_date` must be paid by; None if unknown.

    The due day is a day-of-month, so the due date is the first date STRICTLY AFTER the
    close date carrying that day — the same month when the due day is still to come,
    otherwise the next one. That is how a card closing on the 15th and due on the 10th
    lands in the following month, and it gives every combination of the two days a grace
    window of at least one day. Strictly-after is what makes a card whose due day equals
    its close day behave like a real one: the statement closing on the 31st and due on
    the 31st is due on the *next* 31st (28 February, clamped), not the instant it
    closed. A statement payable on the day it closes would leave no window to pay it in,
    so no cycle after the first could ever be graced.

    Both days are clamped into short months by `core/periods.py::clamp_day_to_month`, so
    "due on the 31st" is due on 28 February. The first day of the following month is
    obtained by asking for that month's day 31 and adding a day rather than by
    incrementing a month here: no month arithmetic outside `core/periods.py`
    (PLAN.md §4.1).
    """
    if payment_due_day is None:
        return None
    same_month = clamp_day_to_month(
        close_date.year, close_date.month, payment_due_day
    )
    if same_month > close_date:
        return same_month
    next_month_day = (
        clamp_day_to_month(close_date.year, close_date.month, _LAST_POSSIBLE_DAY)
        + _ONE_DAY
    )
    return clamp_day_to_month(
        next_month_day.year, next_month_day.month, payment_due_day
    )


class _CycleCarry(NamedTuple):
    """What one cycle hands the next (PLAN.md §7.4). Immutable by construction.

    * `summaries` — the rows built so far, in order.
    * `estimated_interest_minor` — the cumulative balance effect of interest this fold
      *estimated* for earlier cycles. Estimates exist nowhere else, so a later cycle
      would open on a balance short by exactly this much. Interest that was **pinned**
      by a recorded `InterestCharged` is deliberately absent: that event is in the
      ledger and already moves the balance through `_account_delta`, on its own date.
      Adding it here as well is the double-count this split exists to prevent.
    * `paid_in_full_by_due_date` — the previous statement's, which decides this cycle's
      grace.
    """

    summaries: tuple[StatementCycleSummary, ...]
    estimated_interest_minor: Minor
    paid_in_full_by_due_date: bool


def fold_statement_cycles(
    account: Account,
    account_versions: Sequence[Account],
    events: Sequence[Event],
    cycles: Sequence[tuple[CycleId, dt.date, dt.date]],
) -> Sequence[StatementCycleSummary]:
    """Fold cycles IN ORDER, carrying close balance and paid-in-full status forward.

    Cycles cannot be computed in isolation (PLAN.md §7.4).

    Preconditions:
        cycles ascending, contiguous, non-overlapping
        events sorted and void-filtered

    Postconditions:
        APR resolved at each cycle's START date
        grace_applied is True iff the PREVIOUS cycle was paid in full by its due
            date; when True, interest_minor == 0
        an InterestCharged for a cycle PINS it: interest_minor is the recorded
            amount and is_estimate is False
        a pinned cycle's figure is independent of any backdated event within it
        interest is identical under both BudgetTiming modes — timing affects only
            budget recognition, never computation (PLAN.md §6.4)

    Mode-invariance is structural rather than defended: `budget_timing` is never read
    in this file. There is no branch for it to take.

    Decisions taken where the contract is silent:

    * **`close_balance_minor` is SIGNED**, like every other field in this codebase
      whose name says "balance" — a card carrying $1,200 closes at `-120_000`, and the
      $1,200 that interest accrues on is `_accrual_base_minor` of it. It is the balance
      *before* this cycle's own interest, which is what "interest is computed on the
      statement-close balance" (PLAN.md §7.1) requires, and this cycle's interest then
      opens the next one.
    * **Version resolution falls back to `account`.** The APR and the payment due day
      come from the version effective at the cycle's START date (PLAN.md §7.4: a rate
      change effective mid-cycle applies from the following cycle, so a past cycle's
      interest never moves because of a rate edit). When no version of this account is
      effective then — a cycle running from a genesis earlier than the account's first
      version — `account` itself is used, which is both total and the answer a caller
      passing a single version expects.
    * **A card's first cycle is graced.** The carry starts at "the previous statement
      was paid in full", vacuously: there was no previous statement, so there is no
      unpaid one. Any other seed would charge interest on an opening balance nobody has
      yet had the chance to pay, and it would falsify the grace property (CLAUDE.md
      §5.1 property 11) for the first cycle of every card. Grace is a statement rule,
      so it never reaches an asset account: a savings account earns on its very first
      period like any other.
    * **A statement is paid in full when credits between the close and the due date
      cover what was owed at the close.** Credits are the positive balance movements in
      that window — a `TransferMade` into the card, which is exactly how the contract
      says a card bill is paid (PLAN.md §1). Purchases made in the window are ignored:
      they belong to the next statement, not this one. Nothing owed at the close is
      paid in full trivially.
    * **Grace never applies to an asset account.** `paid_in_full_by_due_date` is
      reported False for one, because a savings account has no statement to pay, and a
      True there would waive its interest forever.
    * **A pin outranks grace.** The two postconditions above can disagree only when the
      ledger records interest for a cycle this fold believes was graced — the bank
      charged anyway. PLAN.md §7.3 settles it: when the tool and the bank disagree, the
      bank is right, so the recorded amount is reported. `grace_applied` then reads
      False, because its own contract is "True == interest waived this cycle"
      (CONTRACTS.md §5.2) and claiming a waiver beside a non-zero charge would be a
      statement about the world that is not true. A recorded charge of zero is a
      recorded waiver and leaves `grace_applied` True.
    * **The last recorded interest event for a cycle wins.** Two actuals for one cycle
      are a correction entered twice; later supersedes earlier, in ledger order, which
      is the same rule `supersede_expected` applies (CONTRACTS.md §8.5).
    """
    account_id = account.entity_id
    versions = tuple(account_versions)

    # Balance movements for this account, as (date, delta) pairs. Cycle membership is
    # decided by the business date alone -- `recorded_at` is audit and tie-break only
    # (CONTRACTS.md §3.1), and it has already done its tie-breaking in the sort.
    movements = tuple(
        (event.date, _account_delta(event, account_id))
        for event in events
        if _account_delta(event, account_id) != 0
    )

    # cycle_id -> recorded interest. A later entry overwrites an earlier one and the
    # events are sorted, so the last actual in ledger order is what remains.
    pins: dict[CycleId, Minor] = {
        event.cycle_id: event.amount_minor
        for event in events
        if isinstance(event, (InterestCharged, InterestEarned))
        and event.account_id == account_id
    }

    def step(
        carry: _CycleCarry, cycle: tuple[CycleId, dt.date, dt.date]
    ) -> _CycleCarry:
        cycle_id, start_date, end_date_exclusive = cycle

        resolved = resolve_version(versions, account_id, start_date)
        version = resolved if resolved is not None else account
        kind = version.kind

        # Everything that happened before the cycle closed, plus the interest this fold
        # estimated for earlier cycles. Summing from the beginning each time rather than
        # accumulating a running balance means an event dated before the first cycle
        # still lands, and there is no accumulator to reassign (CLAUDE.md §4.2).
        close_balance_minor = (
            sum(delta for date, delta in movements if date < end_date_exclusive)
            + carry.estimated_interest_minor
        )

        # Grace is a statement rule and applies to liabilities only. An asset accrues
        # on its balance at every period close (PLAN.md §7.1), and `_is_paid_in_full`
        # already answers False for one -- this guard is what stops the vacuously-true
        # seed below from waiving a savings account's very first period.
        graced = carry.paid_in_full_by_due_date and kind in _LIABILITY_KINDS
        pinned_minor = pins.get(cycle_id)

        if pinned_minor is not None:
            interest_minor = pinned_minor
            is_estimate = False
            grace_applied = graced and pinned_minor == 0
            # The pinning event moves the balance itself, on its own date.
            estimated_interest_minor = carry.estimated_interest_minor
        else:
            interest_minor = (
                0
                if graced
                else interest_for_cycle(
                    _accrual_base_minor(kind, close_balance_minor),
                    version.apr_bps,
                    (end_date_exclusive - start_date).days,
                )
            )
            is_estimate = True
            grace_applied = graced
            estimated_interest_minor = carry.estimated_interest_minor + (
                _interest_balance_delta(kind, interest_minor)
            )

        paid_in_full = _is_paid_in_full(
            kind=kind,
            close_balance_minor=close_balance_minor,
            end_date_exclusive=end_date_exclusive,
            payment_due_day=version.payment_due_day,
            movements=movements,
        )

        summary = StatementCycleSummary(
            cycle_id=cycle_id,
            account_id=account_id,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            close_balance_minor=close_balance_minor,
            interest_minor=interest_minor,
            is_estimate=is_estimate,
            paid_in_full_by_due_date=paid_in_full,
            grace_applied=grace_applied,
        )
        return _CycleCarry(
            summaries=carry.summaries + (summary,),
            estimated_interest_minor=estimated_interest_minor,
            paid_in_full_by_due_date=paid_in_full,
        )

    return functools.reduce(step, cycles, _CycleCarry((), 0, True)).summaries


def _is_paid_in_full(
    *,
    kind: AccountKind,
    close_balance_minor: Minor,
    end_date_exclusive: dt.date,
    payment_due_day: int | None,
    movements: Sequence[tuple[dt.date, Minor]],
) -> bool:
    """Was the statement closing this cycle settled by its due date?

    See `fold_statement_cycles` for why an asset account always answers False and why
    only credits inside the window count.
    """
    if kind not in _LIABILITY_KINDS:
        return False
    due_date = _payment_due_date(end_date_exclusive - _ONE_DAY, payment_due_day)
    if due_date is None:
        return False
    owed_minor = _accrual_base_minor(kind, close_balance_minor)
    credited_minor = sum(
        delta
        for date, delta in movements
        if end_date_exclusive <= date <= due_date and delta > 0
    )
    return credited_minor >= owed_minor
