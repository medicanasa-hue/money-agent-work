"""Pixabay video provider."""
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import (
    get_api_key,
    get_search_page,
    get_tls_verify,
    raise_for_http_error,
    safe_error_details,
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
        video_width, video_height = aspect.to_resolution()

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
            r = requests.get(
                query_url,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(r)
            response = r.json()
            video_items: List[MaterialInfo] = []

            if "hits" not in response:
                logger.error("[pixabay] search returned unexpected response")
                return video_items

            for v in response["hits"]:
                duration = v["duration"]
                if duration < minimum_duration:
                    continue
                best_video = None
                best_pixels = -1
                for video in v["videos"].values():
                    w = int(video["width"])
                    h = int(video.get("height", 0))
                    if w >= video_width and h >= video_height and w * h > best_pixels:
                        best_video = (video, w, h)
                        best_pixels = w * h
                if best_video:
                    video, w, h = best_video
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
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
