import json
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable

from app.services import cost_estimate
from app.utils import utils

HISTORY_FILENAME = "production_history.json"
METRICS_SYNC_RUN_FILENAME = "metrics_sync_run.json"
MAX_HISTORY_ITEMS = 100
MAX_PUBLISH_METRIC_SNAPSHOTS = 30
DEFAULT_SUBJECT_LOOKBACK_DAYS = 5
DEFAULT_SUBJECT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_SCRIPT_LOOKBACK_DAYS = 30
DEFAULT_SCRIPT_SIMILARITY_THRESHOLD = 0.78
MIN_SCRIPT_REPEAT_TOKENS = 12
METRICS_SYNC_OUTCOME_KEYS = (
    "synced",
    "no_data",
    "transient_error",
    "permanent_error",
)
PUBLISH_METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves")


def get_history_path(create: bool = False) -> str:
    return os.path.join(utils.storage_dir("history", create=create), HISTORY_FILENAME)


def get_metrics_sync_run_path(create: bool = False) -> str:
    return os.path.join(
        utils.storage_dir("history", create=create),
        METRICS_SYNC_RUN_FILENAME,
    )


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


def _metrics_sync_recorded_at(value: str | datetime | None = None) -> str:
    if isinstance(value, datetime):
        parsed = value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    else:
        parsed = _parse_datetime(value) if value else None
    return (parsed or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _safe_metrics_sync_run(summary: Any, *, status: Any, recorded_at: Any) -> dict:
    summary = summary if isinstance(summary, dict) else {}
    raw_outcomes = summary.get("outcomes")
    raw_outcomes = raw_outcomes if isinstance(raw_outcomes, dict) else {}
    errors = summary.get("errors")
    error_count = (
        len(errors)
        if isinstance(errors, (list, tuple, set))
        else _normalize_metric_int(errors)
    )
    return {
        "recorded_at": _metrics_sync_recorded_at(recorded_at),
        "status": status.strip() if isinstance(status, str) and status.strip() else "completed",
        "eligible": _normalize_metric_int(summary.get("eligible")),
        "synced": _normalize_metric_int(summary.get("synced")),
        "skipped": _normalize_metric_int(summary.get("skipped")),
        "errors": error_count,
        "outcomes": {
            key: _normalize_metric_int(raw_outcomes.get(key))
            for key in METRICS_SYNC_OUTCOME_KEYS
        },
    }


def record_metrics_sync_run(
    summary: dict[str, Any],
    *,
    status: str = "completed",
    recorded_at: str | datetime | None = None,
) -> dict:
    """Persist a safe, aggregate-only summary of a metrics-sync run."""
    payload = _safe_metrics_sync_run(
        summary,
        status=status,
        recorded_at=recorded_at,
    )
    path = get_metrics_sync_run_path(create=True)
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)
    return payload


def get_last_metrics_sync_run() -> dict | None:
    """Return the most recent safe metrics-sync summary, if present."""
    path = get_metrics_sync_run_path(create=False)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or _parse_datetime(payload.get("recorded_at")) is None:
        return None
    return _safe_metrics_sync_run(
        payload,
        status=payload.get("status"),
        recorded_at=payload.get("recorded_at"),
    )


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


def _normalize_script_tokens(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)


def _script_similarity(left: Any, right: Any) -> float:
    left_text = left.strip() if isinstance(left, str) else ""
    right_text = right.strip() if isinstance(right, str) else ""
    left_tokens = _normalize_script_tokens(left_text)
    right_tokens = _normalize_script_tokens(right_text)
    if (
        len(left_tokens) < MIN_SCRIPT_REPEAT_TOKENS
        or len(right_tokens) < MIN_SCRIPT_REPEAT_TOKENS
    ):
        return 0.0

    sequence_score = SequenceMatcher(
        None,
        " ".join(left_tokens),
        " ".join(right_tokens),
    ).ratio()
    left_ngrams = set(zip(left_tokens, left_tokens[1:], left_tokens[2:]))
    right_ngrams = set(zip(right_tokens, right_tokens[1:], right_tokens[2:]))
    ngram_score = (
        len(left_ngrams & right_ngrams) / len(left_ngrams | right_ngrams)
        if left_ngrams and right_ngrams
        else 0.0
    )
    return max(sequence_score, ngram_score)


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    normalized.setdefault("created_at", _utc_now_iso())
    normalized.setdefault("task_id", "")
    normalized.setdefault("subject", "")
    normalized.setdefault("script", "")
    normalized.setdefault("language", "")
    normalized.setdefault("status", "completed")
    normalized.setdefault("videos", [])
    normalized.setdefault("materials", [])
    normalized.setdefault("material_attributions", None)
    normalized.setdefault("metadata", None)
    normalized.setdefault("viral_analysis", None)
    normalized.setdefault("thumbnail_candidates", None)
    normalized.setdefault("thumbnail_candidate_error", "")
    normalized.setdefault("publish_metrics", None)
    normalized.setdefault("metrics_sync", None)
    normalized.setdefault("cooldown", None)
    normalized.setdefault("pending_uploads", None)
    normalized.setdefault("cost_estimate", None)
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


