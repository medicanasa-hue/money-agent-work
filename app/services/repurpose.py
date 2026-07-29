import math
import os
import re
import subprocess
import unicodedata
from typing import Any

from app.services import subtitle
from app.utils import utils


RENDER_MODE_FAST = "fast"
RENDER_MODE_PRECISE = "precise"
RENDER_MODES = frozenset((RENDER_MODE_FAST, RENDER_MODE_PRECISE))
RENDER_ASPECT_SOURCE = "source"
RENDER_ASPECT_PORTRAIT = "9:16"
RENDER_ASPECTS = frozenset((RENDER_ASPECT_SOURCE, RENDER_ASPECT_PORTRAIT))
DEFAULT_PRECISE_VIDEO_CODEC = "libx264"
SELECTION_MODE_BALANCED = "balanced"
SELECTION_MODE_SUBTITLE = "subtitle"
SUPPORTED_PRECISE_VIDEO_CODECS = frozenset(
    (
        DEFAULT_PRECISE_VIDEO_CODEC,
        "h264_nvenc",
        "h264_amf",
        "h264_qsv",
        "h264_mf",
        "h264_videotoolbox",
    )
)
_SRT_TIMESTAMP_PATTERN = re.compile(r"(\d{1,2}:\d{2}:\d{2},\d{3})")
_SUBTITLE_HOOK_TOKENS = frozenset(
    (
        "why",
        "how",
        "secret",
        "mistake",
        "truth",
        "warning",
        "never",
        "stop",
        "money",
        "inflation",
        "neden",
        "nasil",
        "sir",
        "hata",
        "gercek",
        "dikkat",
        "asla",
        "para",
        "enflasyon",
    )
)


