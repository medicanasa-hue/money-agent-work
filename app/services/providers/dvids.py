"""DVIDS public-media video provider.

API: https://api.dvidshub.net
Key: A free DVIDS public API key is required.
License: DVIDS identifies DoD and federal-agency media as public domain unless
an asset indicates a different copyright status. Attribution is retained for
each selected clip.
"""
from typing import List, Optional

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import (
    DEFAULT_UA,
    get_api_key,
    get_tls_verify,
    raise_for_http_error,
    safe_error_details,
)

_SEARCH_URL = "https://api.dvidshub.net/search"
_ASSET_URL = "https://api.dvidshub.net/asset"
_COPYRIGHT_URL = "https://api.dvidshub.net/docs/copyright"


def _positive_dimension(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        dimension = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, dimension)


def _positive_duration(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        duration = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, duration)


def _split_keywords(value: object) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def _credit_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    credits = []
    for entry in value:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if isinstance(name, str) and name.strip():
            credits.append(name.strip())
    return ", ".join(credits)


def _has_explicitly_restricted_rights(asset: dict) -> bool:
    for field in ("copyright", "rights", "license", "license_url"):
        value = asset.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.casefold()
        if "public domain" in normalized or "cc0" in normalized:
            continue
        if any(
            marker in normalized
            for marker in ("all rights reserved", "copyrighted", "restricted")
        ):
            return True
    return False


def _select_mp4_file(files: object, video_aspect: VideoAspect) -> Optional[dict]:
    if not isinstance(files, list):
        return None

    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        target_width, target_height = VideoAspect.portrait.to_resolution()
    target_aspect = target_width / target_height

    selected = None
    selected_score = None
    for index, file_info in enumerate(files):
        if not isinstance(file_info, dict):
            continue
        media_type = str(file_info.get("type") or "").split(";", 1)[0].strip()
        source_url = file_info.get("src")
        if media_type != "video/mp4" or not isinstance(source_url, str) or not source_url.strip():
            continue

        width = _positive_dimension(file_info.get("width"))
        height = _positive_dimension(file_info.get("height"))
        if width and height:
            source_aspect = width / height
            aspect_fit = min(source_aspect, target_aspect) / max(
                source_aspect, target_aspect
            )
            pixels = width * height
        else:
            aspect_fit = 0.0
            pixels = 0
        bitrate = _positive_dimension(file_info.get("bitrate"))
        score = (aspect_fit, pixels, bitrate, -index)
        if selected_score is None or score > selected_score:
            selected = file_info
            selected_score = score
    return selected


class DVIDSProvider(VideoProvider):
    """Search DVIDS for HD public-media clips with preserved credit metadata."""

    name = "dvids"
    quality_weight = 0.78

    def is_available(self) -> bool:
        return bool(config.app.get("dvids_api_keys"))

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        try:
            api_key = get_api_key("dvids_api_keys")
        except ValueError as error:
            logger.warning(f"[dvids] {error}")
            return []

        params = {
            "api_key": api_key,
            "q": search_term,
            "type": "video",
            "hd": 1,
            "from_duration": max(1, int(minimum_duration)),
            "max_results": 12,
            "sort": "score",
            "sortdir": "desc",
        }
        logger.info("[dvids] searching HD videos")
        try:
            response = requests.get(
                _SEARCH_URL,
                params=params,
                headers={"User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(response)
            results = response.json().get("results", [])
        except Exception as error:
            logger.error(f"[dvids] search failed: {safe_error_details(error)}")
            return []

        if not isinstance(results, list):
            logger.error("[dvids] search returned unexpected response")
            return []

        video_items = []
        for result in results[:5]:
            if not isinstance(result, dict) or result.get("type") != "video":
                continue
            if _positive_duration(result.get("duration")) < minimum_duration:
                continue
            asset_id = result.get("id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                continue

            item = self._fetch_asset(
                asset_id.strip(),
                api_key,
                minimum_duration,
                video_aspect,
            )
            if not item:
                continue
            item.search_query = search_term
            if not item.title and isinstance(result.get("title"), str):
                item.title = result["title"].strip()
            video_items.append(item)

        logger.info(f"[dvids] search returned {len(video_items)} videos")
        return video_items

    def _fetch_asset(
        self,
        asset_id: str,
        api_key: str,
        minimum_duration: int,
        video_aspect: VideoAspect,
    ) -> Optional[MaterialInfo]:
        try:
            response = requests.get(
                _ASSET_URL,
                params={"api_key": api_key, "id": asset_id},
                headers={"User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(15, 30),
            )
            raise_for_http_error(response)
            asset = response.json().get("results")
        except Exception as error:
            logger.warning(
                f"[dvids] asset fetch failed: {safe_error_details(error)}"
            )
            return None

        if not isinstance(asset, dict) or asset.get("type") != "video":
            return None
        if _positive_duration(asset.get("duration")) < minimum_duration:
            return None
        if _has_explicitly_restricted_rights(asset):
            logger.info("[dvids] skipped an asset with explicit rights restrictions")
            return None

        selected_file = _select_mp4_file(asset.get("files"), video_aspect)
        if not selected_file:
            return None

        source_url = selected_file.get("src")
        if not isinstance(source_url, str) or not source_url.strip():
            return None

        item = MaterialInfo()
        item.provider = self.name
        item.url = source_url.strip()
        item.duration = _positive_duration(asset.get("duration"))
        item.width = _positive_dimension(selected_file.get("width"))
        item.height = _positive_dimension(selected_file.get("height"))
        item.preview_url = str(asset.get("image") or "").strip()
        title = asset.get("title")
        if isinstance(title, str):
            item.title = title.strip()
        description = asset.get("description")
        if isinstance(description, str):
            item.description = description.strip()
        item.tags = _split_keywords(asset.get("keywords"))
        item.license = "DVIDS public domain (unless otherwise indicated)"
        item.license_url = _COPYRIGHT_URL
        credit = _credit_text(asset.get("credit"))
        source_page = str(asset.get("url") or "").strip()
        attribution_parts = ["DVIDS"]
        if credit:
            attribution_parts.append(credit)
        if source_page:
            attribution_parts.append(source_page)
        item.attribution = " - ".join(attribution_parts)
        return item
