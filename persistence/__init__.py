"""SQLAlchemy 2.0 models, Alembic migrations, repositories, uniqueness constraints.

Owned by `module/persistence`, which owns `persistence/**` and `alembic/**` (PLAN.md
§13.2). Two agents generating migrations produces two Alembic heads, which cannot be
resolved automatically — hence the single owner.

This package is intentionally empty at Phase 0.5. CONTRACTS.md §8 defines no stubs for
`persistence/`, so there is no frozen surface to transcribe here, and inventing one
would be amending a contract (CLAUDE.md §6). See the Phase 0.5 report: this is the one
unmet item in the CONTRACTS.md §9 frozen-surface checklist.

Non-negotiable constraints this package is built under:
  * There is no DELETE in this codebase. No `session.delete()`, no `DROP`, no
    `TRUNCATE`, no `UPDATE` against the events table (CLAUDE.md §4.3).
  * The events table is immutable and append-only. `dedupe_key` is UNIQUE.
  * Closing a definition version by setting `effective_to` is the single permitted
    `UPDATE`, and it goes through the repository method that exists for it.
"""
