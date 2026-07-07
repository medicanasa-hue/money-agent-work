import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from app.utils import utils

COOLDOWN_FILENAME = "video_cooldown.json"
MAX_COOLDOWN_ITEMS = 1000


def get_cooldown_path(create: bool = False) -> str:
    return os.path.join(utils.storage_dir("history", create=create), COOLDOWN_FILENAME)


def normalize_url(url: str) -> str:
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
    return value.split("?", 1)[0].split("#", 1)[0]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _parse_used_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def list_records() -> list[dict]:
    path = get_cooldown_path(create=False)
    if not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []

    return [item for item in payload if isinstance(item, dict)]


def save_records(records: list[dict]) -> str:
    path = get_cooldown_path(create=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(records[:MAX_COOLDOWN_ITEMS], fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return path


def mark_used(url: str, provider: str = "") -> str:
    normalized_url = normalize_url(url)
    if not normalized_url:
        return ""

    records = [
        item
        for item in list_records()
        if normalize_url(item.get("url", "")) != normalized_url
    ]
    records.insert(
        0,
        {
            "url": normalized_url,
            "provider": str(provider or ""),
            "used_at": _utc_now_iso(),
        },
    )
    return save_records(records)


def recent_urls(days: int = 7) -> set[str]:
    try:
        cooldown_days = max(1, int(days))
    except (TypeError, ValueError):
        cooldown_days = 7

    cutoff = _utc_now() - timedelta(days=cooldown_days)
    urls = set()
    for item in list_records():
        used_at = _parse_used_at(item.get("used_at", ""))
        if used_at is None or used_at < cutoff:
            continue
        url = normalize_url(item.get("url", ""))
        if url:
            urls.add(url)
    return urls


def filter_recently_used(items: Iterable, days: int = 7) -> tuple[list, list]:
    blocked_urls = recent_urls(days)
    if not blocked_urls:
        material_items = list(items)
        return material_items, []

    available = []
    skipped = []
    for item in items:
        url = normalize_url(getattr(item, "url", ""))
        if url and url in blocked_urls:
            skipped.append(item)
        else:
            available.append(item)
    return available, skipped
