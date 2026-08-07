"""`ingestion/` never deletes and never updates. Checked structurally and behaviourally.

CLAUDE.md §4.3 is a rule about code that does not exist yet as much as about code that
does, so it is worth a test that keeps holding after this branch is merged and someone
adds a "just fix that one row" path. `tests/unit/persistence/test_no_hard_deletes.py`
does this for `persistence/` and `alembic/` — the directories where the SQL lives. This
does it for `ingestion/`, which is the only other module that writes.

Parsed to an AST rather than grepped, the same reasoning as
`tools/check_domain_purity.py`: a docstring may say "no DELETE" and a comment may say
"never UPDATEs" without tripping anything. Both do, in this module.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ingestion import append_event, ingest_receipt, normalize_event
from ingestion.receipts import ReceiptUpload
from persistence.models import EventRow, ReceiptBlobRow

REPO_ROOT = Path(__file__).resolve().parents[3]
INGESTION = REPO_ROOT / "ingestion"

UTC = dt.timezone.utc
RECORDED_AT = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
RECEIPT = b"%PDF-1.7 fake receipt bytes"

#: Anchored on a following keyword so prose in a message argument is not a false
#: positive, while real destructive SQL is.
DESTRUCTIVE_SQL = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+(TABLE|INDEX|CONSTRAINT|SCHEMA|DATABASE)|TRUNCATE)\b"
)

#: Callee names that would mutate or remove stored rows. `update` is included as a bare
#: name because that is how `sqlalchemy.update` is called; `close_version` in
#: `persistence/` is the codebase's single permitted UPDATE and `ingestion/` has no
#: business reaching it.
FORBIDDEN_CALLS = frozenset({"delete", "drop", "truncate", "update", "close_version"})


def _modules() -> list[Path]:
    paths = sorted(INGESTION.rglob("*.py"))
    assert paths, "no modules found — the path is wrong, not the code"
    return paths


def _called_names(tree: ast.AST) -> list[str]:
    """Every callee name as written; `a.b.f(...)` reduces to `f`."""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
    return names


def test_no_destructive_call_anywhere_in_ingestion() -> None:
    """No `session.delete(...)`, no `delete(...)`, no `update(...)`."""
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _called_names(tree):
            if name in FORBIDDEN_CALLS:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}(...)")
    assert offenders == []


def test_no_destructive_sql_literal_anywhere_in_ingestion() -> None:
    """Not even in a string handed to `text(...)`."""
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if DESTRUCTIVE_SQL.search(node.value.upper()):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_ingestion_does_not_import_the_orm_delete_or_update_constructs() -> None:
    """Nothing in `ingestion/` should be building its own statements at all.

    The module is a seam over `persistence/`, and an import of `sqlalchemy.update` here
    would be the first sign it had stopped being one.
    """
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "sqlalchemy"
            ):
                for alias in node.names:
                    if alias.name in {"delete", "update"}:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: {alias.name}")
    assert offenders == []


# ------------------------------------------------------------------- behaviourally


def _coffee(amount_minor: int, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "event_type": "ExpenseRecorded",
        "date": dt.date(2026, 5, 1),
        "amount_minor": amount_minor,
        "category": "coffee",
        "account_id": "checking",
    }
    payload.update(overrides)
    return normalize_event(payload, recorded_at=RECORDED_AT)


def test_a_duplicate_append_leaves_the_stored_row_byte_identical(
    session: Session,
) -> None:
    """"Leaves the table unchanged" is checked column by column, not by row count.

    A row count would pass for an implementation that deleted and reinserted, or that
    overwrote the stored row with the second attempt's `event_id` and `recorded_at`.
    Both are exactly what §4.3 forbids and neither changes `COUNT(*)`.
    """
    first = _coffee(4_599)
    append_event(session, first)
    row = session.scalars(select(EventRow)).one()
    before = {
        column.name: getattr(row, key)
        for key, column in EventRow.__mapper__.columns.items()
    }

    later = normalize_event(
        {
            "event_type": "ExpenseRecorded",
            "date": dt.date(2026, 5, 1),
            "amount_minor": 4_599,
            "category": "coffee",
            "account_id": "checking",
        },
        recorded_at=dt.datetime(2026, 6, 30, 23, 59, tzinfo=UTC),
    )
    assert later.dedupe_key == first.dedupe_key
    assert later.event_id != first.event_id

    append_event(session, later)
    session.expire_all()
    row_after = session.scalars(select(EventRow)).one()
    after = {
        column.name: getattr(row_after, key)
        for key, column in EventRow.__mapper__.columns.items()
    }

    assert after == before


def test_a_duplicate_receipt_upload_leaves_the_blob_row_untouched(
    session: Session,
) -> None:
    """The blob is content-addressed, so a second upload has nothing to add. Its
    `content_type` is not rewritten either — that would be an UPDATE for no gain."""

    def upload(content_type: str, event_id: int) -> ReceiptUpload:
        return ReceiptUpload(
            blob=RECEIPT,
            content_type=content_type,
            date=dt.date(2026, 5, 1),
            amount_minor=4_599,
            category="groceries",
            account_id="visa",
            recorded_at=RECORDED_AT,
            event_id=UUID(int=event_id),
        )

    ingest_receipt(session, upload("application/pdf", 1))
    row = session.scalars(select(ReceiptBlobRow)).one()
    before = (row.blob_id, row.content_sha256, row.content_type, row.byte_size, row.content)

    ingest_receipt(session, upload("image/png", 2))
    session.expire_all()
    row_after = session.scalars(select(ReceiptBlobRow)).one()
    after = (
        row_after.blob_id,
        row_after.content_sha256,
        row_after.content_type,
        row_after.byte_size,
        row_after.content,
    )

    assert after == before
