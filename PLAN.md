# PLAN.md — Architecture and build plan

Single-user personal budgeting tool. Append-only event ledger, pure projection.

This document is the *why*. `CONTRACTS.md` is the *what* (types and signatures).
`CLAUDE.md` is the *how* (conventions and prohibitions). Implementation agents should
read this file once for orientation, then work from `CONTRACTS.md`.

---

## 1. The recognition principle

Read this before anything else. It is the rule most likely to be implemented wrong,
because it is the one place where "money moved" and "budget changed" come apart.

> **An outflow affects the budget exactly once, at the moment the underlying expense or
> obligation is *recognized* — not when the money moves.**

Consequences, all of which are load-bearing:

| Action | Budget effect | Balance effect |
|---|---|---|
| Card purchase (`AT_PURCHASE` mode) | discretionary −amount, at purchase | card liability +amount |
| Paying the card bill | **none** — already recognized at purchase | checking −amount, card +amount |
| Loan principal payment | fixed cost, off the top | checking −amount, loan liability +principal |
| Loan disbursement | **not income** | loan liability +principal, checking +principal |
| Transfer between own accounts | **none** | both sides move |
| Card interest charged | expense (see §6.4 for the mode caveat) | card liability +interest |
| Interest earned on savings | **not allocatable income** | savings +interest |

The recurring failure mode is double-counting: recording a card purchase as an expense
*and* the card statement payment as a fixed cost. The `TransferMade` event exists
specifically to make the payment side non-budgetary. There are property tests for this
in both card timing modes; see `CLAUDE.md` §5.

---

## 2. Scope

### 2.1 In scope

Income and gift tracking; fixed obligations with partial payments; a configurable
allocation policy; discretionary spend tracking; accounts (checking, savings, credit
card, loan) with balances; interest on all four account kinds; receipt upload; a
read-only API serving a tabular ledger view, chart aggregates, and uploads.

### 2.2 Explicit non-goals

- **Multi-user, auth, tenancy.** Single user, single ledger, no login.
- **Multi-currency.** One currency, fixed at 2 decimal places.
- **Statement reconciliation.** The tool derives balances from events. It does not
  import or match against real statements, so its checking balance is only as right as
  the events entered. Divergence is expected and is not an error condition.
- **Amortization schedules.** A loan's payment is a `FixedCost`; the tool tracks the
  outstanding balance but does not generate or validate a payment schedule.
- **Sinking funds.** A $1,200 annual premium lands entirely in the period it is due and
  will distort that period. Known limitation, deliberately deferred.
- **Refunds/returns** as a distinct concept. Model as a negative-amount expense.
- **Live bank/card aggregation.** See §9 — deferred, with the seam preserved.

### 2.3 Scope history

The original brief was an allocation calculator over a ledger. Planning expanded it to
include loans and credit cards, which required accounts, which required interest. The
fold architecture absorbed this, but two properties narrowed as a result — see §6.3.
This is worth knowing because several design choices below were made for the smaller
system and re-justified for the larger one.

---

## 3. Data flow

```
                  ┌────────────────────────────────────────────┐
   HTTP POST      │ ingestion/                                 │
   receipt / ───► │  1. normalize to canonical event payload   │
   manual entry   │  2. compute dedupe_key (content hash for   │
                  │     receipts, natural key otherwise)       │
                  │  3. INSERT ... ON CONFLICT DO NOTHING      │
                  │     -> re-upload is a no-op, returns       │
                  │        the existing event, HTTP 200        │
                  └────────────────┬───────────────────────────┘
                                   │ append only, never UPDATE/DELETE
                                   ▼
                  ┌────────────────────────────────────────────┐
                  │ persistence/   events table (immutable)    │
                  │                definitions tables          │
                  │                (versioned, effective-dated)│
                  └────────────────┬───────────────────────────┘
                                   │ read ALL events + definitions
                                   ▼
                  ┌────────────────────────────────────────────┐
                  │ domain/projection.py                       │
                  │  project(events, definitions, as_of_date)  │
                  │  PURE: no I/O, no clock, no DB, no mutation│
                  │   a. filter voided                         │
                  │   b. sort (date, recorded_at, event_id)    │
                  │   c. expand FixedCost -> expected obligs   │
                  │   d. fold statement cycles in order        │
                  │   e. fold period allocation                │
                  │        -> State                            │
                  └────────────────┬───────────────────────────┘
                                   ▼
                  ┌────────────────────────────────────────────┐
                  │ api/  serialize State -> response DTOs     │
                  │       /state  /ledger  /charts/series      │
                  └────────────────────────────────────────────┘
```