def list_jobs_pending_metrics_sync(
    max_age_hours: int = 72,
    recheck_after_hours: int = 24,
    *,
    now: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """List recent published jobs whose metrics are absent or stale."""
    if isinstance(now, datetime):
        current_time = (
            now.astimezone(timezone.utc)
            if now.tzinfo
            else now.replace(tzinfo=timezone.utc)
        )
    else:
        current_time = _parse_datetime(now) if now else datetime.now(timezone.utc)
    if current_time is None:
        current_time = datetime.now(timezone.utc)

    try:
        max_age = max(0, int(max_age_hours))
    except (TypeError, ValueError):
        max_age = 72
    try:
        recheck_after = max(0, int(recheck_after_hours))
    except (TypeError, ValueError):
        recheck_after = 24

    age_cutoff = current_time - timedelta(hours=max_age)
    recheck_cutoff = current_time - timedelta(hours=recheck_after)
    candidates: list[dict[str, Any]] = []

    for entry in list_history():
        task_id = entry.get("task_id")
        pending_uploads = entry.get("pending_uploads")
        created_at = _parse_datetime(entry.get("created_at"))
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(pending_uploads, list)
            or not pending_uploads
            or created_at is None
            or created_at < age_cutoff
        ):
            continue

        publish_metrics = entry.get("publish_metrics")
        captured_at = (
            _parse_datetime(publish_metrics.get("captured_at"))
            if isinstance(publish_metrics, dict)
            else None
        )
        sync_state = entry.get("metrics_sync")
        attempted_at = (
            _parse_datetime(sync_state.get("attempted_at"))
            if isinstance(sync_state, dict)
            else None
        )
        if attempted_at is not None and attempted_at > recheck_cutoff:
            continue
        if captured_at is None or captured_at <= recheck_cutoff:
            candidates.append(dict(entry))

    return candidates


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


def backfill_render_quality_reports(
    inspect_video: Callable[[str], dict[str, Any]],
    *,
    persist: bool = False,
) -> dict[str, int | bool]:
    """Inspect local historical outputs without replacing existing reports."""
    entries = list_history()
    summary: dict[str, int | bool] = {
        "dry_run": not persist,
        "jobs_checked": 0,
        "jobs_with_new_reports": 0,
        "updated_jobs": 0,
        "inspected_videos": 0,
        "skipped_videos": 0,
        "missing_videos": 0,
        "inspection_errors": 0,
    }

    for entry in entries:
        videos = entry.get("videos")
        if not isinstance(videos, (list, tuple)):
            continue
        video_paths = [
            str(video_path).strip()
            for video_path in videos
            if isinstance(video_path, str) and str(video_path).strip()
        ]
        if not video_paths:
            continue
        summary["jobs_checked"] += 1

        existing_reports = entry.get("render_quality_reports")
        if existing_reports is None:
            existing_reports = []
        if not isinstance(existing_reports, list):
            continue
        reported_paths = {
            str(report.get("video_path") or "").strip()
            for report in existing_reports
            if isinstance(report, dict)
        }
        new_reports = []
        for video_path in dict.fromkeys(video_paths):
            if video_path in reported_paths:
                summary["skipped_videos"] += 1
                continue
            if not os.path.isfile(video_path):
                summary["missing_videos"] += 1
                continue
            summary["inspected_videos"] += 1
            try:
                report = inspect_video(video_path)
            except Exception:
                summary["inspection_errors"] += 1
                continue
            if not isinstance(report, dict):
                summary["inspection_errors"] += 1
                continue
            new_reports.append({**report, "video_path": video_path})

        if not new_reports:
            continue
        summary["jobs_with_new_reports"] += 1
        if persist:
            entry["render_quality_reports"] = [*existing_reports, *new_reports]
            summary["updated_jobs"] += 1

    if persist and summary["updated_jobs"]:
        save_history(entries)
    return summary


