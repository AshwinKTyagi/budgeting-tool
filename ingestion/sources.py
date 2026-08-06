"""The `IngestionSource` seam (CONTRACTS.md §8.8, PLAN.md §9)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol

from domain.events import Event


class IngestionSource(Protocol):
    """A producer of canonical events. Receipt upload and manual entry implement
    this today; a bank/card aggregator would implement it unchanged (PLAN.md §9).
    """

    def fetch(self, since: dt.date) -> Sequence[Event]: ...
