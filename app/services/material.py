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

import os
import random
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import video_cooldown
from app.services.providers.utils import (
    get_search_page,
    raise_for_http_error,
    safe_error_details,
)
from app.utils import utils

# ─── Thread-safe API key rotasyonu (eski işlevler için) ──────────────────────
_api_key_counter = 0
_api_key_lock = threading.Lock()


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
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
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
        r = requests.get(
            query_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        raise_for_http_error(r)
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error("[pexels] search returned unexpected response")
            return video_items
        for v in response["videos"]:
            duration = v["duration"]
            if duration < minimum_duration:
                continue
            best_video = None
            best_pixels = -1
            for video in v["video_files"]:
                w = int(video["width"])
                h = int(video["height"])
                if (
                    w >= video_width
                    and h >= video_height
                    and w * video_height == h * video_width
                    and w * h > best_pixels
                ):
                    best_video = (video, w, h)
                    best_pixels = w * h
            if best_video:
                video, w, h = best_video
                item = MaterialInfo()
                item.provider = "pexels"
                item.url = video["link"]
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
    video_width, video_height = aspect.to_resolution()
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
        r = requests.get(
            query_url, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(30, 60),
        )
        raise_for_http_error(r)
        response = r.json()
        video_items = []
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

def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_hash = utils.md5(_material_url_key(video_url) or video_url.split("?")[0])
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/115.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(
            video_url, headers=headers, proxies=config.proxy,
            verify=_get_tls_verify(), timeout=(60, 240),
        )
    except Exception as e:
        logger.warning(f"video download request failed: {video_url} => {str(e)}")
        return ""

    # getattr ile erişiyoruz: gerçek requests.Response nesnelerinde bu alanlar
    # her zaman mevcut, ama testlerdeki basit mock nesneleri sadece .content
    # tanımlayabiliyor; onlarla geriye dönük uyumluluğu koruyoruz.
    status_code = getattr(resp, "status_code", 200)
    if status_code != 200:
        logger.warning(
            f"video download failed: {video_url} => HTTP {status_code}"
        )
        return ""

    resp_headers = getattr(resp, "headers", {}) or {}
    content_type = resp_headers.get("Content-Type", "")
    if content_type and not (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    ):
        logger.warning(
            f"video download returned non-video content: {video_url} => "
            f"Content-Type={content_type}"
        )
        return ""

    content = resp.content
    declared_length = resp_headers.get("Content-Length")
    if declared_length is not None:
        try:
            if int(declared_length) > len(content):
                logger.warning(
                    f"video download truncated: {video_url} => expected "
                    f"{declared_length} bytes, got {len(content)}"
                )
                return ""
        except ValueError:
            pass

    with open(video_path, "wb") as f:
        f.write(content)

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, "
                    f"error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, "
                        f"error: {str(close_error)}"
                    )
    return ""


# ─── Çok kaynaklı yardımcı fonksiyonlar ──────────────────────────────────────

