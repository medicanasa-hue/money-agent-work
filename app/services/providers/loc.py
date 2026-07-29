"""Explicitly public-domain motion pictures from the Library of Congress."""

from collections import deque
from html import unescape
import re
import threading
import time
from typing import List
from urllib.parse import urlsplit, urlunsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import get_tls_verify, raise_for_http_error, safe_error_details


_SEARCH_URL = "https://www.loc.gov/film-and-videos/"
_LOC_HOST = "www.loc.gov"
_DOWNLOAD_HOST = "tile.loc.gov"
_DOWNLOAD_PATH_PREFIX = "/storage-services/"
_LICENSE = "Library of Congress Public Domain"
_LOC_USER_AGENT = "MoneyPrinterTurbo public-domain material search"
_MAX_SEARCH_RESULTS = 8
_MAX_ITEM_LOOKUPS = 2
_MAX_QUERY_TERMS = 8
_MAX_QUERY_TERM_LENGTH = 48
_RATE_LIMIT_REQUESTS = 16
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_BACKOFF_SECONDS = 60.0
_RESTRICTED_RIGHTS_MARKERS = (
    "all rights reserved",
    "copyright restrictions may apply",
    "may be subject to copyright",
    "protected by copyright",
    "rights may be restricted",
    "written permission",
    "permission of the copyright owner",
    "permission required",
    "educational purposes only",
    "not in the public domain",
)
_request_lock = threading.Lock()
_request_times: deque[float] = deque()
_rate_limited_until = 0.0


def _query_terms(value: object) -> List[str]:
    """Return bounded plain words without forwarding search syntax to LoC."""
    if not isinstance(value, str):
        return []
    return [
        term
        for term in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if len(term) <= _MAX_QUERY_TERM_LENGTH
    ][:_MAX_QUERY_TERMS]


def _text_values(value: object) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]


def _first_text(value: object) -> str:
    values = _text_values(value)
    return values[0] if values else ""


def _has_explicit_public_domain_rights(value: object) -> bool:
    """Fail closed unless item-level LoC rights text explicitly says public domain."""
    rights_text = " ".join(
        re.sub(r"<[^>]+>", " ", unescape(entry)).casefold()
        for entry in _text_values(value)
    )
    return bool(rights_text) and "public domain" in rights_text and not any(
        marker in rights_text for marker in _RESTRICTED_RIGHTS_MARKERS
    )


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError, OverflowError):
        return 0


def _catalog_urls(value: object) -> tuple[str, str]:
    """Return an item JSON endpoint and canonical page only for LoC item URLs."""
    if not isinstance(value, str) or not value.strip():
        return "", ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return "", ""
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.casefold() != _LOC_HOST
        or not parsed.path.startswith("/item/")
    ):
        return "", ""
    path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    page_url = urlunsplit(("https", _LOC_HOST, path, "", ""))
    return f"{page_url}?fo=json", page_url


def _allowed_download_url(value: object, *, video: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != _DOWNLOAD_HOST
        or not parsed.path.startswith(_DOWNLOAD_PATH_PREFIX)
    ):
        return ""
    suffix = parsed.path.casefold()
    if video and not suffix.endswith(".mp4"):
        return ""
    if not video and not suffix.endswith((".jpg", ".jpeg", ".png", ".gif")):
        return ""
    return value.strip()


def _download_url_key(value: object) -> str:
    """Compare approved LoC media URLs without query-string differences."""
    url = _allowed_download_url(value, video=True)
    if not url:
        return ""
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", "")
    )


def _is_restricted(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_explicitly_not_downloadable(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"0", "false", "no", "off"}
    return value is False or value == 0


def _reserve_request_slot() -> bool:
    """Keep this optional provider below LoC's public JSON API request limit."""
    now = time.monotonic()
    with _request_lock:
        if now < _rate_limited_until:
            return False
        while _request_times and _request_times[0] <= now - _RATE_LIMIT_WINDOW_SECONDS:
            _request_times.popleft()
        if len(_request_times) >= _RATE_LIMIT_REQUESTS:
            return False
        _request_times.append(now)
        return True


def _record_rate_limit(response: requests.Response) -> None:
    """Stop further calls briefly when LoC explicitly rejects the request rate."""
    global _rate_limited_until

    retry_after = response.headers.get("Retry-After") if response.headers else None
    try:
        delay = max(_RATE_LIMIT_BACKOFF_SECONDS, float(str(retry_after)))
    except (TypeError, ValueError):
        delay = _RATE_LIMIT_BACKOFF_SECONDS
    with _request_lock:
        _rate_limited_until = max(_rate_limited_until, time.monotonic() + delay)
        _request_times.clear()


def _loc_get(
    url: str,
    *,
    params: dict | None = None,
    timeout: tuple[int, int],
) -> requests.Response | None:
    """Make one bounded LoC request without allowing this source to block others."""
    if not _reserve_request_slot():
        logger.warning("[loc] request budget exhausted; skipping this optional source")
        return None
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": _LOC_USER_AGENT},
        proxies=config.proxy,
        verify=get_tls_verify(),
        timeout=timeout,
    )
    if getattr(response, "status_code", None) == requests.codes.too_many_requests:
        _record_rate_limit(response)
        logger.warning("[loc] rate limited; pausing optional source requests")
        return None
    raise_for_http_error(response)
    return response


def _iter_file_mappings(value: object):
    """Flatten a small, heterogeneous LoC ``files`` response without recursion."""
    pending = [value]
    visited = 0
    while pending and visited < 32:
        current = pending.pop(0)
        visited += 1
        if isinstance(current, dict):
            yield current
        elif isinstance(current, list):
            pending.extend(current)


