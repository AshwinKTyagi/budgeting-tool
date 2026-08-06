"""Receipt blob storage and content hashing (CONTRACTS.md §8.8, §6.4)."""

from __future__ import annotations


def store_receipt(blob: bytes, content_type: str) -> tuple[str, str]:
    """Persist a receipt blob.

    Preconditions:
        content_type is an accepted image or PDF type, else
        AppError(UNSUPPORTED_MEDIA_TYPE)

    Postconditions:
        returns (blob_id, sha256_hex)
        identical bytes always yield the identical sha256 and reuse the same blob
    """
    raise NotImplementedError
