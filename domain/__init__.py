"""The model. Pure — no I/O, no clock, no DB.

`domain/` imports only `core/` and `domain/`. Nothing here may ever import from
`persistence/`, `ingestion/`, or `api/` (CLAUDE.md §3.1). Checked by
`tools/check_domain_purity.py`.
"""
