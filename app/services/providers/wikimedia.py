"""Wikimedia Commons video provider.

API: https://commons.wikimedia.org/w/api.php
Key: Gerekmez
Lisans: Dosyaya göre değişir (CC0 / CC-BY / CC-BY-SA); her kullanımda
        kaynak belirtmek gerekebilir.
Notlar:
  - Dosyalar WebM veya OGV olabilir; MP4 derivative varsa o kullanılır,
    yoksa doğrudan dosya URL'si alınır (ffmpeg/moviepy ikisini de okur).
  - Arama + toplu video-info iki API çağrısıyla gerçekleşir.
"""
import html
import re
from typing import List

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect

from .base import VideoProvider
from .utils import get_tls_verify, raise_for_http_error, safe_error_details

_API_URL = "https://commons.wikimedia.org/w/api.php"
_COMMON_PARAMS = {"format": "json", "origin": "*"}
# Wikimedia, User-Agent göndermeyen ya da generic (ör. "python-requests/x.y")
# UA gönderen istekleri 403 ile reddediyor. Politika, tarayıcı UA'sı taklit
# etmek yerine betiği tanımlayan açıklayıcı bir UA istiyor:
# https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
_UA = "MoneyPrinterTurbo/1.0 (https://github.com/harry0703/MoneyPrinterTurbo)"
_HEADERS = {"User-Agent": _UA}


def _clean_extmetadata_value(value) -> str:
    if not isinstance(value, dict):
        return ""
    raw_value = value.get("value")
    if not isinstance(raw_value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_value)
    text = html.unescape(text)
    return " ".join(text.split())


def _build_attribution(title: str, artist: str, license_name: str) -> str:
    parts = [part for part in (title, artist, license_name) if part]
    return " - ".join(parts)


def _positive_dimension(value) -> int:
    try:
        dimension = int(value)
    except (TypeError, ValueError):
        return 0
    return dimension if dimension > 0 else 0


def _select_mp4_derivative(derivatives, video_aspect: VideoAspect):
    """Choose the closest native aspect before preferring more pixels."""
    if not isinstance(derivatives, list):
        return None

    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        target_width, target_height = VideoAspect.portrait.to_resolution()
    target_aspect = target_width / target_height

    selected = None
    selected_score = None
    for index, derivative in enumerate(derivatives):
        if not isinstance(derivative, dict) or derivative.get("type") != "video/mp4":
            continue
        source_url = derivative.get("src")
        if not isinstance(source_url, str) or not source_url.strip():
            continue

        width = _positive_dimension(derivative.get("width"))
        height = _positive_dimension(derivative.get("height"))
        if width and height:
            source_aspect = width / height
            aspect_fit = min(source_aspect, target_aspect) / max(
                source_aspect, target_aspect
            )
            pixels = width * height
        else:
            aspect_fit = 0.0
            pixels = 0
        score = (aspect_fit, pixels, -index)
        if selected_score is None or score > selected_score:
            selected = derivative
            selected_score = score

    return selected


class WikimediaProvider(VideoProvider):
    name = "wikimedia"
    quality_weight = 0.70

    def is_available(self) -> bool:
        return True

    def search(
        self,
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect = VideoAspect.portrait,
    ) -> List[MaterialInfo]:
        # ── 1. Dosya namespace'inde video ara ────────────────────────────
        search_params = {
            **_COMMON_PARAMS,
            "action": "query",
            "list": "search",
            "srsearch": f"{search_term} filetype:video",
            "srnamespace": "6",   # File: namespace
            "srlimit": 15,
        }
        logger.info("[wikimedia] searching videos")

        try:
            r = requests.get(
                _API_URL,
                params=search_params,
                headers=_HEADERS,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(r)
            results = r.json().get("query", {}).get("search", [])
        except Exception as e:
            logger.error(f"[wikimedia] search failed: {safe_error_details(e)}")
            return []

        if not results:
            logger.info("[wikimedia] search returned 0 videos")
            return []

        # ── 2. Toplu videoinfo çek ────────────────────────────────────────
        titles = "|".join(res["title"] for res in results[:10])
        info_params = {
            **_COMMON_PARAMS,
            "action": "query",
            "prop": "videoinfo",
            "viprop": "url|size|duration|mediatype|mime|derivatives|extmetadata",
            "titles": titles,
        }
        try:
            ir = requests.get(
                _API_URL,
                params=info_params,
                headers=_HEADERS,
                proxies=config.proxy,
                verify=get_tls_verify(),
                timeout=(30, 60),
            )
            raise_for_http_error(ir)
            pages = ir.json().get("query", {}).get("pages", {})
        except Exception as e:
            logger.error(
                f"[wikimedia] videoinfo fetch failed: {safe_error_details(e)}"
            )
            return []

        video_items: List[MaterialInfo] = []
        for page_id, page in pages.items():
            if page_id == "-1":
                continue
            vi_list = page.get("videoinfo", [])
            if not vi_list:
                continue
            vi = vi_list[0]

            # Sadece VIDEO medya tipi
            if vi.get("mediatype", "") != "VIDEO":
                continue

            duration = vi.get("duration") or 0
            try:
                duration = int(float(duration))
            except (TypeError, ValueError):
                duration = 0
            if duration < minimum_duration:
                continue

            # MP4 derivative varsa onu al; yoksa doğrudan kaynağı kullan
            url: str | None = None
            selected_derivative = _select_mp4_derivative(
                vi.get("derivatives"), video_aspect
            )
            if selected_derivative:
                url = selected_derivative.get("src")

            if not url:
                # Native MP4 veya WebM/OGV (ffmpeg her ikisini de okur)
                mime = vi.get("mime", "")
                if mime in ("video/mp4", "video/webm", "video/ogg"):
                    url = vi.get("url")

            if not url:
                continue

            item = MaterialInfo()
            item.provider = "wikimedia"
            item.url = url
            item.duration = duration
            item.search_query = search_term
            derivative_width = _positive_dimension(
                selected_derivative.get("width") if selected_derivative else None
            )
            derivative_height = _positive_dimension(
                selected_derivative.get("height") if selected_derivative else None
            )
            if derivative_width and derivative_height:
                item.width = derivative_width
                item.height = derivative_height
            else:
                item.width = _positive_dimension(vi.get("width"))
                item.height = _positive_dimension(vi.get("height"))
            title = page.get("title")
            if isinstance(title, str):
                item.title = title.strip()
            extmetadata = vi.get("extmetadata", {})
            if isinstance(extmetadata, dict):
                artist = _clean_extmetadata_value(extmetadata.get("Artist"))
                license_name = _clean_extmetadata_value(
                    extmetadata.get("LicenseShortName")
                ) or _clean_extmetadata_value(extmetadata.get("UsageTerms"))
                license_url = _clean_extmetadata_value(extmetadata.get("LicenseUrl"))
                item.license = license_name
                item.license_url = license_url
                item.attribution = _build_attribution(
                    item.title,
                    artist,
                    license_name,
                )
            video_items.append(item)

        logger.info(f"[wikimedia] search returned {len(video_items)} videos")
        return video_items
