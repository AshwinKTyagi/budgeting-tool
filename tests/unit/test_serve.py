"""The static mount must not shadow the API.

`serve.py` mounts `SpaStaticFiles` at `/`, which matches *every* path. Starlette tests
routes in registration order, so the only thing standing between the mount and the whole
API is that the routers were included first. That is an ordering invariant with no type
to enforce it and no symptom when it breaks that names its own cause: a shadowed API
returns 404 from the static handler, and the app's exception handlers reshape it into
exactly the `{code, message, details}` body a genuinely-missing route produces. The
suite would go green and every request would fail.

The assertions are about *dispatch*, not about route classes. FastAPI 0.141 keeps an
included router as an opaque `_IncludedRouter` in `app.routes` rather than flattening it
into `APIRoute`s, and an earlier version of this file asserted the flattened shape — so
it broke on a detail it was never trying to pin. What matters is only ever "does this
path reach the API or the static handler", which `Route.matches` answers directly and
which no FastAPI version is free to change.

Deliberately DB-free. `create_app()` with no engine builds nothing — the engine is
constructed on first *use* from `$BUDGET_DATABASE_URL` — so importing `serve` is safe.
Hitting a read endpoint for real would open the repository's own `budget.db`, which no
test may do, and routing is decided before a handler runs in any case.
"""

from __future__ import annotations

from typing import Any

from starlette.routing import Match, Mount
from starlette.testclient import TestClient

import serve

#: The catch-all. Registered last by `serve`, which is the invariant under test.
MOUNT = serve.app.routes[-1]

#: Every path the client actually calls: one per router, plus a parameterised one. A
#: mount registered too early swallows them uniformly, so a single spot check could not
#: tell "routing is correct" from "this one path happens to be listed first".
API_PATHS = (
    "/api/v1/state",
    "/api/v1/accounts",
    "/api/v1/periods",
    "/api/v1/periods/2026-01",
    "/api/v1/ledger",
    "/api/v1/charts/series",
    "/api/v1/definitions/account",
)

#: Client-side routes owned by React Router. They must hit the SPA mount, not the API.
SPA_PATHS = (
    "/setup",
    "/recurring",
    "/record",
    "/overview",
)


def _scope(path: str) -> dict[str, Any]:
    """A minimal ASGI scope for `Route.matches`."""
    return {"type": "http", "method": "GET", "path": path, "root_path": ""}


def _dispatch(path: str) -> object:
    """The route Starlette would dispatch `path` to.

    Mirrors `starlette.routing.Router.app`: a full match wins outright, and a partial
    match is the answer only when nothing ahead of it matched fully. Written against
    the same rule rather than a re-implementation of it, so it cannot drift.
    """
    partial: object | None = None
    scope = _scope(path)
    for route in serve.app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
        if match == Match.PARTIAL and partial is None:
            partial = route
    return partial


def test_static_mount_is_registered_last() -> None:
    """The catch-all is last, so every API route is tested before it."""
    assert isinstance(MOUNT, Mount)


def test_api_paths_do_not_reach_the_static_handler() -> None:
    """Every documented path is claimed by something ahead of the mount."""
    for path in API_PATHS:
        assert _dispatch(path) is not MOUNT, path


def test_api_paths_are_matched_at_all() -> None:
    """A path reaching nothing would also pass the test above, vacuously."""
    for path in API_PATHS:
        assert _dispatch(path) is not None, path


def test_unmatched_path_falls_through_to_the_mount() -> None:
    """A path the API does not claim is the static handler's to answer."""
    assert _dispatch("/index.html") is MOUNT


def test_spa_paths_fall_through_to_the_mount() -> None:
    """Deep links are served by the SPA, not claimed by the API."""
    for path in SPA_PATHS:
        assert _dispatch(path) is MOUNT, path


def test_index_is_served_at_the_root() -> None:
    """`html=True` turns `/` into `web/dist/index.html`."""
    with TestClient(serve.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_spa_deep_link_serves_index() -> None:
    """A hard refresh on `/setup` returns the SPA shell."""
    with TestClient(serve.app) as client:
        response = client.get("/setup")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert b"root" in response.content


def test_built_assets_are_referenced() -> None:
    """The built index pulls hashed assets from `/assets/`."""
    index = (serve.WEB_DIST / "index.html").read_text(encoding="utf-8")
    assert "/assets/" in index or "src=" in index
