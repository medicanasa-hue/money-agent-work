"""Internet Archive video provider.

API: https://archive.org/advancedsearch.php  +  /metadata/{id}
Key: Gerekmez
Lisans: Kamu malı (Prelinger Archives, NASA koleksiyonu vb.)
Notlar:
  - Arama → metadata → dosya listesi: her öğe için ek bir GET gerekir.
    Bu nedenle ilk 5 sonuçla sınırlıyız.
  - Dosya boyutuna göre en iyi MP4 seçilir.
  - `length` alanı bazen None veya boş gelir; o durumda 30 sn. tahmin kullanılır.
"""
from typing import List, Optional

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

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_META_URL = "https://archive.org/metadata"
_DOWNLOAD_URL = "https://archive.org/download"
_FALLBACK_DURATION = 30


def _parse_duration(value) -> int:
    """Archive.org length alanını int saniyeye çevirir."""
    if value is None:
        return _FALLBACK_DURATION
    try:
        f = float(str(value).strip())
        return max(1, int(f))
    except (ValueError, TypeError):
        return _FALLBACK_DURATION


class ArchiveOrgProvider(VideoProvider):
    name = "archive_org"
    quality_weight = 0.65

    def is_available(self) -> bool:
        return True

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        params = {
            "q": f"{search_term} mediatype:movies",
            "fl[]": ["identifier", "title"],
            "rows": 8,
            "output": "json",
            "sort[]": "downloads desc",
        }
        logger.info("[archive_org] searching videos")

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
            docs = r.json().get("response", {}).get("docs", [])
        except Exception as e:
            logger.error(f"[archive_org] search failed: {safe_error_details(e)}")
            return []

        video_items: List[MaterialInfo] = []
        for doc in docs[:5]:   # Metadata fetch sayısını sınırla
            identifier = doc.get("identifier")
            if not identifier:
                continue

            item = self._fetch_best_mp4(identifier, minimum_duration)
            if item:
                item.search_query = search_term
                title = doc.get("title")
                if not item.title and isinstance(title, str):
                    item.title = title.strip()
                video_items.append(item)

        logger.info(f"[archive_org] search returned {len(video_items)} videos")
        return video_items

    def _fetch_best_mp4(
        self, identifier: str, minimum_duration: int
    ) -> Optional[MaterialInfo]:
        """Bir Archive.org item'ının en kaliteli MP4 dosyasını döndürür."""
        try:
            r = requests.get(
                f"{_META_URL}/{identifier}",
                headers={"User-Agent": DEFAULT_UA},
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(15, 30),
            )
            raise_for_http_error(r)
            response = r.json()
            files = response.get("files", [])
            raw_metadata = response.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        except Exception as e:
            logger.warning(
                f"[archive_org] metadata fetch failed: {safe_error_details(e)}"
            )
            return None

        # MP4 dosyalarını filtrele
        mp4_files = [
            f for f in files
            if (
                f.get("format") in ("MPEG4", "h.264", "MPEG4 Video", "512Kb MPEG4")
                or str(f.get("name", "")).lower().endswith(".mp4")
            )
            and f.get("source", "") != "derivative"  # Türev dosyaları atla; önce asıl
        ]
        # Asıl yoksa türev de olur
        if not mp4_files:
            mp4_files = [
                f for f in files
                if str(f.get("name", "")).lower().endswith(".mp4")
            ]

        if not mp4_files:
            return None

        # En büyük dosyayı seç (genellikle en yüksek kalite)
        try:
            best = max(mp4_files, key=lambda f: int(f.get("size", 0) or 0))
        except (ValueError, TypeError):
            best = mp4_files[0]

        filename = best.get("name")
        if not filename:
            return None

        duration = _parse_duration(best.get("length"))
        if duration < minimum_duration:
            return None

        item = MaterialInfo()
        item.provider = "archive_org"
        item.url = f"{_DOWNLOAD_URL}/{identifier}/{filename}"
        item.duration = duration
        title = metadata.get("title")
        if isinstance(title, str):
            item.title = title.strip()
        description = metadata.get("description")
        if isinstance(description, str):
            item.description = description.strip()
        subject = metadata.get("subject")
        if isinstance(subject, list):
            item.tags = [
                tag.strip()
                for tag in subject
                if isinstance(tag, str) and tag.strip()
            ]
        elif isinstance(subject, str):
            item.tags = [
                tag.strip()
                for tag in subject.split(";")
                if tag.strip()
            ]
        return item
