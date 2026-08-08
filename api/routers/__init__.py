"""Routers, one module per table in CONTRACTS.md §6.

Owned by `module/api` (PLAN.md §13.2).

* `ingestion` — §6.1 and §6.4: `POST /events`, `/events/batch`,
  `/events/{event_id}/void`, `/receipts`. Everything here goes through `ingestion/`;
  no router issues SQL of its own.
* `read` — §6.2's read rows: `/state`, `/periods`, `/periods/{period_id}`, `/ledger`,
  `/charts/series`, `/accounts`. Every one folds a `State` built by
  `api.deps.load_state`.
* `definitions` — §6.2's `/definitions/{kind}` pair, the one read endpoint that answers
  from the definition tables rather than from `State`.

All three are mounted under `/api/v1` by `api.app.create_app`; none of them declares a
prefix of its own, so the base path is stated in exactly one place.
"""
