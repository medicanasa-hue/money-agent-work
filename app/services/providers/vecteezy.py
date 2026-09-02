"""Vecteezy Free-plan video provider with deferred download resolution.

Vecteezy's resource search response intentionally does not expose a permanent
video URL.  The provider therefore returns an opaque resource reference and
requests the short-lived download URL only after MPT has selected that clip.
"""
from typing import List
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import (
    DEFAULT_UA,
    get_api_key,
    get_search_page,
    get_tls_verify,
    raise_for_http_error,
    safe_error_details,
)


_API_URL = "https://api.vecteezy.com/v2"
_RESOURCE_URL_PREFIX = "vecteezy-resource://"
_FREE_LICENSE = "Vecteezy Free License"
_FREE_LICENSE_URL = "https://www.vecteezy.com/licensing-agreement"
_MAX_SEARCH_DURATION_SECONDS = 3600


def _account_id() -> str:
    """Return a valid account id without accepting arbitrary URL fragments."""
    value = str(config.app.get("vecteezy_account_id") or "").strip()
    return value if value.isdigit() else ""


def _has_configured_api_key() -> bool:
    """Avoid advertising the provider when config contains only blank keys."""
    keys = config.app.get("vecteezy_api_keys")
    if isinstance(keys, str):
        return bool(keys.strip())
    if not isinstance(keys, (list, tuple)):
        return False
    return any(isinstance(key, str) and key.strip() for key in keys)


def _resource_id(resource_url: object) -> str:
    value = str(resource_url or "").strip()
    if not value.startswith(_RESOURCE_URL_PREFIX):
        return ""
    value = value[len(_RESOURCE_URL_PREFIX) :]
    return value if value.isdigit() else ""


def _is_mp4_resource(resource: dict) -> bool:
    """Accept video resources unless metadata explicitly rules MP4 out."""
    metadata = resource.get("file_metadata")
    if not isinstance(metadata, dict):
        return True
    file_types = metadata.get("available_file_types")
    if not isinstance(file_types, list) or not file_types:
        return True
    extensions = {
        str(entry.get("extension") or "").strip().lower().lstrip(".")
        for entry in file_types
        if isinstance(entry, dict)
    }
    return "mp4" in extensions


def _https_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return value.strip()


def resolve_vecteezy_download_url(item: MaterialInfo) -> str:
    """Resolve one selected Vecteezy clip without exposing signed URLs in logs.

    Free-plan downloads can mandate an attribution URL.  When Vecteezy marks a
    clip as requiring attribution but does not supply that URL, the provider
    fails closed so MPT cannot silently publish an uncredited asset.
    """
    resource_id = _resource_id(getattr(item, "url", ""))
    account_id = _account_id()
    if not resource_id or not account_id:
        return ""

    try:
        api_key = get_api_key("vecteezy_api_keys")
    except ValueError as error:
        logger.warning(f"[vecteezy] {error}")
        return ""

    try:
        response = requests.get(
            f"{_API_URL}/{account_id}/resources/{resource_id}/download",
            params={"file_type": "mp4"},
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": DEFAULT_UA},
            proxies=config.proxy,
            verify=get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
    except Exception as error:
        logger.warning(f"[vecteezy] download URL request failed: {safe_error_details(error)}")
        return ""

    if not isinstance(payload, dict):
        logger.warning("[vecteezy] download URL response was invalid")
        return ""

    download_url = _https_url(payload.get("url"))
    if not download_url:
        logger.warning("[vecteezy] download URL response omitted a usable URL")
        return ""

    if bool(payload.get("requires_attribution")):
        attribution_url = _https_url(payload.get("required_attribution_url"))
        if not attribution_url:
            logger.warning("[vecteezy] skipped a clip with unrecordable required attribution")
            return ""
        item.attribution = f"Vecteezy attribution: {attribution_url}"

    item.license = _FREE_LICENSE
    item.license_url = _FREE_LICENSE_URL
    return download_url


class VecteezyProvider(VideoProvider):
    """Search commercial, family-friendly Vecteezy video resources."""

    name = "vecteezy"
    quality_weight = 0.80

    def is_available(self) -> bool:
        return _has_configured_api_key() and bool(_account_id())

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        del video_aspect  # The Vecteezy API exposes no video orientation filter.
        account_id = _account_id()
        if not account_id:
            logger.warning("[vecteezy] account id is missing or invalid")
            return []

        try:
            api_key = get_api_key("vecteezy_api_keys")
        except ValueError as error:
            logger.warning(f"[vecteezy] {error}")
            return []

        try:
            required_duration = max(1, int(minimum_duration))
        except (TypeError, ValueError):
            required_duration = 1

        params = {
            "term": str(search_term or "").strip(),
            "content_type": "video",
            "page": get_search_page(self.name),
            "per_page": 20,
            "sort_by": "relevance",
            "license_type": "commercial",
            "family_friendly": True,
            # Vecteezy search metadata has no duration field.  Ask its API to
            # return only clips that meet MPT's minimum before treating them as
            # usable candidates in the material-duration budget.
            "duration": f"{required_duration}_{_MAX_SEARCH_DURATION_SECONDS}",
        }
        if not params["term"]:
            return []

        logger.info("[vecteezy] searching commercial videos")
        try:
            response = requests.get(
                f"{_API_URL}/{account_id}/resources",
                params=params,
                headers={"Authorization": f"Bearer {api_key}", "User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(response)
            resources = response.json().get("resources", [])
        except Exception as error:
            logger.error(f"[vecteezy] search failed: {safe_error_details(error)}")
            return []

        if not isinstance(resources, list):
            logger.error("[vecteezy] search returned unexpected response")
            return []

        items: List[MaterialInfo] = []
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("content_type") != "video":
                continue
            license_type = resource.get("license_type")
            if license_type is not None and (
                not isinstance(license_type, str)
                or license_type.strip().casefold() != "commercial"
            ):
                continue
            resource_id = resource.get("id")
            if isinstance(resource_id, bool) or not str(resource_id).isdigit():
                continue
            if not _is_mp4_resource(resource):
                continue

            title = resource.get("title")
            items.append(
                MaterialInfo(
                    provider=self.name,
                    url=f"{_RESOURCE_URL_PREFIX}{resource_id}",
                    duration=required_duration,
                    search_query=params["term"],
                    title=title.strip() if isinstance(title, str) else "",
                    license=_FREE_LICENSE,
                    license_url=_FREE_LICENSE_URL,
                )
            )
        return items