def _score_resolution(
    item: MaterialInfo,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> float:
    width = int(getattr(item, "width", 0) or 0)
    height = int(getattr(item, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return 1.0

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
        source_orientation = width - height
        target_orientation = target_width - target_height
        if source_orientation and target_orientation and (
            source_orientation > 0
        ) != (target_orientation > 0):
            resolution_score *= 0.85
    except Exception:
        pass
    return resolution_score


def _score_material(
    item: MaterialInfo,
    max_clip_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> float:
    """
    0.0–1.0 arasında bir kalite skoru döndürür.

    Bileşenler:
      - Kalite ağırlığı (%50): Pexels > Pixabay > Coverr > NASA > Wikimedia > Archive.org
      - Süre uyumu (%50)    : max_clip_duration'a yakın veya uzun videolar tercih edilir
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
    return 0.40 * quality + 0.40 * duration_score + 0.20 * resolution_score


def _normalized_word_tokens(value) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return {
        token
        for token in re.findall(r"[^\W_]+", text)
        if len(token) > 1
    }


def _score_content_match(item: MaterialInfo) -> float:
    query_tokens = _normalized_word_tokens(getattr(item, "search_query", ""))
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
    return len(query_tokens & content_tokens) / len(query_tokens)


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
    return list(filtered_by_url.values())


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
            key=lambda it: (
                _score_material(it, max_clip_duration, video_aspect),
                _score_content_match(it),
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

    ranked = rank_pool(items)
    if cooldown_items:
        ranked.extend(rank_pool(cooldown_items))
    return ranked


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
    if not (attribution or license_name or license_url):
        return {}
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


def _append_material_attribution(
    attribution_records: Optional[list],
    item: MaterialInfo,
    video_path: str,
) -> None:
    if attribution_records is None:
        return
    record = _material_attribution_record(item, video_path)
    if record:
        attribution_records.append(record)


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
            saved_video_path = save_video(video_url=url, save_dir=material_directory)
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

    # Rastgele mod: yüksek puanlı adayları karıştır ama tamamen rastgele değil
    # → En iyi %40'ı koru, kalanı karıştır
    # İndir
    video_paths: List[str] = []
    total_duration = 0.0

    for item in scored:
        try:
            provider_name = _provider_name_for_log(item)
            logger.info(f"[multi] indiriliyor [{provider_name}]")
            saved = save_video(video_url=item.url, save_dir=material_directory)
            if saved:
                video_paths.append(saved)
                _append_material_attribution(attribution_records, item, saved)
                _mark_video_cooldown_used(item)
                total_duration += min(max_clip_duration, item.duration)
                if total_duration >= audio_duration:
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
        items = []
        for candidate_term in _script_order_search_variants(term):
            items = _search_all_providers(
                candidate_term, providers, max_clip_duration, video_aspect
            )
            if items:
                if candidate_term != term:
                    fallback_used += 1
                break
        if not items:
            unresolved += 1
        # Skor bazlı sırala; tekrarları filtrele
        unique = []
        for it in _rank_materials(
            items,
            max_clip_duration,
            randomize=False,
            cooldown_stats=cooldown_stats,
            video_aspect=video_aspect,
        ):
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
                saved = save_video(
                    video_url=item.url, save_dir=material_directory
                )
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
) -> List[str]:
    """
    Video indirir. Çok kaynaklı mod yalnızca source="multi" iken çalışır.
    Tekli kaynak çağrıları config.app['enabled_video_sources'] tarafından
    ezilmez; böylece API/test çağrılarında geriye dönük davranış korunur.
    """
    # ── Çok kaynaklı mod kontrolü ────────────────────────────────────────
    search_terms = _normalize_search_terms(search_terms)
    enabled_sources: List[str] = config.app.get("enabled_video_sources", [])
    # "local", "douyin" vb. API provider'ı değil; filtrele
    api_sources = [
        s for s in enabled_sources
        if s not in ("local", "douyin", "bilibili", "xiaohongshu")
    ]

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
                return _download_multi_ordered(
                    task_id=task_id,
                    search_terms=search_terms,
                    providers=providers,
                    video_aspect=video_aspect,
                    audio_duration=audio_duration,
                    max_clip_duration=max_clip_duration,
                    material_directory=material_dir,
                    cooldown_stats=cooldown_stats,
                    attribution_records=attribution_records,
                )
            else:
                return _download_multi_source(
                    task_id=task_id,
                    search_terms=search_terms,
                    providers=providers,
                    video_aspect=video_aspect,
                    video_concat_mode=video_concat_mode,
                    audio_duration=audio_duration,
                    max_clip_duration=max_clip_duration,
                    material_directory=material_dir,
                    cooldown_stats=cooldown_stats,
                    attribution_records=attribution_records,
                )
        else:
            logger.warning(
                "[multi] hiçbir aktif provider bulunamadı, "
                "ilk kaynak kullanılıyor"
            )
            if api_sources:
                source = api_sources[0]

    # ── Tekli kaynak modu (orijinal davranış) ────────────────────────────
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "coverr":
        search_videos = search_videos_coverr

    material_directory = _build_material_directory(task_id)

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
            cooldown_stats=cooldown_stats,
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
        f"required duration: {audio_duration} seconds, "
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

    total_duration = 0.0
    for item in valid_video_items:
        try:
            provider_name = _provider_name_for_log(item)
            logger.info(f"downloading video [{provider_name}]")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
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
                if total_duration > audio_duration:
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
    return video_paths


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
        video_items = []
        for candidate_term in _script_order_search_variants(search_term):
            video_items = search_videos(
                search_term=candidate_term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
            )
            if video_items:
                if candidate_term != search_term:
                    fallback_used += 1
                break
        if not video_items:
            unresolved += 1
        term_items = []
        for item in _rank_materials(
            video_items,
            max_clip_duration,
            cooldown_stats=cooldown_stats,
            video_aspect=video_aspect,
        ):
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
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
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
            break
        candidate_index += 1

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