**Every read recomputes from genesis.** There is no cached state, no materialized
balance column, no incremental update path. A backdated receipt entered today changes
the answer for every period after it, automatically, because nothing was ever stored
that could go stale.

This is affordable: a single user generating 200 events/month accumulates ~24k events
per decade. Folding that is milliseconds. If it ever stops being affordable the answer
is a snapshot cache keyed by `(max(recorded_at), as_of_date)` — a pure memoization that
does not change semantics. Do not build it now.

---

## 4. Period and timezone semantics

### 4.1 Periods

A period is a **calendar month**, half-open: `[first day, first day of next month)`.
Period id is `"YYYY-MM"`. An event belongs to the period containing its business `date`
— for obligations, its `due_date`.

Period boundaries are pure `datetime.date` comparisons. There is no time component on
any business date, so there is no midnight ambiguity and no DST edge case.

`PeriodResolver` is a protocol; `CalendarMonthResolver` is the only implementation
built. Paycheck-driven periods (a new period opening at each `IncomeReceived`) are a
future second resolver. Nothing outside `core/periods.py` may assume months — that is
what keeps the swap cheap.

### 4.2 Timezone

- **Business dates are timezone-free.** `datetime.date`, no time, no zone. A receipt
  dated 2026-03-31 is in period 2026-03 regardless of where anyone is standing.
- **One configured zone, used in one place.** `BUDGET_TZ` (IANA, default
  `America/Los_Angeles`) is read at the **API boundary only**, to turn "now" into a
  default `as_of_date` when the caller omits one.
- **The projection never reads a clock.** `as_of_date` is always an explicit argument.
  This is what makes the projection testable and time-travel queries free.
- `recorded_at` is a UTC instant stored per event for audit and deterministic
  tie-breaking. It is *not* the business date and never affects period membership.

Ledger ordering is `(date, recorded_at, event_id)` — total and stable.

---

## 5. Rounding policy

### 5.1 Rule

`split_bps(total, buckets)` where `buckets` is an ordered sequence of `(name, bps)` and
`sum(bps) == 10000`:

1. Work on `abs(total)`.
2. Each bucket receives `abs(total) * bps // 10000` (floor).
3. Compute `leftover = abs(total) - sum(shares)`. It is strictly less than the number of
   buckets.
4. Distribute leftover minor units **one at a time**, in descending order of fractional
   remainder `(abs(total) * bps) % 10000`. Ties break by **declared bucket order**
   (savings is declared before discretionary).
5. Reapply `sign(total)` to every share.

The sum is exact **by construction** — step 3 measures the shortfall and step 4
distributes all of it. The invariant is not something the implementation checks
afterward; it cannot fail.

Working on the absolute value makes rounding symmetric about zero:
`split(-n) == [-x for x in split(n)]`. This matters because allocatable income can be
negative (§6.1), and floor division on negatives would otherwise bias one bucket.

### 5.2 Worked example

`allocatable_income = 100_001` ($1,000.01), policy 50/50:

```
abs(total) = 100001

savings        = 100001 * 5000 // 10000 = 50000    remainder (100001*5000) % 10000 = 5000
discretionary  = 100001 * 5000 // 10000 = 50000    remainder                        = 5000

leftover = 100001 - (50000 + 50000) = 1
remainders tie at 5000 -> declared order -> savings takes the unit

savings       = 50001
discretionary = 50000
                -------
                100001   == allocatable_income   ✓
```