def _positive_duration(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration <= 0 or not math.isfinite(duration):
        return None
    return duration


def _positive_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _non_negative_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or not math.isfinite(parsed):
        return None
    return parsed


def plan_short_clips(
    source_duration_seconds: Any,
    *,
    clip_duration_seconds: Any,
    clip_count: Any,
) -> dict[str, Any] | None:
    """Plan evenly distributed, non-overlapping short-clip windows.

    This only proposes windows. Rendering, upload, and semantic selection are
    intentionally left to later stages.
    """
    source_duration = _positive_duration(source_duration_seconds)
    requested_duration = _positive_duration(clip_duration_seconds)
    requested_count = _positive_count(clip_count)
    if (
        source_duration is None
        or requested_duration is None
        or requested_count is None
    ):
        return None

    clip_duration = min(requested_duration, source_duration)
    available_count = max(1, int(source_duration // clip_duration))
    planned_count = min(requested_count, available_count)
    remaining_duration = source_duration - planned_count * clip_duration
    gap_duration = (
        remaining_duration / (planned_count - 1) if planned_count > 1 else 0.0
    )

    clips = [
        {
            "index": index + 1,
            "start_seconds": round(index * (clip_duration + gap_duration), 3),
            "duration_seconds": round(clip_duration, 3),
        }
        for index in range(planned_count)
    ]
    return {
        "source_duration_seconds": round(source_duration, 3),
        "clip_duration_seconds": round(clip_duration, 3),
        "requested_clip_count": requested_count,
        "clip_count": planned_count,
        "clips": clips,
    }


def _subtitle_timestamp_seconds(value: str) -> float | None:
    try:
        hours, minutes, seconds_and_milliseconds = value.split(":")
        seconds, milliseconds = seconds_and_milliseconds.split(",")
        return (
            int(hours) * 3600
            + int(minutes) * 60
            + int(seconds)
            + int(milliseconds) / 1000
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _subtitle_segments(subtitle_path: str) -> list[dict[str, Any]]:
    if not isinstance(subtitle_path, str) or not subtitle_path.strip():
        return []
    try:
        subtitle_blocks = subtitle.file_to_subtitles(subtitle_path)
    except (OSError, UnicodeError):
        return []

    segments = []
    for _, timestamp_range, text in subtitle_blocks:
        timestamps = _SRT_TIMESTAMP_PATTERN.findall(str(timestamp_range or ""))
        if len(timestamps) != 2 or not isinstance(text, str) or not text.strip():
            continue
        start_seconds = _subtitle_timestamp_seconds(timestamps[0])
        end_seconds = _subtitle_timestamp_seconds(timestamps[1])
        if (
            start_seconds is None
            or end_seconds is None
            or end_seconds <= start_seconds
        ):
            continue
        segments.append(
            {
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "text": text.strip(),
            }
        )
    return segments


def _subtitle_text_score(text: str, start_seconds: float) -> float:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).replace("ı", "i")
    tokens = re.findall(r"[^\W_]+", normalized)
    if not tokens:
        return 0.0

    hook_matches = sum(token in _SUBTITLE_HOOK_TOKENS for token in tokens)
    score = min(len(tokens), 12) * 0.05
    score += min(len(set(tokens)), 8) * 0.05
    score += min(hook_matches, 3)
    if "?" in text or "!" in text:
        score += 0.5
    if any(character.isdigit() for character in text):
        score += 0.3
    if start_seconds <= 15:
        score += 0.2
    return score


def _clips_overlap(left: dict[str, float], right: dict[str, float]) -> bool:
    left_end = left["start_seconds"] + left["duration_seconds"]
    right_end = right["start_seconds"] + right["duration_seconds"]
    return max(left["start_seconds"], right["start_seconds"]) < min(left_end, right_end)


def plan_subtitle_guided_short_clips(
    source_duration_seconds: Any,
    *,
    clip_duration_seconds: Any,
    clip_count: Any,
    subtitle_path: str,
) -> dict[str, Any] | None:
    """Prefer non-overlapping subtitle segments with strong local hook signals."""
    balanced_plan = plan_short_clips(
        source_duration_seconds,
        clip_duration_seconds=clip_duration_seconds,
        clip_count=clip_count,
    )
    if balanced_plan is None:
        return None

    segments = _subtitle_segments(subtitle_path)
    planned_count = balanced_plan["clip_count"]
    clip_duration = balanced_plan["clip_duration_seconds"]
    source_duration = balanced_plan["source_duration_seconds"]
    candidates = []
    for segment in segments:
        start_seconds = min(
            max(0.0, segment["start_seconds"]),
            source_duration - clip_duration,
        )
        candidates.append(
            {
                "start_seconds": round(start_seconds, 3),
                "duration_seconds": clip_duration,
                "score": _subtitle_text_score(segment["text"], start_seconds),
            }
        )

    selected = []
    for candidate in sorted(
        candidates,
        key=lambda item: (-item["score"], item["start_seconds"]),
    ):
        if any(_clips_overlap(candidate, existing) for existing in selected):
            continue
        selected.append(candidate)
        if len(selected) == planned_count:
            break

    if len(selected) != planned_count:
        return {
            **balanced_plan,
            "selection_mode": SELECTION_MODE_BALANCED,
            "subtitle_segment_count": len(segments),
        }

    clips = [
        {
            "index": index,
            "start_seconds": candidate["start_seconds"],
            "duration_seconds": candidate["duration_seconds"],
        }
        for index, candidate in enumerate(
            sorted(selected, key=lambda item: item["start_seconds"]),
            start=1,
        )
    ]
    return {
        **balanced_plan,
        "clips": clips,
        "selection_mode": SELECTION_MODE_SUBTITLE,
        "subtitle_segment_count": len(segments),
    }


def render_short_clips(
    input_video_path: str,
    output_dir: str,
    clips: Any,
    *,
    render_mode: str = RENDER_MODE_FAST,
    video_codec: str = DEFAULT_PRECISE_VIDEO_CODEC,
    target_aspect: str = RENDER_ASPECT_SOURCE,
) -> dict[str, int]:
    """Write planned clips without overwriting files."""
    normalized_target_aspect = str(target_aspect or RENDER_ASPECT_SOURCE).strip().lower()
    if (
        not isinstance(output_dir, str)
        or not output_dir.strip()
        or render_mode not in RENDER_MODES
        or normalized_target_aspect not in RENDER_ASPECTS
        or (
            normalized_target_aspect != RENDER_ASPECT_SOURCE
            and render_mode != RENDER_MODE_PRECISE
        )
    ):
        return {"rendered_clip_count": 0, "error_count": 1}
    precise_video_codec = str(
        video_codec or DEFAULT_PRECISE_VIDEO_CODEC
    ).strip().lower()
    if (
        render_mode == RENDER_MODE_PRECISE
        and precise_video_codec not in SUPPORTED_PRECISE_VIDEO_CODECS
    ):
        return {"rendered_clip_count": 0, "error_count": 1}
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError:
        return {"rendered_clip_count": 0, "error_count": 1}

    rendered_count = 0
    error_count = 0
    ffmpeg_binary = utils.get_ffmpeg_binary()
    for raw_clip in clips if isinstance(clips, (list, tuple)) else ():
        if not isinstance(raw_clip, dict):
            error_count += 1
            continue
        index = _positive_count(raw_clip.get("index"))
        start_time = _non_negative_time(raw_clip.get("start_seconds"))
        duration = _positive_duration(raw_clip.get("duration_seconds"))
        if index is None or start_time is None or duration is None:
            error_count += 1
            continue

        output_file = os.path.join(output_dir, f"short_clip_{index:02d}.mp4")
        if os.path.exists(output_file):
            error_count += 1
            continue
        command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-n"]
        if render_mode == RENDER_MODE_PRECISE:
            precise_args = [
                "-i",
                input_video_path,
                "-ss",
                str(start_time),
                "-t",
                str(duration),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                precise_video_codec,
            ]
            if precise_video_codec == DEFAULT_PRECISE_VIDEO_CODEC:
                precise_args.extend(["-preset", "medium", "-crf", "20"])
            if normalized_target_aspect == RENDER_ASPECT_PORTRAIT:
                precise_args.extend(
                    [
                        "-vf",
                        "scale=1080:1920:force_original_aspect_ratio=increase,"
                        "crop=1080:1920",
                    ]
                )
            precise_args.extend(
                [
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                ]
            )
            command.extend(precise_args)
        else:
            command.extend(
                [
                    "-ss",
                    str(start_time),
                    "-i",
                    input_video_path,
                    "-t",
                    str(duration),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                ]
            )
        command.extend(["-movflags", "+faststart", output_file])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            error_count += 1
            continue
        if result.returncode == 0:
            rendered_count += 1
        else:
            error_count += 1

    return {"rendered_clip_count": rendered_count, "error_count": error_count}
