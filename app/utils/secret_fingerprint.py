"""Process-local opaque fingerprints for cache invalidation by credential value."""

from __future__ import annotations

import hashlib
import secrets


_PROCESS_CACHE_SALT = secrets.token_bytes(32)
_PBKDF2_ITERATIONS = 120_000


def for_cache(value: object) -> str:
    """Return a stable process-local derived value without retaining the input."""
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.pbkdf2_hmac(
        "sha256",
        normalized_value.encode("utf-8"),
        _PROCESS_CACHE_SALT,
        _PBKDF2_ITERATIONS,
    ).hex()