Negative case, `allocatable_income = -100_001`: shares are `-50001` and `-50000`. Same
magnitudes, sign reapplied, still exact.

### 5.3 The top-level invariant

```
fixed_due_minor
  + savings_allocated_minor
  + discretionary_allocated_minor
  == allocatable_income_minor
```

Holds exactly, for every period, including when `allocatable_income` is negative and
including when `fixed_due` exceeds it. Fixed comes off the top; `split_bps` divides the
exact remainder; the remainder plus fixed is the whole.

---

## 6. Allocation semantics

### 6.1 Insufficient income

When fixed costs exceed income, `allocatable_income − fixed_due` is negative. That
negative remainder is split by policy exactly as a positive one would be, so **savings
and discretionary both go negative**. Nothing is clamped.

Rejected alternative: clamping to zero and reporting a separate `shortfall` field. It
keeps every displayed number non-negative, but it needs a special case in the split, a
second invariant for the clamped branch, and it hides that you are 50/50 short across
two buckets rather than short in one place. Signed arithmetic with one invariant is
smaller and harder to get wrong.

### 6.2 Savings

Savings is **cumulative across all periods**; discretionary **resets** each period.
Leftover discretionary is not carried forward.

Allocation to savings **implies a transfer**: each period's savings allocation is a
derived checking→savings movement, so the budget figure and the account balance are
equal by construction. There is no "budgeted vs. actually moved" gap.

- The implied transfer is **derived inside the projection and never persisted**. The
  projection cannot write events; it computes the movement as part of the fold.
- It posts on the **period's last day**, in one movement. The in-progress period's
  allocation is therefore not yet in the savings balance, and is reported separately as
  `pending_savings_allocation_minor`.
- A **negative** allocation reverses the transfer's direction — savings drains to
  checking automatically during a shortfall.

Rejected alternative: posting the implied transfer proportionally as each income event
arrives. More cash-flow-accurate, and it would make mid-period interest slightly more
correct, but it multiplies the number of derived movements and interacts badly with
backdated income. Period-close posting is one movement per period per account and is
trivially deterministic.

`SavingsDrawn` therefore means a **deliberate top-up of discretionary from savings**,
beyond the automatic shortfall drain. A draw exceeding the balance available on that
date raises a warning in `State` and is **never rejected** — backdating legitimately
reorders events, so a draw that looks overdrawn today may be fine once an earlier
income event arrives tomorrow.

### 6.3 What the interest expansion narrowed

Two properties held for the original design and hold more weakly now. Both are stated
here rather than discovered later:

1. **Period independence is now a property of allocation only.** Allocation for
   2026-03 depends on nothing outside 2026-03. But statement *cycles* are chained: the
   grace-period rule makes each cycle depend on whether the previous one was paid in
   full by its due date. State as a whole is not period-independent.

2. **"Order independence" must be stated precisely.** *Ingestion*-order independence
   holds: shuffling the arrival order of the same event set yields identical State,
   because events are sorted before folding. That is **not** the same as immunity to
   backdating. A backdated card expense changes a past cycle's close balance, hence its
   interest, hence the opening balance of every later cycle. This cascade is correct
   under rebuild-from-scratch, and it is deterministic and pure — but it is real, and it
   means one late receipt can change numbers you already looked at.

### 6.4 Credit card timing

Per-account flag `budget_timing`, default `AT_PURCHASE`.

- **`AT_PURCHASE`** — the card purchase reduces discretionary at purchase. Paying the
  statement is a pure transfer. Card interest *is* separately budget-relevant, since it
  was never recognized anywhere else.
- **`AT_STATEMENT_PAYMENT`** — purchases do not touch discretionary; the statement
  payment does, for its full amount. Card interest is **not** separately budget-relevant
  in this mode, because the payment amount already contains it. Charging it again is the
  double-count this flag most easily produces.

`AT_PURCHASE` is the default because a single bill covering two months of spending
otherwise lands entirely in one period and distorts it.

