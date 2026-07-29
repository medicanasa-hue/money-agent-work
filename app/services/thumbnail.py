from __future__ import annotations

import math
import os
import subprocess
from typing import Any

from app.services import video as video_service
from app.utils import utils

DEFAULT_THUMBNAIL_TIMESTAMPS = (1.0, 3.0, 5.0)
DEFAULT_THUMBNAIL_COUNT = 3


def _clean_concepts(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        items = [line.strip() for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        items = []
    return [item for item in items if item][:limit]


def _thumbnail_timestamps(timestamps=None, count: int = DEFAULT_THUMBNAIL_COUNT):
    values = list(timestamps or DEFAULT_THUMBNAIL_TIMESTAMPS)
    result = []
    seen = set()
    for value in values:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timestamp):
            continue
        timestamp = max(0.0, timestamp)
        if timestamp in seen:
            continue
        seen.add(timestamp)
        result.append(timestamp)
        if len(result) >= count:
            break
    return result or list(DEFAULT_THUMBNAIL_TIMESTAMPS[:count])


def thumbnail_output_dir(task_id: str) -> str:
    output_dir = os.path.join(utils.task_dir(task_id), "thumbnails")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def extract_thumbnail_candidates(
    video_path: str,
    output_dir: str,
    *,
    thumbnail_concepts=None,
    timestamps=None,
    count: int = DEFAULT_THUMBNAIL_COUNT,
) -> dict[str, Any]:
    video_path = str(video_path or "").strip()
    if not video_path or not os.path.isfile(video_path):
        return {"candidates": [], "error": "Video file not found."}

    os.makedirs(output_dir, exist_ok=True)
    concepts = _clean_concepts(thumbnail_concepts, count)
    candidates = []
    errors = []

    for index, timestamp in enumerate(_thumbnail_timestamps(timestamps, count), start=1):
        output_path = os.path.join(output_dir, f"thumbnail-{index}.jpg")
        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"Could not clear old thumbnail: {str(exc)}")
            continue
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-ss",
            f"{timestamp:g}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            output_path,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "candidates": candidates,
                "error": f"ffmpeg unavailable: {str(exc)}",
            }

        if result.returncode != 0:
            errors.append((result.stderr or result.stdout or "").strip())
            continue

        if not os.path.isfile(output_path):
            errors.append(f"Thumbnail file was not created: {output_path}")
            continue

        candidates.append(
            {
                "path": output_path,
                "timestamp_sec": timestamp,
                "concept": concepts[index - 1] if index - 1 < len(concepts) else "",
            }
        )

    default_timestamps = _thumbnail_timestamps(None, count)
    missing_output_errors = errors and all(
        error.startswith("Thumbnail file was not created:") for error in errors
    )
    if (
        not candidates
        and timestamps
        and missing_output_errors
        and _thumbnail_timestamps(timestamps, count) != default_timestamps
    ):
        return extract_thumbnail_candidates(
            video_path,
            output_dir,
            thumbnail_concepts=thumbnail_concepts,
            timestamps=None,
            count=count,
        )

    error = "" if candidates else (errors[0] if errors else "No thumbnail generated.")
    return {"candidates": candidates, "error": error}


def generate_thumbnail_candidates(
    *,
    task_id: str,
    video_paths,
    thumbnail_concepts=None,
    hook_timestamps=None,
    count: int = DEFAULT_THUMBNAIL_COUNT,
) -> dict[str, Any]:
    first_video = ""
    for video_path in video_paths or []:
        if str(video_path or "").strip():
            first_video = str(video_path).strip()
            break
    if not first_video:
        return {"candidates": [], "error": "Video file not found."}

    timestamps = hook_timestamps
    if hook_timestamps:
        duration = video_service.get_video_duration(first_video)
        if duration is not None:
            timestamps = [
                timestamp
                for timestamp in _thumbnail_timestamps(hook_timestamps, count)
                if timestamp <= duration
            ] or None

    return extract_thumbnail_candidates(
        first_video,
        thumbnail_output_dir(task_id),
        thumbnail_concepts=thumbnail_concepts,
        timestamps=timestamps,
        count=count,
    )