def add_history(entry: dict[str, Any]) -> str:
    entries = list_history()
    normalized = _normalize_entry(entry)
    if not isinstance(normalized.get("cost_estimate"), dict):
        normalized["cost_estimate"] = cost_estimate.estimate_history_cost(normalized)
    entries.insert(0, normalized)
    return save_history(entries)


def find_recent_similar_subjects(
    subject: str,
    *,
    days: int = DEFAULT_SUBJECT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_SUBJECT_SIMILARITY_THRESHOLD,
    now: str | datetime | None = None,
    limit: int = 3,
    semantic_similarity: Callable[[str, str], float | None] | None = None,
    semantic_threshold: float | None = None,
    semantic_candidate_limit: int = 0,
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
    try:
        lexical_threshold = float(threshold)
    except (TypeError, ValueError):
        lexical_threshold = DEFAULT_SUBJECT_SIMILARITY_THRESHOLD
    try:
        semantic_minimum = float(
            semantic_threshold if semantic_threshold is not None else lexical_threshold
        )
    except (TypeError, ValueError):
        semantic_minimum = lexical_threshold
    try:
        semantic_budget = max(0, int(semantic_candidate_limit))
    except (TypeError, ValueError):
        semantic_budget = 0

    matches: list[dict[str, Any]] = []
    semantic_checked = 0
    for entry in list_history():
        entry_subject = entry.get("subject", "")
        created_at = _parse_datetime(entry.get("created_at"))
        if not entry_subject or created_at is None or created_at < cutoff:
            continue

        lexical_similarity = _subject_similarity(current_subject, entry_subject)
        semantic_score = None
        if (
            lexical_similarity < lexical_threshold
            and semantic_similarity is not None
            and semantic_checked < semantic_budget
        ):
            semantic_checked += 1
            try:
                raw_semantic_score = semantic_similarity(current_subject, entry_subject)
                if isinstance(raw_semantic_score, (int, float)):
                    semantic_score = min(1.0, max(0.0, float(raw_semantic_score)))
            except Exception:
                semantic_score = None
        similarity = max(lexical_similarity, semantic_score or 0.0)
        if lexical_similarity < lexical_threshold and (
            semantic_score is None or semantic_score < semantic_minimum
        ):
            continue

        match = dict(entry)
        match["similarity"] = round(similarity, 3)
        if semantic_score is not None:
            match["semantic_similarity"] = round(semantic_score, 3)
        matches.append(match)

    matches.sort(
        key=lambda item: (
            item.get("similarity", 0),
            item.get("created_at", ""),
        ),
        reverse=True,
    )
    return matches[: max(0, int(limit))]


def find_recent_similar_scripts(
    script: str,
    *,
    days: int = DEFAULT_SCRIPT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_SCRIPT_SIMILARITY_THRESHOLD,
    now: str | datetime | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Find recent near-duplicate scripts without returning their text."""
    current_script = script.strip() if isinstance(script, str) else ""
    if len(_normalize_script_tokens(current_script)) < MIN_SCRIPT_REPEAT_TOKENS:
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
        lookback_days = DEFAULT_SCRIPT_LOOKBACK_DAYS
    try:
        similarity_threshold = float(threshold)
    except (TypeError, ValueError):
        similarity_threshold = DEFAULT_SCRIPT_SIMILARITY_THRESHOLD
    try:
        result_limit = max(0, int(limit))
    except (TypeError, ValueError):
        result_limit = 3
    cutoff = current_time - timedelta(days=lookback_days)

    matches: list[dict[str, Any]] = []
    for entry in list_history():
        entry_script = entry.get("script")
        created_at = _parse_datetime(entry.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue

        similarity = _script_similarity(current_script, entry_script)
        if similarity < similarity_threshold:
            continue

        matches.append(
            {
                "task_id": entry.get("task_id", ""),
                "subject": entry.get("subject", ""),
                "created_at": entry.get("created_at", ""),
                "similarity": round(similarity, 3),
            }
        )

    matches.sort(
        key=lambda item: (
            item.get("similarity", 0),
            item.get("created_at", ""),
        ),
        reverse=True,
    )
    return matches[:result_limit]


def _normalize_metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed_value)


def _normalize_platform_metrics(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, int]] = {}
    for raw_platform, raw_metrics in value.items():
        platform = str(raw_platform or "").strip().casefold()
        if not platform or not re.fullmatch(r"[a-z0-9._-]{1,64}", platform):
            continue
        if not isinstance(raw_metrics, dict):
            continue
        normalized[platform] = {
            field: _normalize_metric_int(raw_metrics.get(field))
            for field in PUBLISH_METRIC_FIELDS
        }
    return normalized


def normalize_publish_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics if isinstance(metrics, dict) else {}
    normalized = {
        field: _normalize_metric_int(metrics.get(field))
        for field in PUBLISH_METRIC_FIELDS
    }
    normalized.update(
        {
        "captured_at": str(metrics.get("captured_at") or _utc_now_iso()).strip(),
        }
    )
    platform_metrics = _normalize_platform_metrics(metrics.get("platform_metrics"))
    if platform_metrics:
        normalized["platform_metrics"] = platform_metrics
    return normalized


def update_publish_metrics(task_id: str, metrics: dict[str, Any]) -> bool:
    entries = list_history()
    normalized_metrics = normalize_publish_metrics(metrics)

    for entry in entries:
        if entry.get("task_id") != task_id:
            continue
        entry["publish_metrics"] = normalized_metrics
        snapshots = entry.get("publish_metric_snapshots")
        if not isinstance(snapshots, list):
            snapshots = []
        normalized_snapshots = [
            normalize_publish_metrics(snapshot)
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        ]
        normalized_snapshots.append(normalized_metrics)
        entry["publish_metric_snapshots"] = normalized_snapshots[
            -MAX_PUBLISH_METRIC_SNAPSHOTS:
        ]
        save_history(entries)
        return True
    return False


def update_metrics_sync_state(
    task_id: str,
    outcome: str,
    *,
    attempted_at: str | datetime | None = None,
) -> bool:
    """Record a non-successful metrics-sync attempt for recheck throttling."""
    if not isinstance(task_id, str) or not task_id.strip():
        return False
    if not isinstance(outcome, str) or not outcome.strip():
        return False

    if isinstance(attempted_at, datetime):
        timestamp = (
            attempted_at.astimezone(timezone.utc)
            if attempted_at.tzinfo
            else attempted_at.replace(tzinfo=timezone.utc)
        ).isoformat(timespec="seconds")
    else:
        parsed_attempt = _parse_datetime(attempted_at)
        timestamp = (
            parsed_attempt.isoformat(timespec="seconds")
            if parsed_attempt is not None
            else _utc_now_iso()
        )

    entries = list_history()
    for entry in entries:
        if entry.get("task_id") != task_id:
            continue
        state = entry.get("metrics_sync")
        if not isinstance(state, dict):
            state = {}
        state["outcome"] = outcome.strip()
        state["attempted_at"] = timestamp
        entry["metrics_sync"] = state
        save_history(entries)
        return True
    return False


def update_pending_upload_result(
    task_id: str,
    video_path: str,
    result: dict[str, Any],
    *,
    disclosure_reviewed: bool | None = None,
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
            continue

        for pending_upload in pending_uploads:
            if not isinstance(pending_upload, dict):
                continue
            if pending_upload.get("video_path") != video_path:
                continue

            pending_upload["status"] = upload_status
            pending_upload["result"] = upload_result
            pending_upload["updated_at"] = updated_at
            if disclosure_reviewed is not None:
                pending_upload["disclosure_review"] = {
                    "reviewed": bool(disclosure_reviewed),
                    "reviewed_at": updated_at,
                }
            save_history(entries)
            return True

    return False


def clear_history() -> None:
    path = get_history_path(create=False)
    if os.path.isfile(path):
        os.remove(path)
