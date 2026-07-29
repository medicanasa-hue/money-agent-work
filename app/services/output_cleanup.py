"""Safe retention cleanup for completed task output directories."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from typing import Iterable


_VIDEO_CACHE_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_active_task_ids(task_ids: Iterable[object]) -> set[str]:
    return {
        str(task_id).strip()
        for task_id in task_ids
        if str(task_id).strip()
    }


def cleanup_task_outputs(
    task_root: str | Path,
    *,
    retention_days: int,
    active_task_ids: Iterable[object] = (),
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    """Preview or remove expired, inactive direct children of a task root.

    The caller must opt in with ``apply=True`` to delete anything. Symbolic
    links and paths outside ``task_root`` are never followed or removed.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise ValueError("retention_days must be a positive integer")
    if retention_days < 1:
        raise ValueError("retention_days must be a positive integer")

    root = Path(task_root)
    summary = {
        "dry_run": not apply,
        "retention_days": retention_days,
        "scanned": 0,
        "eligible": [],
        "deleted": [],
        "skipped_active": 0,
        "errors": [],
    }
    if not root.is_dir():
        return summary

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        summary["errors"].append(f"task root unavailable: {error}")
        return summary

    active_ids = _normalized_active_task_ids(active_task_ids)
    cutoff = (now or _utc_now()) - timedelta(days=retention_days)

    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_symlink() or not child.is_dir():
            continue
        summary["scanned"] += 1
        if child.name in active_ids:
            summary["skipped_active"] += 1
            continue

        try:
            if child.resolve(strict=True).parent != resolved_root:
                continue
            modified_at = datetime.fromtimestamp(
                child.stat().st_mtime,
                tz=timezone.utc,
            )
        except OSError as error:
            summary["errors"].append(f"{child.name}: {error}")
            continue

        if modified_at >= cutoff:
            continue
        summary["eligible"].append(child.name)
        if not apply:
            continue
        try:
            shutil.rmtree(child)
        except OSError as error:
            summary["errors"].append(f"{child.name}: {error}")
            continue
        summary["deleted"].append(child.name)

    return summary


def cleanup_video_cache(
    cache_root: str | Path,
    *,
    retention_days: int,
    active_tasks_present: bool = False,
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    """Preview or remove expired direct video-cache files.

    Only known video files inside the cache root are considered. Deletion stays
    opt-in and is blocked while a task is processing, so a running render cannot
    lose a cached source clip.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise ValueError("retention_days must be a positive integer")
    if retention_days < 1:
        raise ValueError("retention_days must be a positive integer")

    root = Path(cache_root)
    summary = {
        "dry_run": not apply,
        "retention_days": retention_days,
        "scanned": 0,
        "eligible": [],
        "eligible_bytes": 0,
        "deleted": [],
        "deleted_bytes": 0,
        "blocked_by_active_tasks": bool(apply and active_tasks_present),
        "errors": [],
    }
    if not root.is_dir():
        return summary

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        summary["errors"].append(f"cache root unavailable: {error}")
        return summary

    cutoff = (now or _utc_now()) - timedelta(days=retention_days)
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if (
            child.is_symlink()
            or not child.is_file()
            or child.suffix.lower() not in _VIDEO_CACHE_SUFFIXES
        ):
            continue
        summary["scanned"] += 1

        try:
            if child.resolve(strict=True).parent != resolved_root:
                continue
            stats = child.stat()
            modified_at = datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc)
        except OSError as error:
            summary["errors"].append(f"{child.name}: {error}")
            continue

        if modified_at >= cutoff:
            continue
        summary["eligible"].append(child.name)
        summary["eligible_bytes"] += stats.st_size
        if not apply or summary["blocked_by_active_tasks"]:
            continue
        try:
            child.unlink()
        except OSError as error:
            summary["errors"].append(f"{child.name}: {error}")
            continue
        summary["deleted"].append(child.name)
        summary["deleted_bytes"] += stats.st_size

    return summary
