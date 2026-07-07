from typing import Any
from urllib.parse import quote_plus

import requests

MAX_RSS_TREND_LENGTH = 500
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}"


def _clean_rss_title(value: Any) -> str:
    return str(value or "").strip()


def fetch_rss_trend(topic: str, limit: int = 3, timeout: float = 3.0) -> str:
    topic = str(topic or "").strip()
    if not topic:
        return ""

    try:
        import feedparser
    except Exception:
        return ""

    try:
        url = GOOGLE_NEWS_RSS_URL.format(query=quote_plus(topic))
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception:
        return ""

    titles = []
    for entry in getattr(feed, "entries", [])[: max(1, int(limit or 1))]:
        title = _clean_rss_title(getattr(entry, "title", ""))
        if title:
            titles.append(title)

    summary = "; ".join(titles)
    return summary[:MAX_RSS_TREND_LENGTH]
