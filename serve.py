"""Development entry point: the API plus the data-entry page, on one origin.

`api/app.py` deliberately exports a factory rather than a module-level `app`, so that a
test can bind the whole application to a temporary database. `uvicorn` needs a module
attribute, so this file is where the two meet:

    uvicorn serve:app --reload

Serving the page from the *same origin* as the API is the reason no CORS middleware
exists anywhere in this codebase. A browser asking `/api/v1/state` from a page delivered
by this same process is a same-origin request, so there is no preflight and nothing to
configure. A second dev server on another port would need `CORSMiddleware`; this does
not, and the absence is deliberate rather than an oversight.

Nothing here belongs to `module/api` (PLAN.md §13.2): this file mounts an application,
it does not extend one. No route is added, no handler is replaced.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.app import create_app

#: The static page. Resolved from `__file__` rather than given as the relative string
#: `"web"`, because `StaticFiles` resolves a relative directory against the *current
#: working directory* — so `cd /tmp && uvicorn serve:app` would fail at import with a
#: directory-not-found, and a deployment that ran from anywhere but the repository root
#: would break for a reason that reads as unrelated to the change that caused it.
WEB_ROOT = Path(__file__).parent / "web"

app: FastAPI = create_app()

# Mounted AFTER `create_app` has included the routers, and that ordering is load-bearing.
# Starlette matches routes in registration order, so the `/api/v1/*` routes are tested
# first and this catch-all only ever sees what they declined. Mounting it before them
# would shadow the entire API with a 404 from the static handler — silently, since a
# mount that finds no file produces the same shape as a route that does not exist.
# `tests/unit/test_serve.py` pins the ordering so it cannot regress unnoticed.
#
# `html=True` serves `index.html` for `/`.
app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
