#!/usr/bin/env python3
"""Token-level purity gate for `core/` and `domain/`.

Enforces the money and purity rules in CLAUDE.md §2.1 and §4 by parsing each file
to an AST rather than grepping its text. Comments, docstrings, and string literals
are not code and are never flagged — a docstring may say "no floats here" and a URL
may contain a slash.

Rules:
    D001  true division (`/`)          -> use `//` (CLAUDE.md §2.1)
    D002  float literal                -> integer minor units only (§4.1)
    D003  float / Decimal / round      -> banned names in domain logic (§2.1)
    D004  math / decimal import        -> no floating point in domain logic (§2.1)
    D005  clock read                   -> as_of_date is a parameter (§4.4)

Usage:
    python tools/check_domain_purity.py [paths...]      # default: core domain

Exit status 0 when clean, 1 when any violation is found.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

DEFAULT_ROOTS = ("core", "domain")

BANNED_NAMES = {
    "float": "D003",
    "Decimal": "D003",
    "round": "D003",
}
BANNED_MODULES = {"math", "decimal"}
CLOCK_ATTRS = {"now", "utcnow", "today"}
CLOCK_CALLS = {("time", "time"), ("time", "monotonic"), ("time", "perf_counter")}

MESSAGES = {
    "D001": "true division `/` produces a float; use `//` (CLAUDE.md §2.1)",
    "D002": "float literal {!r}; money is integer minor units (CLAUDE.md §4.1)",
    "D003": "`{}` is banned in core/ and domain/ (CLAUDE.md §2.1)",
    "D004": "importing `{}` is banned in core/ and domain/ (CLAUDE.md §2.1)",
    "D005": "clock read `{}`; as_of_date must be a parameter (CLAUDE.md §4.4)",
}


class Violation:
    __slots__ = ("path", "line", "col", "code", "message")

    def __init__(self, path: Path, node: ast.AST, code: str, message: str) -> None:
        self.path = path
        self.line = getattr(node, "lineno", 0)
        self.col = getattr(node, "col_offset", 0)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.col + 1}: {self.code} {self.message}"


def check_tree(path: Path, tree: ast.AST) -> Iterator[Violation]:
    for node in ast.walk(tree):
        # D001 — true division, including `/=`. FloorDiv is untouched.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            yield Violation(path, node, "D001", MESSAGES["D001"])
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
            yield Violation(path, node, "D001", MESSAGES["D001"])

        # D002 — float literals. `isinstance(True, int)` is True, so bool is
        # excluded implicitly by checking float first.
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            yield Violation(
                path, node, "D002", MESSAGES["D002"].format(node.value)
            )

        # D003 — banned names, in expressions and annotations alike. A `float`
        # inside a docstring is a str Constant and never reaches here.
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            code = BANNED_NAMES[node.id]
            yield Violation(path, node, code, MESSAGES[code].format(node.id))

        # D004 — module imports
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    yield Violation(
                        path, node, "D004", MESSAGES["D004"].format(alias.name)
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_MODULES:
                yield Violation(
                    path, node, "D004", MESSAGES["D004"].format(node.module)
                )

        # D005 — clock reads. Matches any receiver, so `datetime.now()`,
        # `dt.datetime.now()`, and `dt.date.today()` are all caught.
        elif isinstance(node, ast.Attribute):
            if node.attr in CLOCK_ATTRS:
                yield Violation(
                    path, node, "D005", MESSAGES["D005"].format(node.attr)
                )
            elif (
                isinstance(node.value, ast.Name)
                and (node.value.id, node.attr) in CLOCK_CALLS
            ):
                name = f"{node.value.id}.{node.attr}"
                yield Violation(path, node, "D005", MESSAGES["D005"].format(name))


def iter_files(roots: list[str]) -> Iterator[Path]:
    for root in roots:
        base = Path(root)
        if base.is_file() and base.suffix == ".py":
            yield base
        else:
            yield from sorted(base.rglob("*.py"))


def main(argv: list[str]) -> int:
    roots = argv[1:] or list(DEFAULT_ROOTS)
    violations: list[Violation] = []
    checked = 0

    for path in iter_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            print(f"{path}:{exc.lineno}: E000 could not parse: {exc.msg}")
            return 1
        checked += 1
        violations.extend(check_tree(path, tree))

    for violation in sorted(violations, key=lambda v: (str(v.path), v.line, v.col)):
        print(violation)

    if violations:
        print(f"\n{len(violations)} violation(s) in {checked} file(s).")
        return 1

    print(f"clean: {checked} file(s) checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
