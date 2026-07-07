import json
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from app.utils import utils

HISTORY_FILENAME = "production_history.json"
MAX_HISTORY_ITEMS = 100
DEFAULT_SUBJECT_LOOKBACK_DAYS = 5
DEFAULT_SUBJECT_SIMILARITY_THRESHOLD = 0.5


def get_history_path(create: bool = False) -> str:
    return os.path.join(utils.storage_dir("history", create=create), HISTORY_FILENAME)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_subject_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {
        token
        for token in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(token) > 2
    }


def _subject_similarity(left: Any, right: Any) -> float:
    left_text = left.strip() if isinstance(left, str) else ""
    right_text = right.strip() if isinstance(right, str) else ""
    if not left_text or not right_text:
        return 0.0

    left_tokens = _normalize_subject_tokens(left_text)
    right_tokens = _normalize_subject_tokens(right_text)
    token_score = 0.0
    if left_tokens and right_tokens:
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    sequence_score = SequenceMatcher(
        None, left_text.casefold(), right_text.casefold()
    ).ratio()
    return max(token_score, sequence_score)


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    normalized.setdefault("created_at", _utc_now_iso())
    normalized.setdefault("task_id", "")
    normalized.setdefault("subject", "")
    normalized.setdefault("status", "completed")
    normalized.setdefault("videos", [])
    normalized.setdefault("materials", [])
    normalized.setdefault("material_attributions", None)
    normalized.setdefault("metadata", None)
    normalized.setdefault("cooldown", None)
    normalized.setdefault("pending_uploads", None)
    normalized.setdefault("error", "")
    return normalized


def list_history(limit: int | None = None) -> list[dict[str, Any]]:
    path = get_history_path(create=False)
    if not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []

    entries = [entry for entry in payload if isinstance(entry, dict)]
    if limit is None:
        return entries
    return entries[: max(0, int(limit))]


def save_history(entries: list[dict[str, Any]]) -> str:
    path = get_history_path(create=True)
    temp_path = f"{path}.tmp"
    normalized = [_normalize_entry(entry) for entry in entries]
    try:
        with open(temp_path, "w", encoding="utf-8") as fp:
            json.dump(normalized[:MAX_HISTORY_ITEMS], fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)
    return path


def add_history(entry: dict[str, Any]) -> str:
    entries = list_history()
    entries.insert(0, _normalize_entry(entry))
    return save_history(entries)


def find_recent_similar_subjects(
    subject: str,
    *,
    days: int = DEFAULT_SUBJECT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_SUBJECT_SIMILARITY_THRESHOLD,
    now: str | datetime | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    current_subject = subject.strip() if isinstance(subject, str) else ""
    if not current_subject:
        return []

    if isinstance(now, datetime):
        current_time = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        current_time = _parse_datetime(now) if now else datetime.now(timezone.utc)
    if current_time is None:
        current_time = datetime.now(timezone.utc)

    try:
        lookback_days = max(0, int(days))
    except (TypeError, ValueError):
        lookback_days = DEFAULT_SUBJECT_LOOKBACK_DAYS
    cutoff = current_time - timedelta(days=lookback_days)

    matches: list[dict[str, Any]] = []
    for entry in list_history():
        entry_subject = entry.get("subject", "")
        created_at = _parse_datetime(entry.get("created_at"))
        if not entry_subject or created_at is None or created_at < cutoff:
            continue

        similarity = _subject_similarity(current_subject, entry_subject)
        if similarity < threshold:
            continue

        match = dict(entry)
        match["similarity"] = round(similarity, 3)
        matches.append(match)

    matches.sort(
        key=lambda item: (
            item.get("similarity", 0),
            item.get("created_at", ""),
        ),
        reverse=True,
    )
    return matches[: max(0, int(limit))]


def update_pending_upload_result(
    task_id: str,
    video_path: str,
    result: dict[str, Any],
) -> bool:
    entries = list_history()
    upload_result = dict(result or {})
    upload_status = "uploaded" if upload_result.get("success") else "failed"
    updated_at = _utc_now_iso()

    for entry in entries:
        if entry.get("task_id") != task_id:
            continue

        pending_uploads = entry.get("pending_uploads") or []
        if not isinstance(pending_uploads, list):
            return False

        for pending_upload in pending_uploads:
            if not isinstance(pending_upload, dict):
                continue
            if pending_upload.get("video_path") != video_path:
                continue

            pending_upload["status"] = upload_status
            pending_upload["result"] = upload_result
            pending_upload["updated_at"] = updated_at
            save_history(entries)
            return True

    return False


def clear_history() -> None:
    path = get_history_path(create=False)
    if os.path.isfile(path):
        os.remove(path)
