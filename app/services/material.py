"""
app/services/material.py

Değişiklik özeti (orijinal kod aynen korundu, yeni özellikler eklendi):
  - _score_material()          : Aday videoyu puanlar
  - _search_all_providers()    : Tüm aktif provider'ları paralel arar
  - _download_multi_source()   : Havuzdan en iyileri indirir (rastgele mod)
  - _download_multi_ordered()  : Senaryo sırasını koruyarak indirir (script-order mod)
  - download_videos()          : config'de enabled_video_sources varsa otomatik
                                 çok kaynaklı moda geçer; yoksa eski davranış.

Geriye dönük uyumluluk: task.py'ye dokunmadan çalışır.
"""

import difflib
from io import BytesIO
import math
import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import numpy as np
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import material_cache, review_feedback, video_cooldown
from app.services.providers.utils import (
    get_search_page,
    raise_for_http_error,
    safe_error_details,
    select_best_video_variant,
)
from app.services.providers.openverse import (
    is_openverse_photo_fallback_enabled,
    is_safe_openverse_image_url,
    search_photos_openverse,
)
from app.services.providers.europeana import (
    is_europeana_photo_fallback_enabled,
    is_safe_europeana_image_url,
    search_photos_europeana,
)
from app.services.providers.museum import (
    is_museum_photo_fallback_enabled,
    is_safe_museum_image_url,
    search_photos_artic,
    search_photos_met,
)
from app.services.url_security import (
    is_public_ip_address,
    public_http_url_addresses,
)
from app.utils import utils

# ─── Thread-safe API key rotasyonu (eski işlevler için) ──────────────────────
_api_key_counter = 0
_api_key_lock = threading.Lock()

# Stock providers are an optional material source. A stalled media host should
# fail over to the next candidate instead of holding an unattended render for
# several minutes.
_VIDEO_DOWNLOAD_TIMEOUT = (30, 90)
_VIDEO_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 120
_VIDEO_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_VIDEO_DOWNLOAD_BYTES = 512 * 1024 * 1024
_MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024
_MAX_PREVIEW_DOWNLOAD_BYTES = 10 * 1024 * 1024
_MAX_REMOTE_MATERIAL_REDIRECTS = 3


def _read_bounded_response_content(
    response,
    *,
    max_bytes: int,
    resource_name: str,
    total_timeout_seconds: int | None = None,
) -> bytes | None:
    headers = getattr(response, "headers", {}) or {}
    declared_length_value = headers.get("Content-Length")
    declared_length = None
    if declared_length_value is not None:
        try:
            declared_length = int(declared_length_value)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            logger.warning(f"{resource_name} exceeded its size limit")
            return None

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        content = getattr(response, "content", b"")
        if not isinstance(content, bytes):
            return None
        if len(content) > max_bytes:
            logger.warning(f"{resource_name} exceeded its size limit")
            return None
        if declared_length is not None and declared_length > len(content):
            logger.warning(
                f"{resource_name} truncated: expected {declared_length} bytes, "
                f"got {len(content)}"
            )
            return None
        return content

    deadline = (
        time.monotonic() + total_timeout_seconds
        if total_timeout_seconds is not None
        else None
    )
    content = bytearray()
    try:
        for chunk in iter_content(chunk_size=_VIDEO_DOWNLOAD_CHUNK_SIZE):
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(f"{resource_name} exceeded its total time budget")
                return None
            if isinstance(chunk, bytes) and chunk:
                if len(content) + len(chunk) > max_bytes:
                    logger.warning(f"{resource_name} exceeded its size limit")
                    return None
                content.extend(chunk)
    except requests.RequestException as error:
        logger.warning(f"{resource_name} stream failed: {safe_error_details(error)}")
        return None
    if declared_length is not None and declared_length > len(content):
        logger.warning(
            f"{resource_name} truncated: expected {declared_length} bytes, "
            f"got {len(content)}"
        )
        return None
    return bytes(content)


def _read_video_response_content(response) -> bytes | None:
    """Read a stock-video response without letting a slow stream run forever."""
    return _read_bounded_response_content(
        response,
        max_bytes=_MAX_VIDEO_DOWNLOAD_BYTES,
        resource_name="video download",
        total_timeout_seconds=_VIDEO_DOWNLOAD_TOTAL_TIMEOUT_SECONDS,
    )


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _response_peer_address(response) -> str | None:
    """Best-effort origin peer lookup for DNS-rebinding protection."""
    raw_response = getattr(response, "raw", None)
    socket_paths = (
        ("_connection", "sock"),
        ("connection", "sock"),
        ("_fp", "fp", "raw", "_sock"),
        ("_original_response", "fp", "raw", "_sock"),
    )
    for path in socket_paths:
        socket_object = raw_response
        try:
            for attribute in path:
                socket_object = getattr(socket_object, attribute)
        except (AttributeError, TypeError):
            continue
        getpeername = getattr(socket_object, "getpeername", None)
        if not callable(getpeername):
            continue
        try:
            peer = getpeername()
        except OSError:
            continue
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
    return None


def _request_uses_proxy(request_url: str) -> bool:
    configured_proxies = config.proxy if isinstance(config.proxy, dict) else {}
    if any(value for value in configured_proxies.values()):
        return True
    try:
        return bool(requests.utils.get_environ_proxies(request_url))
    except (AttributeError, OSError, ValueError):
        return False


def _response_peer_is_safe(response, request_url: str) -> bool:
    # When a trusted configured/environment proxy is in use, the connected peer
    # is the proxy rather than the origin. The target still passes the DNS gate.
    if _request_uses_proxy(request_url):
        return True
    peer_address = _response_peer_address(response)
    return peer_address is None or is_public_ip_address(peer_address)


def _request_safe_remote_response(
    request_url: str,
    *,
    headers: dict[str, str],
    timeout: tuple[int, int],
    resource_name: str,
    redirect_url_validator: Optional[Callable[[str], bool]] = None,
):
    """GET an untrusted material URL after validating every redirect hop."""
    for redirect_count in range(_MAX_REMOTE_MATERIAL_REDIRECTS + 1):
        if public_http_url_addresses(request_url) is None:
            logger.warning(f"{resource_name} URL was rejected by the network safety gate")
            return None

        try:
            response = requests.get(
                request_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
        except Exception as error:
            logger.warning(
                f"{resource_name} request failed: {safe_error_details(error)}"
            )
            return None

        try:
            peer_is_safe = _response_peer_is_safe(response, request_url)
            status_code = getattr(response, "status_code", 200)
            is_redirect = isinstance(status_code, int) and 300 <= status_code < 400
            response_headers = getattr(response, "headers", {}) or {}
            location = response_headers.get("Location") if is_redirect else None
        except Exception:
            _close_response(response)
            logger.warning(
                f"{resource_name} response metadata could not be validated"
            )
            return None

        if not peer_is_safe:
            _close_response(response)
            logger.warning(
                f"{resource_name} connection was rejected by the network safety gate"
            )
            return None

        if not is_redirect:
            return response

        try:
            redirected_url = urljoin(request_url, str(location or ""))
        except (TypeError, ValueError):
            redirected_url = ""
        _close_response(response)

        if not location:
            logger.warning(f"{resource_name} redirect did not include a target")
            return None
        if redirect_count >= _MAX_REMOTE_MATERIAL_REDIRECTS:
            logger.warning(f"{resource_name} exceeded the redirect limit")
            return None
        if redirect_url_validator is not None:
            try:
                redirect_is_allowed = bool(redirect_url_validator(redirected_url))
            except Exception:
                redirect_is_allowed = False
            if not redirect_is_allowed:
                logger.warning(f"{resource_name} redirect target was rejected")
                return None
        request_url = redirected_url

    return None


def _get_tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )
    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(f"{cfg_key} is not set in config.toml")
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


# ─── Mevcut tekli-kaynak arama fonksiyonları (backward compat) ───────────────

def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    from urllib.parse import urlencode
    aspect = VideoAspect(video_aspect)
    video_orientation = (
        VideoAspect.portrait.name
        if aspect is VideoAspect.portrait_4_5
        else aspect.name
    )
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0.0.0 Safari/537.36",
    }
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
            headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        video_items = []
        if "videos" not in response:
            logger.error("[pexels] search returned unexpected response")
            return video_items
        for v in response["videos"]:
            duration = v["duration"]
            if duration < minimum_duration:
                continue
            best_video = select_best_video_variant(
                v.get("video_files"),
                video_aspect=aspect,
                url_key="link",
            )
            if best_video:
                video, w, h = best_video
                item = MaterialInfo()
                item.provider = "pexels"
                item.url = video["link"]
                item.preview_url = str(v.get("image") or "").strip()
                item.duration = duration
                item.width = w
                item.height = h
                item.search_query = search_term
                video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"[pexels] search failed: {safe_error_details(e)}")
    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    from urllib.parse import urlencode
    aspect = VideoAspect(video_aspect)
    api_key = get_api_key("pixabay_api_keys")
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
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        video_items = []
        if "hits" not in response:
            logger.error("[pixabay] search returned unexpected response")
            return video_items
        for v in response["hits"]:
            duration = v["duration"]
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
                item = MaterialInfo()
                item.provider = "pixabay"
                item.url = video["url"]
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


def _photo_orientation(video_aspect: VideoAspect, *, pixabay: bool = False) -> str:
    aspect = VideoAspect(video_aspect)
    if aspect == VideoAspect.landscape:
        return "horizontal" if pixabay else "landscape"
    if aspect == VideoAspect.square:
        return "all" if pixabay else "square"
    return "vertical" if pixabay else "portrait"


_SMITHSONIAN_SEARCH_URL = "https://api.si.edu/openaccess/api/v1.0/search"
_SMITHSONIAN_IMAGE_HOST = "ids.si.edu"
_SMITHSONIAN_CC0_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"


def _smithsonian_cc0_access(value: object) -> bool:
    return isinstance(value, dict) and str(value.get("access") or "").strip().casefold() == "cc0"


def _smithsonian_image_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return ""
    hostname = parsed.hostname
    if (
        parsed.scheme.casefold() != "https"
        or not isinstance(hostname, str)
        or hostname.casefold() != _SMITHSONIAN_IMAGE_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/ids/")
    ):
        return ""
    return value.strip()


