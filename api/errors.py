"""AppError -> HTTP mapping (CONTRACTS.md §8.9, §7.1).

Errors are for input that could never be valid. Warnings are data in `State` and never
reach this module. Duplicate ingestion is not an error either — it is a 200 with
`deduplicated: true` (CONTRACTS.md §7.1).
"""

from __future__ import annotations

from core.types import AppError, ErrorResponse


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
    raise NotImplementedError
