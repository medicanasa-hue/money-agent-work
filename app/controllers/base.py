import hmac
import threading
import time
from uuid import uuid4

from fastapi import Request

from app.config import config
from app.models.exception import HttpException


_DEFAULT_AUTH_MAX_FAILURES = 5
_DEFAULT_AUTH_FAILURE_WINDOW_SECONDS = 60.0
_MAX_AUTH_RATE_LIMIT_CLIENTS = 2048
MAX_TASK_ID_LENGTH = 128
_auth_failure_lock = threading.Lock()
_auth_failure_attempts: dict[str, list[float]] = {}


def reset_auth_rate_limits():
    """Clear in-memory invalid API-key attempt counters."""
    with _auth_failure_lock:
        _auth_failure_attempts.clear()


def _configured_auth_max_failures() -> int:
    value = config.app.get("api_auth_max_failures", _DEFAULT_AUTH_MAX_FAILURES)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_AUTH_MAX_FAILURES
    return parsed if parsed > 0 else _DEFAULT_AUTH_MAX_FAILURES


def _configured_auth_failure_window_seconds() -> float:
    value = config.app.get(
        "api_auth_failure_window_seconds", _DEFAULT_AUTH_FAILURE_WINDOW_SECONDS
    )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_AUTH_FAILURE_WINDOW_SECONDS
    return parsed if parsed > 0 else _DEFAULT_AUTH_FAILURE_WINDOW_SECONDS


def _auth_client_id(request: Request) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host if host else "unknown"


def _trim_auth_rate_limit_clients(
    now: float, window_seconds: float, preserve_client_id: str
):
    if len(_auth_failure_attempts) < _MAX_AUTH_RATE_LIMIT_CLIENTS:
        return

    for client_id, attempts in list(_auth_failure_attempts.items()):
        fresh_attempts = [
            attempt for attempt in attempts if now - attempt < window_seconds
        ]
        if fresh_attempts:
            _auth_failure_attempts[client_id] = fresh_attempts
        else:
            _auth_failure_attempts.pop(client_id, None)

    overflow = len(_auth_failure_attempts) - _MAX_AUTH_RATE_LIMIT_CLIENTS + 1
    if overflow <= 0:
        return
    eviction_candidates = [
        (attempts[-1], client_id)
        for client_id, attempts in _auth_failure_attempts.items()
        if client_id != preserve_client_id
    ]
    if not eviction_candidates:
        eviction_candidates = [
            (attempts[-1], client_id)
            for client_id, attempts in _auth_failure_attempts.items()
        ]
    for _, client_id in sorted(eviction_candidates)[:overflow]:
        _auth_failure_attempts.pop(client_id, None)


def _is_auth_rate_limited(request: Request) -> bool:
    """Record an invalid API-key attempt and report whether it is blocked."""
    now = time.monotonic()
    client_id = _auth_client_id(request)
    window_seconds = _configured_auth_failure_window_seconds()
    max_failures = _configured_auth_max_failures()

    with _auth_failure_lock:
        _trim_auth_rate_limit_clients(now, window_seconds, client_id)
        attempts = _auth_failure_attempts.get(client_id, [])
        attempts = [attempt for attempt in attempts if now - attempt < window_seconds]
        if len(attempts) >= max_failures:
            _auth_failure_attempts[client_id] = attempts
            return True
        attempts.append(now)
        _auth_failure_attempts[client_id] = attempts
    return False


def _clear_auth_rate_limit(request: Request):
    with _auth_failure_lock:
        _auth_failure_attempts.pop(_auth_client_id(request), None)


def get_task_id(request: Request) -> str:
    """Keep printable trace IDs, replacing unsafe client input before logging."""
    task_id = request.headers.get("x-task-id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > MAX_TASK_ID_LENGTH
        or not task_id.isprintable()
    ):
        return str(uuid4())
    return task_id


def get_api_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key


def get_api_key_values(request: Request) -> list[str]:
    """Preserve duplicate headers instead of trusting a proxy's value ordering."""
    get_list = getattr(request.headers, "getlist", None)
    if callable(get_list):
        return get_list("x-api-key")
    api_key = get_api_key(request)
    return [api_key] if isinstance(api_key, str) else []


def verify_token(request: Request):
    configured_token = config.app.get("api_key", "")
    if configured_token is None:
        return
    if not isinstance(configured_token, str):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=500,
            message="API authentication is misconfigured",
        )

    expected_token = configured_token.strip()
    if not expected_token:
        return

    token_values = get_api_key_values(request)
    valid_token = (
        len(token_values) == 1
        and isinstance(token_values[0], str)
        and hmac.compare_digest(
            token_values[0].strip().encode("utf-8"), expected_token.encode("utf-8")
        )
    )
    if not valid_token:
        request_id = get_task_id(request)
        if _is_auth_rate_limited(request):
            raise HttpException(
                task_id=request_id,
                status_code=429,
                message="too many invalid API key attempts",
            )
        raise HttpException(
            task_id=request_id,
            status_code=401,
            message="invalid API key",
        )

    _clear_auth_rate_limit(request)
