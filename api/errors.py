"""AppError -> HTTP mapping (CONTRACTS.md §8.9, §7.1).

Errors are for input that could never be valid. Warnings are data in `State` and never
reach this module. Duplicate ingestion is not an error either — it is a 200 with
`deduplicated: true` (CONTRACTS.md §7.1).

Everything that leaves the API on a failure path leaves as an `ErrorResponse`:
`{code, message, details}`. FastAPI's own default body is `{"detail": ...}`, so its
two error shapes — request validation and `HTTPException` — are re-mapped here rather
than left to leak a second error format the client would have to branch on.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.types import AppError, ErrorCode, ErrorResponse

#: CONTRACTS.md §7.1, verbatim. Keyed by `ErrorCode` rather than matched in a chain of
#: `if`s so that totality is a property of the data and can be asserted — see the
#: import-time check below and `tests/unit/api/test_errors.py`.
_HTTP_STATUS_BY_CODE: Final[dict[ErrorCode, int]] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.UNKNOWN_ACCOUNT: 422,
    ErrorCode.UNKNOWN_OBLIGATION: 422,
    ErrorCode.UNKNOWN_EVENT: 404,
    ErrorCode.ALREADY_VOIDED: 409,
    ErrorCode.CANNOT_VOID_A_VOID: 422,
    ErrorCode.POLICY_BPS_NOT_10000: 422,
    ErrorCode.OVERLAPPING_VERSIONS: 409,
    ErrorCode.EFFECTIVE_RANGE_INVALID: 422,
    ErrorCode.PAYMENT_SPLIT_MISMATCH: 422,
    ErrorCode.TRANSFER_SAME_ACCOUNT: 422,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.INTERNAL: 500,
}

#: Totality, enforced at import. A code added to `ErrorCode` without a row here fails
#: the build on the first import of `api/` rather than at the moment it is first
#: raised — which, for a code like `ALREADY_VOIDED`, could be a long way into
#: production. The postcondition on `to_error_response` says "total over ErrorCode";
#: this is what makes that true rather than intended.
_UNMAPPED: Final[frozenset[ErrorCode]] = frozenset(ErrorCode) - frozenset(
    _HTTP_STATUS_BY_CODE
)
if _UNMAPPED:  # pragma: no cover - a build failure, not a runtime path
    raise RuntimeError(
        "ErrorCode members with no HTTP mapping (CONTRACTS.md §7.1): "
        + ", ".join(sorted(code.value for code in _UNMAPPED))
    )


def to_error_response(exc: AppError) -> tuple[int, ErrorResponse]:
    """Map an AppError to (http_status, body) per the table in §7.1.

    Postcondition: total over ErrorCode — every code has a mapping.

    The table (CONTRACTS.md §7.1):
        VALIDATION_FAILED        422
        UNKNOWN_ACCOUNT          422
        UNKNOWN_OBLIGATION       422
        UNKNOWN_EVENT            404
        ALREADY_VOIDED           409
        CANNOT_VOID_A_VOID       422
        POLICY_BPS_NOT_10000     422
        OVERLAPPING_VERSIONS     409
        EFFECTIVE_RANGE_INVALID  422
        PAYMENT_SPLIT_MISMATCH   422
        TRANSFER_SAME_ACCOUNT    422
        UNSUPPORTED_MEDIA_TYPE   415
        INTERNAL                 500
    """
    return (
        _HTTP_STATUS_BY_CODE[exc.code],
        ErrorResponse(code=exc.code, message=exc.message, details=exc.details),
    )


def validation_app_error(exc: ValidationError, context: str) -> AppError:
    """Flatten a pydantic `ValidationError` into `AppError(VALIDATION_FAILED)`.

    CONTRACTS.md §7.1 maps "Pydantic rejection; float where `Minor` expected" to
    VALIDATION_FAILED / 422.

    The per-error entries are reduced to plain strings on the way in. `details` is
    `dict[str, object]` and ends up in a JSON body, and a pydantic `ctx` can carry an
    arbitrary exception object that does not serialize. Mirrors what
    `ingestion.append` does with the same exception type, so a client sees one shape
    whichever layer rejected it.
    """
    return AppError(
        ErrorCode.VALIDATION_FAILED,
        f"{exc.error_count()} validation error(s) for {context}",
        {
            "context": context,
            "errors": [
                {
                    "loc": ".".join(str(part) for part in error["loc"]),
                    "msg": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors(include_url=False)
            ],
        },
    )


def error_json_response(exc: AppError) -> JSONResponse:
    """`to_error_response`, rendered.

    `jsonable_encoder` rather than `model_dump(mode="json")` because `details` is typed
    `dict[str, object]`: pydantic will not know how to serialize a `dt.date` or a `UUID`
    that a raise site put in there, and would emit a warning and a `str()` fallback
    instead of an error. The encoder handles both properly.
    """
    status, body = to_error_response(exc)
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


# ------------------------------------------------------------------------- handlers
# Registered against `Exception` subclasses, and therefore annotated `Exception` —
# Starlette types its handler registry that way and mypy --strict holds us to it. Each
# narrows with an isinstance before doing anything, so a mis-registration is a visible
# failure rather than an attribute error.


async def _app_error_handler(request: Request, exc: Exception) -> Response:
    """The main path: every deliberate rejection in the codebase is an `AppError`."""
    if not isinstance(exc, AppError):  # pragma: no cover - registration guard
        raise exc
    return error_json_response(exc)


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    """Pydantic rejections that did not already come wrapped.

    Covers both FastAPI's `RequestValidationError` (a query parameter, a path enum, a
    multipart form field) and a bare `ValidationError` escaping a model construction.
    """
    if isinstance(exc, RequestValidationError):
        return error_json_response(
            AppError(
                ErrorCode.VALIDATION_FAILED,
                f"{len(exc.errors())} validation error(s) for the request",
                {
                    "context": "request",
                    "errors": [
                        {
                            "loc": ".".join(str(part) for part in error["loc"]),
                            "msg": error["msg"],
                            "type": error["type"],
                        }
                        for error in exc.errors()
                    ],
                },
            )
        )
    if isinstance(exc, ValidationError):
        return error_json_response(validation_app_error(exc, "request"))
    raise exc  # pragma: no cover - registration guard


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Starlette's own 404/405 and anything raising `HTTPException`.

    Re-shaped into an `ErrorResponse` so the API has exactly one error body. A route
    that does not exist is `VALIDATION_FAILED` rather than `UNKNOWN_EVENT`: the
    resource-not-found codes in §7.1 are about ledger entities, not URLs.
    """
    if not isinstance(exc, HTTPException):  # pragma: no cover - registration guard
        raise exc
    code = ErrorCode.INTERNAL if exc.status_code >= 500 else ErrorCode.VALIDATION_FAILED
    _, body = to_error_response(AppError(code, str(exc.detail), {"path": request.url.path}))
    return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(body))


async def _unhandled_handler(request: Request, exc: Exception) -> Response:
    """`INTERNAL` / 500 — "everything else" in the §7.1 table.

    The message is the exception's type, never its text: an unhandled exception's
    message is not a contract and may carry a file path or a query fragment.
    """
    return error_json_response(
        AppError(
            ErrorCode.INTERNAL,
            "internal error",
            {"type": type(exc).__name__},
        )
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Wire every failure path onto the `ErrorResponse` body shape."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(ValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
