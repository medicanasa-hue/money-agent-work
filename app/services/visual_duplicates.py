"""Read-only cross-task visual duplicate scan for completed local renders."""

import os

import numpy as np
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.utils import utils


_DEFAULT_MAX_VIDEOS = 20
_DEFAULT_SAMPLES_PER_VIDEO = 3
_DEFAULT_HASH_DISTANCE_THRESHOLD = 12
_HASH_SIDE_LENGTH = 16
_MAX_DUPLICATE_PAIRS = 100


def _frame_hash(frame) -> str | None:
    """Return a luminance hash with a small brightness signature."""
    try:
        pixels = np.asarray(frame)
        if pixels.ndim == 3:
            pixels = pixels[..., :3]
            if pixels.shape[-1] < 3:
                return None
            luma = (
                pixels[..., 0] * 0.299
                + pixels[..., 1] * 0.587
                + pixels[..., 2] * 0.114
            )
        elif pixels.ndim == 2:
            luma = pixels
        else:
            return None
        if not luma.size:
            return None
        luma = np.asarray(luma, dtype=float)
        if not np.isfinite(luma).all():
            return None
        if float(luma.max()) <= 1.0:
            luma *= 255.0
        height, width = luma.shape[:2]
        row_indices = np.linspace(0, height - 1, _HASH_SIDE_LENGTH).astype(int)
        column_indices = np.linspace(0, width - 1, _HASH_SIDE_LENGTH).astype(int)
        sampled = luma[np.ix_(row_indices, column_indices)]
        mean_luma = float(sampled.mean())
        mean_bucket = max(0, min(15, int(round(mean_luma / 17.0))))
        return f"{mean_bucket:04b}" + "".join(
            "1" if value else "0" for value in (sampled > mean_luma).ravel()
        )
    except Exception:
        return None


def _hamming_distance(first_hash: str | None, second_hash: str | None) -> int | None:
    if not first_hash or not second_hash or len(first_hash) != len(second_hash):
        return None
    return sum(left != right for left, right in zip(first_hash, second_hash))


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _recent_final_video_paths(max_videos: int) -> list[str]:
    task_root = utils.storage_dir("tasks", create=False)
    if not os.path.isdir(task_root):
        return []
    candidates = []
    for directory, _subdirs, filenames in os.walk(task_root):
        for filename in filenames:
            normalized_name = filename.casefold()
            if not (
                normalized_name.startswith("final")
                and normalized_name.endswith(".mp4")
            ):
                continue
            video_path = os.path.join(directory, filename)
            try:
                modified_at = os.path.getmtime(video_path)
            except OSError:
                continue
            candidates.append((modified_at, video_path))
    candidates.sort(reverse=True)
    return [video_path for _modified_at, video_path in candidates[:max_videos]]


def _sample_video_hashes(video_path: str, samples_per_video: int) -> list[dict]:
    clip = None
    hashes = []
    try:
        clip = VideoFileClip(video_path)
        duration = float(getattr(clip, "duration", 0) or 0)
        if duration <= 0:
            return []
        sample_count = _positive_int(samples_per_video, _DEFAULT_SAMPLES_PER_VIDEO)
        sample_times = np.linspace(duration * 0.15, duration * 0.85, sample_count)
        for sample_index, sample_time in enumerate(sample_times):
            frame_hash = _frame_hash(clip.get_frame(float(sample_time)))
            if frame_hash:
                hashes.append({"sample_index": sample_index, "hash": frame_hash})
    except Exception:
        return []
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass
    return hashes


def _video_identity(video_path: str) -> dict[str, str]:
    return {
        "task_id": os.path.basename(os.path.dirname(video_path)) or "unknown",
        "video_name": os.path.basename(video_path),
    }


def find_cross_task_visual_duplicates(
    *,
    max_videos: int = _DEFAULT_MAX_VIDEOS,
    samples_per_video: int = _DEFAULT_SAMPLES_PER_VIDEO,
    distance_threshold: int = _DEFAULT_HASH_DISTANCE_THRESHOLD,
) -> dict:
    """Compare sampled frames across task folders without changing video selection."""
    max_videos = _positive_int(max_videos, _DEFAULT_MAX_VIDEOS)
    samples_per_video = _positive_int(samples_per_video, _DEFAULT_SAMPLES_PER_VIDEO)
    try:
        distance_threshold = max(0, int(distance_threshold))
    except (TypeError, ValueError):
        distance_threshold = _DEFAULT_HASH_DISTANCE_THRESHOLD

    video_paths = _recent_final_video_paths(max_videos)
    if not video_paths:
        return {
            "ok": True,
            "status": "no_final_videos",
            "scanned_video_count": 0,
            "duplicate_pair_count": 0,
            "duplicates": [],
        }

    samples = []
    unreadable_video_count = 0
    for video_path in video_paths:
        identity = _video_identity(video_path)
        video_hashes = _sample_video_hashes(video_path, samples_per_video)
        if not video_hashes:
            unreadable_video_count += 1
            continue
        for entry in video_hashes:
            samples.append({**identity, **entry})

    duplicates = []
    for index, left in enumerate(samples):
        for right in samples[index + 1 :]:
            if left["task_id"] == right["task_id"]:
                continue
            distance = _hamming_distance(left["hash"], right["hash"])
            if distance is None or distance > distance_threshold:
                continue
            duplicates.append(
                {
                    "left": {
                        "task_id": left["task_id"],
                        "video_name": left["video_name"],
                        "sample_index": left["sample_index"],
                    },
                    "right": {
                        "task_id": right["task_id"],
                        "video_name": right["video_name"],
                        "sample_index": right["sample_index"],
                    },
                    "hash_distance": distance,
                }
            )
            if len(duplicates) >= _MAX_DUPLICATE_PAIRS:
                break
        if len(duplicates) >= _MAX_DUPLICATE_PAIRS:
            break

    return {
        "ok": True,
        "status": "completed",
        "scanned_video_count": len(video_paths),
        "unreadable_video_count": unreadable_video_count,
        "duplicate_pair_count": len(duplicates),
        "duplicates": duplicates,
    }
