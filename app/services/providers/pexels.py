"""Pexels video provider."""
from typing import List
from urllib.parse import urlencode

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
        video_orientation = aspect.name
        video_width, video_height = aspect.to_resolution()

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
            r = requests.get(
                query_url,
                headers=headers,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(r)
            response = r.json()
            video_items: List[MaterialInfo] = []

            if "videos" not in response:
                logger.error("[pexels] search returned unexpected response")
                return video_items

            for v in response["videos"]:
                duration = v["duration"]
                if duration < minimum_duration:
                    continue
                best_file = None
                best_pixels = -1
                for vf in v["video_files"]:
                    w = int(vf["width"])
                    h = int(vf["height"])
                    if (
                        w >= video_width
                        and h >= video_height
                        and w * video_height == h * video_width
                        and w * h > best_pixels
                    ):
                        best_file = (vf, w, h)
                        best_pixels = w * h
                if best_file:
                    vf, w, h = best_file
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = vf["link"]
                    item.duration = duration
                    item.width = w
                    item.height = h
                    item.search_query = search_term
                    video_items.append(item)
            return video_items

        except Exception as e:
            logger.error(f"[pexels] search failed: {safe_error_details(e)}")
        return []
