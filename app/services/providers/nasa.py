"""NASA Image and Video Library provider.

API: https://images-api.nasa.gov
Key: Gerekmez
Lisans: Kamu malı (public domain)
Notlar:
  - Uzay, bilim, doğa temalı içerikler için idealdir.
  - Her arama sonucu için ayrı bir collection.json fetch'i gerekir;
    bu nedenle ilk 5 sonuçla sınırlıyız.
  - Duration bilgisi NASA arama API'sinde dönmez; 30 sn. varsayılan kullanılır.
    Gerçek süre indirme sonrası VideoFileClip ile doğrulanır.
"""
from typing import List

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import (
    DEFAULT_UA,
    get_tls_verify,
    raise_for_http_error,
    safe_error_details,
)

_SEARCH_URL = "https://images-api.nasa.gov/search"
_ESTIMATED_DURATION = 30  # NASA API duration vermez; güvenli tahmin


class NASAProvider(VideoProvider):
    name = "nasa"
    quality_weight = 0.75

    # API key gerektirmez
    def is_available(self) -> bool:
        return True

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        if _ESTIMATED_DURATION < minimum_duration:
            # Tahmin edilen süre minimum'dan kısa; aramayı atla
            logger.info(
                f"[nasa] estimated duration ({_ESTIMATED_DURATION}s) < minimum ({minimum_duration}s), skipping"
            )
            return []

        params = {
            "q": search_term,
            "media_type": "video",
            "page_size": 10,
        }
        logger.info("[nasa] searching videos")

        try:
            r = requests.get(
                _SEARCH_URL,
                params=params,
                headers={"User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(r)
            data = r.json()
            items = data.get("collection", {}).get("items", [])[:5]

            video_items: List[MaterialInfo] = []
            for item in items:
                collection_url = item.get("href")
                if not collection_url:
                    continue
                item_data = item.get("data")
                metadata = (
                    item_data[0]
                    if isinstance(item_data, list)
                    and item_data
                    and isinstance(item_data[0], dict)
                    else {}
                )
                try:
                    cr = requests.get(
                        collection_url,
                        headers={"User-Agent": DEFAULT_UA},
                        proxies=config.proxy,
                        verify=get_tls_verify(),
                        timeout=(15, 30),
                    )
                    raise_for_http_error(cr)
                    assets: List[str] = cr.json()

                    # Tercih sırası: ~orig > plain mp4 > diğer mp4
                    # ~preview ve ~mobile'den kaçın (çok kısa / düşük kalite)
                    orig = [a for a in assets if a.endswith(".mp4") and "~orig" in a]
                    plain = [
                        a for a in assets
                        if a.endswith(".mp4")
                        and "~" not in a.split("/")[-1]
                    ]
                    fallback = [
                        a for a in assets
                        if a.endswith(".mp4")
                        and "~preview" not in a
                        and "~mobile" not in a
                    ]

                    mp4_url = None
                    for pool in (orig, plain, fallback):
                        if pool:
                            mp4_url = pool[0]
                            break

                    if not mp4_url:
                        continue

                    mi = MaterialInfo()
                    mi.provider = "nasa"
                    mi.url = mp4_url
                    mi.duration = _ESTIMATED_DURATION
                    mi.search_query = search_term
                    title = metadata.get("title")
                    if isinstance(title, str):
                        mi.title = title.strip()
                    description = metadata.get("description")
                    if isinstance(description, str):
                        mi.description = description.strip()
                    keywords = metadata.get("keywords")
                    if isinstance(keywords, list):
                        mi.tags = [
                            keyword.strip()
                            for keyword in keywords
                            if isinstance(keyword, str) and keyword.strip()
                        ]
                    video_items.append(mi)

                except Exception as e:
                    logger.warning(
                        f"[nasa] collection fetch failed: {safe_error_details(e)}"
                    )

            logger.info(f"[nasa] search returned {len(video_items)} videos")
            return video_items

        except Exception as e:
            logger.error(f"[nasa] search failed: {safe_error_details(e)}")
        return []
