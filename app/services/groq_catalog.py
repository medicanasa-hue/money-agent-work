"""Safe, bounded discovery of models exposed by Groq's official API."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.services.url_security import public_http_url_addresses


GROQ_API_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODELS_URL = f"{GROQ_API_BASE_URL}/models"
MAX_CATALOG_BYTES = 1024 * 1024
MAX_MODEL_COUNT = 1000
MAX_MODEL_ID_LENGTH = 200
_READ_CHUNK_BYTES = 64 * 1024


def _uses_official_base_url(base_url: object) -> bool:
    candidate = str(base_url or GROQ_API_BASE_URL).strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return False

    return bool(
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.groq.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == "/openai/v1"
        and not parsed.query
        and not parsed.fragment
    )


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _read_bounded_json(response: object) -> object:
    headers = getattr(response, "headers", {}) or {}
    raw_length = headers.get("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > MAX_CATALOG_BYTES:
                raise ValueError("Groq model catalog is too large")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Groq model catalog length") from exc

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        raise ValueError("Groq model catalog response is not streamable")

    chunks: list[bytes] = []
    total_bytes = 0
    for chunk in iter_content(chunk_size=_READ_CHUNK_BYTES):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ValueError("invalid Groq model catalog body")
        total_bytes += len(chunk)
        if total_bytes > MAX_CATALOG_BYTES:
            raise ValueError("Groq model catalog is too large")
        chunks.append(chunk)

    return json.loads(b"".join(chunks).decode("utf-8"))


def _model_ids_from_payload(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []

    model_ids: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            continue
        clean_id = model_id.strip()
        if (
            not clean_id
            or len(clean_id) > MAX_MODEL_ID_LENGTH
            or not clean_id.isprintable()
        ):
            continue
        model_ids.add(clean_id)
        if len(model_ids) >= MAX_MODEL_COUNT:
            break
    return sorted(model_ids)


def get_model_ids(api_key: str, base_url: str) -> list[str]:
    """Fetch models only from Groq's fixed origin; custom endpoints use manual input."""
    if not api_key or not _uses_official_base_url(base_url):
        return []
    if public_http_url_addresses(GROQ_MODELS_URL) is None:
        logger.warning("Groq model catalog URL failed the network safety check")
        return []

    response = None
    try:
        response = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(5, 10),
            stream=True,
            allow_redirects=False,
        )
        status_code = getattr(response, "status_code", 0)
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            return []
        return _model_ids_from_payload(_read_bounded_json(response))
    except Exception:
        logger.warning("Groq model catalog request failed")
        return []
    finally:
        if response is not None:
            _close_response(response)
