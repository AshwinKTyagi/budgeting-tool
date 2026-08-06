# CLAUDE.md — Conventions for this repository

Read `PLAN.md` §1 (the recognition principle) before writing any code that touches
money. Read `CONTRACTS.md` for the types you are implementing against.

This file is enforcement: what is required, what is forbidden, and how both are checked.

---

## 1. The one rule that outranks the others

> **An outflow affects the budget exactly once, at the moment the underlying expense or
> obligation is *recognized* — not when the money moves.**

Moving money between your own accounts never touches discretionary. Paying a credit card
bill is a transfer, not an expense, because the purchase was already recognized. See
`PLAN.md` §1 for the full table and §6.4 for the credit-card timing modes.

If you find yourself writing code that reduces `discretionary` in two different places
for the same underlying purchase, stop — that is the bug this rule exists to prevent.

---

## 2. Money representation

### 2.1 Rules

- **All money is `int` minor units (cents).** Never a `float`. Never a `Decimal`. Never a
  string. Not even transiently, not even inside a single expression.
- **All percentages and rates are basis points** (`int`, 1 bps = 0.01%). `savings_bps =
  5000` is 50%. `apr_bps = 2199` is 21.99% APR.
- **Division is always `//`.** Floor division, integer operands, integer result. A bare
  `/` anywhere in `core/` or `domain/` is a bug even when the operands happen to divide
  evenly, because it produces a float.
- **Multiply before dividing.** `balance * apr_bps * days // (10000 * 365)`, never
  `balance * (apr_bps // 10000) * ...`. Dividing early discards precision that integer
  arithmetic cannot recover.
- **Currency formatting happens in one place only:** the API serialization layer. `core/`
  and `domain/` never produce a display string.

### 2.2 Types

```python
# core/money.py
type Minor = int   # signed minor units (cents)
type Bps   = int   # basis points; 10_000 == 100%
```

These are PEP 695 aliases, not `NewType` — they document intent and read well in
signatures. They do **not** stop you passing a raw `int`, and that is fine; the
enforcement below is what actually holds the line.

Every money field is named with a `_minor` suffix and every rate with a `_bps` suffix.
A money field without `_minor` in its name is a review failure. This is deliberately
redundant with the type alias: the suffix survives into JSON, database columns, and log
lines, where the alias does not.

### 2.3 How this is enforced

1. **Pydantic `strict=True`** on every model (`CONTRACTS.md` §2). Strict mode rejects
   `1.0` where an `int` is declared, instead of silently coercing it. Without strict
   mode a float reaching the boundary is accepted and rounded, which is exactly the
   failure this rule exists to prevent.
2. **`mypy --strict`** across the whole tree. No implicit `Any`, no untyped defs.
3. **CI purity gate** — `python tools/check_domain_purity.py`, run over `core/` and
   `domain/`. It parses each file to an AST rather than grepping its text, so
   comments, docstrings, and string literals are never flagged: a docstring may say
   "no floats here" and a URL may contain a slash.

   | Code | Fires on | Rule |
   |---|---|---|
   | `D001` | `/` and `/=` (true division; `//` untouched) | §2.1 |
   | `D002` | float literals, including `.5` | §4.1 |
   | `D003` | `float`, `Decimal`, `round` as names — in expressions *and* annotations | §2.1 |
   | `D004` | `import math`, `import decimal` | §2.1 |
   | `D005` | `.now()`, `.utcnow()`, `.today()` on any receiver; `time.time()` and friends | §4.4 |

   Because the check is token-level rather than textual, **there is no suppression
   mechanism and none is needed**. If it fires, it found real code. Fix the code.
4. **Hypothesis property tests** (§5) that assert exactness rather than closeness. There
   is no tolerance parameter anywhere in this codebase. `assertAlmostEqual` and
   `pytest.approx` are forbidden — if a test needs a tolerance, the arithmetic is wrong.

---

## 3. Layout and naming

### 3.1 Layout

```
core/          pure primitives, zero domain knowledge, zero dependencies on domain/
  money.py       Minor, Bps, split_bps, rounding
  periods.py     PeriodResolver protocol, CalendarMonthResolver, period algebra
  interest.py    integer interest engine, day count, cycle math

domain/        the model. pure. no I/O, no clock, no DB.
  events.py      event models, discriminated union, dedupe keys
  definitions.py RecurringIncome, FixedCost, AllocationPolicy, effective-dating
  accounts.py    account definitions, versioned APR, balance folding, statement cycles
  projection.py  project() and the State model

persistence/   SQLAlchemy 2.0 models, Alembic migrations, repositories
ingestion/     IngestionSource protocol, receipt upload, hashing, idempotent append
api/           FastAPI routers, DTOs, error mapping, BUDGET_TZ handling

tools/
  check_domain_purity.py   AST purity gate over core/ and domain/ (§2.3)

tests/
  properties/    Hypothesis strategies and the invariant suite
  examples/      worked examples from PLAN.md, transcribed literally
```

