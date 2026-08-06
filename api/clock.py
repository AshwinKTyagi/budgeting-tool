"""`as_of` resolution — the only clock read in the codebase (CONTRACTS.md §8.9).

Business dates are timezone-free. `BUDGET_TZ` (IANA, default `America/Los_Angeles`)
exists solely to turn "now" into a default `as_of_date` when a caller omits one
(PLAN.md §4.2). Everything downstream receives `as_of_date` as an explicit argument.

This module is outside `core/` and `domain/`, so the purity gate's D005 does not apply
here — this is the one place a clock read is legitimate.
"""

from __future__ import annotations

import datetime as dt

BUDGET_TZ_DEFAULT = "America/Los_Angeles"


def resolve_as_of(as_of: dt.date | None, tz: str) -> dt.date:
    """Resolve the effective as_of_date.

    THE ONLY CLOCK READ IN THE CODEBASE (CLAUDE.md §4.4).

    Preconditions:
        tz is a valid IANA zone name

    Postconditions:
        as_of is not None -> returned verbatim, INCLUDING future dates
        as_of is None     -> today in `tz`
        never called from core/ or domain/
    """
    raise NotImplementedError
