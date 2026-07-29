import threading
import time
from typing import Any
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import requests
from loguru import logger

MAX_RSS_TREND_LENGTH = 500
MAX_RSS_SOURCE_TITLE_LENGTH = 200
MAX_RSS_SOURCE_URL_LENGTH = 2048
MAX_RSS_SOURCE_PUBLISHER_LENGTH = 120
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}{locale}"
TURKISH_GOOGLE_NEWS_LOCALE = "&hl=tr&gl=TR&ceid=TR:tr"
DEFAULT_RSS_TREND_CACHE_SECONDS = 300.0
_MAX_RSS_TREND_CACHE_ENTRIES = 64
_rss_trend_cache: dict[tuple[str, int, str], tuple[float, str]] = {}
_rss_trend_cache_lock = threading.Lock()


def _clean_rss_title(value: Any) -> str:
    return str(value or "").strip()


def _safe_rss_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or len(url) > MAX_RSS_SOURCE_URL_LENGTH:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _normalized_cache_time(value: object | None) -> float:
    if value is None:
        return time.monotonic()
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.monotonic()


def _normalized_limit(value: object) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _normalized_cache_seconds(value: object) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _normalized_news_locale(value: object | None) -> str:
    language = str(value or "").strip().casefold().replace("_", "-")
    if language in {"tr", "tr-tr"}:
        return "tr"
    return ""


def _google_news_rss_url(topic: str, language: object | None = None) -> str:
    locale = (
        TURKISH_GOOGLE_NEWS_LOCALE
        if _normalized_news_locale(language) == "tr"
        else ""
    )
    return GOOGLE_NEWS_RSS_URL.format(query=quote_plus(topic), locale=locale)


def clear_rss_trend_cache() -> None:
    """Clear in-memory RSS results, primarily for controlled maintenance/tests."""
    with _rss_trend_cache_lock:
        _rss_trend_cache.clear()


def _cached_rss_trend(key: tuple[str, int, str], now: float) -> str | None:
    with _rss_trend_cache_lock:
        cached = _rss_trend_cache.get(key)
        if cached is None:
            return None
        expires_at, summary = cached
        if expires_at <= now:
            _rss_trend_cache.pop(key, None)
            return None
        return summary


def _cache_rss_trend(
    key: tuple[str, int, str], summary: str, cache_seconds: float, now: float
) -> None:
    if not summary or cache_seconds <= 0:
        return
    with _rss_trend_cache_lock:
        expired_keys = [
            cached_key
            for cached_key, (expires_at, _value) in _rss_trend_cache.items()
            if expires_at <= now
        ]
        for expired_key in expired_keys:
            _rss_trend_cache.pop(expired_key, None)
        if len(_rss_trend_cache) >= _MAX_RSS_TREND_CACHE_ENTRIES:
            oldest_key = min(
                _rss_trend_cache,
                key=lambda cached_key: _rss_trend_cache[cached_key][0],
            )
            _rss_trend_cache.pop(oldest_key, None)
        _rss_trend_cache[key] = (now + cache_seconds, summary)


def fetch_rss_trend(
    topic: str,
    limit: int = 3,
    timeout: float = 3.0,
    *,
    cache_ttl_seconds: float = DEFAULT_RSS_TREND_CACHE_SECONDS,
    now: float | None = None,
    language: object | None = None,
) -> str:
    topic = " ".join(str(topic or "").split())
    if not topic:
        return ""

    normalized_limit = _normalized_limit(limit)
    cache_seconds = _normalized_cache_seconds(cache_ttl_seconds)
    current_time = _normalized_cache_time(now)
    locale = _normalized_news_locale(language)
    cache_key = (topic.casefold(), normalized_limit, locale)
    cached = _cached_rss_trend(cache_key, current_time) if cache_seconds else None
    if cached is not None:
        return cached

    try:
        url = _google_news_rss_url(topic, language)
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except Exception as exc:
        logger.warning(f"failed to fetch RSS trend context for '{topic}': {exc}")
        return ""

    titles = []
    for item in root.findall(".//channel/item")[:normalized_limit]:
        title = _clean_rss_title(item.findtext("title", default=""))
        if title:
            titles.append(title)

    summary = "; ".join(titles)
    summary = summary[:MAX_RSS_TREND_LENGTH]
    _cache_rss_trend(cache_key, summary, cache_seconds, current_time)
    return summary


def fetch_rss_trend_items(
    topic: str,
    limit: int = 3,
    timeout: float = 3.0,
    *,
    language: object | None = None,
) -> list[dict[str, str]]:
    """Fetch RSS planning items with optional public source provenance."""
    topic = " ".join(str(topic or "").split())
    if not topic:
        return []

    try:
        url = _google_news_rss_url(topic, language)
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except Exception as exc:
        logger.warning(f"failed to fetch RSS trend context for '{topic}': {exc}")
        return []

    items: list[dict[str, str]] = []
    for item in root.findall(".//channel/item")[: _normalized_limit(limit)]:
        title = _clean_rss_title(item.findtext("title", default=""))[
            :MAX_RSS_SOURCE_TITLE_LENGTH
        ]
        if not title:
            continue
        publisher = _clean_rss_title(item.findtext("source", default=""))[
            :MAX_RSS_SOURCE_PUBLISHER_LENGTH
        ]
        items.append(
            {
                "title": title,
                "url": _safe_rss_url(item.findtext("link", default="")),
                "publisher": publisher,
            }
        )
    return items