def _candidate_metadata(candidate: dict, resource: dict) -> tuple[int, int, int]:
    """Prefer file metadata because LoC may omit it at the resource level."""
    return (
        _positive_int(candidate.get("duration"))
        or _positive_int(resource.get("duration")),
        _positive_int(candidate.get("width")) or _positive_int(resource.get("width")),
        _positive_int(candidate.get("height"))
        or _positive_int(resource.get("height")),
    )


def _resource_video_candidates(resource: dict):
    if _is_restricted(resource.get("rights_restricted")):
        return
    file_mappings = list(_iter_file_mappings(resource.get("files")))
    restricted_file_urls = set()
    for file_info in file_mappings:
        if not (
            _is_explicitly_not_downloadable(file_info.get("canDownload"))
            or _is_restricted(file_info.get("rights_restricted"))
            or _is_restricted(file_info.get("download_restricted"))
        ):
            continue
        for field in ("download", "url"):
            url_key = _download_url_key(file_info.get(field))
            if url_key:
                restricted_file_urls.add(url_key)

    direct_url = _allowed_download_url(resource.get("video"), video=True)
    if direct_url and _download_url_key(direct_url) not in restricted_file_urls:
        yield direct_url, _candidate_metadata(resource, resource)
    for file_info in file_mappings:
        if (
            _is_explicitly_not_downloadable(file_info.get("canDownload"))
            or _is_restricted(file_info.get("rights_restricted"))
            or _is_restricted(file_info.get("download_restricted"))
        ):
            continue
        media_type = str(file_info.get("mimetype") or "").casefold()
        if media_type and "mp4" not in media_type and "video" not in media_type:
            continue
        for field in ("download", "url"):
            video_url = _allowed_download_url(file_info.get(field), video=True)
            if video_url:
                yield video_url, _candidate_metadata(file_info, resource)


def _select_resource(
    resources: object,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> dict | None:
    if not isinstance(resources, list):
        return None
    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        target_width, target_height = VideoAspect.portrait.to_resolution()
    target_aspect = target_width / target_height

    selected = None
    selected_score = None
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict) or _is_restricted(
            resource.get("download_restricted")
        ):
            continue
        preview_url = _allowed_download_url(
            resource.get("image") or resource.get("poster"), video=False
        )
        for video_url, (duration, width, height) in _resource_video_candidates(resource):
            if duration < minimum_duration:
                continue
            aspect_fit = 0.0
            if width and height:
                source_aspect = width / height
                aspect_fit = min(source_aspect, target_aspect) / max(
                    source_aspect, target_aspect
                )
            score = (aspect_fit, width * height, duration, -index)
            if selected_score is None or score > selected_score:
                selected = {
                    "url": video_url,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "preview_url": preview_url,
                }
                selected_score = score
    return selected


class LibraryOfCongressProvider(VideoProvider):
    """Search explicitly public-domain, directly downloadable LoC MP4 footage."""

    name = "loc"
    quality_weight = 0.65

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        query = " ".join(_query_terms(search_term))
        if not query:
            return []
        try:
            required_duration = max(1, int(minimum_duration))
        except (TypeError, ValueError):
            required_duration = 1

        try:
            response = _loc_get(
                _SEARCH_URL,
                params={"q": query, "fo": "json", "c": _MAX_SEARCH_RESULTS},
                timeout=(30, 60),
            )
            if response is None:
                return []
            results = response.json().get("results", [])
        except Exception as error:
            logger.warning(f"[loc] search failed: {safe_error_details(error)}")
            return []

        if not isinstance(results, list):
            logger.warning("[loc] search returned unexpected response")
            return []

        materials: List[MaterialInfo] = []
        for result in results[:_MAX_ITEM_LOOKUPS]:
            if not isinstance(result, dict):
                continue
            item_api_url, page_url = _catalog_urls(result.get("id"))
            if not item_api_url:
                continue
            item = self._fetch_item(
                item_api_url,
                page_url,
                result.get("title"),
                search_term,
                required_duration,
                video_aspect,
            )
            if item:
                materials.append(item)

        logger.info(f"[loc] search returned {len(materials)} public-domain videos")
        return materials

    def _fetch_item(
        self,
        item_api_url: str,
        page_url: str,
        fallback_title: object,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect,
    ) -> MaterialInfo | None:
        try:
            response = _loc_get(
                item_api_url,
                timeout=(15, 30),
            )
            if response is None:
                return None
            payload = response.json()
        except Exception as error:
            logger.warning(f"[loc] item fetch failed: {safe_error_details(error)}")
            return None

        item_data = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item_data, dict):
            return None
        if _is_restricted(item_data.get("access_restricted")):
            return None
        if not _has_explicit_public_domain_rights(item_data.get("rights")):
            return None

        selected = _select_resource(
            payload.get("resources"), minimum_duration, video_aspect
        )
        if not selected:
            return None

        return MaterialInfo(
            provider=self.name,
            url=selected["url"],
            duration=selected["duration"],
            width=selected["width"],
            height=selected["height"],
            search_query=search_term,
            title=_first_text(item_data.get("title")) or _first_text(fallback_title),
            description=_first_text(item_data.get("description")),
            tags=_text_values(item_data.get("subject"))[:12],
            license=_LICENSE,
            license_url=page_url,
            attribution=f"Library of Congress ({page_url})",
            preview_url=selected["preview_url"],
        )
