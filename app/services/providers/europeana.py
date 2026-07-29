"""Europeana CC0 and public-domain photo fallback search.

Europeana's broad ``reusability=open`` filter also contains CC BY material.
This provider therefore treats that parameter only as a search optimisation and
accepts an item after its record-level rights are verified as CC0 or PDM.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable, List
from urllib.parse import quote, urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .utils import DEFAULT_UA, get_api_key, get_tls_verify, raise_for_http_error, safe_error_details


_SEARCH_URL = "https://api.europeana.eu/record/v2/search.json"
_RECORD_URL_PREFIX = "https://api.europeana.eu/record/v2"
_MAX_RECORDS_PER_SEARCH = 8
_ACCEPTED_RIGHTS = {
    "http://creativecommons.org/publicdomain/zero/1.0/": "CC0",
    "https://creativecommons.org/publicdomain/zero/1.0/": "CC0",
    "http://creativecommons.org/publicdomain/mark/1.0/": "Public Domain Mark",
    "https://creativecommons.org/publicdomain/mark/1.0/": "Public Domain Mark",
}


def _configured_api_key() -> str:
    try:
        api_key = get_api_key("europeana_api_keys")
    except ValueError:
        return ""
    return api_key.strip() if isinstance(api_key, str) else ""


def _has_configured_api_key() -> bool:
    api_keys = config.app.get("europeana_api_keys")
    if isinstance(api_keys, str):
        return bool(api_keys.strip())
    if isinstance(api_keys, list):
        return any(isinstance(api_key, str) and api_key.strip() for api_key in api_keys)
    return False


def is_europeana_photo_fallback_enabled() -> bool:
    """Return whether an optional, configured Europeana fallback may be used."""
    value = config.app.get("europeana_photo_fallback_enabled", True)
    if isinstance(value, str):
        enabled = value.strip().casefold() in {"1", "true", "yes", "on"}
    else:
        enabled = bool(value)
    return enabled and _has_configured_api_key()


def _safe_https_media_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return ""
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() != "https"
        or not isinstance(hostname, str)
        or not hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    normalized_host = hostname.casefold().rstrip(".")
    if normalized_host == "localhost" or normalized_host.endswith(".local"):
        return ""
    try:
        if not ipaddress.ip_address(normalized_host).is_global:
            return ""
    except ValueError:
        pass
    return value.strip()


def is_safe_europeana_image_url(value: object) -> bool:
    """Return whether a provider-hosted Europeana image or redirect is safe."""
    return bool(_safe_https_media_url(value))


def _values(value: object) -> Iterable[object]:
    if isinstance(value, (list, tuple)):
        return value
    return (value,)


def _first_text(*values: object) -> str:
    for value in values:
        for entry in _values(value):
            if isinstance(entry, str) and entry.strip():
                return " ".join(entry.split())
    return ""


def _rights_label(value: object) -> tuple[str, str] | None:
    for entry in _values(value):
        if not isinstance(entry, str):
            continue
        normalized = entry.strip()
        if normalized and not normalized.endswith("/"):
            normalized = f"{normalized}/"
        label = _ACCEPTED_RIGHTS.get(normalized)
        if label:
            return label, normalized
    return None


def _record_identifier(value: object) -> str:
    """Normalize a Search API record id without allowing URL injection."""
    if not isinstance(value, str):
        return ""
    identifier = value.strip().strip("/")
    if (
        not identifier
        or len(identifier) > 512
        or any(character.isspace() or ord(character) < 32 for character in identifier)
        or any(character in identifier for character in "?#\\")
    ):
        return ""
    parts = identifier.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return quote(identifier, safe="/")


def _record_url(record_id: object) -> str:
    identifier = _record_identifier(record_id)
    return f"{_RECORD_URL_PREFIX}/{identifier}.json" if identifier else ""


def _record_media(record: dict) -> tuple[str, str, str, str] | None:
    aggregations = record.get("aggregations")
    if not isinstance(aggregations, list):
        return None
    for aggregation in aggregations:
        if not isinstance(aggregation, dict):
            continue
        rights = _rights_label(aggregation.get("edmRights"))
        if rights is None:
            continue
        for field in ("edmIsShownBy", "hasView"):
            for media_url in _values(aggregation.get(field)):
                safe_url = _safe_https_media_url(media_url)
                if not safe_url:
                    continue
                provider = _first_text(
                    aggregation.get("edmDataProvider"),
                    aggregation.get("edmProvider"),
                )
                return safe_url, rights[0], rights[1], provider
    return None


def _fetch_record(record_id: object, api_key: str) -> dict | None:
    record_url = _record_url(record_id)
    if not record_url:
        return None
    try:
        response = requests.get(
            record_url,
            headers={"X-Api-Key": api_key, "User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[europeana] record lookup failed: {safe_error_details(error)}")
        return None
    record = payload.get("object") if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else None


def search_photos_europeana(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Return Europeana images whose record explicitly grants CC0 or PDM."""
    del video_aspect
    query = " ".join(str(search_term or "").split())
    api_key = _configured_api_key()
    if not query or not is_europeana_photo_fallback_enabled() or not api_key:
        return []

    logger.info("[europeana] searching CC0/public-domain photos for fallback")
    try:
        response = requests.get(
            _SEARCH_URL,
            params={
                "query": query,
                "media": "true",
                "thumbnail": "true",
                "reusability": "open",
                "rows": _MAX_RECORDS_PER_SEARCH,
            },
            headers={"X-Api-Key": api_key, "User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[europeana] photo search failed: {safe_error_details(error)}")
        return []

    candidates = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        logger.warning("[europeana] photo search returned unexpected response")
        return []

    items = []
    seen_urls = set()
    for candidate in candidates[:_MAX_RECORDS_PER_SEARCH]:
        if not isinstance(candidate, dict):
            continue
        record = _fetch_record(candidate.get("id"), api_key)
        if record is None:
            continue
        media = _record_media(record)
        if media is None:
            continue
        image_url, license_label, license_url, provider = media
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        attribution = "Europeana"
        if provider:
            attribution = f"{attribution}: {provider}"
        items.append(
            MaterialInfo(
                provider="europeana",
                url=image_url,
                preview_url=image_url,
                search_query=query,
                title=_first_text(record.get("title"), record.get("dcTitle")),
                tags=query.split(),
                license=license_label,
                license_url=license_url,
                attribution=attribution,
            )
        )
    return items
