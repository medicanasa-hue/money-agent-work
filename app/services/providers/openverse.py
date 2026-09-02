"""Openverse CC0 and public-domain photo fallback search."""

from __future__ import annotations

import ipaddress
from typing import List
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .utils import DEFAULT_UA, get_tls_verify, raise_for_http_error, safe_error_details


_SEARCH_URL = "https://api.openverse.org/v1/images/"
_MIN_IMAGE_DIMENSION = 480
_IMAGE_FILE_EXTENSIONS = {
    "avif",
    "bmp",
    "gif",
    "jpeg",
    "jpg",
    "png",
    "tif",
    "tiff",
    "webp",
}
_ACCEPTED_LICENSES = {
    (
        "cc0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    ): "CC0",
    (
        "pdm",
        "https://creativecommons.org/publicdomain/mark/1.0/",
    ): "Public Domain Mark",
}


def is_openverse_photo_fallback_enabled() -> bool:
    value = config.app.get("openverse_photo_fallback_enabled", True)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_https_url(value: object) -> str:
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


def is_safe_openverse_image_url(value: object) -> bool:
    """Return whether an Openverse source or redirect stays a safe HTTPS URL."""
    return bool(_safe_https_url(value))


def _positive_dimension(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        dimension = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, dimension)


def _is_mature(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    tags = []
    for entry in value:
        tag = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(tag, str) and tag.strip():
            tags.append(tag.strip())
    return tags


def _license_label(item: dict) -> tuple[str, str] | None:
    license_name = str(item.get("license") or "").strip().casefold()
    license_url = str(item.get("license_url") or "").strip()
    label = _ACCEPTED_LICENSES.get((license_name, license_url))
    if label is None:
        return None
    return label, license_url


def _is_image_filetype(value: object) -> bool:
    filetype = str(value or "").strip().casefold()
    if not filetype:
        return True
    if "/" in filetype:
        return filetype.startswith("image/")
    return filetype.lstrip(".") in _IMAGE_FILE_EXTENSIONS


def _image_score(item: MaterialInfo, video_aspect: VideoAspect) -> tuple[float, int]:
    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        target_width, target_height = VideoAspect.portrait.to_resolution()
    source_aspect = item.width / item.height
    target_aspect = target_width / target_height
    aspect_fit = min(source_aspect, target_aspect) / max(source_aspect, target_aspect)
    return aspect_fit, item.width * item.height


def search_photos_openverse(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Return high-resolution Openverse images with an exact CC0/PDM license."""
    if (
        not is_openverse_photo_fallback_enabled()
        or not isinstance(search_term, str)
        or not search_term.strip()
    ):
        return []

    logger.info("[openverse] searching CC0/public-domain photos for fallback")
    try:
        response = requests.get(
            _SEARCH_URL,
            params={
                "q": search_term.strip(),
                "license": "cc0,pdm",
                "page_size": 20,
                "mature": "false",
            },
            headers={"User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[openverse] photo search failed: {safe_error_details(error)}")
        return []

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        logger.warning("[openverse] photo search returned unexpected response")
        return []

    items = []
    seen_urls = set()
    for result in results:
        if not isinstance(result, dict) or _is_mature(result.get("mature")):
            continue
        license_data = _license_label(result)
        image_url = _safe_https_url(result.get("url"))
        width = _positive_dimension(result.get("width"))
        height = _positive_dimension(result.get("height"))
        if (
            license_data is None
            or not image_url
            or width < _MIN_IMAGE_DIMENSION
            or height < _MIN_IMAGE_DIMENSION
            or not _is_image_filetype(result.get("filetype"))
            or image_url in seen_urls
        ):
            continue
        seen_urls.add(image_url)
        license_label, license_url = license_data
        creator = str(result.get("creator") or "").strip()
        source = str(result.get("source") or "").strip()
        attribution = "Openverse"
        if creator:
            attribution = f"{attribution}: {creator}"
        elif source:
            attribution = f"{attribution}: {source}"
        items.append(
            MaterialInfo(
                provider="openverse",
                url=image_url,
                preview_url=_safe_https_url(result.get("thumbnail")),
                width=width,
                height=height,
                search_query=search_term.strip(),
                title=str(result.get("title") or "").strip(),
                tags=_tags(result.get("tags")),
                license=license_label,
                license_url=license_url,
                attribution=attribution,
            )
        )
    return sorted(items, key=lambda item: _image_score(item, video_aspect), reverse=True)
