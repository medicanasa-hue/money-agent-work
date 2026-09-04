"""Process-local opaque fingerprints for cache invalidation by credential value."""

from __future__ import annotations

import hashlib
import hmac
import secrets


_PROCESS_CACHE_KEY = secrets.token_bytes(32)


def for_cache(value: object) -> str:
    """Return a stable process-local HMAC without retaining the input credential."""
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hmac.new(
        _PROCESS_CACHE_KEY,
        normalized_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
