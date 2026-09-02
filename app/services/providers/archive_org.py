"""Internet Archive video provider.

API: https://archive.org/advancedsearch.php  +  /metadata/{id}
Key: Gerekmez
Lisans: Karışık; yalnızca açıkça Public Domain, CC0, CC BY veya CC BY-SA
        olarak işaretlenen öğeler otomatik seçilir.
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
    select_best_video_variant,
)

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_META_URL = "https://archive.org/metadata"
_DOWNLOAD_URL = "https://archive.org/download"
_FALLBACK_DURATION = 30
_OPEN_LICENSE_MARKERS = (
    "public domain",
    "cc0",
    "creative commons zero",
    "creativecommons.org/publicdomain/",
    "creativecommons.org/licenses/by/",
    "creativecommons.org/licenses/by-sa/",
    "creative commons attribution",
    "cc by",
    "cc-by",
)
_RESTRICTED_LICENSE_MARKERS = (
    "all rights reserved",
    "copyrighted",
    "restricted",
    "non-commercial",
    "noncommercial",
    "no derivatives",
    "permission required",
    "not public domain",
    "cc by-nc",
    "cc-by-nc",
    "cc by-nd",
    "cc-by-nd",
    "/by-nc",
    "/by-nd",
)


def _parse_duration(value) -> int:
    """Archive.org length alanını int saniyeye çevirir."""
    if value is None:
        return _FALLBACK_DURATION
    try:
        f = float(str(value).strip())
        return max(1, int(f))
    except (ValueError, TypeError):
        return _FALLBACK_DURATION


def _parse_dimension(value: object) -> int:
    """Normalize optional Archive.org frame dimensions without guessing."""
    if isinstance(value, bool):
        return 0
    try:
        dimension = int(float(str(value).strip()))
    except (ValueError, TypeError, OverflowError):
        return 0
    return max(0, dimension)


def _metadata_text(metadata: dict, keys: tuple[str, ...]) -> str:
    """Return compact text from optional Archive.org metadata fields."""
    values = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, (list, tuple)):
            values.extend(
                entry.strip()
                for entry in value
                if isinstance(entry, str) and entry.strip()
            )
    return " ".join(values)


def _open_license_details(metadata: dict) -> tuple[str, str] | None:
    """Return explicit safe license fields, otherwise fail closed."""
    license_url = _metadata_text(metadata, ("licenseurl", "license_url"))
    license_name = _metadata_text(metadata, ("license",))
    rights = _metadata_text(metadata, ("rights",))
    policy_text = " ".join((license_url, license_name, rights)).casefold()
    if not policy_text:
        return None
    if any(marker in policy_text for marker in _RESTRICTED_LICENSE_MARKERS):
        return None
    if not any(marker in policy_text for marker in _OPEN_LICENSE_MARKERS):
        return None

    if not license_name:
        license_name = rights or "Public Domain"
    return license_name, license_url


def _archive_attribution(metadata: dict, identifier: str) -> str:
    creator = _metadata_text(metadata, ("creator", "contributor", "publisher"))
    details_url = f"https://archive.org/details/{identifier}"
    if creator:
        return f"{creator} via Internet Archive ({details_url})"
    return f"Internet Archive ({details_url})"


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

            item = self._fetch_best_mp4(
                identifier,
                minimum_duration,
                video_aspect=video_aspect,
            )
            if item:
                item.search_query = search_term
                title = doc.get("title")
                if not item.title and isinstance(title, str):
                    item.title = title.strip()
                video_items.append(item)

        logger.info(f"[archive_org] search returned {len(video_items)} videos")
        return video_items

    def _fetch_best_mp4(
        self,
        identifier: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
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

        license_details = _open_license_details(metadata)
        if not license_details:
            logger.info("[archive_org] skipping item without an allowed license")
            return None
        license_name, license_url = license_details

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

        # Prefer native target aspect when Archive exposes width/height.
        selected_variant = select_best_video_variant(
            mp4_files,
            video_aspect=video_aspect,
            url_key="name",
        )
        if selected_variant:
            best, _, _ = selected_variant
        else:
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
        item.width = _parse_dimension(best.get("width"))
        item.height = _parse_dimension(best.get("height"))
        item.license = license_name
        item.license_url = license_url
        item.attribution = _archive_attribution(metadata, identifier)
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
