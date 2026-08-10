# budgeting-tool

Single-user personal budgeting tool: an append-only event ledger with a pure projection.

Income, fixed obligations, savings allocation, discretionary spend, and account balances (checking, savings, credit card, loan) are derived by folding the full ledger — never from cached balances. Money is always integer minor units (cents); rates are basis points.

## Recognition principle

An outflow affects the budget exactly once, when the expense or obligation is **recognized** — not when cash moves.

Paying a credit card bill is a transfer, not an expense: the purchase was already recognized. Transfers between your own accounts never touch discretionary. See [PLAN.md](PLAN.md) §1 for the full table.

## Requirements

- Python 3.12+
- Node.js 22+ (for the Vite/React data-entry UI)
- SQLite by default (any SQLAlchemy-supported URL works)

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head

cd web && npm install && npm run build && cd ..
uvicorn serve:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the data-entry UI. The API is at `/api/v1` (OpenAPI at `/docs`).

`serve.py` mounts the built UI (`web/dist`) and the API on one origin so the browser needs no CORS setup.

### Frontend development

For hot reload, run the API and Vite together:

```bash
# terminal 1
uvicorn serve:app --reload

# terminal 2
cd web && npm run dev
```

Vite serves the UI at [http://127.0.0.1:5173](http://127.0.0.1:5173) and proxies `/api` to uvicorn. Pages: `/account`, `/setup`, `/recurring`, `/record`, `/overview`.

```bash
cd web && npm test    # Vitest (money helpers)
cd web && npm run build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BUDGET_DATABASE_URL` | `sqlite+pysqlite:///budget.db` | SQLAlchemy engine URL |
| `BUDGET_TZ` | `America/Los_Angeles` | IANA zone used only to turn “now” into a default business `as_of` date |

Schema is created by Alembic, not at app startup:

```bash
alembic upgrade head
# optional scratch DB:
alembic -x db_url=sqlite+pysqlite:///scratch.db upgrade head
```

## Layout

```
core/          pure primitives (money, periods, interest) — no domain knowledge
domain/        events, definitions, accounts, project() — no I/O, no clock
persistence/   SQLAlchemy models, repositories, Alembic migrations
ingestion/     append-only event + receipt ingestion
api/           FastAPI routers and DTOs
web/           Vite + React + TypeScript data-entry SPA (build → web/dist)
tools/         domain purity gate (AST check over core/ and domain/)
tests/         unit, property (Hypothesis), and PLAN.md example tests
```

Dependencies point upward only: `core` ← `domain` ← `persistence` / `ingestion` / `api`.

## API (sketch)

Base path: `/api/v1`

| Area | Endpoints |
|---|---|
| Write | `POST /events`, `POST /events/{id}/void`, `POST /receipts`, … |
| Read | `GET /state`, `/periods`, `/ledger`, `/charts/series`, `/accounts` |
| Definitions | `GET` / `POST /definitions/{kind}` (`recurring-income`, `fixed-cost`, `allocation-policy`, `account`) |

Every read recomputes from genesis for the given `as_of` date (defaults to today in `BUDGET_TZ`). Full shapes and error taxonomy live in [CONTRACTS.md](CONTRACTS.md) §6–§7.

## Development

```bash
# purity gate — no floats, true division, or clock reads in core/domain
python tools/check_domain_purity.py

mypy --strict .
pytest

cd web && npm test && npm run build
```

CI runs the Python gates and the frontend build/tests on Python 3.12 and 3.14 ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Design docs

| Doc | Role |
|---|---|
| [PLAN.md](PLAN.md) | Why — architecture, recognition principle, scope |
| [CONTRACTS.md](CONTRACTS.md) | What — types, signatures, API contracts |
| [CLAUDE.md](CLAUDE.md) | How — conventions, forbidden patterns, property tests |

## Non-goals

No multi-user auth, multi-currency, live bank aggregation, or statement reconciliation. Balances are only as correct as the events entered. See [PLAN.md](PLAN.md) §2.2.