**Interest computation is identical in both modes.** `budget_timing` affects only
whether interest additionally reduces discretionary. It never affects whether interest
is computed, whether it can be overridden, or the card's outstanding balance.

---

## 7. Interest policy

### 7.1 Rule

Integer-only, floor division, actual/365 day count, no compounding within a cycle:

```
interest_minor = balance_minor * apr_bps * cycle_days // (10000 * 365)
```

- **Credit cards** — `balance_minor` is the **statement-close balance**. Interest is
  zero when the previous statement was paid in full by its due date (grace period).
- **Asset accounts** (checking, savings) — `balance_minor` is the balance at period
  close; interest is credited to that same account and is **not allocatable income**.

Rejected alternative: average daily balance. More accurate for anyone who pays down
mid-cycle, and it is what most real cards use — but it requires materializing a daily
balance series per account, which is a large jump in both computation and test surface
for an estimate that is superseded by the actual figure anyway (§7.3).

### 7.2 Worked example

Card, `apr_bps = 2199` (21.99% APR), statement-close balance `120_000` ($1,200.00),
31-day cycle, previous statement not paid in full:

```
120000 * 2199              = 263_880_000
263_880_000 * 31           = 8_180_280_000
10000 * 365                = 3_650_000
8_180_280_000 // 3_650_000 = 2241        (exact quotient 2241.17..., floored)

=> interest 2241  ($22.41)
```

Savings, `apr_bps = 450` (4.50%), balance `500_000` ($5,000.00), 30-day period:

```
500000 * 450 * 30 // 3_650_000 = 1849    ($18.49)

savings balance      500_000 -> 501_849
allocatable_income   unchanged
discretionary        unchanged
```

### 7.3 Estimate vs. actual

The projection computes interest as an **estimate**, always flagged as such. A
user-entered `InterestCharged` / `InterestEarned` event for that cycle **supersedes**
the estimate.

This is the same "actual beats forecast" pattern used for `FixedCost` → expected
obligation → explicit `ObligationRaised` (§8.1). Reusing one pattern for both keeps the
supersession logic in one shape.

Rejected alternative: making the projection authoritative, with no interest events at
all. Fewer event types and nothing to key in by hand — but then entering one backdated
receipt silently rewrites what you were charged three months ago, and it will disagree
with your statement. When the tool and the bank disagree, the bank is right, so the tool
must be able to record what the bank said.

Rejected alternative: recorded-only, no computation. Always matches reality and the math
disappears entirely — but the tool then cannot answer "what will carrying this balance
cost me," which is most of the reason to track card debt at all.

### 7.4 Cycle chaining, cascade, and barriers