**Dependencies point strictly upward.** `core/` imports nothing from the project.
`domain/` imports only `core/`. `persistence/`, `ingestion/`, and `api/` may import
`core/` and `domain/`. Nothing in `core/` or `domain/` may ever import from
`persistence/`, `ingestion/`, or `api/`. A cycle is a build failure, not a style issue.

### 3.2 Naming

| Kind | Convention | Example |
|---|---|---|
| Money field | `*_minor` | `amount_minor`, `fixed_due_minor` |
| Rate field | `*_bps` | `savings_bps`, `apr_bps` |
| Business date | `*_date` or bare `date` | `due_date`, `as_of_date` |
| UTC instant | `*_at` | `recorded_at` |
| Identifier | `*_id` | `obligation_id`, `account_id` |
| Event class | past-tense verb phrase | `IncomeReceived`, `PaymentMade` |
| Definition class | noun | `FixedCost`, `AllocationPolicy` |
| Pure fold helper | `fold_*` | `fold_statement_cycles` |
| Derived value | `derive_*` | `derive_obligation_status` |
| Boolean | `is_*` / `has_*` | `is_closed`, `is_estimate` |

Event classes are past tense because the ledger records what happened, not what should.
`PaymentMade`, never `MakePayment` or `Payment`.

---

## 4. Forbidden patterns

Each of these is a build failure, not a preference.

### 4.1 Floats for money

```python
amount = 19.99                      # NO
amount_minor = int(19.99 * 100)     # NO — 1998, not 1999
share = total * 0.5                 # NO
share = total * 5000 // 10000       # yes
```

`0.1 + 0.2 != 0.3`. This is not a hypothetical in a system whose central invariant is an
exact sum.

### 4.2 Mutable state in the projection

`project()` is pure: same inputs, same output, always, with no observable side effects.

```python
def project(events, definitions, as_of_date, *, resolver=None) -> State:
    running_total = 0
    for e in events:
        running_total += e.amount_minor     # NO — accumulator mutation
        state.periods[pid].total += ...     # NO — mutating the output
        cache[key] = value                  # NO — module-level cache
```

Fold with immutable accumulators — build a new value per step, or use
`functools.reduce` with frozen carriers. Every model in `domain/` is
`frozen=True`. `State` and everything reachable from it is immutable; construct it once
at the end.

No logging, no metrics, no file access, no network, no database session, no environment
reads inside `project()` or anything it calls.

### 4.3 Hard deletes

There is no `DELETE` in this codebase. No `session.delete()`, no `DROP`, no `TRUNCATE`,
no `UPDATE` against the events table.

Corrections are `EventVoided` (`PLAN.md` §8.4). Definition changes are a new version with
`effective_to` set on the prior one — closing a version is the single permitted `UPDATE`,
it touches only `effective_to`, and it goes through the repository method that exists for
it. Nothing else writes to a row that already exists.

### 4.4 Clock reads inside domain logic

```python
# in core/ or domain/  — all forbidden
datetime.now()      date.today()      time.time()      datetime.utcnow()
```

`as_of_date` is always an explicit parameter, threaded from the API boundary. The
timezone (`BUDGET_TZ`) is read in exactly one place — `api/`, to turn "now" into a
default `as_of_date` when the caller omits it. See `PLAN.md` §4.2.

A projection that reads a clock cannot be tested, cannot answer historical queries, and
returns different results on two calls with identical inputs.

### 4.5 Naive datetimes

Every `datetime` is timezone-aware and UTC. Business dates are `datetime.date` with no
time component at all — that is the type, not a convention. If you find yourself
converting a business date to a datetime, you are about to introduce a boundary bug.

### 4.6 Tolerance in assertions

```python
assert result == pytest.approx(expected)     # NO
assertAlmostEqual(a, b)                      # NO
assert abs(a - b) <= 1                       # NO
assert a == b                                # yes
```

Integer arithmetic is exact. A test that needs slack is testing broken code.

---

