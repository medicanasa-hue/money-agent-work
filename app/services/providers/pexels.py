"""Pexels video provider."""
import re
from typing import List
from urllib.parse import unquote, urlencode, urlparse

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import material_cache

from .base import VideoProvider
from .utils import (
    DEFAULT_UA,
    get_api_key,
    get_search_page,
    get_tls_verify,
    safe_error_details,
    select_best_video_variant,
)


def _pexels_page_title(page_url: object) -> str:
    """Extract Pexels' human-readable video title from its detail page URL."""
    if not isinstance(page_url, str) or not page_url.strip():
        return ""
    try:
        path_segments = [
            unquote(segment).strip()
            for segment in urlparse(page_url).path.split("/")
            if segment.strip()
        ]
    except (TypeError, ValueError):
        return ""
    if len(path_segments) < 2 or path_segments[-2].casefold() not in {"video", "videos"}:
        return ""

    slug = path_segments[-1]
    if slug.isdigit():
        return ""
    title = re.sub(r"[-_]+", " ", re.sub(r"-\d{5,}$", "", slug)).strip()
    return "" if not title or title.isdigit() else title


class PexelsProvider(VideoProvider):
    name = "pexels"
    quality_weight = 1.0

    def is_available(self) -> bool:
        return bool(config.app.get("pexels_api_keys"))

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        aspect = VideoAspect(video_aspect)
        video_orientation = (
            VideoAspect.portrait.name
            if aspect is VideoAspect.portrait_4_5
            else aspect.name
        )

        try:
            api_key = get_api_key("pexels_api_keys")
        except ValueError as e:
            logger.warning(f"[pexels] {e}")
            return []

        headers = {"Authorization": api_key, "User-Agent": DEFAULT_UA}
        params = {
            "query": search_term,
            "per_page": 20,
            "orientation": video_orientation,
            "page": get_search_page("pexels"),
        }
        query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
        logger.info("[pexels] searching videos")

        try:
            response = material_cache.get_search_json(
                query_url,
                items_key="videos", request_get=requests.get,
                headers=headers,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            video_items: List[MaterialInfo] = []

            if "videos" not in response:
                logger.error("[pexels] search returned unexpected response")
                return video_items

            for v in response["videos"]:
                if not isinstance(v, dict):
                    continue
                try:
                    duration = float(v.get("duration"))
                except (TypeError, ValueError):
                    continue
                if duration < minimum_duration:
                    continue
                best_file = select_best_video_variant(
                    v.get("video_files"),
                    video_aspect=aspect,
                    url_key="link",
                )
                if best_file:
                    vf, w, h = best_file
                    video_url = vf.get("link")
                    if not isinstance(video_url, str) or not video_url.strip():
                        continue
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video_url
                    item.preview_url = str(v.get("image") or "").strip()
                    item.title = _pexels_page_title(v.get("url"))
                    item.duration = duration
                    item.width = w
                    item.height = h
                    item.search_query = search_term
                    video_items.append(item)
            return video_items

        except Exception as e:
            logger.error(f"[pexels] search failed: {safe_error_details(e)}")
        return []