def _smithsonian_high_resolution_resource(media: object):
    if not isinstance(media, dict):
        return None
    resources = media.get("resources")
    if not isinstance(resources, list):
        return None

    candidates = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        image_url = _smithsonian_image_url(resource.get("url"))
        try:
            width = int(resource.get("width") or 0)
            height = int(resource.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if not image_url or width < 480 or height < 480:
            continue
        label = str(resource.get("label") or "").casefold()
        candidates.append(
            (
                width * height,
                "high-resolution" in label,
                image_url,
                width,
                height,
            )
        )
    if not candidates:
        return None
    _, _, image_url, width, height = max(candidates)
    return image_url, width, height


def search_photos_smithsonian(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Find only high-resolution Smithsonian CC0 images for photo fallback."""
    del video_aspect  # Smithsonian does not expose a reliable orientation query.
    if not config.app.get("smithsonian_api_keys"):
        return []

    params = {
        "q": search_term,
        "rows": 20,
        "api_key": get_api_key("smithsonian_api_keys"),
    }
    logger.info("[smithsonian] searching CC0 photos for fallback")
    try:
        response = requests.get(
            _SMITHSONIAN_SEARCH_URL,
            params=params,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        payload = response.json()
        rows = payload.get("response", {}).get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            logger.error("[smithsonian] photo search returned unexpected response")
            return []

        items = []
        seen_urls = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            descriptive = (
                content.get("descriptiveNonRepeating")
                if isinstance(content, dict)
                else None
            )
            online_media = (
                descriptive.get("online_media")
                if isinstance(descriptive, dict)
                else None
            )
            media_items = online_media.get("media") if isinstance(online_media, dict) else None
            if not isinstance(media_items, list):
                continue
            title = str(
                row.get("title")
                or (descriptive.get("title") if isinstance(descriptive, dict) else "")
                or ""
            ).strip()
            for media in media_items:
                if (
                    not isinstance(media, dict)
                    or str(media.get("type") or "").strip().casefold() != "images"
                    or not _smithsonian_cc0_access(media.get("usage"))
                ):
                    continue
                resource = _smithsonian_high_resolution_resource(media)
                if resource is None:
                    continue
                image_url, width, height = resource
                if image_url in seen_urls:
                    continue
                seen_urls.add(image_url)
                item = MaterialInfo(
                    provider="smithsonian",
                    url=image_url,
                    preview_url=_smithsonian_image_url(media.get("thumbnail")),
                    width=width,
                    height=height,
                    search_query=search_term,
                    title=title,
                    license="CC0",
                    license_url=_SMITHSONIAN_CC0_LICENSE_URL,
                    attribution="Smithsonian Open Access (CC0)",
                )
                items.append(item)
        return items
    except Exception:
        logger.error("[smithsonian] photo search failed")
    return []


def search_photos_pexels(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    from urllib.parse import urlencode

    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36",
    }
    params = {
        "query": search_term,
        "per_page": 20,
        "orientation": _photo_orientation(video_aspect),
        "size": "large",
        "page": get_search_page("pexels"),
    }
    query_url = f"https://api.pexels.com/v1/search?{urlencode(params)}"
    logger.info("[pexels] searching photos for fallback")
    try:
        response = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        photos = response.json().get("photos")
        if not isinstance(photos, list):
            logger.error("[pexels] photo search returned unexpected response")
            return []

        items = []
        for photo in photos:
            if not isinstance(photo, dict):
                continue
            src = photo.get("src")
            image_url = src.get("original") if isinstance(src, dict) else ""
            try:
                width = int(photo.get("width") or 0)
                height = int(photo.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if not image_url or width < 480 or height < 480:
                continue
            photographer = str(photo.get("photographer") or "").strip()
            item = MaterialInfo(
                provider="pexels",
                url=image_url,
                width=width,
                height=height,
                search_query=search_term,
                title=str(photo.get("alt") or "").strip(),
                license="Pexels License",
                license_url="https://www.pexels.com/license/",
                attribution=(
                    f"Photo by {photographer} on Pexels" if photographer else ""
                ),
            )
            items.append(item)
        return items
    except Exception as error:
        logger.error(f"[pexels] photo search failed: {safe_error_details(error)}")
    return []


def search_photos_pixabay(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    from urllib.parse import urlencode

    api_key = get_api_key("pixabay_api_keys")
    params = {
        "q": search_term,
        "image_type": "photo",
        "orientation": _photo_orientation(video_aspect, pixabay=True),
        "per_page": 50,
        "page": get_search_page("pixabay"),
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/?{urlencode(params)}"
    logger.info("[pixabay] searching photos for fallback")
    try:
        response = requests.get(
            query_url,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        raise_for_http_error(response)
        hits = response.json().get("hits")
        if not isinstance(hits, list):
            logger.error("[pixabay] photo search returned unexpected response")
            return []

        items = []
        for photo in hits:
            if not isinstance(photo, dict):
                continue
            image_url = photo.get("largeImageURL") or photo.get("webformatURL")
            try:
                width = int(photo.get("imageWidth") or 0)
                height = int(photo.get("imageHeight") or 0)
            except (TypeError, ValueError):
                continue
            if not image_url or width < 480 or height < 480:
                continue
            user = str(photo.get("user") or "").strip()
            raw_tags = photo.get("tags")
            item = MaterialInfo(
                provider="pixabay",
                url=str(image_url),
                width=width,
                height=height,
                search_query=search_term,
                license="Pixabay Content License",
                license_url="https://pixabay.com/service/license-summary/",
                attribution=(f"Photo by {user} on Pixabay" if user else ""),
                tags=(
                    [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
                    if isinstance(raw_tags, str)
                    else []
                ),
            )
            items.append(item)
        return items
    except Exception as error:
        logger.error(f"[pixabay] photo search failed: {safe_error_details(error)}")
    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    from urllib.parse import urlencode
    api_key = get_api_key("coverr_api_keys")
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
            query_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
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
            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue
            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.preview_url = str(v.get("thumbnail") or v.get("poster") or "").strip()
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


# ─── Video kaydetme (değişmedi) ───────────────────────────────────────────────

_VIDEO_QUALITY_SAMPLE_RATIOS = (0.2, 0.5, 0.8)
_VIDEO_MOTION_EXTRA_SAMPLE_RATIOS = (0.35, 0.65)
_MIN_FRAME_BRIGHTNESS = 18.0
_MIN_FRAME_CONTRAST = 3.0
_MIN_FRAME_DETAIL = 0.75
_MAX_STATIC_FRAME_MOTION = 0.75
_MIN_MOTION_SAMPLE_PAIRS = 2
_MIN_SLIDESHOW_STATIC_PAIR_COUNT = 2
_MIN_SLIDESHOW_ABRUPT_MOTION = 32.0
_MIN_SLIDESHOW_ABRUPT_PAIR_COUNT = 2
_MIN_PREVIEW_QUALITY_SCORE = 0.20
_MAX_PREVIEW_IMAGE_PIXELS = 16_000_000
_DEFAULT_PREVIEW_QUALITY_RERANK_MAX_CANDIDATES = 3
_MAX_PREVIEW_QUALITY_RERANK_CANDIDATES = 5
_PREVIEW_QUALITY_RERANK_WEIGHT = 0.15


def _is_video_visual_quality_filter_enabled() -> bool:
    value = config.app.get("video_visual_quality_filter_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _frame_visual_quality_metrics(frame) -> Optional[tuple[float, float, float]]:
    """Return brightness, contrast, and detail metrics for a decoded video frame."""
    try:
        dimensions = getattr(frame, "ndim", 0)
        sampled_frame = frame[::3, ::3]
        if dimensions == 2:
            luma = sampled_frame.astype("float32")
        elif dimensions == 3 and sampled_frame.shape[2] >= 3:
            luma = sampled_frame[..., :3].astype("float32").mean(axis=2)
        else:
            return None
        if not luma.size:
            return None

        detail_values = []
        if luma.shape[0] > 1:
            detail_values.append(float(abs(luma[1:, :] - luma[:-1, :]).mean()))
        if luma.shape[1] > 1:
            detail_values.append(float(abs(luma[:, 1:] - luma[:, :-1]).mean()))
        detail = sum(detail_values) / len(detail_values) if detail_values else 0.0
        return float(luma.mean()), float(luma.std()), detail
    except Exception:
        return None


def _is_video_frame_visually_weak(frame) -> Optional[bool]:
    metrics = _frame_visual_quality_metrics(frame)
    if metrics is None:
        return None
    brightness, contrast, detail = metrics
    return (
        brightness < _MIN_FRAME_BRIGHTNESS
        or (
            contrast < _MIN_FRAME_CONTRAST
            and detail < _MIN_FRAME_DETAIL
        )
    )


def _frame_motion_score(first_frame, second_frame) -> Optional[float]:
    """Return sampled luma change between two decoded frames."""
    try:
        first = np.asarray(first_frame)[::6, ::6]
        second = np.asarray(second_frame)[::6, ::6]
        if first.shape != second.shape:
            return None
        if first.ndim == 2:
            first_luma = first.astype("float32")
            second_luma = second.astype("float32")
        elif first.ndim == 3 and first.shape[2] >= 3:
            first_luma = first[..., :3].astype("float32").mean(axis=2)
            second_luma = second[..., :3].astype("float32").mean(axis=2)
        else:
            return None
        if not first_luma.size:
            return None
        if max(float(first_luma.max()), float(second_luma.max())) <= 1.0:
            first_luma *= 255.0
            second_luma *= 255.0
        return float(abs(first_luma - second_luma).mean())
    except Exception:
        return None


def _is_preview_quality_filter_enabled() -> bool:
    value = config.app.get("preview_quality_filter_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _is_preview_quality_rerank_enabled() -> bool:
    value = config.app.get("preview_quality_rerank_enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _preview_quality_rerank_max_candidates() -> int:
    if (
        not _is_preview_quality_rerank_enabled()
        or not _is_preview_quality_filter_enabled()
    ):
        return 0
    try:
        requested_limit = int(
            config.app.get(
                "preview_quality_rerank_max_candidates",
                _DEFAULT_PREVIEW_QUALITY_RERANK_MAX_CANDIDATES,
            )
        )
    except (TypeError, ValueError):
        return 0
    return min(_MAX_PREVIEW_QUALITY_RERANK_CANDIDATES, max(0, requested_limit))


def _preview_quality_score_from_frame(frame) -> Optional[float]:
    metrics = _frame_visual_quality_metrics(frame)
    if metrics is None:
        return None
    brightness, contrast, detail = metrics
    brightness_score = min(
        1.0,
        max(0.0, (brightness - _MIN_FRAME_BRIGHTNESS) / 64.0),
    )
    contrast_score = min(
        1.0,
        max(0.0, (contrast - _MIN_FRAME_CONTRAST) / 32.0),
    )
    detail_score = min(
        1.0,
        max(0.0, (detail - _MIN_FRAME_DETAIL) / 12.0),
    )
    if (
        brightness < _MIN_FRAME_BRIGHTNESS
        or (contrast < _MIN_FRAME_CONTRAST and detail < _MIN_FRAME_DETAIL)
    ):
        return 0.15 * brightness_score
    return 0.25 * brightness_score + 0.35 * contrast_score + 0.40 * detail_score


def _preview_visual_quality_score(item: MaterialInfo) -> Optional[float]:
    """Return a cached 0-1 preview score, or None when a preview cannot be read."""
    if not _is_preview_quality_filter_enabled():
        return None

    cached_score = getattr(item, "preview_quality_score", None)
    if isinstance(cached_score, (int, float)) and 0.0 <= cached_score <= 1.0:
        return float(cached_score)

    preview_url = str(getattr(item, "preview_url", "") or "").strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    }
    response = _request_safe_remote_response(
        preview_url,
        headers=headers,
        timeout=(10, 20),
        resource_name="material preview download",
    )
    if response is None:
        return None

    try:
        if getattr(response, "status_code", 200) != 200:
            return None
        response_headers = getattr(response, "headers", {}) or {}
        content_type = str(response_headers.get("Content-Type", "")).lower()
        if content_type and not content_type.startswith("image/"):
            return None
        content = _read_bounded_response_content(
            response,
            max_bytes=_MAX_PREVIEW_DOWNLOAD_BYTES,
            resource_name="material preview download",
        )
        if not content:
            return None
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width * height > _MAX_PREVIEW_IMAGE_PIXELS
            ):
                return None
            frame = np.asarray(image.convert("RGB"))
    except Exception as error:
        logger.debug(f"could not score material preview: {safe_error_details(error)}")
        return None
    finally:
        _close_response(response)

    score = _preview_quality_score_from_frame(frame)
    if score is not None:
        item.preview_quality_score = score
    return score


def _rerank_materials_with_preview_quality(
    items: List[MaterialInfo],
    max_clip_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """Modestly reorder only a bounded ranked prefix using provider previews.

    This deliberately stays outside ``_rank_materials`` because preview scoring
    performs HTTP image requests. Candidate-search and manual-selection flows
    must remain read-only until material download begins.
    """
    candidate_limit = _preview_quality_rerank_max_candidates()
    if candidate_limit <= 0 or len(items) < 2:
        return items

    scored_items = []
    for index, item in enumerate(items[:candidate_limit]):
        try:
            preview_score = _preview_visual_quality_score(item)
        except Exception:
            preview_score = None
        if not isinstance(preview_score, (int, float)) or not math.isfinite(
            preview_score
        ):
            continue
        try:
            base_score = _score_material(item, max_clip_duration, video_aspect)
        except Exception:
            continue
        combined_score = base_score + _PREVIEW_QUALITY_RERANK_WEIGHT * min(
            1.0, max(0.0, float(preview_score))
        )
        scored_items.append((index, combined_score, item))

    if len(scored_items) < 2:
        return items

    reranked = list(items)
    original_positions = sorted(index for index, _, _ in scored_items)
    quality_order = sorted(scored_items, key=lambda entry: (-entry[1], entry[0]))
    for target_index, (_, _, item) in zip(original_positions, quality_order):
        reranked[target_index] = item
    logger.info(
        "reranked {} top material candidates using provider preview quality",
        len(scored_items),
    )
    return reranked


def _save_provider_material(item: MaterialInfo, save_dir: str) -> str:
    """Resolve provider-specific download references only for selected clips."""
    video_url = str(getattr(item, "url", "") or "").strip()
    if not video_url:
        return ""
    provider_name = str(getattr(item, "provider", "") or "").strip()
    minimum_duration = 0.0
    if provider_name == "vecteezy":
        from app.services.providers.vecteezy import resolve_vecteezy_download_url

        video_url = resolve_vecteezy_download_url(item)
        if not video_url:
            return ""
    if provider_name in {"vecteezy", "noaa_ocean"}:
        try:
            minimum_duration = max(0.0, float(getattr(item, "duration", 0) or 0))
        except (TypeError, ValueError):
            minimum_duration = 0.0
    return save_video(
        video_url=video_url,
        save_dir=save_dir,
        minimum_duration=minimum_duration,
    )


def _save_ranked_material(item: MaterialInfo, save_dir: str) -> str:
    """Skip clearly weak provider previews before requesting the video bytes."""
    video_url = str(getattr(item, "url", "") or "").strip()
    if not video_url:
        return ""
    preview_score = _preview_visual_quality_score(item)
    if (
        preview_score is not None
        and preview_score < _MIN_PREVIEW_QUALITY_SCORE
    ):
        logger.info("skipping video material with weak provider preview")
        return ""
    return _save_provider_material(item, save_dir)


def _is_video_clip_visually_acceptable(clip) -> bool:
    """Reject clips whose sampled frames are consistently near-black or flat."""
    if not _is_video_visual_quality_filter_enabled():
        return True

    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return True
    try:
        duration = max(0.0, float(getattr(clip, "duration", 0) or 0))
    except (TypeError, ValueError):
        return True

    weak_samples = 0
    inspected_samples = 0
    sampled_frames = []
    for ratio in _VIDEO_QUALITY_SAMPLE_RATIOS:
        try:
            frame = get_frame(duration * ratio)
        except Exception:
            continue
        is_weak = _is_video_frame_visually_weak(frame)
        if is_weak is None:
            continue
        inspected_samples += 1
        weak_samples += int(is_weak)
        sampled_frames.append((ratio, frame))

    if not inspected_samples:
        return True
    if weak_samples * 2 > inspected_samples:
        return False

    for ratio in _VIDEO_MOTION_EXTRA_SAMPLE_RATIOS:
        try:
            frame = get_frame(duration * ratio)
        except Exception:
            continue
        if _frame_visual_quality_metrics(frame) is not None:
            sampled_frames.append((ratio, frame))

    sampled_frames.sort(key=lambda sample: sample[0])
    has_complete_motion_samples = len(sampled_frames) == (
        len(_VIDEO_QUALITY_SAMPLE_RATIOS)
        + len(_VIDEO_MOTION_EXTRA_SAMPLE_RATIOS)
    )
    motion_scores = [
        motion_score
        for motion_score in (
            _frame_motion_score(previous, current)
            for (_, previous), (_, current) in zip(
                sampled_frames,
                sampled_frames[1:],
            )
        )
        if motion_score is not None
    ]
    if (
        has_complete_motion_samples
        and len(motion_scores) >= _MIN_MOTION_SAMPLE_PAIRS
        and all(score < _MAX_STATIC_FRAME_MOTION for score in motion_scores)
    ):
        logger.debug("rejecting frozen video material with no sampled motion")
        return False
    if (
        has_complete_motion_samples
        and sum(score < _MAX_STATIC_FRAME_MOTION for score in motion_scores)
        >= _MIN_SLIDESHOW_STATIC_PAIR_COUNT
        and sum(score >= _MIN_SLIDESHOW_ABRUPT_MOTION for score in motion_scores)
        >= _MIN_SLIDESHOW_ABRUPT_PAIR_COUNT
    ):
        logger.debug("rejecting slideshow-like video material")
        return False
    return True


def _is_saved_video_usable(video_path: str, minimum_duration: float = 0.0) -> bool:
    clip = None
    try:
        clip = VideoFileClip(video_path)
        duration = clip.duration
        fps = clip.fps
        if duration <= 0 or fps <= 0:
            return False
        try:
            required_duration = max(0.0, float(minimum_duration or 0))
        except (TypeError, ValueError):
            required_duration = 0.0
        if duration < required_duration:
            logger.warning("rejecting video material shorter than required duration")
            return False
        if _is_video_clip_visually_acceptable(clip):
            return True
        logger.warning("rejecting visibly weak video material")
        return False
    except Exception as error:
        logger.warning(f"invalid video file: {video_path} => {str(error)}")
        return False
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception as close_error:
                logger.warning(
                    f"failed to close video clip: {video_path}, "
                    f"error: {str(close_error)}"
                )


def save_video(
    video_url: str,
    save_dir: str = "",
    minimum_duration: float = 0.0,
) -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_hash = utils.stable_cache_key(
        _material_url_key(video_url) or video_url.split("?")[0]
    )
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return (
            video_path
            if _is_saved_video_usable(video_path, minimum_duration=minimum_duration)
            else ""
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0.0.0 Safari/537.36"
    }
    resp = _request_safe_remote_response(
        video_url,
        headers=headers,
        timeout=_VIDEO_DOWNLOAD_TIMEOUT,
        resource_name="video download",
    )
    if resp is None:
        return ""

    try:
        # getattr ile erişiyoruz: gerçek requests.Response nesnelerinde bu alanlar
        # her zaman mevcut, ama testlerdeki basit mock nesneleri sadece .content
        # tanımlayabiliyor; onlarla geriye dönük uyumluluğu koruyoruz.
        status_code = getattr(resp, "status_code", 200)
        if status_code != 200:
            logger.warning(f"video download failed: HTTP {status_code}")
            return ""

        resp_headers = getattr(resp, "headers", {}) or {}
        content_type = resp_headers.get("Content-Type", "")
        if content_type and not (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        ):
            logger.warning(
                "video download returned non-video content: "
                f"Content-Type={content_type}"
            )
            return ""

        content = _read_video_response_content(resp)
        if content is None:
            return ""

        try:
            with open(video_path, "wb") as f:
                f.write(content)
        except OSError as error:
            logger.warning(
                f"failed to save downloaded video: {safe_error_details(error)}"
            )
            return ""
    finally:
        _close_response(resp)

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        if _is_saved_video_usable(video_path, minimum_duration=minimum_duration):
            return video_path
        try:
            os.remove(video_path)
        except Exception as remove_error:
            logger.warning(
                f"failed to remove invalid video file: {video_path}, "
                f"error: {str(remove_error)}"
            )
    return ""


# ─── Çok kaynaklı yardımcı fonksiyonlar ──────────────────────────────────────

def save_image(
    image_url: str,
    save_dir: str = "",
    *,
    redirect_url_validator: Optional[Callable[[str], bool]] = None,
) -> str:
    if not save_dir:
        save_dir = utils.storage_dir("local_videos", create=True)
    elif not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_hash = utils.stable_cache_key(
        _material_url_key(image_url) or image_url.split("?")[0]
    )
    image_path = os.path.join(save_dir, f"img-{url_hash}.jpg")
    if os.path.isfile(image_path) and os.path.getsize(image_path) > 0:
        logger.info(f"image already exists: {image_path}")
        return image_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    }
    response = _request_safe_remote_response(
        image_url,
        headers=headers,
        timeout=(60, 240),
        resource_name="image download",
        redirect_url_validator=redirect_url_validator,
    )
    if response is None:
        return ""

    try:
        if getattr(response, "status_code", 200) != 200:
            logger.warning("image download failed")
            return ""

        response_headers = getattr(response, "headers", {}) or {}
        content_type = str(response_headers.get("Content-Type", "")).lower()
        if content_type and not content_type.startswith("image/"):
            logger.warning("image download returned non-image content")
            return ""

        content = _read_bounded_response_content(
            response,
            max_bytes=_MAX_IMAGE_DOWNLOAD_BYTES,
            resource_name="image download",
        )
        if not content:
            logger.warning("image download returned empty content")
            return ""

        with open(image_path, "wb") as file:
            file.write(content)
        return image_path if os.path.getsize(image_path) > 0 else ""
    finally:
        _close_response(response)


def _score_resolution(
    item: MaterialInfo,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> float:
    width = int(getattr(item, "width", 0) or 0)
    height = int(getattr(item, "height", 0) or 0)
    if width <= 0 or height <= 0:
        # Keep metadata-poor providers usable as a fallback, but do not let
        # an unknown resolution tie with a verified native HD source.
        return 0.70

    long_edge = max(width, height)
    short_edge = min(width, height)
    if long_edge < 720 or short_edge < 360:
        resolution_score = 0.55
    elif long_edge < 1080 or short_edge < 540:
        resolution_score = 0.75
    else:
        resolution_score = 1.0

    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
        source_aspect = width / height
        target_aspect = target_width / target_height
        aspect_compatibility = min(source_aspect, target_aspect) / max(
            source_aspect, target_aspect
        )
        # Prefer sources that retain more of the original frame when filling
        # the requested output aspect. This also distinguishes a square clip
        # from a true portrait clip, where the older orientation-only check
        # treated both as equally suitable.
        resolution_score *= 0.75 + 0.25 * aspect_compatibility
        upscale_factor = max(target_width / width, target_height / height)
        if upscale_factor > 1:
            # Keep a useful lower-resolution clip available when it is the
            # only relevant option, while preferring a source that will not
            # be enlarged during the final render.
            resolution_score *= max(0.6, 1 / upscale_factor)
        source_orientation = width - height
        target_orientation = target_width - target_height
        if source_orientation and target_orientation and (
            source_orientation > 0
        ) != (target_orientation > 0):
            resolution_score *= 0.85
    except Exception as exc:
        logger.debug(f"failed to apply material orientation score adjustment: {exc}")
    return resolution_score


def _score_material(
    item: MaterialInfo,
    max_clip_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> float:
    """Return a 0.0-1.0 material score from quality and relevance signals.

    Components: provider baseline (20%), duration fit (35%), resolution and
    orientation (20%), and search-query/content match (25%).
    """
    try:
        from app.services.providers import PROVIDER_QUALITY_WEIGHTS
        quality = PROVIDER_QUALITY_WEIGHTS.get(item.provider, 0.70)
    except ImportError:
        quality = 0.70

    # Klip süresi hedef süreden uzunsa ffmpeg keser → sorun yok, küçük penaltı
    # Klip süresi hedeften kısaysa kullanışlılık düşer → daha büyük penaltı
    if item.duration >= max_clip_duration:
        surplus = item.duration - max_clip_duration
        duration_score = max(0.70, 1.0 - surplus / 120.0)
    else:
        duration_score = 0.50 * (item.duration / max(max_clip_duration, 1))

    resolution_score = _score_resolution(item, video_aspect)
    content_match_score = _score_content_match(item)
    base_score = (
        0.20 * quality
        + 0.35 * duration_score
        + 0.20 * resolution_score
        + 0.25 * content_match_score
    )
    try:
        feedback_adjustment = review_feedback.get_provider_feedback_score_adjustment(
            item.provider
        )
    except Exception:
        feedback_adjustment = 0.0
    return max(0.0, min(1.0, base_score + feedback_adjustment))


def _normalized_word_tokens(value) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return {
        token
        for token in re.findall(r"[^\W_]+", text)
        if len(token) > 1
    }


_STEM_MATCH_CREDIT = 0.85
_CLOSE_MATCH_CREDIT = 0.6
_CLOSE_MATCH_CUTOFF = 0.8
_CONCEPT_MATCH_CREDIT = 0.45
# One concrete visual-concept match is enough to distinguish a scene-relevant
# candidate from results that only share a generic query term.
_SUBSTANTIVE_CONTENT_MATCH_THRESHOLD = 0.4
_VISUAL_CONCEPT_GROUPS = (
    frozenset(
        {
            "economy",
            "economic",
            "economics",
            "finance",
            "financial",
            "ekonomi",
            "finans",
        }
    ),
    frozenset(
        {
            "inflation",
            "price",
            "prices",
            "pricing",
            "cost",
            "costs",
            "grocery",
            "groceries",
            "enflasyon",
            "fiyat",
            "fiyatlar",
            "pahalilik",
        }
    ),
    frozenset(
        {
            "currency",
            "currencies",
            "exchange",
            "forex",
            "dollar",
            "euro",
            "money",
            "para",
            "doviz",
            "kur",
        }
    ),
    frozenset(
        {
            "investment",
            "investing",
            "investor",
            "stock",
            "stocks",
            "share",
            "shares",
            "trading",
            "trade",
            "market",
            "markets",
            "yatirim",
            "borsa",
        }
    ),
    frozenset(
        {
            "interest",
            "rate",
            "rates",
            "loan",
            "loans",
            "mortgage",
            "bank",
            "banks",
            "faiz",
            "kredi",
            "banka",
        }
    ),
)


def _shares_visual_concept(query_token: str, content_tokens: set[str]) -> bool:
    return any(
        query_token in group and bool(content_tokens & group)
        for group in _VISUAL_CONCEPT_GROUPS
    )


def _light_stem(token: str) -> str:
    """Return a conservative stem for normalized material-search tokens."""
    if not isinstance(token, str):
        return ""

    if token.endswith("ies") and len(token) - 3 >= 3:
        return f"{token[:-3]}y"
    if token.endswith(("ing", "ed")):
        suffix_length = 3 if token.endswith("ing") else 2
        root = token[:-suffix_length]
        if len(root) >= 3:
            if len(root) >= 4 and root[-1] == root[-2]:
                root = root[:-1]
            return root
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        root = token[:-1]
        if len(root) >= 3:
            return root

    for suffix in ("iyor", "ıyor", "uyor", "üyor"):
        root = token.removesuffix(suffix)
        if root != token and len(root) >= 3:
            return root
    for suffix in ("lar", "ler", "dan", "den", "tan", "ten"):
        root = token.removesuffix(suffix)
        if root != token and len(root) >= 4:
            return root
    for suffix in ("da", "de", "ta", "te"):
        root = token.removesuffix(suffix)
        if root != token and len(root) >= 5:
            return root
    return token


def _score_content_match(
    item: MaterialInfo,
    search_query: str | None = None,
) -> float:
    query_tokens = _normalized_word_tokens(
        getattr(item, "search_query", "") if search_query is None else search_query
    )
    if not query_tokens:
        return 0.0

    content_tokens = set()
    content_tokens.update(_normalized_word_tokens(getattr(item, "title", "")))
    content_tokens.update(_normalized_word_tokens(getattr(item, "description", "")))

    tags = getattr(item, "tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, (list, tuple, set)):
        for tag in tags:
            if isinstance(tag, str):
                content_tokens.update(_normalized_word_tokens(tag))

    if not content_tokens:
        return 0.0

    exact_matches = query_tokens & content_tokens
    score = float(len(exact_matches))
    content_stems = {_light_stem(token) for token in content_tokens}
    for query_token in query_tokens - exact_matches:
        if _light_stem(query_token) in content_stems:
            score += _STEM_MATCH_CREDIT
        elif difflib.get_close_matches(
            query_token,
            content_tokens,
            n=1,
            cutoff=_CLOSE_MATCH_CUTOFF,
        ):
            score += _CLOSE_MATCH_CREDIT
        elif _shares_visual_concept(query_token, content_tokens):
            score += _CONCEPT_MATCH_CREDIT

    return min(1.0, score / len(query_tokens))


def _dedupe_candidate_score(
    item: MaterialInfo,
    max_clip_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> tuple[float, float]:
    return (
        _score_material(item, max_clip_duration, video_aspect),
        _score_content_match(item),
    )


def _get_max_material_duration() -> int:
    try:
        return int(config.app.get("max_material_duration", 180))
    except (TypeError, ValueError):
        return 180


def _filter_quality_materials(
    items: List[MaterialInfo],
    max_clip_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    max_duration = _get_max_material_duration()
    filtered_by_url = {}
    for item in items:
        url = str(getattr(item, "url", "") or "").strip()
        url_key = _material_url_key(url)
        duration = float(getattr(item, "duration", 0) or 0)
        if not url_key:
            continue
        if duration < max_clip_duration:
            continue
        if max_duration > 0 and duration > max_duration:
            continue
        previous = filtered_by_url.get(url_key)
        if previous is None or (
            _dedupe_candidate_score(item, max_clip_duration, video_aspect)
            > _dedupe_candidate_score(previous, max_clip_duration, video_aspect)
        ):
            filtered_by_url[url_key] = item

    deduplicated_items = list(filtered_by_url.values())
    items_by_query = {}
    for item in deduplicated_items:
        query_key = " ".join(
            str(getattr(item, "search_query", "") or "").split()
        ).casefold()
        if query_key:
            items_by_query.setdefault(query_key, []).append(item)

    filtered_items = []
    for item in deduplicated_items:
        width = int(getattr(item, "width", 0) or 0)
        height = int(getattr(item, "height", 0) or 0)
        if width <= 0 or height <= 0 or _score_resolution(item, video_aspect) >= 0.75:
            filtered_items.append(item)
            continue

        query_key = " ".join(
            str(getattr(item, "search_query", "") or "").split()
        ).casefold()
        alternatives = items_by_query.get(query_key, [])
        content_score = _score_content_match(item)
        has_equally_relevant_high_resolution_alternative = any(
            alternative is not item
            and int(getattr(alternative, "width", 0) or 0) > 0
            and int(getattr(alternative, "height", 0) or 0) > 0
            and _score_resolution(alternative, video_aspect) >= 0.75
            and _score_content_match(alternative) >= content_score
            for alternative in alternatives
        )
        if not has_equally_relevant_high_resolution_alternative:
            filtered_items.append(item)

    return filtered_items


def _is_video_cooldown_enabled() -> bool:
    value = config.app.get("video_cooldown_enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _get_video_cooldown_days() -> int:
    try:
        return max(1, int(config.app.get("video_cooldown_days", 7)))
    except (TypeError, ValueError):
        return 7


def _split_video_cooldown_materials(
    items: List[MaterialInfo],
    cooldown_stats: Optional[dict] = None,
) -> tuple[List[MaterialInfo], List[MaterialInfo]]:
    if not _is_video_cooldown_enabled():
        return items, []

    available, skipped = video_cooldown.filter_recently_used(
        items,
        days=_get_video_cooldown_days(),
    )
    if skipped and available:
        if cooldown_stats is not None:
            cooldown_stats["moved_recent_count"] = (
                int(cooldown_stats.get("moved_recent_count", 0) or 0)
                + len(skipped)
            )
            cooldown_stats["days"] = _get_video_cooldown_days()
        logger.info(
            f"video cooldown moved {len(skipped)} recently used candidates "
            "behind fresh candidates"
        )
        return available, skipped
    if skipped:
        logger.warning(
            "video cooldown found only recently used candidates; "
            "falling back to original pool"
        )
    return items, []


def _split_ordered_candidate_groups_by_cooldown(
    candidate_groups: List[tuple],
) -> tuple[List[tuple], List[tuple]]:
    """Keep recent scene candidates as a fallback until fresh groups are spent."""
    if not candidate_groups or not _is_video_cooldown_enabled():
        return candidate_groups, []

    recently_used_urls = video_cooldown.recent_urls(_get_video_cooldown_days())
    if not recently_used_urls:
        return candidate_groups, []

    fresh_groups = []
    deferred_groups = []
    for search_term, items in candidate_groups:
        fresh_items = []
        deferred_items = []
        for item in items:
            normalized_url = video_cooldown.normalize_url(
                getattr(item, "url", "")
            )
            if normalized_url and normalized_url in recently_used_urls:
                deferred_items.append(item)
            else:
                fresh_items.append(item)
        if fresh_items:
            fresh_groups.append((search_term, fresh_items))
        if deferred_items:
            deferred_groups.append((search_term, deferred_items))

    return fresh_groups, deferred_groups


def _mark_video_cooldown_used(item: MaterialInfo) -> None:
    if not _is_video_cooldown_enabled():
        return
    url = str(getattr(item, "url", "") or "").strip()
    if not url:
        return
    try:
        video_cooldown.mark_used(
            url,
            provider=str(getattr(item, "provider", "") or ""),
        )
    except Exception as e:
        logger.warning(f"failed to update video cooldown store: {e}")


def _material_rank_key(
    item: MaterialInfo,
    max_clip_duration: int,
    video_aspect: VideoAspect,
) -> tuple[bool, float, float]:
    content_match_score = _score_content_match(item)
    return (
        content_match_score >= _SUBSTANTIVE_CONTENT_MATCH_THRESHOLD,
        _score_material(item, max_clip_duration, video_aspect),
        content_match_score,
    )


_DEFAULT_TWELVELABS_MATERIAL_RERANK_MAX_CANDIDATES = 6
_MAX_TWELVELABS_MATERIAL_RERANK_CANDIDATES = 12
_DEFAULT_TWELVELABS_VISUAL_RERANK_MAX_CANDIDATES = 2
_MAX_TWELVELABS_VISUAL_RERANK_CANDIDATES = 3


def _twelvelabs_material_rerank_max_candidates() -> int:
    """Return the bounded semantic-ranking budget for one material pool."""
    if not config.app.get("twelvelabs_material_rerank_enabled"):
        return 0
    try:
        requested_limit = int(
            config.app.get(
                "twelvelabs_material_rerank_max_candidates",
                _DEFAULT_TWELVELABS_MATERIAL_RERANK_MAX_CANDIDATES,
            )
        )
    except (TypeError, ValueError):
        return 0
    return min(_MAX_TWELVELABS_MATERIAL_RERANK_CANDIDATES, max(0, requested_limit))


def _twelvelabs_visual_rerank_max_candidates() -> int:
    """Return the small, bounded video-understanding budget for one pool."""
    if not config.app.get("twelvelabs_visual_rerank_enabled"):
        return 0
    try:
        requested_limit = int(
            config.app.get(
                "twelvelabs_visual_rerank_max_candidates",
                _DEFAULT_TWELVELABS_VISUAL_RERANK_MAX_CANDIDATES,
            )
        )
    except (TypeError, ValueError):
        return 0
    return min(_MAX_TWELVELABS_VISUAL_RERANK_CANDIDATES, max(0, requested_limit))


def _material_semantic_text(item: MaterialInfo) -> str:
    """Build the provider metadata text used for a semantic relevance check."""
    parts = []
    for value in (getattr(item, "title", ""), getattr(item, "description", "")):
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    tags = getattr(item, "tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, (list, tuple, set)):
        parts.extend(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip())
    return " ".join(parts)


def _rerank_materials_with_twelvelabs(items: List[MaterialInfo]) -> List[MaterialInfo]:
    """Reorder a small ranked pool by Marengo similarity without widening it."""
    candidate_limit = _twelvelabs_material_rerank_max_candidates()
    if candidate_limit <= 0 or len(items) < 2:
        return items
    try:
        from app.services import twelvelabs
    except Exception:
        return items
    if not twelvelabs.is_enabled():
        return items

    scored_items = []
    reviewed_count = 0
    for index, item in enumerate(items):
        if reviewed_count >= candidate_limit:
            break
        query = str(getattr(item, "search_query", "") or "").strip()
        candidate_text = _material_semantic_text(item)
        if not query or not candidate_text:
            continue
        reviewed_count += 1
        try:
            similarity = twelvelabs.semantic_text_similarity(query, candidate_text)
            score = float(similarity) if similarity is not None else None
        except (TypeError, ValueError):
            score = None
        except Exception:
            score = None
        if score is not None and math.isfinite(score):
            scored_items.append((index, score, item))

    if len(scored_items) < 2:
        return items

    reranked = list(items)
    original_positions = sorted(index for index, _, _ in scored_items)
    semantic_order = sorted(scored_items, key=lambda entry: (-entry[1], entry[0]))
    for target_index, (_, _, item) in zip(original_positions, semantic_order):
        reranked[target_index] = item
    logger.info(
        "TwelveLabs Marengo reranked {} material candidates by semantic relevance.",
        len(scored_items),
    )
    return reranked


def _rerank_materials_with_twelvelabs_visual(
    items: List[MaterialInfo],
) -> List[MaterialInfo]:
    """Reorder a small ranked pool using actual video content when available."""
    candidate_limit = _twelvelabs_visual_rerank_max_candidates()
    if candidate_limit <= 0 or len(items) < 2:
        return items
    try:
        from app.services import twelvelabs
    except Exception:
        return items
    if not twelvelabs.is_enabled():
        return items

    scored_items = []
    reviewed_count = 0
    for index, item in enumerate(items):
        if reviewed_count >= candidate_limit:
            break
        query = str(getattr(item, "search_query", "") or "").strip()
        url = str(getattr(item, "url", "") or "").strip()
        parsed_url = urlsplit(url)
        if (
            not query
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
        ):
            continue
        reviewed_count += 1
        try:
            similarity = twelvelabs.visual_video_similarity(query, url)
            score = float(similarity) if similarity is not None else None
        except (TypeError, ValueError):
            score = None
        except Exception:
            score = None
        if score is not None and math.isfinite(score):
            scored_items.append((index, score, item))

    if len(scored_items) < 2:
        return items

    reranked = list(items)
    original_positions = sorted(index for index, _, _ in scored_items)
    visual_order = sorted(scored_items, key=lambda entry: (-entry[1], entry[0]))
    for target_index, (_, _, item) in zip(original_positions, visual_order):
        reranked[target_index] = item
    logger.info(
        "TwelveLabs Marengo visually reranked {} material candidates.",
        len(scored_items),
    )
    return reranked


def _rank_materials(
    items: List[MaterialInfo],
    max_clip_duration: int,
    randomize: bool = False,
    cooldown_stats: Optional[dict] = None,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    items = _filter_quality_materials(items, max_clip_duration, video_aspect)
    items, cooldown_items = _split_video_cooldown_materials(items, cooldown_stats)

    def rank_pool(pool: List[MaterialInfo]) -> List[MaterialInfo]:
        ranked_pool = sorted(
            pool,
            key=lambda it: _material_rank_key(
                it,
                max_clip_duration=max_clip_duration,
                video_aspect=video_aspect,
            ),
            reverse=True,
        )
        if randomize and len(ranked_pool) > 1:
            top_n = max(1, len(ranked_pool) // 3)
            top_pool = ranked_pool[:top_n]
            rest = ranked_pool[top_n:]
            random.shuffle(rest)
            ranked_pool = top_pool + rest
        return ranked_pool

    ranked = _rerank_materials_with_twelvelabs(rank_pool(items))
    ranked = _rerank_materials_with_twelvelabs_visual(ranked)
    if cooldown_items:
        cooldown_ranked = _rerank_materials_with_twelvelabs(
            rank_pool(cooldown_items)
        )
        ranked.extend(_rerank_materials_with_twelvelabs_visual(cooldown_ranked))
    return _screen_twelvelabs_material_candidates(ranked)


def _twelvelabs_clip_qa_max_candidates() -> int:
    if not config.app.get("twelvelabs_clip_qa_enabled"):
        return 0
    try:
        requested_limit = int(
            config.app.get("twelvelabs_clip_qa_max_candidates", 0)
        )
    except (TypeError, ValueError):
        return 0
    return min(5, max(0, requested_limit))


def _screen_twelvelabs_material_candidates(
    items: List[MaterialInfo],
) -> List[MaterialInfo]:
    """Use optional Pegasus QA only for a small number of ranked public URLs."""
    candidate_limit = _twelvelabs_clip_qa_max_candidates()
    if candidate_limit <= 0:
        return items
    try:
        from app.services import twelvelabs
    except Exception:
        return items

    screened_items = []
    reviewed_count = 0
    rejected_count = 0
    for item in items:
        query = str(getattr(item, "search_query", "") or "").strip()
        url = str(getattr(item, "url", "") or "").strip()
        verdict = None
        parsed_url = urlsplit(url)
        is_public_url = (
            parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)
        )
        if reviewed_count < candidate_limit and query and is_public_url:
            reviewed_count += 1
            try:
                verdict = twelvelabs.clip_relevance_verdict(url, query)
            except Exception:
                verdict = None
        if verdict is False:
            rejected_count += 1
            continue
        screened_items.append(item)

    if reviewed_count:
        logger.info(
            "TwelveLabs clip QA reviewed {} ranked material candidates; "
            "{} were explicitly rejected.",
            reviewed_count,
            rejected_count,
        )
    return screened_items


def _single_source_searcher(source: str):
    if source == "pixabay":
        return search_videos_pixabay
    if source == "coverr":
        return search_videos_coverr
    return search_videos_pexels


def _api_sources_from_enabled(enabled_sources: Optional[List[str]] = None) -> List[str]:
    sources = enabled_sources
    if sources is None:
        sources = config.app.get("enabled_video_sources", [])
    if isinstance(sources, str):
        sources = [sources]
    return [
        s for s in (sources or [])
        if s not in ("local", "douyin", "bilibili", "xiaohongshu")
    ]


def _is_photo_fallback_enabled() -> bool:
    value = config.app.get("photo_fallback_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _required_material_count(audio_duration: float, max_clip_duration: int) -> int:
    try:
        duration = max(0.0, float(audio_duration or 0))
    except (TypeError, ValueError):
        duration = 0.0
    try:
        clip_duration = max(1, int(max_clip_duration or 1))
    except (TypeError, ValueError):
        clip_duration = 1
    return max(1, math.ceil(duration / clip_duration))


def _material_duration_target(
    audio_duration: float,
    max_clip_duration: int,
    minimum_unique_visual_count: int | None = None,
) -> float:
    try:
        target_duration = max(0.0, float(audio_duration or 0))
    except (TypeError, ValueError):
        target_duration = 0.0
    try:
        clip_duration = max(1.0, float(max_clip_duration or 1))
    except (TypeError, ValueError):
        clip_duration = 1.0
    try:
        required_count = int(minimum_unique_visual_count or 0)
    except (TypeError, ValueError):
        required_count = 0
    if required_count <= 0:
        return target_duration
    return max(target_duration, required_count * clip_duration - 0.001)


def _photo_searchers_for_source(source: str):
    source = str(source or "").strip().lower()
    if source == "smithsonian":
        return [search_photos_smithsonian]

    if source == "pexels":
        searchers = [search_photos_pexels]
    elif source == "pixabay":
        searchers = [search_photos_pixabay]
    else:
        enabled_sources = _api_sources_from_enabled()
        searchers = []
        if "pexels" in enabled_sources and config.app.get("pexels_api_keys"):
            searchers.append(search_photos_pexels)
        if "pixabay" in enabled_sources and config.app.get("pixabay_api_keys"):
            searchers.append(search_photos_pixabay)
        if not searchers:
            if config.app.get("pexels_api_keys"):
                searchers.append(search_photos_pexels)
            if config.app.get("pixabay_api_keys"):
                searchers.append(search_photos_pixabay)
    if config.app.get("smithsonian_api_keys"):
        searchers.append(search_photos_smithsonian)
    if is_openverse_photo_fallback_enabled():
        searchers.append(search_photos_openverse)
    if is_europeana_photo_fallback_enabled():
        searchers.append(search_photos_europeana)
    return searchers


def _search_photo_fallback_candidates(
    search_terms: List[str],
    source: str,
    video_aspect: VideoAspect,
    searchers: Optional[List[Callable]] = None,
) -> List[MaterialInfo]:
    candidates = []
    seen_urls = set()
    for searcher in searchers or _photo_searchers_for_source(source):
        for search_term in search_terms:
            try:
                items = searcher(search_term, video_aspect)
            except Exception:
                logger.warning(
                    "[photo-fallback] a photo source search failed; trying the next source"
                )
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                url_key = _material_url_key(getattr(item, "url", ""))
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                candidates.append(item)
    return candidates


def _append_photo_fallback_candidates(
    video_paths: List[str],
    candidates: List[MaterialInfo],
    *,
    required_count: int,
    max_clip_duration: int,
    attribution_records: Optional[list] = None,
) -> List[str]:
    try:
        from app.services import video as video_service
    except ImportError:
        return video_paths

    for item in candidates:
        if len(video_paths) >= required_count:
            break
        redirect_url_validator = None
        if item.provider == "openverse":
            redirect_url_validator = is_safe_openverse_image_url
        elif item.provider == "europeana":
            redirect_url_validator = is_safe_europeana_image_url
        elif item.provider in {"met", "artic"}:
            redirect_url_validator = is_safe_museum_image_url
        image_path = save_image(
            item.url,
            redirect_url_validator=redirect_url_validator,
        )
        if not image_path:
            continue
        try:
            processed_items = video_service.preprocess_video(
                [MaterialInfo(provider="local", url=image_path)],
                clip_duration=max_clip_duration,
            )
        except Exception as error:
            logger.warning(
                f"failed to preprocess fallback photo: {safe_error_details(error)}"
            )
            continue
        if not processed_items:
            continue
        video_path = str(getattr(processed_items[0], "url", "") or "")
        if not video_path:
            continue
        video_paths.append(video_path)
        _append_material_attribution(attribution_records, item, video_path)
        _mark_video_cooldown_used(item)

    return video_paths


def _append_photo_fallback(
    video_paths: List[str],
    *,
    search_terms: List[str],
    source: str,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    attribution_records: Optional[list] = None,
) -> List[str]:
    video_paths = list(video_paths or [])
    required_count = _required_material_count(audio_duration, max_clip_duration)
    if not _is_photo_fallback_enabled() or len(video_paths) >= required_count:
        return video_paths

    logger.info(
        f"video materials are insufficient ({len(video_paths)}/{required_count}); "
        "searching photos for fallback"
    )
    video_paths = _append_photo_fallback_candidates(
        video_paths,
        _search_photo_fallback_candidates(search_terms, source, video_aspect),
        required_count=required_count,
        max_clip_duration=max_clip_duration,
        attribution_records=attribution_records,
    )
    if len(video_paths) < required_count and is_museum_photo_fallback_enabled():
        logger.info(
            "primary photo sources were insufficient; searching public-domain museum photos"
        )
        video_paths = _append_photo_fallback_candidates(
            video_paths,
            _search_photo_fallback_candidates(
                search_terms,
                source,
                video_aspect,
                searchers=[search_photos_met, search_photos_artic],
            ),
            required_count=required_count,
            max_clip_duration=max_clip_duration,
            attribution_records=attribution_records,
        )

    return video_paths


def _normalize_search_terms(search_terms) -> List[str]:
    if isinstance(search_terms, str):
        raw_terms = search_terms.replace("\n", ",").split(",")
    else:
        raw_terms = search_terms or []
    terms = []
    seen = set()
    for term in raw_terms:
        term = " ".join(str(term).split())
        term_key = term.casefold()
        if not term or term_key in seen:
            continue
        terms.append(term)
        seen.add(term_key)
    return terms


def _material_url_key(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme and parts.netloc:
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path,
                "",
                "",
            )
        )
    return value


def _script_order_search_variants(search_term: str) -> List[str]:
    term = str(search_term or "").strip()
    if not term:
        return []

    variants = [term]
    words = term.split()
    if len(words) >= 4:
        variants.append(" ".join(words[:-1]))
    if len(words) >= 5:
        variants.append(" ".join(words[:3]))

    deduped = []
    seen = set()
    for variant in variants:
        normalized = " ".join(variant.split()).lower()
        if normalized and normalized not in seen:
            deduped.append(variant)
            seen.add(normalized)
    return deduped


def _has_substantive_content_match(
    items: List[MaterialInfo],
    search_term: str,
) -> bool:
    for item in items or []:
        try:
            if (
                _score_content_match(item, search_query=search_term)
                >= _SUBSTANTIVE_CONTENT_MATCH_THRESHOLD
            ):
                return True
        except Exception:
            continue
    return False


def _search_script_order_candidates(search_term: str, search_candidates) -> tuple[List[MaterialInfo], bool]:
    """Retry only low-relevance scene results while retaining a usable first pool."""
    first_items: List[MaterialInfo] = []
    first_term = ""
    selected_items: List[MaterialInfo] = []
    selected_term = ""

    for candidate_term in _script_order_search_variants(search_term):
        items = list(search_candidates(candidate_term) or [])
        if not items:
            continue
        if not first_items:
            first_items = items
            first_term = candidate_term
        if _has_substantive_content_match(items, search_term):
            selected_items = items
            selected_term = candidate_term
            break

    if not selected_items:
        selected_items = first_items
        selected_term = first_term
    return selected_items, bool(selected_term and selected_term != search_term)


def search_video_candidates(
    search_terms,
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
    limit: int = 24,
    enabled_sources: Optional[List[str]] = None,
    cooldown_stats: Optional[dict] = None,
) -> List[MaterialInfo]:
    """Search and rank candidates without downloading them."""
    terms = _normalize_search_terms(search_terms)
    if not terms:
        return []

    candidates: List[MaterialInfo] = []

    if source == "multi" or source not in ("pexels", "pixabay", "coverr"):
        api_sources = _api_sources_from_enabled(enabled_sources)
        if source != "multi" and source not in api_sources:
            api_sources = [source]
        if not api_sources:
            api_sources = ["pexels"]

        try:
            from app.services.providers import get_active_providers
            providers = get_active_providers(api_sources)
        except ImportError:
            providers = []

        if providers:
            for term in terms:
                for item in _search_all_providers(
                    term, providers, max_clip_duration, video_aspect
                ):
                    candidates.append(item)
        elif "pexels" in api_sources:
            source = "pexels"

    if not candidates and source in ("pexels", "pixabay", "coverr"):
        search_videos = _single_source_searcher(source)
        for term in terms:
            for item in search_videos(
                search_term=term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
            ):
                candidates.append(item)

    ranked = _rank_materials(
        candidates,
        max_clip_duration,
        randomize=False,
        cooldown_stats=cooldown_stats,
        video_aspect=video_aspect,
    )
    return ranked[: max(1, int(limit or 1))]


def _provider_name_for_log(item: MaterialInfo) -> str:
    provider = str(getattr(item, "provider", "") or "").strip()
    safe_provider = "".join(
        character
        for character in provider
        if character.isalnum() or character in ("-", "_")
    )
    return safe_provider[:40] or "unknown"


def _material_attribution_record(item: MaterialInfo, video_path: str) -> dict:
    attribution = str(getattr(item, "attribution", "") or "").strip()
    license_name = str(getattr(item, "license", "") or "").strip()
    license_url = str(getattr(item, "license_url", "") or "").strip()
    return {
        "video_path": video_path,
        "provider": str(getattr(item, "provider", "") or "").strip(),
        "title": str(getattr(item, "title", "") or "").strip(),
        "license": license_name,
        "license_url": license_url,
        "attribution": attribution,
        "source_url": str(getattr(item, "url", "") or "").strip(),
    }


def format_material_attributions(attribution_records: Optional[list]) -> str:
    if not attribution_records:
        return ""

    lines = []
    seen = set()
    for record in attribution_records:
        if not isinstance(record, dict):
            continue
        attribution = str(record.get("attribution") or "").strip()
        title = str(record.get("title") or "").strip()
        provider = str(record.get("provider") or "").strip()
        license_name = str(record.get("license") or "").strip()
        license_url = str(record.get("license_url") or "").strip()
        source_url = str(record.get("source_url") or "").strip()

        if not (attribution or license_name or license_url):
            continue

        label = attribution or title or provider or source_url
        if not label:
            continue

        details = []
        if license_name and license_name not in label:
            details.append(license_name)
        if license_url and license_url not in label:
            details.append(license_url)
        if source_url and source_url not in label:
            details.append(source_url)

        line = f"- {label}"
        if details:
            line = f"{line} ({'; '.join(details)})"
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    if not lines:
        return ""
    return "Credits:\n" + "\n".join(lines)


def append_material_attributions(text: str, attribution_records: Optional[list]) -> str:
    attribution_text = format_material_attributions(attribution_records)
    base_text = str(text or "").strip()
    if not attribution_text:
        return base_text
    if not base_text:
        return attribution_text
    return f"{base_text}\n\n{attribution_text}"


def append_material_attribution_record(
    attribution_records: Optional[list],
    item: MaterialInfo,
    video_path: str,
) -> None:
    if attribution_records is None:
        return
    record = _material_attribution_record(item, video_path)
    if record:
        attribution_records.append(record)


def _append_material_attribution(
    attribution_records: Optional[list],
    item: MaterialInfo,
    video_path: str,
) -> None:
    """Backward-compatible private alias for older material download paths."""
    append_material_attribution_record(attribution_records, item, video_path)


def download_selected_videos(
    task_id: str,
    selected_items: List[MaterialInfo],
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    attribution_records: Optional[list] = None,
) -> List[str]:
    """Download user-selected online material URLs in the chosen order."""
    material_directory = _build_material_directory(task_id)
    video_paths: List[str] = []
    seen_urls = set()
    total_duration = 0.0

    for item in selected_items or []:
        url = str(getattr(item, "url", "") or "").strip()
        url_key = _material_url_key(url)
        if not url_key or url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        try:
            provider_name = _provider_name_for_log(item)
            logger.info(f"downloading selected video [{provider_name}]")
            saved_video_path = _save_provider_material(item, material_directory)
            if saved_video_path:
                video_paths.append(saved_video_path)
                _append_material_attribution(
                    attribution_records,
                    item,
                    saved_video_path,
                )
                _mark_video_cooldown_used(item)
                duration = float(getattr(item, "duration", 0) or 0)
                total_duration += min(max_clip_duration, duration or max_clip_duration)
                if audio_duration and total_duration >= audio_duration:
                    break
        except Exception as e:
            provider_name = _provider_name_for_log(item)
            logger.error(
                f"failed to download selected video [{provider_name}]: "
                f"{safe_error_details(e)}"
            )

    logger.success(f"downloaded {len(video_paths)} selected videos")
    return video_paths


def _safe_provider_search(provider, search_term: str, minimum_duration: int,
                           video_aspect: VideoAspect) -> List[MaterialInfo]:
    """Provider aramasını exception yakayla çalıştırır."""
    try:
        results = provider.search(search_term, minimum_duration, video_aspect)
        logger.info(f"[{provider.name}] search returned {len(results)} results")
        return results
    except Exception as e:
        logger.warning(f"[{provider.name}] search failed: {type(e).__name__}")
        return []


def _search_all_providers(
    search_term: str,
    providers: list,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    """
    Tüm provider'ları paralel sorgular; sonuçları havuzda birleştirir.
    Tekrar eden URL'ler temizlenir.
    """
    items_by_url: dict[str, MaterialInfo] = {}

    max_workers = min(len(providers), 6)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _safe_provider_search, p, search_term, minimum_duration, video_aspect
            ): p.name
            for p in providers
        }
        for future in as_completed(futures):
            for item in future.result():
                url_key = _material_url_key(item.url)
                if not url_key:
                    continue
                previous = items_by_url.get(url_key)
                if previous is None or (
                    _dedupe_candidate_score(item, minimum_duration, video_aspect)
                    > _dedupe_candidate_score(previous, minimum_duration, video_aspect)
                ):
                    items_by_url[url_key] = item

    return list(items_by_url.values())


def _build_material_directory(task_id: str) -> str:
    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        return utils.task_dir(task_id)
    if material_directory and not os.path.isdir(material_directory):
        return ""
    return material_directory


# ─── Çok kaynaklı indirme: rastgele mod ──────────────────────────────────────

def _download_multi_source(
    task_id: str,
    search_terms: List[str],
    providers: list,
    video_aspect: VideoAspect,
    video_concat_mode: VideoConcatMode,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    cooldown_stats: Optional[dict] = None,
    attribution_records: Optional[list] = None,
) -> List[str]:
    """Tüm provider'lardan arama yap, en iyi adayları indir."""
    candidates: List[MaterialInfo] = []

    # Her terim için tüm provider'ları paralel ara; sonuçları birleştir
    for term in search_terms:
        items = _search_all_providers(
            term, providers, max_clip_duration, video_aspect
        )
        for item in items:
            candidates.append(item)

    logger.info(
        f"[multi] toplam aday: {len(candidates)}, "
        f"hedef süre: {audio_duration}s"
    )

    if not candidates:
        logger.warning("[multi] hiç aday bulunamadı")
        return []

    # Puanla ve sırala
    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    scored = _rank_materials(
        candidates,
        max_clip_duration,
        randomize=concat_mode_value == VideoConcatMode.random.value,
        cooldown_stats=cooldown_stats,
        video_aspect=video_aspect,
    )
    scored = _rerank_materials_with_preview_quality(
        scored,
        max_clip_duration=max_clip_duration,
        video_aspect=video_aspect,
    )

    # Rastgele mod: yüksek puanlı adayları karıştır ama tamamen rastgele değil
    # → En iyi %40'ı koru, kalanı karıştır
    # İndir
    video_paths: List[str] = []
    total_duration = 0.0
    available_providers = {
        str(getattr(item, "provider", "") or "").strip().casefold()
        for item in scored
    }
    required_provider_count = min(2, len(available_providers))
    selected_providers = set()

    for index, item in enumerate(scored):
        provider_key = str(getattr(item, "provider", "") or "").strip().casefold()
        if total_duration >= audio_duration:
            if len(selected_providers) >= required_provider_count:
                break
            remaining_new_providers = {
                str(getattr(candidate, "provider", "") or "").strip().casefold()
                for candidate in scored[index:]
                if str(getattr(candidate, "provider", "") or "").strip().casefold()
                not in selected_providers
            }
            if not remaining_new_providers:
                break
            if provider_key in selected_providers:
                continue
        try:
            provider_name = _provider_name_for_log(item)
            logger.info(f"[multi] indiriliyor [{provider_name}]")
            saved = _save_ranked_material(item, material_directory)
            if saved:
                video_paths.append(saved)
                _append_material_attribution(attribution_records, item, saved)
                _mark_video_cooldown_used(item)
                total_duration += min(max_clip_duration, item.duration)
                selected_providers.add(provider_key)
                if (
                    total_duration >= audio_duration
                    and len(selected_providers) >= required_provider_count
                ):
                    logger.info(
                        f"[multi] yeterli süre toplandı: {total_duration}s"
                    )
                    break
        except Exception as e:
            provider_name = _provider_name_for_log(item)
            logger.error(
                f"[multi] indirme hatası [{provider_name}]: "
                f"{safe_error_details(e)}"
            )

    logger.success(f"[multi] {len(video_paths)} video indirildi")
    return video_paths


# ─── Çok kaynaklı indirme: senaryo sırası modu ───────────────────────────────

def _download_multi_ordered(
    task_id: str,
    search_terms: List[str],
    providers: list,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    cooldown_stats: Optional[dict] = None,
    attribution_records: Optional[list] = None,
) -> List[str]:
    """
    Senaryo terimlerinin sırasına göre video indirir.
    Her terim kendi aday havuzuna sahiptir; round-robin ile dönülür.
    """
    logger.info("[multi-ordered] senaryo sırası modu aktif")

    seen_urls: set = set()
    candidate_groups: List[tuple] = []   # (term, [MaterialInfo, ...])
    fallback_used = 0
    unresolved = 0

    for term in search_terms:
        items, used_fallback = _search_script_order_candidates(
            term,
            lambda candidate_term: _search_all_providers(
                candidate_term, providers, max_clip_duration, video_aspect
            ),
        )
        if used_fallback:
            fallback_used += 1
        if not items:
            unresolved += 1
        # Skor bazlı sırala; tekrarları filtrele
        unique = []
        ranked_items = _rerank_materials_with_preview_quality(
            _rank_materials(
                items,
                max_clip_duration,
                randomize=False,
                cooldown_stats=cooldown_stats,
                video_aspect=video_aspect,
            ),
            max_clip_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        for it in ranked_items:
            url_key = _material_url_key(it.url)
            if url_key and url_key not in seen_urls:
                seen_urls.add(url_key)
                unique.append(it)
        if unique:
            candidate_groups.append((term, unique))

    total_candidates = sum(len(g[1]) for g in candidate_groups)
    search_summary = (
        "ordered material search: mode=multi, "
        f"scenes={len(search_terms)}, fallback_used={fallback_used}, "
        f"unresolved={unresolved}"
    )
    (logger.warning if unresolved else logger.info)(search_summary)
    logger.info(
        f"[multi-ordered] {total_candidates} aday, "
        f"{len(candidate_groups)} terim grubu"
    )

    fresh_candidate_groups, deferred_candidate_groups = (
        _split_ordered_candidate_groups_by_cooldown(candidate_groups)
    )
    candidate_groups = fresh_candidate_groups or deferred_candidate_groups
    if not fresh_candidate_groups:
        deferred_candidate_groups = []

    video_paths: List[str] = []
    total_duration = 0.0
    round_idx = 0

    while candidate_groups and total_duration < audio_duration:
        progressed = False
        for term, term_items in candidate_groups:
            if round_idx >= len(term_items):
                continue
            item = term_items[round_idx]
            progressed = True
            try:
                provider_name = _provider_name_for_log(item)
                logger.info(f"[multi-ordered] indiriliyor [{provider_name}]")
                saved = _save_ranked_material(item, material_directory)
                if saved:
                    video_paths.append(saved)
                    _append_material_attribution(attribution_records, item, saved)
                    _mark_video_cooldown_used(item)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration >= audio_duration:
                        break
            except Exception as e:
                provider_name = _provider_name_for_log(item)
                logger.error(
                    f"[multi-ordered] indirme hatası [{provider_name}]: "
                    f"{safe_error_details(e)}"
                )
        if not progressed:
            if deferred_candidate_groups:
                candidate_groups = deferred_candidate_groups
                deferred_candidate_groups = []
                round_idx = 0
                continue
            break
        round_idx += 1

    logger.success(f"[multi-ordered] {len(video_paths)} video indirildi")
    return video_paths


# ─── Ana indirme fonksiyonu ───────────────────────────────────────────────────

def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
    cooldown_stats: Optional[dict] = None,
    attribution_records: Optional[list] = None,
    minimum_unique_visual_count: int | None = None,
) -> List[str]:
    """
    Video indirir. Çok kaynaklı mod yalnızca source="multi" iken çalışır.
    Tekli kaynak çağrıları config.app['enabled_video_sources'] tarafından
    ezilmez; böylece API/test çağrılarında geriye dönük davranış korunur.
    """
    # ── Çok kaynaklı mod kontrolü ────────────────────────────────────────
    search_terms = _normalize_search_terms(search_terms)
    material_duration_target = _material_duration_target(
        audio_duration,
        max_clip_duration,
        minimum_unique_visual_count,
    )
    if source == "smithsonian":
        return _append_photo_fallback(
            [],
            search_terms=search_terms,
            source=source,
            video_aspect=video_aspect,
            audio_duration=material_duration_target,
            max_clip_duration=max_clip_duration,
            attribution_records=attribution_records,
        )
    enabled_sources: List[str] = config.app.get("enabled_video_sources", [])
    # "local", "douyin" vb. API provider'ı değil; filtrele
    api_sources = [
        s for s in enabled_sources
        if s not in ("local", "douyin", "bilibili", "xiaohongshu")
    ]

    # Pexels, Pixabay, and Coverr retain their established single-source
    # implementations. Other registered providers must go through the common
    # provider pipeline even when the caller selects only one of them.
    single_registered_provider = False
    if source not in {"multi", "pexels", "pixabay", "coverr"}:
        try:
            from app.services.providers import PROVIDER_REGISTRY
        except ImportError:
            PROVIDER_REGISTRY = {}
        if source in PROVIDER_REGISTRY:
            api_sources = [source]
            source = "multi"
            single_registered_provider = True

    if source == "multi":
        # Çok kaynaklı mod
        if not api_sources:
            api_sources = [source]  # fallback
        try:
            from app.services.providers import get_active_providers
            providers = get_active_providers(api_sources)
        except ImportError:
            logger.warning(
                "providers modülü bulunamadı, tekli kaynak moduna dönülüyor"
            )
            providers = []

        if providers:
            material_dir = _build_material_directory(task_id)
            logger.info(
                f"[multi] aktif kaynaklar: {[p.name for p in providers]}"
            )
            if match_script_order:
                video_paths = _download_multi_ordered(
                    task_id=task_id,
                    search_terms=search_terms,
                    providers=providers,
                    video_aspect=video_aspect,
                    audio_duration=material_duration_target,
                    max_clip_duration=max_clip_duration,
                    material_directory=material_dir,
                    cooldown_stats=cooldown_stats,
                    attribution_records=attribution_records,
                )
            else:
                video_paths = _download_multi_source(
                    task_id=task_id,
                    search_terms=search_terms,
                    providers=providers,
                    video_aspect=video_aspect,
                    video_concat_mode=video_concat_mode,
                    audio_duration=material_duration_target,
                    max_clip_duration=max_clip_duration,
                    material_directory=material_dir,
                    cooldown_stats=cooldown_stats,
                    attribution_records=attribution_records,
                )
            return _append_photo_fallback(
                video_paths,
                search_terms=search_terms,
                source=source,
                video_aspect=video_aspect,
                audio_duration=material_duration_target,
                max_clip_duration=max_clip_duration,
                attribution_records=attribution_records,
            )
        else:
            logger.warning(
                "[multi] hiçbir aktif provider bulunamadı, "
                "ilk kaynak kullanılıyor"
            )
            if api_sources:
                source = api_sources[0]
            if single_registered_provider:
                return _append_photo_fallback(
                    [],
                    search_terms=search_terms,
                    source=source,
                    video_aspect=video_aspect,
                    audio_duration=material_duration_target,
                    max_clip_duration=max_clip_duration,
                    attribution_records=attribution_records,
                )

    # ── Tekli kaynak modu (orijinal davranış) ────────────────────────────
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr

    material_directory = _build_material_directory(task_id)

    if match_script_order:
        video_paths = _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=material_duration_target,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
            cooldown_stats=cooldown_stats,
            attribution_records=attribution_records,
        )
        return _append_photo_fallback(
            video_paths,
            search_terms=search_terms,
            source=source,
            video_aspect=video_aspect,
            audio_duration=material_duration_target,
            max_clip_duration=max_clip_duration,
            attribution_records=attribution_records,
        )

    valid_video_items = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos")
        for item in video_items:
            valid_video_items.append(item)
            found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, "
        f"required duration: {material_duration_target} seconds, "
        f"found duration: {found_duration} seconds"
    )
    video_paths = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    valid_video_items = _rank_materials(
        valid_video_items,
        max_clip_duration,
        randomize=concat_mode_value == VideoConcatMode.random.value,
        cooldown_stats=cooldown_stats,
        video_aspect=video_aspect,
    )
    valid_video_items = _rerank_materials_with_preview_quality(
        valid_video_items,
        max_clip_duration=max_clip_duration,
        video_aspect=video_aspect,
    )

    total_duration = 0.0
    for item in valid_video_items:
        try:
            provider_name = _provider_name_for_log(item)
            logger.info(f"downloading video [{provider_name}]")
            saved_video_path = _save_ranked_material(item, material_directory)
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                _append_material_attribution(
                    attribution_records,
                    item,
                    saved_video_path,
                )
                _mark_video_cooldown_used(item)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > material_duration_target:
                    logger.info(
                        f"total duration of downloaded videos: "
                        f"{total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            provider_name = _provider_name_for_log(item)
            logger.error(
                f"failed to download video [{provider_name}]: "
                f"{safe_error_details(e)}"
            )
    logger.success(f"downloaded {len(video_paths)} videos")
    return _append_photo_fallback(
        video_paths,
        search_terms=search_terms,
        source=source,
        video_aspect=video_aspect,
        audio_duration=material_duration_target,
        max_clip_duration=max_clip_duration,
        attribution_records=attribution_records,
    )


# ─── Senaryo sırası indirme (tekli kaynak, değişmedi) ────────────────────────

def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    cooldown_stats: Optional[dict] = None,
    attribution_records: Optional[list] = None,
) -> List[str]:
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0
    fallback_used = 0
    unresolved = 0

    for search_term in search_terms:
        video_items, used_fallback = _search_script_order_candidates(
            search_term,
            lambda candidate_term: search_videos(
                search_term=candidate_term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
            ),
        )
        if used_fallback:
            fallback_used += 1
        if not video_items:
            unresolved += 1
        term_items = []
        ranked_items = _rerank_materials_with_preview_quality(
            _rank_materials(
                video_items,
                max_clip_duration,
                cooldown_stats=cooldown_stats,
                video_aspect=video_aspect,
            ),
            max_clip_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        for item in ranked_items:
            url_key = _material_url_key(item.url)
            if not url_key or url_key in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(url_key)
            found_duration += item.duration
        if term_items:
            candidate_groups.append((search_term, term_items))

    search_summary = (
        "ordered material search: mode=single, "
        f"scenes={len(search_terms)}, fallback_used={fallback_used}, "
        f"unresolved={unresolved}"
    )
    (logger.warning if unresolved else logger.info)(search_summary)
    logger.info(
        f"found total ordered video candidates: "
        f"{sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, "
        f"found duration: {found_duration} seconds"
    )

    fresh_candidate_groups, deferred_candidate_groups = (
        _split_ordered_candidate_groups_by_cooldown(candidate_groups)
    )
    candidate_groups = fresh_candidate_groups or deferred_candidate_groups
    if not fresh_candidate_groups:
        deferred_candidate_groups = []

    video_paths = []
    total_duration = 0.0
    candidate_index = 0
    while candidate_groups and total_duration <= audio_duration:
        has_candidate = False
        for search_term, term_items in candidate_groups:
            if candidate_index >= len(term_items):
                continue
            has_candidate = True
            item = term_items[candidate_index]
            try:
                provider_name = _provider_name_for_log(item)
                logger.info(f"downloading ordered video [{provider_name}]")
                saved_video_path = _save_ranked_material(item, material_directory)
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    _append_material_attribution(
                        attribution_records,
                        item,
                        saved_video_path,
                    )
                    _mark_video_cooldown_used(item)
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: "
                            f"{total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                provider_name = _provider_name_for_log(item)
                logger.error(
                    f"failed to download ordered video [{provider_name}]: "
                    f"{safe_error_details(e)}"
                )
        if not has_candidate:
            if deferred_candidate_groups:
                candidate_groups = deferred_candidate_groups
                deferred_candidate_groups = []
                candidate_index = 0
                continue
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
