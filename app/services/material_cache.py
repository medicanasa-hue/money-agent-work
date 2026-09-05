"""Bounded, process-local JSON cache for remote material searches.

Only request digests and serialized responses are retained. No request settings
are logged or persisted; parsing into task-specific materials stays with callers.
"""

from collections import OrderedDict
from collections.abc import Callable
import hashlib
import json
import threading
import time
from typing import Any

from loguru import logger

from app.config import config

MATERIAL_SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60
_MAX_CACHE_ENTRIES = 64
_MAX_PAYLOAD_BYTES = 1024 * 1024
_entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_state_lock = threading.Lock()
_request_locks = tuple(threading.Lock() for _ in range(32))
_generation = 0


def clear_material_search_cache() -> None:
    """Drop retained responses, including pending writes from active requests."""
    global _generation
    with _state_lock:
        _entries.clear()
        _generation += 1


def _enabled() -> bool:
    value = config.app.get("material_search_cache_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "on", "yes", "1"}
    return value is True


def _request_key(url: str, items_key: str, kwargs: dict) -> str | None:
    try:
        identity = json.dumps(
            [url, items_key, kwargs], sort_keys=True, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return hashlib.sha256(identity).hexdigest()


def _fetch(url: str, request_get: Callable, kwargs: dict) -> tuple[Any, bool]:
    # Local import avoids loading the provider registry just to clear the cache.
    from app.services.providers.utils import raise_for_http_error

    response = request_get(url, **kwargs)
    try:
        raise_for_http_error(response)
        status = getattr(response, "status_code", None)
        successful = type(status) is int and 200 <= status < 300
        return response.json(), successful
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("failed to close material search response")


def _encode_payload(payload: Any, items_key: str) -> bytes | None:
    items = payload.get(items_key) if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not all(
        isinstance(item, dict) for item in items
    ):
        return None
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return encoded if len(encoded) <= _MAX_PAYLOAD_BYTES else None


def _load(key: str) -> tuple[bytes | None, int]:
    with _state_lock:
        entry = _entries.get(key)
        if entry is not None:
            created, encoded = entry
            age = time.monotonic() - created
            if 0 <= age < MATERIAL_SEARCH_CACHE_TTL_SECONDS:
                _entries.move_to_end(key)
                return encoded, _generation
            del _entries[key]
        return None, _generation


def _save(key: str, encoded: bytes, generation: int) -> None:
    with _state_lock:
        if generation != _generation or not _enabled():
            return
        _entries[key] = (time.monotonic(), encoded)
        _entries.move_to_end(key)
        while len(_entries) > _MAX_CACHE_ENTRIES:
            _entries.popitem(last=False)


def get_search_json(
    url: str, *, items_key: str, request_get: Callable, **request_kwargs
) -> Any:
    """Reuse successful nonempty JSON searches without sharing mutable results.

    Actual URL (including page), headers, proxy and TLS settings identify each
    request. Invalid/empty/oversized payloads and HTTP failures are never cached.
    The request lock coalesces identical misses within this process only.
    """
    key = _request_key(url, items_key, request_kwargs) if _enabled() else None
    if key is None:
        return _fetch(url, request_get, request_kwargs)[0]
    encoded, _ = _load(key)
    if encoded is not None:
        return json.loads(encoded)
    with _request_locks[int(key[:8], 16) % len(_request_locks)]:
        encoded, generation = _load(key)
        if encoded is not None and _enabled():
            return json.loads(encoded)
        payload, successful = _fetch(url, request_get, request_kwargs)
        encoded = _encode_payload(payload, items_key) if successful else None
        if encoded is not None:
            _save(key, encoded, generation)
        return payload
