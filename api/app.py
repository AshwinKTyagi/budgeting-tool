"""The application factory. Base path `/api/v1` (CONTRACTS.md §6).

Owned by `module/api` (PLAN.md §13.2).

A factory rather than a module-level `app`, for one reason that matters: a module-level
instance would bind the process-wide engine at import time, and every test would then
share whichever database was configured first. `create_app(engine=...)` lets a test
point the whole application at a temporary SQLite file, which is the only way the suite
can be certain it is not writing to the repository's `budget.db`.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import Engine

from api.errors import install_exception_handlers
from api.routers import definitions, ingestion, read, suggestions
from persistence.engine import configure_engine

#: The base path, stated once. No router declares a prefix of its own.
API_V1_PREFIX = "/api/v1"


def create_app(*, engine: Engine | None = None) -> FastAPI:
    """Build the application.

    `engine`, when given, is installed as the process-wide one — which also reaches
    `ingestion.store_receipt`, whose frozen signature takes no session and so goes
    through `persistence.engine.session_scope()`. Omitted, the engine is built on first
    use from `$BUDGET_DATABASE_URL`.

    Schema creation is deliberately *not* done here: that is `alembic upgrade head`'s
    job, and an app that silently created tables would let a drifted migration go
    unnoticed until the first deployment that ran the migration for real.
    """
    if engine is not None:
        configure_engine(engine)

    app = FastAPI(
        title="budgeting-tool",
        version="1",
        summary=(
            "Append-only event ledger, pure projection. All money is `_minor` "
            "integers; the API never emits a formatted currency string or a float."
        ),
    )
    install_exception_handlers(app)
    app.include_router(ingestion.router, prefix=API_V1_PREFIX)
    app.include_router(read.router, prefix=API_V1_PREFIX)
    app.include_router(definitions.router, prefix=API_V1_PREFIX)
    app.include_router(suggestions.router, prefix=API_V1_PREFIX)
    return app
