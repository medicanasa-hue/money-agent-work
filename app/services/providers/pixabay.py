"""Pixabay video provider."""
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import material_cache

from .base import VideoProvider
from .utils import (
    get_api_key,
    get_search_page,
    get_tls_verify,
    safe_error_details,
    select_best_video_variant,
)


class PixabayProvider(VideoProvider):
    name = "pixabay"
    quality_weight = 0.95

    def is_available(self) -> bool:
        return bool(config.app.get("pixabay_api_keys"))

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        aspect = VideoAspect(video_aspect)

        try:
            api_key = get_api_key("pixabay_api_keys")
        except ValueError as e:
            logger.warning(f"[pixabay] {e}")
            return []

        params = {
            "q": search_term,
            "video_type": "all",
            "per_page": 50,
            "page": get_search_page("pixabay"),
            "key": api_key,
        }
        query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
        logger.info("[pixabay] searching videos")

        try:
            response = material_cache.get_search_json(
                query_url,
                items_key="hits", request_get=requests.get,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            video_items: List[MaterialInfo] = []

            if "hits" not in response:
                logger.error("[pixabay] search returned unexpected response")
                return video_items

            for v in response["hits"]:
                if not isinstance(v, dict):
                    continue
                try:
                    duration = float(v.get("duration"))
                except (TypeError, ValueError):
                    continue
                if duration < minimum_duration:
                    continue
                videos = v.get("videos")
                best_video = select_best_video_variant(
                    videos.values() if isinstance(videos, dict) else [],
                    video_aspect=aspect,
                    url_key="url",
                )
                if best_video:
                    video, w, h = best_video
                    video_url = video.get("url")
                    if not isinstance(video_url, str) or not video_url.strip():
                        continue
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video_url
                    item.preview_url = str(video.get("thumbnail") or "").strip()
                    item.duration = duration
                    item.width = w
                    item.height = h
                    item.search_query = search_term
                    raw_tags = v.get("tags")
                    if isinstance(raw_tags, str):
                        item.tags = [
                            tag.strip()
                            for tag in raw_tags.split(",")
                            if tag.strip()
                        ]
                    video_items.append(item)
            return video_items

        except Exception as e:
            logger.error(f"[pixabay] search failed: {safe_error_details(e)}")
        return []