## 5. Testing expectations

Example-based tests are necessary and **not sufficient**. Every allocation invariant
needs a Hypothesis property test. The reason is specific: the failures this design is
most exposed to — rounding drift, sign asymmetry, double-counting, cascade
non-determinism — are exactly the failures that hand-picked examples miss, because a
person choosing examples picks round numbers.

### 5.1 Required properties

Each must exist as a named Hypothesis test in `tests/properties/`.

**Allocation**
1. `sum(split_bps(total, buckets)) == total` for all `total` (including negative, zero,
   and near `sys.maxsize`) and all bucket sets summing to 10000 bps.
2. `split_bps(-n, b) == [-x for x in split_bps(n, b)]` — sign symmetry.
3. `fixed_due + savings_allocated + discretionary_allocated == allocatable_income`
   exactly, for every period of every generated ledger, **including** ledgers where fixed
   exceeds income.
4. Changing an `AllocationPolicy` with `effective_from` after a period's start leaves
   that period's numbers **bit-identical**.

**Projection**
5. **Determinism** — `project(e, d, t) == project(e, d, t)`. Two calls, identical result,
   no hidden state.
6. **Ingestion-order independence** — shuffling the arrival order of the same event set
   yields identical `State`. This is *not* immunity to backdating; see property 9.
7. **Idempotent re-ingestion** — appending an event whose `dedupe_key` already exists
   leaves `State` unchanged.
8. **Void equivalence** — folding `events` equals folding `events` with voided events and
   their `EventVoided` records both removed.

**Interest and cycles**
9. **Cascade determinism** — inserting a backdated event and recomputing yields the same
   `State` as if that event had been present from the start. (Backdating *does* change
   past interest — property 6 is about arrival order, this one is about the result being
   history-independent.)
10. **Actuals are barriers** — a cycle with a recorded `InterestCharged` produces the same
    figure regardless of any backdated event within that cycle.
11. **Grace period** — a card whose every statement is paid in full by its due date
    accrues zero interest across any generated ledger.
12. **Interest is mode-invariant** — for the same ledger, computed interest and card
    outstanding balance are identical under `AT_PURCHASE` and `AT_STATEMENT_PAYMENT`.
    Only `discretionary` differs.

**Balances**
13. **Savings reconciliation** — savings balance equals
    `opening + Σallocations − Σdraws + Σinterest ± explicit transfers`, exactly, for any
    generated event sequence.
14. **No double-counting** — for any ledger, total discretionary reduction attributable
    to a credit card equals total charged to it (once fully paid), under **both** timing
    modes. This is the property that catches the recognition-principle bug.
15. **Transfers are budget-neutral** — inserting any `TransferMade` between own accounts
    leaves every period's `discretionary_remaining` unchanged.

### 5.2 Strategies

Shared Hypothesis strategies live in `tests/properties/strategies.py` and are imported,
not redefined per module. At minimum: `minor_amounts()` (signed, spanning zero and large
magnitudes), `bps_splits()` (bucket sets summing to exactly 10000), `business_dates()`,
`ledgers()` (coherent event sequences with valid account and obligation references), and
`backdated_ledgers()` (a ledger plus an event to insert out of order).

Bias generators toward the awkward cases: amounts that do not divide evenly, zero, exactly
one minor unit, negative allocatable income, and 3-way or 4-way splits with prime-ish bps
values like `3333/3333/3334`.

### 5.3 Example tests

`tests/examples/` transcribes the worked examples from `PLAN.md` **literally** — the
`100_001` 50/50 split resolving to `50001/50000` (§5.2), and both interest calculations
(§7.2). These are regression anchors on the documented behavior: if a property test and
a worked example disagree, the documentation is the specification and the code is wrong.

---

## 6. Working conventions

- **Do not negotiate contract changes between agents.** If `CONTRACTS.md` is wrong or
  insufficient, stop and raise it. A contract amended in one module and not another is
  the one failure mode this build structure cannot absorb.
- **Stubs are specifications.** Implement against the documented pre/postconditions in
  the docstring, not against what a caller happens to pass.
- **Warnings are data, not exceptions.** A savings draw exceeding the available balance,
  an overpaid obligation, a payment against an unknown obligation — these are `Warning`
  entries in `State`, never raised. Backdating means today's impossible state is
  tomorrow's ordinary one. See `CONTRACTS.md` §7.
- **Errors are for malformed input only** — a request that could never be valid. See the
  taxonomy in `CONTRACTS.md` §7.
