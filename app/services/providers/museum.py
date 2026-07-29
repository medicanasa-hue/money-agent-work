"""Public-domain artwork photos from The Met and Art Institute of Chicago."""

from __future__ import annotations

import re
from typing import List
from urllib.parse import quote, urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .utils import DEFAULT_UA, get_tls_verify, raise_for_http_error, safe_error_details


_MET_SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1/search"
_MET_OBJECT_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects"
_MET_IMAGE_HOST = "images.metmuseum.org"
_ARTIC_SEARCH_URL = "https://api.artic.edu/api/v1/artworks/search"
_ARTIC_IMAGE_HOST = "www.artic.edu"
_ARTIC_IIIF_PATH = "/iiif/2"
_MIN_IMAGE_DIMENSION = 480
_MAX_MET_OBJECTS_PER_SEARCH = 12


def is_museum_photo_fallback_enabled() -> bool:
    value = config.app.get("museum_photo_fallback_enabled", True)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _safe_https_url(value: object, hostname: str, path_prefix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(path_prefix)
    ):
        return ""
    return value.strip()


def _safe_met_image_url(value: object) -> str:
    return _safe_https_url(value, _MET_IMAGE_HOST, "/CRDImages/")


def _safe_artic_iiif_base_url(value: object) -> str:
    base_url = _safe_https_url(value, _ARTIC_IMAGE_HOST, _ARTIC_IIIF_PATH)
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if parsed.path.rstrip("/") != _ARTIC_IIIF_PATH or parsed.query or parsed.fragment:
        return ""
    return base_url.rstrip("/")


def is_safe_museum_image_url(value: object) -> bool:
    """Return whether a museum image URL or redirect remains on a trusted host."""
    return bool(
        _safe_met_image_url(value)
        or _safe_https_url(value, _ARTIC_IMAGE_HOST, f"{_ARTIC_IIIF_PATH}/")
    )


def _positive_dimension(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        dimension = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, dimension)


def _display_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _met_object_id(value: object) -> str:
    identifier = str(value or "").strip()
    return identifier if identifier.isdigit() else ""


def search_photos_met(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Return public-domain Met primary images as a last-resort photo fallback."""
    del video_aspect
    query = _display_text(search_term)
    if not is_museum_photo_fallback_enabled() or not query:
        return []

    logger.info("[met] searching public-domain artwork photos for fallback")
    try:
        response = requests.get(
            _MET_SEARCH_URL,
            params={"q": query, "hasImages": True},
            headers={"User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[met] photo search failed: {safe_error_details(error)}")
        return []

    object_ids = payload.get("objectIDs") if isinstance(payload, dict) else None
    if not isinstance(object_ids, list):
        return []

    items = []
    for raw_object_id in object_ids[:_MAX_MET_OBJECTS_PER_SEARCH]:
        object_id = _met_object_id(raw_object_id)
        if not object_id:
            continue
        try:
            object_response = requests.get(
                f"{_MET_OBJECT_URL}/{object_id}",
                headers={"User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(object_response)
            artwork = object_response.json()
        except Exception as error:
            logger.warning(
                f"[met] could not read object metadata: {safe_error_details(error)}"
            )
            continue
        if not isinstance(artwork, dict) or artwork.get("isPublicDomain") is not True:
            continue
        image_url = _safe_met_image_url(artwork.get("primaryImage"))
        if not image_url:
            continue
        artist = _display_text(artwork.get("artistDisplayName"))
        attribution = "The Metropolitan Museum of Art"
        if artist:
            attribution = f"{attribution}: {artist}"
        items.append(
            MaterialInfo(
                provider="met",
                url=image_url,
                preview_url=image_url,
                search_query=query,
                title=_display_text(artwork.get("title")),
                tags=query.split(),
                license="Public Domain",
                license_url="https://www.metmuseum.org/information/terms-and-conditions",
                attribution=attribution,
            )
        )
    return items


def search_photos_artic(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Return public-domain Art Institute of Chicago images through its IIIF API."""
    del video_aspect
    query = _display_text(search_term)
    if not is_museum_photo_fallback_enabled() or not query:
        return []

    logger.info("[artic] searching public-domain artwork photos for fallback")
    try:
        response = requests.get(
            _ARTIC_SEARCH_URL,
            params={
                "q": query,
                "query[term][is_public_domain]": "true",
                "fields": (
                    "id,title,image_id,is_public_domain,artist_display,thumbnail"
                ),
                "limit": 12,
            },
            headers={"User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[artic] photo search failed: {safe_error_details(error)}")
        return []

    results = payload.get("data") if isinstance(payload, dict) else None
    config_data = payload.get("config") if isinstance(payload, dict) else None
    base_url = _safe_artic_iiif_base_url(
        config_data.get("iiif_url") if isinstance(config_data, dict) else None
    )
    if not isinstance(results, list) or not base_url:
        return []

    items = []
    for artwork in results:
        if not isinstance(artwork, dict) or artwork.get("is_public_domain") is not True:
            continue
        image_id = str(artwork.get("image_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", image_id):
            continue
        thumbnail = artwork.get("thumbnail")
        width = _positive_dimension(thumbnail.get("width") if isinstance(thumbnail, dict) else 0)
        height = _positive_dimension(thumbnail.get("height") if isinstance(thumbnail, dict) else 0)
        if width < _MIN_IMAGE_DIMENSION or height < _MIN_IMAGE_DIMENSION:
            continue
        image_url = f"{base_url}/{quote(image_id, safe='')}/full/1686,/0/default.jpg"
        if not is_safe_museum_image_url(image_url):
            continue
        artist = _display_text(artwork.get("artist_display"))
        attribution = "Art Institute of Chicago"
        if artist:
            attribution = f"{attribution}: {artist}"
        items.append(
            MaterialInfo(
                provider="artic",
                url=image_url,
                preview_url=image_url,
                width=width,
                height=height,
                search_query=query,
                title=_display_text(artwork.get("title")),
                tags=query.split(),
                license="Public Domain",
                license_url="https://www.artic.edu/terms",
                attribution=attribution,
            )
        )
    return items
