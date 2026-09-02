import math
import os
import re
import subprocess

from app.utils import utils


_DEFAULT_BLACKDETECT_TIMEOUT_SECONDS = 30
_DEFAULT_BLACKDETECT_MIN_DURATION_SECONDS = 0.15
_DEFAULT_BLACKDETECT_PIXEL_THRESHOLD = 0.10
_DEFAULT_FREEZEDETECT_TIMEOUT_SECONDS = 30
_DEFAULT_FREEZEDETECT_MIN_DURATION_SECONDS = 1.5
_DEFAULT_FREEZEDETECT_NOISE_TOLERANCE = 0.001
_DEFAULT_SCDET_TIMEOUT_SECONDS = 30
_DEFAULT_SCDET_THRESHOLD = 10.0


def _as_finite_float(value) -> float | None:
    try:
        parsed_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed_value):
        return None
    return parsed_value


def detect_scene_change_timestamps(
    video_path: str,
    *,
    threshold: float = _DEFAULT_SCDET_THRESHOLD,
    timeout_seconds: int = _DEFAULT_SCDET_TIMEOUT_SECONDS,
) -> list[float] | None:
    """Return FFmpeg-detected scene-change times, or None when unavailable."""
    normalized_path = str(video_path or "").strip()
    scene_threshold = _as_finite_float(threshold)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return None
    if (
        not normalized_path
        or not os.path.isfile(normalized_path)
        or scene_threshold is None
        or not 0 <= scene_threshold <= 100
        or timeout_seconds <= 0
    ):
        return None

    try:
        ffmpeg_binary = str(utils.get_ffmpeg_binary() or "").strip()
    except Exception:
        return None
    if not ffmpeg_binary:
        return None

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-i",
        normalized_path,
        "-vf",
        f"scale=320:-2,scdet=threshold={scene_threshold:g}",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    scene_change_times = set()
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    for match in re.finditer(r"lavfi\.scd\.time:\s*([-+0-9.eE]+)", output):
        scene_time = _as_finite_float(match.group(1))
        if scene_time is not None and scene_time >= 0:
            scene_change_times.add(round(scene_time, 3))
    return sorted(scene_change_times)


def detect_sustained_near_black_segments(
    video_path: str,
    *,
    min_duration_seconds: float = _DEFAULT_BLACKDETECT_MIN_DURATION_SECONDS,
    pixel_threshold: float = _DEFAULT_BLACKDETECT_PIXEL_THRESHOLD,
    timeout_seconds: int = _DEFAULT_BLACKDETECT_TIMEOUT_SECONDS,
) -> list[tuple[float, float]] | None:
    """Return FFmpeg-detected near-black intervals, or None when unavailable."""
    normalized_path = str(video_path or "").strip()
    if not normalized_path or not os.path.isfile(normalized_path):
        return None

    try:
        ffmpeg_binary = str(utils.get_ffmpeg_binary() or "").strip()
    except Exception:
        return None
    if not ffmpeg_binary:
        return None

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-i",
        normalized_path,
        "-vf",
        (
            "scale=320:-2,blackdetect="
            f"d={min_duration_seconds}:pix_th={pixel_threshold}"
        ),
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    segments = []
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    for match in re.finditer(r"black_start:([0-9.]+) black_end:([0-9.]+)", output):
        start_time = _as_finite_float(match.group(1))
        end_time = _as_finite_float(match.group(2))
        if (
            start_time is not None
            and end_time is not None
            and end_time >= start_time
        ):
            segments.append((round(start_time, 3), round(end_time, 3)))
    return segments


def detect_sustained_frozen_segments(
    video_path: str,
    *,
    min_duration_seconds: float = _DEFAULT_FREEZEDETECT_MIN_DURATION_SECONDS,
    noise_tolerance: float = _DEFAULT_FREEZEDETECT_NOISE_TOLERANCE,
    timeout_seconds: int = _DEFAULT_FREEZEDETECT_TIMEOUT_SECONDS,
) -> list[tuple[float, float]] | None:
    """Return FFmpeg-detected frozen-video intervals, or None when unavailable."""
    normalized_path = str(video_path or "").strip()
    freeze_duration = _as_finite_float(min_duration_seconds)
    noise = _as_finite_float(noise_tolerance)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return None
    if (
        not normalized_path
        or not os.path.isfile(normalized_path)
        or freeze_duration is None
        or freeze_duration <= 0
        or noise is None
        or not 0 <= noise <= 1
        or timeout_seconds <= 0
    ):
        return None

    try:
        ffmpeg_binary = str(utils.get_ffmpeg_binary() or "").strip()
    except Exception:
        return None
    if not ffmpeg_binary:
        return None

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-i",
        normalized_path,
        "-vf",
        (
            "scale=320:-2,"
            "tpad=stop_mode=add:stop_duration=0.1:color=white,"
            "freezedetect="
            f"n={noise:g}:d={freeze_duration:g}"
        ),
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    pending_start = None
    pending_duration = None
    segments = []
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    for match in re.finditer(
        r"lavfi\.freezedetect\.(freeze_start|freeze_duration|freeze_end):\s*"
        r"([-+0-9.eE]+)",
        output,
    ):
        event, raw_value = match.groups()
        value = _as_finite_float(raw_value)
        if value is None or value < 0:
            continue
        if event == "freeze_start":
            pending_start = value
            pending_duration = None
            continue
        if event == "freeze_duration":
            pending_duration = value
            continue
        if pending_start is None and pending_duration is not None:
            pending_start = max(0.0, value - pending_duration)
        if pending_start is not None and value >= pending_start:
            segments.append((round(pending_start, 3), round(value, 3)))
        pending_start = None
        pending_duration = None

    if pending_start is not None and pending_duration is not None:
        segments.append(
            (round(pending_start, 3), round(pending_start + pending_duration, 3))
        )
    return sorted(set(segments))