**Chaining.** Statement cycles fold strictly in order. Each carries forward two pieces
of state: the closing balance, and whether the statement was paid in full by its due
date (which decides the next cycle's grace). The projection processes cycles
sequentially per account and **cannot compute a cycle in isolation**.

**Cascade.** A backdated event changes its cycle's close balance, hence its interest,
hence every later cycle's opening balance.

**Barriers.** A user-entered `InterestCharged` **pins** its cycle: the recorded actual
replaces the estimate and is what carries forward. Entering actuals truncates the
cascade at that point. This is the main practical reason the estimate is not
authoritative.

**APR version resolution.** The APR applied to a cycle is the version effective on the
cycle's **start** date — matching how `AllocationPolicy` resolves at period start
(§8.2). A rate change effective mid-cycle applies from the following cycle, so a past
cycle's interest never moves because of a rate edit.

---

## 8. Definitions and versioning

### 8.1 Fixed costs expand in the projection

`FixedCost` definitions are expanded by the projection into **expected obligations** for
each period they are effective in. An explicit `ObligationRaised` carrying the same
`recurring_id` and falling in the same due-period **supersedes** the expected one —
actual beats forecast.

Rejected alternative: a background scheduler that materializes `ObligationRaised` rows
into the ledger. The ledger becomes self-contained and literal, which is appealing — but
it introduces a clock-dependent writer (violating the spirit of a pure projection), and
retroactively editing a definition then requires backfilling or deleting rows, which an
append-only ledger cannot do cleanly.

Rejected alternative: definitions as pure forecast, with only explicit events counting.
Maximally literal, but forgetting to enter a bill silently *inflates* discretionary —
the tool would tell you that you have more money than you do, which is the worst
direction for this particular error.

### 8.2 Recurring income is forecast-only

`allocatable_income` counts only actual `IncomeReceived` / `GiftReceived` events.
`RecurringIncome` definitions feed a separate forecast view and never contribute to
allocation.

This is deliberately **asymmetric** with `FixedCost`, and the asymmetry is the point: an
unpaid bill is still owed, so it should reserve money. An unreceived paycheck cannot be
spent, so it must not.

### 8.3 Version resolution

All definitions carry `effective_from` (inclusive) and `effective_to` (exclusive,
nullable for open-ended). Versions of the same logical entity may not overlap; this is
enforced at write time.

- `AllocationPolicy` is resolved at the **period start date**. One policy governs a
  whole period. A policy effective mid-period applies from the *next* period.
- `Account` APR is resolved at the **statement cycle start date**.
- `FixedCost` is resolved at the period start date for expansion purposes.

Resolving at a boundary rather than per-event is what makes "changing the split must not
retroactively alter closed periods" mechanically true rather than merely intended: a
closed period's policy was pinned by a date that has already passed.

### 8.4 Corrections

`EventVoided(target_event_id, reason)` is the **only** correction mechanism. The
projection filters voided events before folding. An amount correction is void +
re-raise, not an edit and not an `Adjusted` variant.

Rejected alternative: typed adjustment events (`ObligationAdjusted`, `AmountCorrected`,
…). More expressive and produces a tidier audit trail — but every adjustment type
multiplies the fold's branch count and needs its own invariants. One universal void
means one code path and one property to test: *the fold over `events` equals the fold
over `events` minus voided targets*.

Hard deletes are forbidden everywhere. See `CLAUDE.md` §4.

---

## 9. Design note: future bank/card aggregation

Live account connections (Quicken/Plaid-style) are a **deliberate non-goal for now**,
and the reason is mostly not code: OAuth flows and credential custody, per-provider
contracts and per-connection cost, PII handling obligations. The hard *modeling* problem
is that aggregator feeds deliver undifferentiated transactions, not obligations — so
matching a feed row to an expected `FixedCost`, and categorizing the rest, becomes its
own subsystem with its own failure modes.

Architecturally it is cheap to stay ready, and the spec does:

- Ingestion sits behind an `IngestionSource` protocol. A provider is another
  implementation of it, producing the same events as a receipt upload.
- Every event carries an optional `external_ref` (provider + provider transaction id)
  that participates in the dedupe key. A provider replaying the same transaction is
  already a no-op under the existing uniqueness constraint.

**Nothing in `core/` or `domain/` changes when a provider is added.** That is the whole
purpose of the seam.

---

## 10. Module boundaries

Each row is one implementation agent's scope, independently implementable from
`CONTRACTS.md`. Dependencies point strictly upward — the graph is acyclic.

Each row is also a **branch and a worktree**, and owns a fixed set of paths that only it
may write. See §13.2 for the ownership table — that rule is what makes merges
conflict-free by construction, and it is not optional.

| Module | Scope | Depends on |
|---|---|---|
| `core/money.py` | `Minor`/`Bps` types, `split_bps`, rounding helpers | — |
| `core/periods.py` | `PeriodResolver` protocol, `CalendarMonthResolver`, period algebra | — |
| `core/interest.py` | Pure integer interest engine, day count, cycle math | money, periods |
| `domain/events.py` | Event models, discriminated union, dedupe key computation | money, periods |
| `domain/definitions.py` | `RecurringIncome`, `FixedCost`, `AllocationPolicy`; effective-dating, non-overlap validation | money, periods |
| `domain/accounts.py` | Account definitions, versioned APR, balance folding, statement cycle construction | money, periods, events |
| `domain/projection.py` | The pure fold and the `State` model | all of the above |
| `persistence/` | SQLAlchemy 2.0 models, Alembic migrations, repositories, uniqueness constraints | events, definitions, accounts |
| `ingestion/` | `IngestionSource` protocol, receipt upload, content hashing, idempotent append | events, persistence |
| `api/` | FastAPI routers, request/response DTOs, error taxonomy, `BUDGET_TZ` handling | all |
| `tests/properties/` | Hypothesis strategies + the invariant suite | all |

---

## 11. Phasing

- **Phase 0** *(sequential — this document, `CLAUDE.md`, `CONTRACTS.md`)*
- **Phase 0.5** *(one agent, on `main`, blocking)* — commit every stub signature from
  `CONTRACTS.md` with `NotImplementedError` bodies, plus `.gitignore` and CI config.
  **This must merge to `main` before any worktree is created**, because every module
  branch needs the stubs present for imports to resolve and `mypy --strict` to pass.
  **Everything after this is parallel.**
- **Phase 1** *(5 agents)* — `money`, `periods`, `interest`, `events`, `definitions`
- **Phase 2** *(2 agents)* — `accounts`, `persistence`
- **Phase 3** *(2 agents)* — `projection`, `ingestion`
- **Phase 4** *(2 agents)* — `api`, property-test suite

Only 0 → 0.5 is a hard barrier. Within a phase there are no cross-dependencies, because
contracts are frozen at Phase 0. An agent that needs a not-yet-implemented function
imports its stub and writes against the documented pre/postconditions.

If contracts must change mid-build, that is a Phase 0 amendment affecting every agent —
stop, amend `CONTRACTS.md`, and restate. Do not let two agents negotiate a contract
change between themselves.

---

## 12. Decision log

Every entry is a place where a real alternative was rejected. Reasoning is in the linked
section.

| Decision | Rejected alternative | Why | § |
|---|---|---|---|
| Calendar month periods | Anchor-day, paycheck-driven, semi-monthly | Unambiguous boundaries, no short-month rule; resolver protocol keeps the swap cheap | 4.1 |
| Negative allocation on shortfall | Clamp to zero + shortfall field | One invariant, one code path, no clamped branch | 6.1 |
| Accrual basis for fixed costs | Cash basis | Unpaid bills must reserve money or "left to spend" overstates | — |
| Allocation implies a savings transfer | Track budgeted and actual separately | One number instead of two; no reconciliation gap to explain | 6.2 |
| Implied transfer posts at period close | Post proportionally per income event | One movement per period; backdating stays simple | 6.2 |
| Projection expands `FixedCost` | Scheduler writes events; forecast-only | No clock-dependent writer; forgetting a bill must not inflate discretionary | 8.1 |
| `RecurringIncome` forecast-only | Symmetric with `FixedCost` | An unreceived paycheck cannot be spent | 8.2 |
| Statement-close balance | Average daily balance | ADB needs a daily balance series for an estimate that gets superseded anyway | 7.1 |
| Interest estimated, actual supersedes | Computed-only; recorded-only | Computed-only drifts from statements; recorded-only cannot forecast | 7.3 |
| `AT_PURCHASE` default | `AT_STATEMENT_PAYMENT` default | A bill spanning two months otherwise distorts one period | 6.4 |
| Earned interest not allocatable | Allocate it like income | Otherwise half your savings interest leaks back to discretionary | 7.1 |
| Universal `EventVoided` | Typed adjustment events | One code path, one property, instead of one per adjustment type | 8.4 |
| Recompute from genesis every read | Incremental/materialized balances | Nothing stored can go stale; volume makes it free | 3 |
| Aggregation deferred, seam kept | Build it now; ignore it entirely | Cost is non-coding; the seam costs one optional field | 9 |
| One worktree per module | Shared checkout; branches without worktrees | Concurrent agents in one directory see each other's uncommitted edits | 13 |

---

## 13. Build mechanics: branches and worktrees

Parallel agents share a repository, not a working directory. Each module agent runs in
its own git worktree on its own branch. A shared checkout would let one agent's
uncommitted edits become visible to another mid-run, which destroys the
"independently implementable" property the §10 module table asserts.

### 13.1 Topology

- `main` holds contracts and stubs. **No module agent commits to it.**
- One worktree + branch per row of the §10 module table: `module/core-money`,
  `module/domain-projection`, `module/persistence`, and so on.
- Worktrees live under `.claude/worktrees/<name>/` and are gitignored.
- **Base ref is the previous phase's integration commit on `main`**, never an
  arbitrary HEAD. A Phase 2 worktree branches from the commit that closed Phase 1.

### 13.2 Path ownership

An agent writes **only** files under the paths it owns. This is what makes merges
conflict-free by construction rather than by luck.

| Branch | Owns |
|---|---|
| `module/core-money` | `core/money.py`, `tests/unit/core/test_money.py` |
| `module/core-periods` | `core/periods.py`, `tests/unit/core/test_periods.py` |
| `module/core-interest` | `core/interest.py`, `tests/unit/core/test_interest.py` |
| `module/domain-events` | `domain/events.py`, `tests/unit/domain/test_events.py` |
| `module/domain-definitions` | `domain/definitions.py`, `tests/unit/domain/test_definitions.py` |
| `module/domain-accounts` | `domain/accounts.py`, `tests/unit/domain/test_accounts.py` |
| `module/domain-projection` | `domain/projection.py`, `tests/unit/domain/test_projection.py` |
| `module/persistence` | `persistence/**`, `alembic/**` |
| `module/ingestion` | `ingestion/**`, `tests/unit/ingestion/**` |
| `module/api` | `api/**`, `tests/unit/api/**` |
| `module/properties` | `tests/properties/**`, `tests/examples/**` |

Writing outside your owned paths has the same status as changing a contract: stop and
raise it (`CLAUDE.md` §6).

### 13.3 Files with a single owner or no owner

| File | Owner | Note |
|---|---|---|
| `PLAN.md`, `CLAUDE.md`, `CONTRACTS.md` | **nobody** | Frozen at Phase 0. A worktree makes editing these feel safe and local. It is not. |
| `pyproject.toml` | integrator only | Module agents declare needed dependencies in their PR description; the integrator adds them in the phase's integration commit. Concurrent dependency edits are the most common avoidable conflict. |
| `tests/properties/strategies.py` | `module/properties` | `CLAUDE.md` §5.2 requires shared strategies live here and be imported. Phase 1–3 agents must not create this file; they write module tests under `tests/unit/`. |
| `alembic/versions/*` | `module/persistence` | Two agents generating migrations produces two heads, which Alembic cannot resolve automatically. |
| `.gitignore`, CI config | Phase 0.5 | Established once, before any worktree exists. |
| `core/types.py` | Phase 0.5, then **nobody** | Enums and id aliases from `CONTRACTS.md` §2. Imported by nearly every module and owned by no branch, so it is frozen once Phase 0.5 lands. Needing to change it is a contract amendment (§13.5). |
| `tools/**` | Phase 0.5 / integrator | The purity gate is build infrastructure. A module agent that needs it changed is almost certainly trying to make its own violation pass. |

### 13.4 Merge protocol

Before a module branch merges, in its own worktree:

1. `python tools/check_domain_purity.py` — exit 0
2. `mypy --strict .` — clean
3. its own tests pass

Merge order **within** a phase is arbitrary; there are no cross-dependencies by
construction. Each phase closes with an integration commit on `main` that runs the
full suite. Only then are the next phase's worktrees created.

The purity gate resolves `core/` and `domain/` relative to the current directory, so
running it inside a worktree checks that worktree's files — which is what you want.

### 13.5 Contract amendments

If `CONTRACTS.md` must change mid-build, it is a commit on `main`, and **every open
worktree rebases onto it before continuing**. Do not let one agent amend a contract in
its own worktree — that is the failure mode described in §11, and worktrees make it
easier to do accidentally, not harder.
