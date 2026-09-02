import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from defusedxml import ElementTree
from loguru import logger

MAX_RSS_TREND_LENGTH = 500
MAX_RSS_SOURCE_TITLE_LENGTH = 200
MAX_RSS_SOURCE_URL_LENGTH = 2048
MAX_RSS_SOURCE_PUBLISHER_LENGTH = 120
MAX_RSS_RESPONSE_BYTES = 1024 * 1024
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
TURKISH_GOOGLE_NEWS_PARAMS = {"hl": "tr", "gl": "TR", "ceid": "TR:tr"}
DEFAULT_RSS_TREND_CACHE_SECONDS = 300.0
_RSS_RESPONSE_CHUNK_BYTES = 64 * 1024
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


def _google_news_rss_params(
    topic: str, language: object | None = None
) -> dict[str, str]:
    params = {"q": topic}
    if _normalized_news_locale(language) == "tr":
        params.update(TURKISH_GOOGLE_NEWS_PARAMS)
    return params


def _bounded_response_content(response: requests.Response) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    content_length = headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid RSS Content-Length") from exc
        if declared_length < 0 or declared_length > MAX_RSS_RESPONSE_BYTES:
            raise ValueError("RSS response exceeds the size limit")

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        content = bytes(response.content)
        if len(content) > MAX_RSS_RESPONSE_BYTES:
            raise ValueError("RSS response exceeds the size limit")
        return content

    content = bytearray()
    for chunk in iter_content(chunk_size=_RSS_RESPONSE_CHUNK_BYTES):
        if not chunk:
            continue
        if len(content) + len(chunk) > MAX_RSS_RESPONSE_BYTES:
            raise ValueError("RSS response exceeds the size limit")
        content.extend(chunk)
    return bytes(content)


def _fetch_google_news_rss(topic: str, timeout: float, language: object | None):
    response = requests.get(
        GOOGLE_NEWS_RSS_URL,
        params=_google_news_rss_params(topic, language),
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    try:
        status_code = int(getattr(response, "status_code", 200))
        if 300 <= status_code < 400:
            raise requests.TooManyRedirects("Google News RSS redirect rejected")
        response.raise_for_status()
        content = _bounded_response_content(response)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    return ElementTree.fromstring(
        content,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )


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
        root = _fetch_google_news_rss(topic, timeout, language)
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
        root = _fetch_google_news_rss(topic, timeout, language)
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
