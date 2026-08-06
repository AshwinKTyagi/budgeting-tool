"""FastAPI routers, request/response DTOs, error mapping, `BUDGET_TZ` handling.

Owned by `module/api` (PLAN.md §13.2).

Two responsibilities live here and nowhere else:
  * Currency formatting, if any. `core/` and `domain/` never produce a display string
    (CLAUDE.md §2.1). In practice the API emits `_minor` integers and never a formatted
    string or a float at all (CONTRACTS.md §6).
  * The clock. `BUDGET_TZ` is read in exactly one place — `resolve_as_of` — to turn
    "now" into a default `as_of_date` when the caller omits it (CLAUDE.md §4.4).
"""
