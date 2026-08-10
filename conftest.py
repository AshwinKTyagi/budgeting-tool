"""Root conftest — exists to put the repository root on `sys.path`.

pytest prepends a collected file's *basedir* (the first parent without an
`__init__.py`) to `sys.path` under the default import mode. With no conftest here, the
root itself was never a basedir, and two imports the suite already relies on could only
work by accident of how the editable install happened to be laid out:

* `from tests.properties.strategies import ...` — the shared Hypothesis strategies that
  `CLAUDE.md` §5.2 requires be imported rather than redefined per module.
* `import serve` — a top-level module, and deliberately not one of the distribution's
  declared packages.

Placing this file at the root makes the root a basedir, so both resolve under a bare
`pytest` — which is exactly how CI invokes it. It adds no fixtures and no configuration;
`pyproject.toml` remains integrator-only (PLAN.md §13.3) and is untouched.
"""

from __future__ import annotations
