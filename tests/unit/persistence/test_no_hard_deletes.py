"""There is no DELETE in this codebase, and exactly one UPDATE.

CLAUDE.md §4.3 is a rule about code that does not exist yet as much as about code that
does. A reviewer can check it once; this test checks it on every run, by parsing the
package to an AST rather than reading it — the same reasoning as
`tools/check_domain_purity.py`, so a docstring may say "no DELETE" and a string literal
may contain the word without tripping anything.

The purity gate itself covers `core/` and `domain/` only. `persistence/` is where the
rule is most likely to be broken, because it is the only package that can break it.
"""

from __future__ import annotations

import ast
from pathlib import Path

PERSISTENCE = Path(__file__).resolve().parents[3] / "persistence"


def _called_names(tree: ast.AST) -> list[str]:
    """Every callee name in the module, as written.

    `f(...)` yields `f`; `a.b.f(...)` yields `f`. Attribute chains are reduced to the
    final attribute so that `session.delete(...)` and `delete(...)` are both caught.
    """
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


def _bare_called_names(tree: ast.AST) -> list[str]:
    """Callees written as a plain name — `f(...)`, never `x.f(...)`.

    The distinction matters for exactly one word. `sqlalchemy.update` is imported as a
    name and called bare, while `dict.update` is only ever reached through an
    attribute; counting both would flag every `values.update(...)` in the mapping layer
    as a mutation of the database.
    """
    return [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def _modules() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(PERSISTENCE.rglob("*.py"))
    ]


def test_nothing_in_persistence_deletes() -> None:
    """No `delete(...)`, no `session.delete(...)`, no `drop(...)`, no `truncate(...)`.

    Corrections are `EventVoided` rows and definition changes are new versions
    (PLAN.md §8.4). Nothing in this package needs to remove a row, so nothing may.
    """
    forbidden = {"delete", "drop", "drop_all", "truncate", "drop_table", "execute_ddl"}
    offenders = [
        (path.name, name)
        for path, tree in _modules()
        for name in _called_names(tree)
        if name in forbidden
    ]
    assert offenders == []


def test_persistence_issues_exactly_one_update() -> None:
    """One `update(` call, and it is `close_version`'s.

    "Closing a version is the single permitted UPDATE" is only checkable if there is
    exactly one place it could happen. This is that check; if a second `update(` ever
    appears, this test names the file it appeared in.
    """
    updates = [
        path.name
        for path, tree in _modules()
        for name in _bare_called_names(tree)
        if name == "update"
    ]
    assert updates == ["repositories.py"]


def test_the_only_update_lives_in_close_version() -> None:
    """Located precisely, not merely counted.

    A single `update(` in the right file but the wrong function would pass the count
    above and still violate §4.3.
    """
    tree = ast.parse((PERSISTENCE / "repositories.py").read_text(encoding="utf-8"))
    hosting_functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for name in _bare_called_names(node)
        if name == "update"
    ]
    assert hosting_functions == ["close_version"]


def test_no_ddl_is_emitted_from_the_repositories() -> None:
    """Schema changes belong to Alembic. A repository that could `create_all` could,
    with one more line, `drop_all`."""
    tree = ast.parse((PERSISTENCE / "repositories.py").read_text(encoding="utf-8"))
    assert "create_all" not in _called_names(tree)


def test_persistence_never_imports_upward() -> None:
    """Dependencies point strictly upward (CLAUDE.md §3.1).

    `persistence/` may import `core/` and `domain/`. Importing `api/` or `ingestion/`
    would make the graph cyclic, and a cycle is a build failure rather than a style
    issue.
    """
    forbidden_roots = {"api", "ingestion"}
    offenders: list[tuple[str, str]] = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_roots:
                        offenders.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.split(".")[0] in forbidden_roots:
                    offenders.append((path.name, node.module))
    assert offenders == []


def test_no_float_or_decimal_column_types() -> None:
    """Money is `int` minor units everywhere (CLAUDE.md §2.1).

    The purity gate does not reach `persistence/`, and a `Float` or `Numeric` column is
    exactly the way a float would get in without any float literal ever appearing in the
    source.
    """
    forbidden = {"Float", "Numeric", "REAL", "DECIMAL", "Decimal"}
    offenders = [
        (path.name, name)
        for path, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        for name in [node.id]
        if name in forbidden
    ]
    assert offenders == []
