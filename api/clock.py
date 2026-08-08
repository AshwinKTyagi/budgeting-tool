"""`as_of` resolution — the only clock read in the codebase (CONTRACTS.md §8.9).

Business dates are timezone-free. `BUDGET_TZ` (IANA, default `America/Los_Angeles`)
exists solely to turn "now" into a default `as_of_date` when a caller omits one
(PLAN.md §4.2). Everything downstream receives `as_of_date` as an explicit argument.

This module is outside `core/` and `domain/`, so the purity gate's D005 does not apply
here — this is the one place a clock read is legitimate.

**There is exactly one `.now()` call in the project and it is `_now_utc` below.** Two
functions read it, and the second needs saying out loud because CONTRACTS.md §8.9 names
only the first:

* `resolve_as_of` — the documented one. Turns "now" into a business date in `BUDGET_TZ`.
* `now_utc` — the `recorded_at` every append needs. `ingestion/` made `recorded_at` an
  explicit parameter on every entry point *precisely* so it would be decided here
  (`ingestion/append.py`'s module docstring says so), and `resolve_as_of` returns a
  `dt.date`, which is not an instant and must never be turned into one (CLAUDE.md §4.5).
  So the instant has to be sampled somewhere, and the only defensible somewhere is
  beside the other clock read, sharing its single call site.

Neither is reachable from `core/` or `domain/`: nothing there imports `api/` (CLAUDE.md
§3.1), and a cycle is a build failure rather than a style issue.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

#: Environment variable naming the budget's business timezone. Read here and nowhere
#: else in the codebase (PLAN.md §4.2).
BUDGET_TZ_ENV = "BUDGET_TZ"

BUDGET_TZ_DEFAULT = "America/Los_Angeles"


def budget_tz() -> str:
    """The configured IANA zone, or `America/Los_Angeles`.

    An empty or unset variable falls back to the default rather than raising: an
    unconfigured deployment is the normal single-user case, not an error.
    """
    return os.environ.get(BUDGET_TZ_ENV) or BUDGET_TZ_DEFAULT


def _now_utc() -> dt.datetime:
    """The current instant, UTC-aware. THE clock read.

    A separate private function rather than an inline call in each of the two public
    ones, so that "how many places read a clock" is answerable by counting call sites of
    this name, and so a test can pin the instant without pinning the process.
    """
    return dt.datetime.now(dt.timezone.utc)


def now_utc() -> dt.datetime:
    """The instant to stamp on an append as `recorded_at`.

    Always UTC-aware, so it satisfies `UtcInstant` (CONTRACTS.md §1) without conversion.
    Never derived from `as_of` and never derived from a business date — `recorded_at` is
    audit and tie-break only and has nothing to do with period membership.
    """
    return _now_utc()


def resolve_as_of(as_of: dt.date | None, tz: str) -> dt.date:
    """Resolve the effective as_of_date.

    THE ONLY CLOCK READ IN THE CODEBASE (CLAUDE.md §4.4).

    Preconditions:
        tz is a valid IANA zone name

    Postconditions:
        as_of is not None -> returned verbatim, INCLUDING future dates
        as_of is None     -> today in `tz`
        never called from core/ or domain/

    A supplied date is returned as it arrived — not clamped to today, not validated
    against the ledger's range. A future `as_of` is a forecast query and a valid one
    (CONTRACTS.md §6.3).

    An unknown `tz` raises `ZoneInfoNotFoundError`, which the app's catch-all handler
    reports as `INTERNAL` / 500. That is the right shape: the zone comes from the
    server's own environment, so a bad one is a misconfiguration and never the caller's
    fault, and 422 would blame the wrong party.
    """
    if as_of is not None:
        return as_of
    return _now_utc().astimezone(ZoneInfo(tz)).date()
