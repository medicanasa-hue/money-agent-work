"""Coverr video provider."""
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


class CoverrProvider(VideoProvider):
    name = "coverr"
    quality_weight = 0.90

    def is_available(self) -> bool:
        return bool(config.app.get("coverr_api_keys"))

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        try:
            api_key = get_api_key("coverr_api_keys")
        except ValueError as e:
            logger.warning(f"[coverr] {e}")
            return []

        headers = {"Authorization": f"Bearer {api_key}"}
        params = {
            "query": search_term,
            "page_size": 20,
            "urls": "true",
            "sort": "popular",
            "page": get_search_page("coverr"),
        }
        query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
        logger.info("[coverr] searching videos")

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

            if not isinstance(response, dict) or "hits" not in response:
                logger.error("[coverr] search returned unexpected response")
                return video_items

            for v in response["hits"]:
                try:
                    duration = int(float(v.get("duration") or 0))
                except (TypeError, ValueError):
                    continue
                if duration < minimum_duration:
                    continue

                mp4_url = (v.get("urls") or {}).get("mp4_download")
                if not mp4_url:
                    continue

                item = MaterialInfo()
                item.provider = "coverr"
                item.url = mp4_url
                item.duration = duration
                item.search_query = search_term
                title = v.get("title")
                if isinstance(title, str):
                    item.title = title.strip()
                description = v.get("description")
                if isinstance(description, str):
                    item.description = description.strip()
                raw_tags = v.get("tags")
                if isinstance(raw_tags, list):
                    item.tags = [
                        tag.strip()
                        for tag in raw_tags
                        if isinstance(tag, str) and tag.strip()
                    ]
                video_items.append(item)

            return video_items

        except Exception as e:
            logger.error(f"[coverr] search failed: {safe_error_details(e)}")
        return []
