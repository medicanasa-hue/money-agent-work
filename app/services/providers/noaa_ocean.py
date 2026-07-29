"""Public-domain underwater footage from the NOAA Ocean Exploration Video Portal."""

import re
from typing import List
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import DEFAULT_UA, get_tls_verify, raise_for_http_error, safe_error_details


_SEARCH_URL = "https://www.ncei.noaa.gov/metadata/granule/geoportal/opensearch"
_NOAA_VIDEO_LOCATIONS = {
    ("www.ncei.noaa.gov", "/data/oceans/oer/video/"),
    ("data.nodc.noaa.gov", "/oer/video/"),
}
_LICENSE = "NOAA Ocean Exploration Public Domain"
_LICENSE_URL = "https://oceanexplorer.noaa.gov/data/access-tools/"
_CREDIT = "NOAA Ocean Exploration"
_DURATION_PATTERN = re.compile(r"\bfor\s+(\d+(?:\.\d+)?)\s+seconds\b", re.IGNORECASE)


def _search_tokens(value: object) -> List[str]:
    """Return harmless portal-query tokens without exposing query syntax."""
    if not isinstance(value, str):
        return []
    return re.findall(r"[A-Za-z0-9]+", value.casefold())[:6]


def _portal_query(tokens: List[str]) -> str:
    keyword_query = " AND ".join(f'"{token}*"' for token in tokens)
    return f"({keyword_query}) AND (NOT (STREAM) AND NOT (HIGHLIGHT))"


def _duration_from_title(value: object) -> int:
    if not isinstance(value, str):
        return 0
    match = _DURATION_PATTERN.search(value)
    if not match:
        return 0
    try:
        return max(0, int(float(match.group(1))))
    except ValueError:
        return 0


def _noaa_video_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not any(
            parsed.netloc.casefold() == host and parsed.path.startswith(path_prefix)
            for host, path_prefix in _NOAA_VIDEO_LOCATIONS
        )
        or not parsed.path.casefold().endswith(".mp4")
    ):
        return ""
    return value.strip()


def _noaa_preview_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not any(
            parsed.netloc.casefold() == host and parsed.path.startswith(path_prefix)
            for host, path_prefix in _NOAA_VIDEO_LOCATIONS
        )
    ):
        return ""
    return value.strip()


def _tags(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return [tag.strip() for tag in value if isinstance(tag, str) and tag.strip()][:12]


class NOAOOceanExplorationProvider(VideoProvider):
    """Find directly downloadable public-domain low-resolution NOAA MP4 clips."""

    name = "noaa_ocean"
    quality_weight = 0.78

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        del video_aspect  # The portal has no orientation filter.
        tokens = _search_tokens(search_term)
        if not tokens:
            return []
        try:
            required_duration = max(1, int(minimum_duration))
        except (TypeError, ValueError):
            required_duration = 1

        params = {
            "q": _portal_query(tokens),
            "start": 1,
            "max": 20,
            "orderBy": "title",
            "f": "pjson",
        }
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
            logger.warning(f"[noaa_ocean] search failed: {safe_error_details(error)}")
            return []

        if not isinstance(results, list):
            logger.warning("[noaa_ocean] search returned unexpected response")
            return []

        materials: List[MaterialInfo] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            source = result.get("_source")
            if not isinstance(source, dict):
                continue
            title = result.get("title")
            description = result.get("description")
            title = title.strip() if isinstance(title, str) else ""
            description = description.strip() if isinstance(description, str) else ""
            if "copyright" in f"{title} {description}".casefold():
                continue
            links = source.get("links_s")
            if not isinstance(links, list):
                continue
            video_url = next(
                (
                    url
                    for url in links
                    if _noaa_video_url(url)
                ),
                "",
            )
            if not video_url:
                continue

            duration = _duration_from_title(title)
            if duration <= 0:
                # The portal frequently omits clip duration metadata. Keep the
                # candidate eligible, then verify the requested duration after
                # downloading the selected MP4.
                duration = required_duration
            if duration < required_duration:
                continue

            materials.append(
                MaterialInfo(
                    provider=self.name,
                    url=video_url,
                    duration=duration,
                    search_query=search_term,
                    title=title,
                    description=description,
                    tags=_tags(source.get("keywords_s")),
                    license=_LICENSE,
                    license_url=_LICENSE_URL,
                    attribution=_CREDIT,
                    preview_url=_noaa_preview_url(source.get("thumbnail_s")),
                )
            )
        return materials
