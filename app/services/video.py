import glob
import itertools
import io
import math
import os
import random
import gc
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from functools import lru_cache
from typing import List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ColorClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFont

from app.config import config
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services.utils import video_effects
from app.utils import file_security, openmontage_materials, utils, video_quality

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
        crop_x_ratio=None,
        crop_y_ratio=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        self.crop_x_ratio = crop_x_ratio
        self.crop_y_ratio = crop_y_ratio
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
_DEFAULT_AUDIO_BITRATE_KBPS = 192
_MIN_AUDIO_BITRATE_KBPS = 32
_MAX_AUDIO_BITRATE_KBPS = 512
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = f"{_DEFAULT_AUDIO_BITRATE_KBPS}k"
_DEFAULT_VIDEO_FPS = 30
_MAX_VIDEO_FPS = 120
_VIDEO_KEYFRAME_INTERVAL_SECONDS = 2
_DEFAULT_CROSSFADE_DURATION = 0.35
_DEFAULT_SUBTITLE_BOTTOM_MARGIN_RATIO = 0.05
_PORTRAIT_SUBTITLE_BOTTOM_SAFE_MARGIN_RATIO = 0.16
_CUE_CUT_ALIGNMENT_TOLERANCE_SECONDS = 0.75
_CUE_CUT_DYNAMIC_MIN_DURATION_SECONDS = 1.5
_CUE_CUT_DYNAMIC_MIN_DURATION_RATIO = 0.5
_SCENE_CUT_ALIGNMENT_TOLERANCE_SECONDS = 0.75
_LEADING_DETECTED_SEGMENT_START_TOLERANCE_SECONDS = 0.05
_MAX_LEADING_DETECTED_SEGMENT_TRIM_SECONDS = 0.75
_NEAR_BLACK_SEGMENT_LUMA_THRESHOLD = 25.5
_NEAR_BLACK_SEGMENT_DARK_PIXEL_RATIO = 0.98
_NEAR_BLACK_SEGMENT_SAMPLE_INTERVAL_SECONDS = 0.25
_MIN_SUSTAINED_NEAR_BLACK_SECONDS = 0.5
_COLOR_LEVELING_MIN_LUMA_DIFFERENCE = 24.0
_COLOR_LEVELING_SAFE_LUMA_RANGE = (48.0, 208.0)
_MAX_COLOR_LEVELING_BRIGHTNESS_ADJUSTMENT = 0.04
_COLOR_LEVELING_MIN_SATURATION_DIFFERENCE = 0.12
_COLOR_LEVELING_SAFE_SATURATION_RANGE = (0.08, 0.80)
_MIN_COLOR_LEVELING_SATURATION_MULTIPLIER = 0.85
_MAX_COLOR_LEVELING_SATURATION_MULTIPLIER = 1.15
_COLOR_LEVELING_MIN_WARMTH_DIFFERENCE = 0.10
_COLOR_LEVELING_SAFE_WARMTH_RANGE = (-0.30, 0.30)
_MAX_COLOR_LEVELING_WARMTH_ADJUSTMENT = 0.06
_COLOR_LEVELING_FRAME_SAMPLE_STRIDE = 16
_PORTRAIT_FOCAL_CROP_MAX_TARGET_RATIO = 0.65
_VERTICAL_FOCAL_CROP_MIN_TARGET_RATIO = 0.8
_FOCAL_POINT_SAMPLE_RATIOS = (0.2, 0.5, 0.8)
_FOCAL_POINT_MAX_SAMPLE_SPREAD_RATIO = 0.35
_FOCAL_POINT_MIN_OFFSET_RATIO = 0.1
_FOCAL_POINT_MAX_FRAME_DIMENSION = 160
fps = _DEFAULT_VIDEO_FPS
# FFmpeg 按帧率拼接/转码时，最终时长可能比 MoviePy 读到的理论时长短几十毫秒。
# 这里给视频素材多留一个很小的安全余量，避免音频末尾因为帧舍入出现黑屏、
# 卡顿或最后一小段旁白没有画面的情况。
_VIDEO_DURATION_SAFETY_MARGIN = 0.1
_MIN_MATERIAL_DIMENSION = 480
_MIN_DIMENSION_TOLERANCE = 10
_DEFAULT_VIDEO_TRANSITION_DURATION = 1.0
_MAX_IMAGE_ZOOM_SCALE = 1.2
_IMAGE_ZOOM_RATE = 0.03
_HIGH_QUALITY_SCALE_FLAGS = "lanczos+accurate_rnd+full_chroma_int"
_BT709_LIMITED_RANGE_SCALE_OPTIONS = ":out_color_matrix=bt709:out_range=tv"
_CONSERVATIVE_DEBAND_FILTER = (
    "deband=1thr=0.012:2thr=0.012:3thr=0.012:range=8:blur=1"
)
_BGM_EXTENSIONS = (".mp3",)
_DEFAULT_VIDEO_CODEC = "libx264"
_DEFAULT_LIBX264_CRF = "20"
_DEFAULT_LIBX264_PRESET = "medium"
_MOVIEPY_AMF_PRESET = "quality"
_AMF_CQP_QP_OFFSET = 8
_MP4_PIXEL_FORMAT_FFMPEG_PARAMS = ("-pix_fmt", "yuv420p")
_MP4_BT709_COLOR_FFMPEG_PARAMS = (
    "-colorspace",
    "bt709",
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-color_range",
    "tv",
)
_H264_BT709_VUI_BSF = (
    "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1"
)
_MP4_FASTSTART_FFMPEG_PARAMS = ("-movflags", "+faststart")
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_SUPPORTED_LIBX264_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
_runtime_disabled_video_codecs = set()
_VIDEO_QUALITY_CONFIG_KEYS = frozenset(
    (
        "video_codec",
        "video_crf",
        "video_encoder_preset",
        "video_fps",
        "audio_bitrate",
        "video_deband_enabled",
    )
)
_video_quality_config: ContextVar[dict[str, object] | None] = ContextVar(
    "video_quality_config",
    default=None,
)


def _clean_video_quality_config(
    overrides: Mapping[str, object] | None,
) -> dict[str, object]:
    if not overrides:
        return {}
    return {
        key: value
        for key, value in dict(overrides).items()
        if key in _VIDEO_QUALITY_CONFIG_KEYS and value is not None
    }


@contextmanager
def video_quality_config(overrides: Mapping[str, object] | None):
    current_config = _video_quality_config.get() or {}
    merged_config = {
        **current_config,
        **_clean_video_quality_config(overrides),
    }
    token = _video_quality_config.set(merged_config)
    try:
        yield
    finally:
        _video_quality_config.reset(token)


def _get_video_quality_config_value(key: str, default):
    current_config = _video_quality_config.get()
    if current_config and key in current_config:
        return current_config[key]
    return config.app.get(key, default)


def _is_configured_video_deband_enabled() -> bool:
    value = _get_video_quality_config_value("video_deband_enabled", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _optional_deband_filter() -> str:
    """Return a conservative, opt-in cleanup stage for compressed B-roll."""
    return f",{_CONSERVATIVE_DEBAND_FILTER}" if _is_configured_video_deband_enabled() else ""


def _source_file_key(file_path: str) -> str:
    value = str(file_path or "").replace("\\", "/")
    return os.path.normcase(os.path.normpath(value))


def _get_required_video_duration(audio_duration: float) -> float:
    """
    返回视频素材拼接的目标时长。

    使用场景：合成视频时需要素材时长覆盖旁白音频。只做到“刚好等于”
    音频时长时，FFmpeg 可能因为帧率舍入让最终视频略短，因此统一加一个
    轻量余量。函数独立出来，便于测试和后续按实际反馈调整余量大小。
    """
    return max(0.0, float(audio_duration) + _VIDEO_DURATION_SAFETY_MARGIN)


def is_material_resolution_acceptable(width: int, height: int) -> bool:
    """Allow tiny encoder rounding while rejecting genuinely low resolution."""
    min_dimension = _MIN_MATERIAL_DIMENSION - _MIN_DIMENSION_TOLERANCE
    return width >= min_dimension and height >= min_dimension


def _frame_focal_ratio(frame, *, axis: int) -> float | None:
    """Return a conservative focal point from visible frame detail on one axis."""
    if axis not in (0, 1):
        return None
    try:
        frame_array = np.asarray(frame)
        if frame_array.ndim < 2 or min(frame_array.shape[:2]) < 2:
            return None
        if frame_array.ndim == 2:
            luma = frame_array.astype(np.float32, copy=False)
        else:
            rgb = frame_array[..., :3].astype(np.float32, copy=False)
            if rgb.shape[-1] < 3:
                return None
            luma = (
                rgb[..., 0] * 0.299
                + rgb[..., 1] * 0.587
                + rgb[..., 2] * 0.114
            )
        if not np.isfinite(luma).all():
            return None

        largest_dimension = max(luma.shape)
        sample_stride = max(
            1,
            math.ceil(largest_dimension / _FOCAL_POINT_MAX_FRAME_DIMENSION),
        )
        luma = luma[::sample_stride, ::sample_stride]
        if min(luma.shape) < 2:
            return None

        contrast = np.abs(luma - np.median(luma))
        horizontal_detail = np.abs(np.diff(luma, axis=1, prepend=luma[:, :1]))
        vertical_detail = np.abs(np.diff(luma, axis=0, prepend=luma[:1, :]))
        saliency = contrast * 0.25 + horizontal_detail + vertical_detail
        threshold = float(np.percentile(saliency, 80))
        weights = np.maximum(saliency - threshold, 0.0)
        total_weight = float(weights.sum())
        if total_weight <= 0 or not math.isfinite(total_weight):
            return None

        axis_weights = weights.sum(axis=1 - axis)
        axis_positions = np.arange(axis_weights.size, dtype=np.float32)
        focal_point = float((axis_weights * axis_positions).sum() / total_weight)
        focal_ratio = focal_point / max(1, axis_weights.size - 1)
        if abs(focal_ratio - 0.5) < _FOCAL_POINT_MIN_OFFSET_RATIO:
            return None
        return min(1.0, max(0.0, focal_ratio))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _frame_focal_x_ratio(frame) -> float | None:
    """Return a conservative horizontal focal point from visible frame detail."""
    return _frame_focal_ratio(frame, axis=1)


def _frame_focal_y_ratio(frame) -> float | None:
    """Return a conservative vertical focal point from visible frame detail."""
    return _frame_focal_ratio(frame, axis=0)


def _image_focal_ratios(image) -> tuple[float | None, float | None]:
    """Return conservative focal points for a still image without changing it."""
    try:
        frame = np.asarray(image)
    except (TypeError, ValueError, MemoryError):
        return None, None
    return _frame_focal_x_ratio(frame), _frame_focal_y_ratio(frame)


def _clip_focal_ratio(
    clip,
    frame_focal_ratio,
    *,
    start_time: float = 0.0,
    end_time: float | None = None,
) -> float | None:
    """Return a stable focal point for an optional interval within a clip."""
    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return None
    try:
        duration = float(getattr(clip, "duration", 0) or 0)
        if not math.isfinite(duration) or duration <= 0:
            return None
        segment_start_time = min(duration, max(0.0, float(start_time)))
        segment_end_time = duration if end_time is None else float(end_time)
        segment_end_time = min(
            duration,
            max(segment_start_time, segment_end_time),
        )
    except Exception:
        return None

    segment_duration = segment_end_time - segment_start_time
    if segment_duration <= 0:
        return None

    focal_points = []
    for sample_ratio in _FOCAL_POINT_SAMPLE_RATIOS:
        try:
            focal_ratio = frame_focal_ratio(
                get_frame(segment_start_time + segment_duration * sample_ratio)
            )
        except Exception:
            continue
        if focal_ratio is not None:
            focal_points.append(focal_ratio)

    if not focal_points:
        return None
    if (
        len(focal_points) > 1
        and max(focal_points) - min(focal_points)
        > _FOCAL_POINT_MAX_SAMPLE_SPREAD_RATIO
    ):
        return None
    return float(np.median(focal_points))


def _clip_focal_x_ratio(
    clip,
    *,
    start_time: float = 0.0,
    end_time: float | None = None,
) -> float | None:
    return _clip_focal_ratio(
        clip,
        _frame_focal_x_ratio,
        start_time=start_time,
        end_time=end_time,
    )


def _clip_focal_y_ratio(
    clip,
    *,
    start_time: float = 0.0,
    end_time: float | None = None,
) -> float | None:
    return _clip_focal_ratio(
        clip,
        _frame_focal_y_ratio,
        start_time=start_time,
        end_time=end_time,
    )


def _bounded_crop_ratio(value) -> float | None:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio):
        return None
    return min(1.0, max(0.0, ratio))


def _focal_crop_offset_ratios(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    focal_x_ratio: float | None = None,
    focal_y_ratio: float | None = None,
) -> tuple[float | None, float | None]:
    """Return crop origins as normalized overflow ratios for a target frame."""
    try:
        source_width = float(source_width)
        source_height = float(source_height)
        target_width = float(target_width)
        target_height = float(target_height)
    except (TypeError, ValueError):
        return None, None
    dimensions = (source_width, source_height, target_width, target_height)
    if not all(math.isfinite(value) and value > 0 for value in dimensions):
        return None, None

    target_ratio = target_width / target_height
    scale_factor = max(target_width / source_width, target_height / source_height)
    scaled_width = _ceil_even_dimension(source_width * scale_factor, int(target_width))
    scaled_height = _ceil_even_dimension(source_height * scale_factor, int(target_height))

    crop_x_ratio = None
    horizontal_overflow = scaled_width - target_width
    normalized_focal_x = _bounded_crop_ratio(focal_x_ratio)
    if (
        normalized_focal_x is not None
        and target_ratio <= _PORTRAIT_FOCAL_CROP_MAX_TARGET_RATIO
        and horizontal_overflow > 0
    ):
        crop_origin = min(
            horizontal_overflow,
            max(0.0, normalized_focal_x * scaled_width - target_width / 2),
        )
        crop_x_ratio = crop_origin / horizontal_overflow

    crop_y_ratio = None
    vertical_overflow = scaled_height - target_height
    normalized_focal_y = _bounded_crop_ratio(focal_y_ratio)
    if (
        normalized_focal_y is not None
        and target_ratio >= _VERTICAL_FOCAL_CROP_MIN_TARGET_RATIO
        and vertical_overflow > 0
    ):
        crop_origin = min(
            vertical_overflow,
            max(0.0, normalized_focal_y * scaled_height - target_height / 2),
        )
        crop_y_ratio = crop_origin / vertical_overflow

    return crop_x_ratio, crop_y_ratio


def _clip_focal_crop_offset_ratios(
    clip,
    target_width: int,
    target_height: int,
    *,
    start_time: float = 0.0,
    end_time: float | None = None,
) -> tuple[float | None, float | None]:
    """Sample a stable focal point only when the target will crop that axis."""
    try:
        clip_width, clip_height = clip.size
        clip_ratio = clip_width / clip_height
        target_ratio = target_width / target_height
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None, None

    focal_x_ratio = (
        _clip_focal_x_ratio(
            clip,
            start_time=start_time,
            end_time=end_time,
        )
        if (
            target_ratio <= _PORTRAIT_FOCAL_CROP_MAX_TARGET_RATIO
            and clip_ratio > target_ratio
        )
        else None
    )
    focal_y_ratio = (
        _clip_focal_y_ratio(
            clip,
            start_time=start_time,
            end_time=end_time,
        )
        if (
            target_ratio >= _VERTICAL_FOCAL_CROP_MIN_TARGET_RATIO
            and clip_ratio < target_ratio
        )
        else None
    )
    return _focal_crop_offset_ratios(
        clip_width,
        clip_height,
        target_width,
        target_height,
        focal_x_ratio=focal_x_ratio,
        focal_y_ratio=focal_y_ratio,
    )


def _fit_clip_to_target_frame(clip, target_width: int, target_height: int):
    clip_w, clip_h = clip.size
    if clip_w == target_width and clip_h == target_height:
        return clip

    clip_ratio = clip_w / clip_h
    target_ratio = target_width / target_height
    logger.debug(
        f"resizing clip, source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, "
        f"target: {target_width}x{target_height}, ratio: {target_ratio:.2f}"
    )

    if abs(clip_ratio - target_ratio) < 0.01:
        return clip.resized(new_size=(target_width, target_height))

    scale_factor = max(target_width / clip_w, target_height / clip_h)
    new_width = _ceil_even_dimension(clip_w * scale_factor, target_width)
    new_height = _ceil_even_dimension(clip_h * scale_factor, target_height)
    resized_clip = clip.resized(new_size=(new_width, new_height))
    crop_x_ratio, crop_y_ratio = _clip_focal_crop_offset_ratios(
        clip,
        target_width,
        target_height,
    )
    x_center = new_width / 2
    if crop_x_ratio is not None:
        x_center = target_width / 2 + (new_width - target_width) * crop_x_ratio
    y_center = new_height / 2
    if crop_y_ratio is not None:
        y_center = target_height / 2 + (new_height - target_height) * crop_y_ratio
    return resized_clip.cropped(
        x_center=x_center,
        y_center=y_center,
        width=target_width,
        height=target_height,
    )


def _is_frame_mostly_near_black(frame) -> bool | None:
    try:
        pixels = np.asarray(frame)
        if not pixels.size:
            return None
        if pixels.ndim >= 3 and pixels.shape[-1] >= 3:
            rgb = pixels[..., :3].astype(float)
            luma = (
                (rgb[..., 0] * 0.2126)
                + (rgb[..., 1] * 0.7152)
                + (rgb[..., 2] * 0.0722)
            )
        elif pixels.ndim == 2:
            luma = pixels.astype(float)
        else:
            return None
        if not np.isfinite(luma).all():
            return None
        if float(luma.max()) <= 1.0:
            luma *= 255.0
    except (TypeError, ValueError):
        return None

    return bool(
        np.mean(luma <= _NEAR_BLACK_SEGMENT_LUMA_THRESHOLD)
        >= _NEAR_BLACK_SEGMENT_DARK_PIXEL_RATIO
    )


def _is_subclip_near_black(clip, start_time: float, end_time: float) -> bool:
    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return False

    try:
        start_time = float(start_time)
        segment_duration = float(end_time) - start_time
    except (TypeError, ValueError):
        return False
    if segment_duration <= 0:
        return False

    sample_count = max(
        1,
        int(
            math.ceil(
                segment_duration / _NEAR_BLACK_SEGMENT_SAMPLE_INTERVAL_SECONDS
            )
        ),
    )
    sample_interval = segment_duration / sample_count
    consecutive_near_black_seconds = 0.0
    all_samples_near_black = True
    for sample_index in range(sample_count):
        try:
            sample_time = start_time + sample_interval * (sample_index + 0.5)
            is_near_black = _is_frame_mostly_near_black(get_frame(sample_time))
        except Exception:
            return False
        if is_near_black is None:
            return False
        if is_near_black:
            consecutive_near_black_seconds += sample_interval
            if consecutive_near_black_seconds >= _MIN_SUSTAINED_NEAR_BLACK_SECONDS:
                return True
        else:
            all_samples_near_black = False
            consecutive_near_black_seconds = 0.0

    return all_samples_near_black


def _filter_near_black_subclips(
    clip,
    subclipped_items: List[SubClippedVideoClip],
    *,
    preserve_when_empty: bool = True,
):
    usable_items = [
        item
        for item in subclipped_items
        if not _is_subclip_near_black(clip, item.start_time, item.end_time)
    ]
    return usable_items or (subclipped_items if preserve_when_empty else [])


def _trim_short_leading_detected_segment(
    item: SubClippedVideoClip,
    normalized_segments: list[tuple[float, float]],
) -> SubClippedVideoClip:
    """Trim a brief detected defect at a clip's start without changing its crop."""
    item_start = float(item.start_time)
    item_end = float(item.end_time)
    leading_segment_ends = [
        min(item_end, segment_end)
        for segment_start, segment_end in normalized_segments
        if (
            segment_start
            <= item_start + _LEADING_DETECTED_SEGMENT_START_TOLERANCE_SECONDS
            and segment_end > item_start
        )
    ]
    if not leading_segment_ends:
        return item

    trimmed_start = max(leading_segment_ends)
    original_duration = item_end - item_start
    remaining_duration = item_end - trimmed_start
    minimum_remaining_duration = min(
        original_duration,
        _CUE_CUT_DYNAMIC_MIN_DURATION_SECONDS,
    )
    if (
        trimmed_start - item_start > _MAX_LEADING_DETECTED_SEGMENT_TRIM_SECONDS
        or remaining_duration < minimum_remaining_duration
    ):
        return item

    return SubClippedVideoClip(
        file_path=item.file_path,
        start_time=trimmed_start,
        end_time=item_end,
        width=item.width,
        height=item.height,
        duration=remaining_duration,
        source_file_path=item.source_file_path,
        crop_x_ratio=item.crop_x_ratio,
        crop_y_ratio=item.crop_y_ratio,
    )


def _filter_subclips_with_detected_segments(
    subclipped_items: List[SubClippedVideoClip],
    detected_segments: list[tuple[float, float]],
    *,
    preserve_when_empty: bool = True,
    trim_leading_prefix: bool = True,
):
    normalized_segments = []
    for segment_start, segment_end in detected_segments:
        try:
            segment_start = float(segment_start)
            segment_end = float(segment_end)
        except (TypeError, ValueError):
            continue
        if math.isfinite(segment_start) and math.isfinite(segment_end):
            if segment_end > segment_start:
                normalized_segments.append((segment_start, segment_end))
    if not normalized_segments:
        return subclipped_items

    usable_items = []
    for item in subclipped_items:
        try:
            item_start = float(item.start_time)
            item_end = float(item.end_time)
        except (AttributeError, TypeError, ValueError):
            usable_items.append(item)
            continue
        if item_end <= item_start:
            usable_items.append(item)
            continue
        if trim_leading_prefix:
            item = _trim_short_leading_detected_segment(item, normalized_segments)
        item_start = float(item.start_time)
        item_end = float(item.end_time)
        overlaps_detected_segment = any(
            max(item_start, segment_start) < min(item_end, segment_end)
            for segment_start, segment_end in normalized_segments
        )
        if not overlaps_detected_segment:
            usable_items.append(item)
    return usable_items or (subclipped_items if preserve_when_empty else [])


def _filter_subclips_with_detected_black_segments(
    subclipped_items: List[SubClippedVideoClip],
    detected_black_segments: list[tuple[float, float]],
    *,
    preserve_when_empty: bool = True,
    trim_leading_prefix: bool = True,
):
    return _filter_subclips_with_detected_segments(
        subclipped_items,
        detected_black_segments,
        preserve_when_empty=preserve_when_empty,
        trim_leading_prefix=trim_leading_prefix,
    )


def _subclip_identity(item: SubClippedVideoClip) -> tuple[str, float, float] | None:
    try:
        return item.file_path, float(item.start_time), float(item.end_time)
    except (AttributeError, TypeError, ValueError):
        return None


def _sample_subclip_luma(
    clip, start_time: float, end_time: float
) -> float | None:
    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return None

    try:
        start_time = float(start_time)
        segment_duration = float(end_time) - start_time
    except (TypeError, ValueError):
        return None
    if segment_duration <= 0:
        return None

    try:
        frame = np.asarray(get_frame(start_time + segment_duration / 2))
        if not frame.size:
            return None
        if frame.ndim >= 2:
            frame = frame[
                ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
                ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
            ]
        if frame.ndim >= 3 and frame.shape[-1] >= 3:
            luma = float(
                np.mean(
                    frame[..., 0] * 0.2126
                    + frame[..., 1] * 0.7152
                    + frame[..., 2] * 0.0722
                )
            )
        else:
            luma = float(frame.mean())
        if np.issubdtype(frame.dtype, np.floating) and luma <= 1.0:
            luma *= 255.0
    except Exception:
        return None

    return luma if math.isfinite(luma) else None


def _sample_subclip_saturation(
    clip, start_time: float, end_time: float
) -> float | None:
    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return None

    try:
        start_time = float(start_time)
        segment_duration = float(end_time) - start_time
    except (TypeError, ValueError):
        return None
    if segment_duration <= 0:
        return None

    try:
        frame = np.asarray(get_frame(start_time + segment_duration / 2))
        if not frame.size or frame.ndim < 3 or frame.shape[-1] < 3:
            return None
        frame = frame[
            ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
            ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
            :3,
        ].astype(float)
        if not frame.size or not np.isfinite(frame).all():
            return None
        max_channel = frame.max(axis=-1)
        min_channel = frame.min(axis=-1)
        saturation = np.divide(
            max_channel - min_channel,
            max_channel,
            out=np.zeros_like(max_channel, dtype=float),
            where=max_channel > 0,
        )
        sampled_saturation = float(np.mean(saturation))
    except Exception:
        return None

    return sampled_saturation if math.isfinite(sampled_saturation) else None


def _sample_subclip_warmth(
    clip, start_time: float, end_time: float
) -> float | None:
    """Return a compact warm/cool measurement from a representative RGB frame."""
    get_frame = getattr(clip, "get_frame", None)
    if not callable(get_frame):
        return None

    try:
        start_time = float(start_time)
        segment_duration = float(end_time) - start_time
    except (TypeError, ValueError):
        return None
    if segment_duration <= 0:
        return None

    try:
        frame = np.asarray(get_frame(start_time + segment_duration / 2))
        if not frame.size or frame.ndim < 3 or frame.shape[-1] < 3:
            return None
        frame = frame[
            ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
            ::_COLOR_LEVELING_FRAME_SAMPLE_STRIDE,
            :3,
        ].astype(float)
        if not frame.size or not np.isfinite(frame).all():
            return None
        red, _green, blue = (float(value) for value in frame.mean(axis=(0, 1)))
        warmth = (red - blue) / max(red + blue, 1e-6)
    except Exception:
        return None

    return warmth if math.isfinite(warmth) else None


def _brightness_adjustment_for_luma(source_luma: float, target_luma: float) -> float:
    try:
        source_luma = float(source_luma)
        target_luma = float(target_luma)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(source_luma) or not math.isfinite(target_luma):
        return 0.0

    safe_min_luma, safe_max_luma = _COLOR_LEVELING_SAFE_LUMA_RANGE
    if not (
        safe_min_luma <= source_luma <= safe_max_luma
        and safe_min_luma <= target_luma <= safe_max_luma
    ):
        return 0.0

    luma_difference = target_luma - source_luma
    if abs(luma_difference) < _COLOR_LEVELING_MIN_LUMA_DIFFERENCE:
        return 0.0
    return max(
        -_MAX_COLOR_LEVELING_BRIGHTNESS_ADJUSTMENT,
        min(
            _MAX_COLOR_LEVELING_BRIGHTNESS_ADJUSTMENT,
            luma_difference / 255.0,
        ),
    )


def _saturation_multiplier_for_levels(
    source_saturation: float, target_saturation: float
) -> float:
    try:
        source_saturation = float(source_saturation)
        target_saturation = float(target_saturation)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(source_saturation) or not math.isfinite(target_saturation):
        return 1.0

    safe_min, safe_max = _COLOR_LEVELING_SAFE_SATURATION_RANGE
    if not (
        safe_min <= source_saturation <= safe_max
        and safe_min <= target_saturation <= safe_max
    ):
        return 1.0
    if (
        abs(target_saturation - source_saturation)
        < _COLOR_LEVELING_MIN_SATURATION_DIFFERENCE
    ):
        return 1.0

    return max(
        _MIN_COLOR_LEVELING_SATURATION_MULTIPLIER,
        min(
            _MAX_COLOR_LEVELING_SATURATION_MULTIPLIER,
            target_saturation / source_saturation,
        ),
    )


def _warmth_adjustment_for_levels(
    source_warmth: float, target_warmth: float
) -> float:
    """Return a bounded midtone red/blue adjustment for a clear color cast."""
    try:
        source_warmth = float(source_warmth)
        target_warmth = float(target_warmth)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(source_warmth) or not math.isfinite(target_warmth):
        return 0.0

    safe_min, safe_max = _COLOR_LEVELING_SAFE_WARMTH_RANGE
    if not (
        safe_min <= source_warmth <= safe_max
        and safe_min <= target_warmth <= safe_max
    ):
        return 0.0
    warmth_difference = target_warmth - source_warmth
    if abs(warmth_difference) < _COLOR_LEVELING_MIN_WARMTH_DIFFERENCE:
        return 0.0
    return max(
        -_MAX_COLOR_LEVELING_WARMTH_ADJUSTMENT,
        min(_MAX_COLOR_LEVELING_WARMTH_ADJUSTMENT, warmth_difference),
    )


def _subclip_brightness_adjustments(
    subclipped_items: List[SubClippedVideoClip],
) -> dict[tuple[str, float, float], float]:
    """Return small source-level brightness adjustments when samples agree."""
    if len(subclipped_items) < 2:
        return {}

    items_by_file_path = {}
    for item in subclipped_items:
        item_key = _subclip_identity(item)
        if item_key is None:
            continue
        items_by_file_path.setdefault(item.file_path, []).append((item, item_key))

    sampled_lumas_by_file_path = {}
    sampled_item_keys_by_file_path = {}
    for file_path, items in items_by_file_path.items():
        clip = None
        try:
            clip = _open_video_clip_quietly(file_path)
            for item, item_key in items:
                luma = _sample_subclip_luma(
                    clip,
                    start_time=item.start_time,
                    end_time=item.end_time,
                )
                if luma is not None:
                    sampled_lumas_by_file_path.setdefault(file_path, []).append(luma)
                    sampled_item_keys_by_file_path.setdefault(file_path, []).append(
                        item_key
                    )
        except Exception as exc:
            logger.debug(
                f"skipping brightness leveling sample for {os.path.basename(file_path)}: {str(exc)}"
            )
        finally:
            close_clip(clip)

    source_lumas = {
        file_path: float(np.median(lumas))
        for file_path, lumas in sampled_lumas_by_file_path.items()
        if lumas
    }
    if len(source_lumas) < 2:
        return {}

    target_luma = float(np.median(list(source_lumas.values())))
    if not math.isfinite(target_luma):
        return {}

    adjustments = {}
    for file_path, source_luma in source_lumas.items():
        adjustment = _brightness_adjustment_for_luma(source_luma, target_luma)
        if adjustment:
            for item_key in sampled_item_keys_by_file_path[file_path]:
                adjustments[item_key] = adjustment
    return adjustments


def _subclip_saturation_adjustments(
    subclipped_items: List[SubClippedVideoClip],
) -> dict[tuple[str, float, float], float]:
    """Return bounded saturation multipliers to make adjacent sources less jarring."""
    if len(subclipped_items) < 2:
        return {}

    items_by_file_path = {}
    for item in subclipped_items:
        item_key = _subclip_identity(item)
        if item_key is None:
            continue
        items_by_file_path.setdefault(item.file_path, []).append((item, item_key))

    source_saturations = {}
    sampled_item_keys = {}
    for file_path, items in items_by_file_path.items():
        clip = None
        try:
            clip = _open_video_clip_quietly(file_path)
            sampled_saturations = []
            for item, item_key in items:
                saturation = _sample_subclip_saturation(
                    clip,
                    start_time=item.start_time,
                    end_time=item.end_time,
                )
                if saturation is not None:
                    sampled_saturations.append(saturation)
                    sampled_item_keys.setdefault(file_path, []).append(item_key)
            if sampled_saturations:
                source_saturations[file_path] = float(np.median(sampled_saturations))
        except Exception as exc:
            logger.debug(
                f"skipping saturation leveling sample for {os.path.basename(file_path)}: {str(exc)}"
            )
        finally:
            close_clip(clip)

    if len(source_saturations) < 2:
        return {}
    target_saturation = float(np.median(list(source_saturations.values())))
    if not math.isfinite(target_saturation):
        return {}

    adjustments = {}
    for file_path, source_saturation in source_saturations.items():
        multiplier = _saturation_multiplier_for_levels(
            source_saturation,
            target_saturation,
        )
        if multiplier != 1.0:
            for item_key in sampled_item_keys[file_path]:
                adjustments[item_key] = multiplier
    return adjustments


def _subclip_warmth_adjustments(
    subclipped_items: List[SubClippedVideoClip],
) -> dict[tuple[str, float, float], float]:
    """Return subtle warm/cool adjustments only when sources clearly disagree."""
    if len(subclipped_items) < 2:
        return {}

    items_by_file_path = {}
    for item in subclipped_items:
        item_key = _subclip_identity(item)
        if item_key is None:
            continue
        items_by_file_path.setdefault(item.file_path, []).append((item, item_key))

    source_warmths = {}
    sampled_item_keys = {}
    for file_path, items in items_by_file_path.items():
        clip = None
        try:
            clip = _open_video_clip_quietly(file_path)
            sampled_warmths = []
            for item, item_key in items:
                warmth = _sample_subclip_warmth(
                    clip,
                    start_time=item.start_time,
                    end_time=item.end_time,
                )
                if warmth is not None:
                    sampled_warmths.append(warmth)
                    sampled_item_keys.setdefault(file_path, []).append(item_key)
            if sampled_warmths:
                source_warmths[file_path] = float(np.median(sampled_warmths))
        except Exception as exc:
            logger.debug(
                f"skipping warmth leveling sample for {os.path.basename(file_path)}: {str(exc)}"
            )
        finally:
            close_clip(clip)

    if len(source_warmths) < 2:
        return {}
    target_warmth = float(np.median(list(source_warmths.values())))
    if not math.isfinite(target_warmth):
        return {}

    adjustments = {}
    for file_path, source_warmth in source_warmths.items():
        adjustment = _warmth_adjustment_for_levels(source_warmth, target_warmth)
        if adjustment:
            for item_key in sampled_item_keys[file_path]:
                adjustments[item_key] = adjustment
    return adjustments


def _get_effective_transition_duration(clip_duration: float) -> float:
    safe_duration = max(0.0, float(clip_duration or 0))
    return min(_DEFAULT_VIDEO_TRANSITION_DURATION, safe_duration / 2)


def _get_effective_crossfade_duration(
    previous_duration: float, next_duration: float
) -> float:
    """Return a safe overlap that fits entirely inside both neighboring clips."""
    try:
        previous_duration = float(previous_duration)
        next_duration = float(next_duration)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(previous_duration) or not math.isfinite(next_duration):
        return 0.0
    return min(
        _DEFAULT_CROSSFADE_DURATION,
        max(0.0, previous_duration) / 2,
        max(0.0, next_duration) / 2,
    )


def _crossfade_timeline_duration(clip_durations: List[float]) -> float:
    """Return the final duration after neighboring clips overlap by crossfades."""
    if not clip_durations:
        return 0.0

    timeline_duration = 0.0
    previous_duration = None
    for raw_duration in clip_durations:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(duration) or duration <= 0:
            continue
        if previous_duration is None:
            timeline_duration = duration
        else:
            timeline_duration += duration - _get_effective_crossfade_duration(
                previous_duration,
                duration,
            )
        previous_duration = duration
    return timeline_duration


def required_unique_material_count(
    audio_duration: float,
    max_clip_duration: int,
    video_transition_mode=None,
) -> int:
    """Return the minimum distinct source clips needed for the selected transition."""
    try:
        duration = max(0.0, float(audio_duration or 0))
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration):
        duration = 0.0
    try:
        clip_duration = max(1.0, float(max_clip_duration or 1))
    except (TypeError, ValueError):
        clip_duration = 1.0
    if not math.isfinite(clip_duration):
        clip_duration = 1.0

    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    if transition_value != VideoTransitionMode.crossfade.value:
        return max(1, math.ceil(duration / clip_duration))

    required_duration = _get_required_video_duration(duration)
    if required_duration <= clip_duration:
        return 1
    overlap = _get_effective_crossfade_duration(clip_duration, clip_duration)
    additional_duration = clip_duration - overlap
    if additional_duration <= 0:
        return max(1, math.ceil(required_duration / clip_duration))
    return 1 + math.ceil((required_duration - clip_duration) / additional_duration)


def _image_zoom_scale(current_time: float, clip_duration: float) -> float:
    safe_time = max(0.0, float(current_time or 0))
    safe_duration = max(0.001, float(clip_duration or 0))
    linear_scale = 1 + (safe_duration * _IMAGE_ZOOM_RATE) * (
        safe_time / safe_duration
    )
    return min(_MAX_IMAGE_ZOOM_SCALE, linear_scale)


def _ceil_even_dimension(value: float, minimum: int) -> int:
    dimension = max(math.ceil(value), int(minimum))
    if dimension % 2:
        dimension += 1
    return max(2, dimension)


def _even_video_size(size) -> tuple[int, int]:
    width, height = int(size[0]), int(size[1])
    return max(2, width - width % 2), max(2, height - height % 2)


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
) -> List[SubClippedVideoClip]:
    """
    优先让每个源素材只出现一次，降低成片里同一素材反复出现的概率。

    线上素材经常会遇到“一个长视频被切成多个短片段”的情况。旧逻辑在
    random 模式下直接打乱所有短片段，导致同一个源视频的多个切片可能
    分布在开头和中间，用户会感知为素材重复。本函数只调整片段顺序：
    先放每个源文件里最长的一个片段，剩余片段作为兜底；当素材总时长不足时，
    仍然允许后续片段补齐音频长度，避免破坏视频生成成功率。优先选择最长
    片段是为了避免随机选中视频尾部的零碎短片段，导致明明有足够素材却过早复用。
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        return subclipped_items

    source_keys = {
        _source_file_key(item.source_file_path) for item in subclipped_items
    }
    if (
        len(source_keys) == 1
        and openmontage_materials.is_openmontage_output_path(
            subclipped_items[0].source_file_path
        )
    ):
        # A finished OpenMontage file is a single authored story, not a pool of
        # interchangeable B-roll. Keep its scenes in timeline order even when
        # the general video mode is random.
        ordered_items = sorted(
            subclipped_items,
            key=lambda item: (
                float(item.start_time or 0),
                float(item.end_time or 0),
            ),
        )
        logger.info(
            "preserving timeline order for {} OpenMontage clips".format(
                len(ordered_items)
            )
        )
        return ordered_items

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(_source_file_key(item.source_file_path), []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        longest_duration = max(item.duration for item in items)
        full_duration_items = [
            item for item in items if item.duration == longest_duration
        ]
        primary_item = random.choice(full_duration_items)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    兼容历史上直接从 video 服务读取 FFmpeg 路径的调用方。

    真正的解析逻辑已经抽到 `app.utils.utils.get_ffmpeg_binary()`，视频、语音
    和后续新增链路都应复用同一套优先级；这里保留薄包装，避免外部脚本或
    旧测试直接导入 `app.services.video.get_ffmpeg_binary` 时出现 AttributeError。
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    读取用户配置的视频编码器。

    该配置面向高级用户，用于尝试启用 NVENC/AMF/QSV/VideoToolbox 等硬件
    编码。这里刻意只允许固定白名单，避免开放任意 FFmpeg 参数后，用户填错
    参数导致输出格式不可控，甚至让生成任务在后续阶段才失败。
    """
    configured_codec = str(
        _get_video_quality_config_value("video_codec", _DEFAULT_VIDEO_CODEC)
        or _DEFAULT_VIDEO_CODEC
    ).strip().lower()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


def _get_configured_video_fps() -> int:
    raw_value = _get_video_quality_config_value("video_fps", _DEFAULT_VIDEO_FPS)
    if isinstance(raw_value, bool):
        return _DEFAULT_VIDEO_FPS
    if isinstance(raw_value, str):
        raw_value = raw_value.strip().lower()
        if raw_value.endswith("fps"):
            raw_value = raw_value[:-3].strip()
    try:
        configured_fps = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_VIDEO_FPS
    if 1 <= configured_fps <= _MAX_VIDEO_FPS:
        return configured_fps
    return _DEFAULT_VIDEO_FPS


def _get_configured_audio_bitrate() -> str:
    raw_value = _get_video_quality_config_value("audio_bitrate", audio_bitrate)
    if isinstance(raw_value, bool):
        return audio_bitrate

    value = str(raw_value).strip().lower()
    if value.endswith("kbps"):
        value = value[:-4].strip()
    elif value.endswith("k"):
        value = value[:-1]

    try:
        kbps = int(value)
    except (TypeError, ValueError):
        return audio_bitrate

    if _MIN_AUDIO_BITRATE_KBPS <= kbps <= _MAX_AUDIO_BITRATE_KBPS:
        return f"{kbps}k"
    return audio_bitrate


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    检查当前 FFmpeg 是否声明支持指定编码器。

    这只能证明 FFmpeg 编译时包含该 encoder，不能证明当前机器硬件和驱动
    一定可用。因此实际编码失败时仍会再回退到 libx264。
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    返回本次实际使用的视频编码器。

    用户选择硬件编码器时，先做 FFmpeg encoder 列表检测；如果本进程里已经
    实际编码失败过，也直接回退，避免一个任务里每个片段都重复失败。
    """
    if preferred_codec:
        selected_codec = str(preferred_codec).strip().lower()
        if selected_codec not in _SUPPORTED_VIDEO_CODECS:
            logger.warning(
                f"unsupported video codec requested: {selected_codec}, "
                f"fallback to {_DEFAULT_VIDEO_CODEC}"
            )
            return _DEFAULT_VIDEO_CODEC
    else:
        selected_codec = _get_configured_video_codec()

    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    codec = str(codec).strip().lower()
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


def _get_temp_audio_dir(output_dir: str) -> str:
    """
    Return the directory to use for MoviePy's temporary audio file.

    On Windows, Windows Defender can lock files written to the task output
    directory while scanning them, causing MoviePy to fail with a
    PermissionError (WinError 32) on the TEMP_MPY_wvf_snd temp file and
    leaving the final MP4 at 0 bytes.  Using the system temp directory
    sidesteps the scan without changing behaviour on other platforms.

    On Linux/macOS/Docker the output directory is returned unchanged so
    existing behaviour is preserved.
    """
    if sys.platform == "win32":
        return tempfile.gettempdir()
    return output_dir


def _ffmpeg_mp4_faststart_args(output_file: str) -> list[str]:
    if os.path.splitext(output_file)[1].lower() != ".mp4":
        return []
    return list(_MP4_FASTSTART_FFMPEG_PARAMS)


def _get_configured_libx264_crf() -> str:
    raw_value = _get_video_quality_config_value("video_crf", _DEFAULT_LIBX264_CRF)
    if isinstance(raw_value, bool):
        return _DEFAULT_LIBX264_CRF
    try:
        crf = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_LIBX264_CRF
    if 0 <= crf <= 51:
        return str(crf)
    return _DEFAULT_LIBX264_CRF


def _get_configured_libx264_preset() -> str:
    raw_value = _get_video_quality_config_value(
        "video_encoder_preset",
        _DEFAULT_LIBX264_PRESET,
    )
    if not isinstance(raw_value, str):
        return _DEFAULT_LIBX264_PRESET
    preset = raw_value.strip().lower()
    if preset in _SUPPORTED_LIBX264_PRESETS:
        return preset
    return _DEFAULT_LIBX264_PRESET


def _ffmpeg_quality_args(
    codec: str | None, *, bitrate=None, existing_params=None
) -> list[str]:
    codec = str(codec or "").strip().lower()
    params = list(existing_params or [])
    if codec == _DEFAULT_VIDEO_CODEC:
        quality_args = []
        if "-preset" not in params:
            quality_args.extend(["-preset", _get_configured_libx264_preset()])
        if not bitrate and "-crf" not in params:
            quality_args.extend(["-crf", _get_configured_libx264_crf()])
        return quality_args

    if codec == "h264_amf":
        explicit_rate_control = {
            "-b:v",
            "-rc",
            "-qp_i",
            "-qp_p",
            "-qp_b",
            "-qvbr_quality_level",
        }
        if bitrate or explicit_rate_control.intersection(params):
            return []

        # AMF QP shares CRF's 0-51 range but not its visual equivalence. A lower
        # QP keeps the default CRF 20 near the previous AMF quality baseline while
        # preserving the existing lower-is-higher-quality ordering.
        qp_i = max(0, int(_get_configured_libx264_crf()) - _AMF_CQP_QP_OFFSET)
        return [
            "-rc",
            "cqp",
            "-qp_i",
            str(qp_i),
            "-qp_p",
            str(min(51, qp_i + 2)),
        ]

    if codec == "h264_nvenc":
        explicit_rate_control = {
            "-b:v",
            "-rc",
            "-cq",
            "-qp",
            "-qmin",
            "-qmax",
            "-maxrate",
            "-bufsize",
        }
        if bitrate or explicit_rate_control.intersection(params):
            return []

        # NVENC's constant-quality VBR mode uses CQ rather than x264's CRF.
        # Both keep the same lower-is-higher-quality ordering, so the existing
        # quality setting remains meaningful when users switch hardware.
        quality = _get_configured_libx264_crf()
        return ["-rc", "vbr", "-cq", quality, "-b:v", "0"]

    if codec == "h264_qsv":
        explicit_rate_control = {
            "-b:v",
            "-global_quality",
            "-q:v",
            "-qscale:v",
            "-maxrate",
            "-bufsize",
        }
        quality_args = []
        if not bitrate and not explicit_rate_control.intersection(params):
            # QSV accepts the generic global-quality value as its quality scale.
            # Its ordering matches CRF: a lower value requests higher quality.
            quality_args.extend(["-global_quality", _get_configured_libx264_crf()])
        if "-look_ahead" not in params:
            quality_args.extend(["-look_ahead", "1"])
        return quality_args

    return []


def _ffmpeg_keyframe_args(*, fps=None, existing_params=None) -> list[str]:
    params = set(existing_params or [])
    if {"-g", "-g:v", "-keyint_min"}.intersection(params):
        return []

    if fps is None:
        fps = _get_configured_video_fps()
    if isinstance(fps, bool):
        return []
    try:
        frame_rate = float(fps)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        return []

    keyframe_interval = max(
        1,
        int(round(frame_rate * _VIDEO_KEYFRAME_INTERVAL_SECONDS)),
    )
    return ["-g", str(keyframe_interval)]


def _ffmpeg_bt709_color_metadata_args(existing_params=None) -> list[str]:
    params = set(existing_params or [])
    color_args = []
    for flag, value in zip(
        _MP4_BT709_COLOR_FFMPEG_PARAMS[::2],
        _MP4_BT709_COLOR_FFMPEG_PARAMS[1::2],
    ):
        if flag not in params and f"{flag}:v" not in params:
            color_args.extend([flag, value])
    return color_args


def _ffmpeg_bt709_h264_vui_args(existing_params=None) -> list[str]:
    params = set(existing_params or [])
    if {"-bsf:v", "-bsf"}.intersection(params):
        return []
    return ["-bsf:v", _H264_BT709_VUI_BSF]


def get_video_encoding_contract() -> dict[str, str | float]:
    """Return the technical MP4 encoding contract used by rendered videos."""
    return {
        "codec": "h264",
        "pixel_format": "yuv420p",
        "fps": float(_get_configured_video_fps()),
        "max_keyframe_gap_seconds": float(_VIDEO_KEYFRAME_INTERVAL_SECONDS),
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "color_range": "tv",
        "sample_aspect_ratio": "1:1",
    }


def _with_mp4_write_ffmpeg_params(
    output_file: str, kwargs: dict, codec: str | None = None
) -> dict:
    if os.path.splitext(output_file)[1].lower() != ".mp4":
        return kwargs

    existing_params = list(kwargs.get("ffmpeg_params") or [])
    if "-pix_fmt" not in existing_params:
        existing_params.extend(_MP4_PIXEL_FORMAT_FFMPEG_PARAMS)
    existing_params.extend(_ffmpeg_bt709_color_metadata_args(existing_params))
    existing_params.extend(_ffmpeg_bt709_h264_vui_args(existing_params))
    existing_params.extend(
        _ffmpeg_quality_args(
            codec,
            bitrate=kwargs.get("bitrate"),
            existing_params=existing_params,
        )
    )
    existing_params.extend(
        _ffmpeg_keyframe_args(
            fps=kwargs.get("fps"),
            existing_params=existing_params,
        )
    )
    if "-movflags" not in existing_params:
        existing_params.extend(_MP4_FASTSTART_FFMPEG_PARAMS)
    return {**kwargs, "ffmpeg_params": existing_params}


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    硬件编码失败后用 libx264 重试，只有重试成功才禁用该硬件编码器。

    Windows 上 FFmpeg 失败原因比较复杂：可能是显卡/驱动不支持，也可能是输出
    文件被占用、目录权限、杀软拦截等通用 IO 问题。只有 libx264 能成功写出时，
    才能判断原始失败大概率来自硬件编码器本身，避免误伤后续任务。
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    """
    使用指定编码器写出视频，失败时自动用 libx264 重试一次。

    硬件编码器是否可用不仅取决于 FFmpeg，还取决于显卡、驱动和当前运行环境。
    生成任务不能因为高级编码器不可用而整体失败，所以这里把回退集中处理。
    """
    effective_codec = _get_effective_video_codec(codec)
    write_kwargs = _with_mp4_write_ffmpeg_params(output_file, kwargs, effective_codec)
    # MoviePy defaults to `-preset medium`, which h264_amf does not accept.
    if effective_codec == "h264_amf":
        write_kwargs["preset"] = _MOVIEPY_AMF_PRESET
    try:
        clip.write_videofile(output_file, codec=effective_codec, **write_kwargs)
        return effective_codec
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        logger.warning(
            f"video write failed with codec {effective_codec}, trying fallback codec: {str(exc)}"
        )
        fallback_kwargs = _with_mp4_write_ffmpeg_params(
            output_file, kwargs, _DEFAULT_VIDEO_CODEC
        )
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **fallback_kwargs,
        )


def _video_encoder_result(configured_codec: str, used_codec: str) -> dict[str, str | bool]:
    return {
        "configured_codec": configured_codec,
        "used_codec": used_codec,
        "fallback_used": used_codec != configured_codec,
    }


def check_video_encoder(
    video_aspect: VideoAspect | str = VideoAspect.portrait,
) -> dict[str, str | bool | int]:
    """Run a short encode at the selected output resolution and report the codec used."""
    aspect = VideoAspect(video_aspect)
    width, height = aspect.to_resolution()
    configured_codec = _get_configured_video_codec()
    descriptor, probe_file = tempfile.mkstemp(suffix=".mp4")
    os.close(descriptor)
    probe_clip = None
    try:
        os.remove(probe_file)
        probe_clip = ColorClip(size=(width, height), color=(0, 0, 0), duration=1)
        used_codec = _write_videofile_with_codec_fallback(
            probe_clip,
            probe_file,
            codec=configured_codec,
            fps=24,
            audio=False,
            logger=None,
        )
        return {
            **_video_encoder_result(configured_codec, used_codec),
            "video_aspect": aspect.value,
            "width": width,
            "height": height,
        }
    finally:
        if probe_clip is not None:
            probe_clip.close()
        if os.path.exists(probe_file):
            os.remove(probe_file)


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    生成 concat demuxer 文件列表中的路径。

    FFmpeg 官方文档要求 concat list 中的特殊字符和空格需要转义；Windows
    绝对路径里的反斜杠也容易被解析成转义字符。这里统一转成正斜杠形式，
    让 `C:\\Users\\...` 变成 `C:/Users/...`，再处理单引号，兼容 macOS/Linux。
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def _format_ffmpeg_ass_filter_path(file_path: str) -> str:
    absolute_path = os.path.abspath(file_path).replace("\\", "/")
    option_value = (
        absolute_path.replace("\\", "\\\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
    )
    return "".join(
        f"\\{character}" if character in "\\'[],;" else character
        for character in option_value
    )


def _build_ass_subtitles_filter(subtitle_file: str) -> str:
    return f"subtitles=filename={_format_ffmpeg_ass_filter_path(subtitle_file)}"


def _hex_to_ass_bgr_color(value: str | None, default: str = "#FFFFFF") -> str:
    color = value if isinstance(value, str) and value.startswith("#") else default
    color = color.lstrip("#")
    if len(color) != 6 or any(
        character not in "0123456789abcdefABCDEF" for character in color
    ):
        color = default.lstrip("#")
    red, green, blue = color[0:2], color[2:4], color[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _srt_subtitle_ffmpeg_supported(params: VideoParams) -> bool:
    if not params.subtitle_enabled:
        return False
    if getattr(params, "subtitle_style", "classic") != "classic":
        return False
    if params.subtitle_position not in {"bottom", "top", "center"}:
        return False
    if getattr(params, "rounded_subtitle_background", False):
        return False
    bg_color = params.text_background_color
    if isinstance(bg_color, bool):
        return not bg_color
    return not bool(bg_color)


def _srt_subtitle_alignment_and_margin(
    params: VideoParams,
    video_height: int,
    video_aspect: VideoAspect,
) -> tuple[int, int]:
    if params.subtitle_position == "top":
        return 8, int(video_height * 0.05)
    if params.subtitle_position == "center":
        return 5, 10
    return 2, int(video_height * get_subtitle_bottom_safe_margin_ratio(video_aspect))


def _subtitle_font_family(font_path: str, fallback_name: str) -> str:
    try:
        font_family = ImageFont.truetype(font_path, 12).getname()[0]
    except (AttributeError, OSError, ValueError):
        font_family = os.path.splitext(os.path.basename(fallback_name or font_path))[0]
    safe_font_family = "".join(
        character
        for character in str(font_family)
        if character.isalnum() or character in {" ", "_", "-", "."}
    ).strip()
    return safe_font_family or "Arial"


def _format_ass_style_number(value: object, default: float) -> str:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = default
    if not math.isfinite(numeric_value) or numeric_value < 0:
        numeric_value = default
    return f"{numeric_value:g}"


def _build_srt_subtitles_filter(
    subtitle_file: str,
    params: VideoParams,
    video_width: int,
    video_height: int,
    video_aspect: VideoAspect,
    font_path: str,
) -> str:
    font_name = _subtitle_font_family(font_path, params.font_name or "")
    alignment, margin_v = _srt_subtitle_alignment_and_margin(
        params,
        video_height,
        video_aspect,
    )
    horizontal_margin = int(video_width * 0.05)
    style = ",".join(
        [
            f"PlayResX={video_width}",
            f"PlayResY={video_height}",
            f"Fontname={font_name}",
            f"Fontsize={int(params.font_size)}",
            f"PrimaryColour={_hex_to_ass_bgr_color(params.text_fore_color)}",
            f"OutlineColour={_hex_to_ass_bgr_color(params.stroke_color, '#000000')}",
            f"Outline={_format_ass_style_number(params.stroke_width, 1.5)}",
            "Shadow=0",
            "BorderStyle=1",
            f"Alignment={alignment}",
            f"MarginL={horizontal_margin}",
            f"MarginR={horizontal_margin}",
            f"MarginV={margin_v}",
        ]
    )
    return (
        f"subtitles=filename={_format_ffmpeg_ass_filter_path(subtitle_file)}"
        f":charenc=UTF-8"
        f":fontsdir={_format_ffmpeg_ass_filter_path(utils.font_dir())}"
        f":force_style='{style}'"
    )


def _ass_burn_temp_output_file(output_file: str) -> str:
    output_dir = os.path.dirname(output_file) or "."
    output_name = os.path.basename(output_file)
    output_stem, output_ext = os.path.splitext(output_name)
    return os.path.join(output_dir, f"{output_stem}.assburn.tmp{output_ext or '.mp4'}")


def _srt_burn_temp_output_file(output_file: str) -> str:
    output_dir = os.path.dirname(output_file) or "."
    output_name = os.path.basename(output_file)
    output_stem, output_ext = os.path.splitext(output_name)
    return os.path.join(output_dir, f"{output_stem}.srtburn.tmp{output_ext or '.mp4'}")


def _remove_file_quietly(file_path: str):
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError as exc:
        logger.warning(f"failed to remove temp file {file_path}: {str(exc)}")


def _burn_ass_subtitles_with_ffmpeg(
    input_file: str,
    subtitle_file: str,
    output_file: str,
    threads: int | None,
) -> str | None:
    temp_output_file = _ass_burn_temp_output_file(output_file)

    def run_burn(codec: str):
        _remove_file_quietly(temp_output_file)
        return subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-y",
                "-i",
                input_file,
                "-vf",
                _build_ass_subtitles_filter(subtitle_file),
                "-c:v",
                codec,
                "-c:a",
                "copy",
                "-threads",
                str(threads or 2),
                "-pix_fmt",
                "yuv420p",
                *_ffmpeg_bt709_color_metadata_args(),
                *_ffmpeg_bt709_h264_vui_args(),
                *_ffmpeg_quality_args(codec),
                *_ffmpeg_keyframe_args(),
                *_ffmpeg_mp4_faststart_args(temp_output_file),
                temp_output_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def finalize_burn_output() -> bool:
        if not os.path.exists(temp_output_file):
            logger.error("ASS subtitle burn-in did not create an output file")
            return False
        os.replace(temp_output_file, output_file)
        return True

    codec = _get_effective_video_codec(_get_configured_video_codec())
    try:
        result = run_burn(codec)
        if result.returncode == 0 and finalize_burn_output():
            return codec

        reason = (result.stderr or result.stdout or "").strip()
        logger.warning(f"failed to burn ASS subtitles with {codec}: {reason}")
        if codec != _DEFAULT_VIDEO_CODEC:
            fallback_result = run_burn(_DEFAULT_VIDEO_CODEC)
            if fallback_result.returncode == 0 and finalize_burn_output():
                _disable_runtime_video_codec(codec, reason)
                return _DEFAULT_VIDEO_CODEC
            reason = (fallback_result.stderr or fallback_result.stdout or "").strip()
            logger.error(f"failed to burn ASS subtitles with fallback codec: {reason}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(f"failed to burn ASS subtitles: {str(exc)}")

    _remove_file_quietly(temp_output_file)
    return None


def _burn_srt_subtitles_with_ffmpeg(
    input_file: str,
    subtitle_file: str,
    output_file: str,
    params: VideoParams,
    video_width: int,
    video_height: int,
    video_aspect: VideoAspect,
    font_path: str,
    threads: int | None,
) -> str | None:
    temp_output_file = _srt_burn_temp_output_file(output_file)

    def run_burn(codec: str):
        _remove_file_quietly(temp_output_file)
        return subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-y",
                "-i",
                input_file,
                "-vf",
                _build_srt_subtitles_filter(
                    subtitle_file,
                    params=params,
                    video_width=video_width,
                    video_height=video_height,
                    video_aspect=video_aspect,
                    font_path=font_path,
                ),
                "-c:v",
                codec,
                "-c:a",
                "copy",
                "-threads",
                str(threads or 2),
                "-pix_fmt",
                "yuv420p",
                *_ffmpeg_bt709_color_metadata_args(),
                *_ffmpeg_bt709_h264_vui_args(),
                *_ffmpeg_quality_args(codec),
                *_ffmpeg_keyframe_args(),
                *_ffmpeg_mp4_faststart_args(temp_output_file),
                temp_output_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def finalize_burn_output() -> bool:
        if not os.path.exists(temp_output_file):
            logger.error("SRT subtitle burn-in did not create an output file")
            return False
        os.replace(temp_output_file, output_file)
        return True

    codec = _get_effective_video_codec(_get_configured_video_codec())
    try:
        result = run_burn(codec)
        if result.returncode == 0 and finalize_burn_output():
            return codec

        reason = (result.stderr or result.stdout or "").strip()
        logger.warning(f"failed to burn SRT subtitles with {codec}: {reason}")
        if codec != _DEFAULT_VIDEO_CODEC:
            fallback_result = run_burn(_DEFAULT_VIDEO_CODEC)
            if fallback_result.returncode == 0 and finalize_burn_output():
                _disable_runtime_video_codec(codec, reason)
                return _DEFAULT_VIDEO_CODEC
            reason = (fallback_result.stderr or fallback_result.stdout or "").strip()
            logger.error(f"failed to burn SRT subtitles with fallback codec: {reason}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(f"failed to burn SRT subtitles: {str(exc)}")

    _remove_file_quietly(temp_output_file)
    return None


def concat_video_clips_with_ffmpeg(
    clip_files: List[str],
    output_file: str,
    threads: int,
    output_dir: str,
    max_duration: float | None = None,
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")

    def build_command(codec: str, stream_copy: bool = False) -> list[str]:
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
        ]
        if stream_copy:
            stream_copy_command = [
                *command,
                "-c",
                "copy",
                *_ffmpeg_bt709_h264_vui_args(),
                *_ffmpeg_mp4_faststart_args(output_file),
            ]
            if max_duration is not None and max_duration > 0:
                stream_copy_command.extend(["-t", f"{max_duration:.3f}"])
            stream_copy_command.append(output_file)
            return stream_copy_command
        encode_command = [
            *command,
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            *_ffmpeg_bt709_color_metadata_args(),
            *_ffmpeg_bt709_h264_vui_args(),
            *_ffmpeg_quality_args(codec),
            *_ffmpeg_keyframe_args(),
            *_ffmpeg_mp4_faststart_args(output_file),
        ]
        if max_duration is not None and max_duration > 0:
            encode_command.extend(["-t", f"{max_duration:.3f}"])
        encode_command.append(output_file)
        return encode_command

    def run_concat(codec: str, stream_copy: bool = False):
        command = build_command(codec, stream_copy=stream_copy)
        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        try:
            return run_concat("copy", stream_copy=True)
        except Exception as exc:
            logger.info(
                f"stream-copy concat failed, re-encoding clips: {str(exc)}"
            )

        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
        delete_files(concat_list_file)


def crossfade_video_clips_with_ffmpeg(
    clip_files: List[str],
    clip_durations: List[float],
    output_file: str,
    threads: int,
    max_duration: float | None = None,
):
    """Join normalized clips with FFmpeg's fade crossfade filter."""
    if len(clip_files) < 2 or len(clip_files) != len(clip_durations):
        raise ValueError("crossfade requires matching lists of at least two clips")

    durations = []
    for raw_duration in clip_durations:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("crossfade clip durations must be numeric") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("crossfade clip durations must be positive")
        durations.append(duration)

    filter_parts = [
        f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]"
        for index in range(len(clip_files))
    ]
    output_label = "v0"
    timeline_duration = durations[0]
    for index, duration in enumerate(durations[1:], start=1):
        transition_duration = _get_effective_crossfade_duration(
            durations[index - 1],
            duration,
        )
        next_output_label = f"xf{index}"
        offset = max(0.0, timeline_duration - transition_duration)
        filter_parts.append(
            f"[{output_label}][v{index}]xfade=transition=fade:"
            f"duration={transition_duration:.3f}:offset={offset:.3f}"
            f"[{next_output_label}]"
        )
        timeline_duration += duration - transition_duration
        output_label = next_output_label
    filter_parts.append(f"[{output_label}]setsar=1[square]")
    output_label = "square"
    filter_graph = ";".join(filter_parts)
    try:
        normalized_max_duration = float(max_duration)
    except (TypeError, ValueError):
        normalized_max_duration = None
    if (
        normalized_max_duration is not None
        and (not math.isfinite(normalized_max_duration) or normalized_max_duration <= 0)
    ):
        normalized_max_duration = None
    output_fps = _get_configured_video_fps()

    def build_command(codec: str) -> list[str]:
        command = [utils.get_ffmpeg_binary(), "-y"]
        for clip_file in clip_files:
            command.extend(["-i", clip_file])
        return [
            *command,
            "-filter_complex",
            filter_graph,
            "-map",
            f"[{output_label}]",
            "-an",
            *(
                ["-t", f"{normalized_max_duration:.3f}"]
                if normalized_max_duration is not None
                else []
            ),
            "-r",
            str(output_fps),
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            *_ffmpeg_bt709_color_metadata_args(),
            *_ffmpeg_bt709_h264_vui_args(),
            *_ffmpeg_quality_args(codec),
            *_ffmpeg_keyframe_args(fps=output_fps),
            *_ffmpeg_mp4_faststart_args(output_file),
            output_file,
        ]

    def run_crossfade(codec: str):
        result = subprocess.run(
            build_command(codec),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg crossfade failed")
        return codec

    effective_codec = _get_effective_video_codec()
    try:
        return run_crossfade(effective_codec)
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        result_codec = run_crossfade(_DEFAULT_VIDEO_CODEC)
        _disable_runtime_video_codec(effective_codec, str(exc))
        return result_codec


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    安静地打开视频文件，避免 MoviePy 2.1.x 把 ffmpeg 探测信息直接打印到 stdout。

    背景：
    当前依赖版本的 `FFMPEG_VideoReader` 内部存在 `print(self.infos)` 和
    `print(ffmpeg command)`，读取无音轨的中间视频时会输出
    `audio_found: False`。这只是输入素材 metadata，不代表最终成片没有音频，
    但会误导 WebUI/终端用户以为生成失败。

    实现：
    1. 只在打开 VideoFileClip 的短窗口内重定向 stdout；
    2. 默认 `audio=False`，因为项目视频素材阶段不需要保留素材原声，
       最终音频会在 `generate_video()` 阶段统一挂载；
    3. 如果依赖库确实输出了内容，降级为 debug 日志，便于必要时排查。
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def get_video_duration(video_path: str) -> float | None:
    """Read a video's usable duration without keeping its reader open."""
    clip = None
    try:
        clip = _open_video_clip_quietly(video_path)
        duration = float(getattr(clip, "duration", 0) or 0)
    except Exception as exc:
        logger.debug(f"failed to read video duration: {video_path}, error: {str(exc)}")
        return None
    finally:
        close_clip(clip)

    if duration <= 0 or not math.isfinite(duration):
        return None
    return duration


def close_clip(clip):
    if clip is None:
        return
        
    try:
        close_method = getattr(clip, "close", None)
        if callable(close_method):
            close_method()

        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    unique_files = dict.fromkeys(file for file in files if file)
    for file in unique_files:
        try:
            os.remove(file)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning(f"failed to delete temporary file {file}: {str(e)}")


def _resolve_bgm_file_path(song_dir: str, bgm_file: str) -> str:
    # 背景音乐只允许读取 resource/songs 目录内的文件，避免用户输入任意路径后
    # 被 MoviePy 打开。这里兼容两种常见输入：
    # 1. output000.mp3：来自 BGM 列表或用户只填写文件名
    # 2. ./resource/songs/output000.mp3：用户按项目目录结构填写的相对路径
    # 两种写法最终都会再次通过 resource/songs 白名单校验，不能绕过目录限制。
    try:
        return file_security.resolve_path_within_directory(song_dir, bgm_file)
    except ValueError as song_dir_exc:
        if os.path.isabs(bgm_file):
            raise song_dir_exc

        project_relative_file = os.path.join(utils.root_dir(), bgm_file)
        try:
            return file_security.resolve_path_within_directory(
                song_dir, project_relative_file
            )
        except ValueError as root_dir_exc:
            raise ValueError(str(root_dir_exc)) from song_dir_exc


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        song_dir = utils.song_dir()
        try:
            resolved_bgm_file = _resolve_bgm_file_path(song_dir, bgm_file)
        except ValueError as exc:
            # API 请求里的 bgm_file 来自用户输入，不能直接把任意绝对路径交给
            # MoviePy 打开。这里强制限制到 resource/songs 目录，阻止读取
            # /etc/passwd、配置文件、密钥等非背景音乐文件。
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, song_dir: {song_dir}, error: {str(exc)}"
            )
            return ""

        if not resolved_bgm_file.lower().endswith(_BGM_EXTENSIONS):
            logger.warning(f"reject unsupported bgm file extension: {resolved_bgm_file}")
            return ""

        return resolved_bgm_file

    if bgm_type == "random":
        # Random playback is limited to the explicitly audited CC0 additions.
        suffix = "cc0_*.mp3"
        song_dir = utils.song_dir()
        files = glob.glob(os.path.join(song_dir, suffix))
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning(f"no verified CC0 bgm files found in song directory: {song_dir}")
            return ""
        return random.choice(files)

    return ""


def _fast_render_clip_with_ffmpeg(
    input_file: str,
    output_file: str,
    start_time: float,
    duration: float,
    target_width: int,
    target_height: int,
    threads: int,
    brightness_adjustment: float = 0.0,
    saturation_multiplier: float = 1.0,
    warmth_adjustment: float = 0.0,
    crop_x_ratio: float | None = None,
    crop_y_ratio: float | None = None,
) -> bool:
    """Render a plain trimmed clip without routing every frame through MoviePy."""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return False
    if duration <= 0 or not os.path.isfile(input_file):
        return False

    try:
        brightness_adjustment = float(brightness_adjustment)
    except (TypeError, ValueError):
        brightness_adjustment = 0.0
    if not math.isfinite(brightness_adjustment):
        brightness_adjustment = 0.0
    brightness_adjustment = max(
        -_MAX_COLOR_LEVELING_BRIGHTNESS_ADJUSTMENT,
        min(_MAX_COLOR_LEVELING_BRIGHTNESS_ADJUSTMENT, brightness_adjustment),
    )
    try:
        saturation_multiplier = float(saturation_multiplier)
    except (TypeError, ValueError):
        saturation_multiplier = 1.0
    if not math.isfinite(saturation_multiplier):
        saturation_multiplier = 1.0
    saturation_multiplier = max(
        _MIN_COLOR_LEVELING_SATURATION_MULTIPLIER,
        min(_MAX_COLOR_LEVELING_SATURATION_MULTIPLIER, saturation_multiplier),
    )
    try:
        warmth_adjustment = float(warmth_adjustment)
    except (TypeError, ValueError):
        warmth_adjustment = 0.0
    if not math.isfinite(warmth_adjustment):
        warmth_adjustment = 0.0
    warmth_adjustment = max(
        -_MAX_COLOR_LEVELING_WARMTH_ADJUSTMENT,
        min(_MAX_COLOR_LEVELING_WARMTH_ADJUSTMENT, warmth_adjustment),
    )
    eq_options = []
    if brightness_adjustment:
        eq_options.append(f"brightness={brightness_adjustment:.4f}")
    if saturation_multiplier != 1.0:
        eq_options.append(f"saturation={saturation_multiplier:.4f}")
    color_filter = f",eq={':'.join(eq_options)}" if eq_options else ""
    warmth_filter = (
        ",colorbalance="
        f"rm={warmth_adjustment:.4f}:bm={-warmth_adjustment:.4f}:pl=1"
        if warmth_adjustment
        else ""
    )
    crop_x_ratio = _bounded_crop_ratio(crop_x_ratio)
    crop_y_ratio = _bounded_crop_ratio(crop_y_ratio)
    crop_filter = f"crop={target_width}:{target_height}"
    if crop_x_ratio is not None or crop_y_ratio is not None:
        crop_x = (
            f"(iw-ow)*{crop_x_ratio:.6f}"
            if crop_x_ratio is not None
            else "(iw-ow)/2"
        )
        crop_y = (
            f"(ih-oh)*{crop_y_ratio:.6f}"
            if crop_y_ratio is not None
            else "(ih-oh)/2"
        )
        crop_filter = f"{crop_filter}:{crop_x}:{crop_y}"

    codec = _get_effective_video_codec()
    output_fps = _get_configured_video_fps()
    deband_filter = _optional_deband_filter()

    def build_command(
        include_optional_deband: bool,
        include_color_normalization: bool,
    ) -> list[str]:
        color_normalization_options = (
            _BT709_LIMITED_RANGE_SCALE_OPTIONS
            if include_color_normalization
            else ""
        )
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-ss",
            str(start_time or 0),
            "-i",
            input_file,
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-vf",
            (
                # Preserve progressive frames; yadif only cleans frames marked interlaced.
                "yadif=deint=interlaced,"
                f"scale={target_width}:{target_height}:"
                "force_original_aspect_ratio=increase:"
                f"flags={_HIGH_QUALITY_SCALE_FLAGS}"
                f"{color_normalization_options},"
                f"{crop_filter}{color_filter}{warmth_filter}"
                f"{deband_filter if include_optional_deband else ''},setsar=1"
            ),
            "-r",
            str(output_fps),
            "-fps_mode",
            "cfr",
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            *_ffmpeg_bt709_color_metadata_args(),
            *_ffmpeg_bt709_h264_vui_args(),
            *_ffmpeg_quality_args(codec),
            *_ffmpeg_keyframe_args(fps=output_fps),
        ]
        if codec != _DEFAULT_VIDEO_CODEC:
            preset = (
                _MOVIEPY_AMF_PRESET
                if codec == "h264_amf"
                else _DEFAULT_LIBX264_PRESET
            )
            command.extend(["-preset", preset])
        command.extend(["-an", *_ffmpeg_mp4_faststart_args(output_file), output_file])
        return command

    def run_fast_render(
        include_optional_deband: bool,
        include_color_normalization: bool,
    ):
        try:
            result = subprocess.run(
                build_command(include_optional_deband, include_color_normalization),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"ffmpeg clip fast path unavailable: {str(exc)}")
            return None, str(exc)
        return result, (result.stderr or result.stdout or "").strip()

    attempts = [(bool(deband_filter), True)]
    if deband_filter:
        attempts.append((False, True))
    attempts.append((False, False))
    reason = ""
    for include_optional_deband, include_color_normalization in attempts:
        result, attempt_reason = run_fast_render(
            include_optional_deband,
            include_color_normalization,
        )
        if result is not None and result.returncode == 0:
            return True
        reason = attempt_reason or reason
        if include_optional_deband:
            logger.info("optional video debanding failed; retrying without cleanup")
        elif include_color_normalization:
            logger.info("BT.709 color normalization failed; retrying without conversion")

    logger.debug(f"ffmpeg clip fast path failed: {reason}")
    return False


def _fast_render_image_with_ffmpeg(
    input_file: str,
    output_file: str,
    width: int,
    height: int,
    duration: float,
    threads: int = 2,
    codec: str | None = None,
    focal_x_ratio: float | None = None,
    focal_y_ratio: float | None = None,
    target_width: int | None = None,
    target_height: int | None = None,
) -> bool:
    """Render an image zoom without routing frames through MoviePy."""
    try:
        duration = float(duration)
        fps = int(_get_configured_video_fps())
        output_width, output_height = _even_video_size((width, height))
        if target_width and target_height:
            output_width, output_height = _even_video_size((target_width, target_height))
        threads = max(1, int(threads or 2))
    except (TypeError, ValueError):
        return False
    if (
        duration <= 0
        or not math.isfinite(duration)
        or fps <= 0
        or not os.path.isfile(input_file)
    ):
        return False

    codec = codec or _get_effective_video_codec()
    scale_filter = (
        f"scale=iw:ih:flags={_HIGH_QUALITY_SCALE_FLAGS}"
    )
    crop_filter = ""
    zoom_x = "iw/2-(iw/zoom/2)"
    normalized_focal_x = _bounded_crop_ratio(focal_x_ratio)
    if normalized_focal_x is not None:
        zoom_x = (
            f"clip({normalized_focal_x:.6f}*iw-iw/(2*zoom)\\,0\\,iw-iw/zoom)"
        )
    zoom_y = "ih/2-(ih/zoom/2)"
    normalized_focal_y = _bounded_crop_ratio(focal_y_ratio)
    if normalized_focal_y is not None:
        zoom_y = (
            f"clip({normalized_focal_y:.6f}*ih-ih/(2*zoom)\\,0\\,ih-ih/zoom)"
        )
    if target_width and target_height:
        crop_x_ratio, crop_y_ratio = _focal_crop_offset_ratios(
            width,
            height,
            output_width,
            output_height,
            focal_x_ratio=focal_x_ratio,
            focal_y_ratio=focal_y_ratio,
        )
        crop_x = "(iw-ow)/2"
        if crop_x_ratio is not None:
            crop_x = f"(iw-ow)*{crop_x_ratio:.6f}"
        crop_y = "(ih-oh)/2"
        if crop_y_ratio is not None:
            crop_y = f"(ih-oh)*{crop_y_ratio:.6f}"
        scale_filter = (
            f"scale={output_width}:{output_height}:"
            "force_original_aspect_ratio=increase:"
            f"flags={_HIGH_QUALITY_SCALE_FLAGS}"
        )
        crop_filter = f"crop={output_width}:{output_height}:{crop_x}:{crop_y},"
        zoom_x = "iw/2-(iw/zoom/2)"
        zoom_y = "ih/2-(ih/zoom/2)"
    def build_command(include_color_normalization: bool) -> list[str]:
        color_normalization_options = (
            _BT709_LIMITED_RANGE_SCALE_OPTIONS
            if include_color_normalization
            else ""
        )
        zoom_filter = (
            f"{scale_filter}{color_normalization_options},{crop_filter}"
            f"zoompan=z='min(1+{_IMAGE_ZOOM_RATE}*on/{fps},"
            f"{_MAX_IMAGE_ZOOM_SCALE})':"
            f"x='{zoom_x}':"
            f"y='{zoom_y}':"
            f"d=1:s={output_width}x{output_height}:fps={fps},setsar=1"
        )
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            input_file,
            "-vf",
            zoom_filter,
            "-t",
            str(duration),
            "-c:v",
            codec,
            "-threads",
            str(threads),
            "-pix_fmt",
            "yuv420p",
            *_ffmpeg_bt709_color_metadata_args(),
            *_ffmpeg_bt709_h264_vui_args(),
            *_ffmpeg_quality_args(codec),
            *_ffmpeg_keyframe_args(fps=fps),
        ]
        if codec != _DEFAULT_VIDEO_CODEC:
            preset = (
                _MOVIEPY_AMF_PRESET
                if codec == "h264_amf"
                else _DEFAULT_LIBX264_PRESET
            )
            command.extend(["-preset", preset])
        command.extend(["-an", *_ffmpeg_mp4_faststart_args(output_file), output_file])
        return command

    reason = ""
    for include_color_normalization in (True, False):
        try:
            result = subprocess.run(
                build_command(include_color_normalization),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"ffmpeg image fast path unavailable: {str(exc)}")
            return False

        if result.returncode == 0:
            return True
        reason = (result.stderr or result.stdout or "").strip() or reason
        if include_color_normalization:
            logger.info("BT.709 color normalization failed; retrying without conversion")

    logger.debug(f"ffmpeg image fast path failed: {reason}")
    if codec != _DEFAULT_VIDEO_CODEC:
        return _fast_render_image_with_ffmpeg(
            input_file=input_file,
            output_file=output_file,
            width=width,
            height=height,
            duration=duration,
            threads=threads,
            codec=_DEFAULT_VIDEO_CODEC,
            focal_x_ratio=focal_x_ratio,
            focal_y_ratio=focal_y_ratio,
            target_width=target_width,
            target_height=target_height,
        )
    return False


def _fast_mux_video_with_audio(
    video_path: str,
    audio_path: str,
    output_file: str,
    video_duration: float,
    audio_bitrate: str,
    voice_volume: float = 1.0,
) -> bool:
    """Attach narration without re-encoding an already rendered video stream."""
    try:
        video_duration = float(video_duration)
        voice_volume = float(voice_volume)
    except (TypeError, ValueError):
        return False
    if (
        video_duration <= 0
        or not math.isfinite(video_duration)
        or not math.isfinite(voice_volume)
        or not os.path.isfile(video_path)
        or not os.path.isfile(audio_path)
    ):
        return False
    if not _video_stream_matches_encoding_contract(video_path):
        logger.debug(
            "ffmpeg audio mux fast path skipped because source video does not meet the encoding contract"
        )
        return False

    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-filter:a",
        _narration_peak_limiter_filter(voice_volume),
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        str(audio_bitrate),
        "-t",
        str(video_duration),
        *_ffmpeg_mp4_faststart_args(output_file),
        output_file,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"ffmpeg audio mux fast path unavailable: {str(exc)}")
        return False

    if result.returncode == 0:
        return True
    reason = (result.stderr or result.stdout or "").strip()
    logger.debug(f"ffmpeg audio mux fast path failed: {reason}")
    return False


_BGM_DUCKING_THRESHOLD = 0.03
_BGM_DUCKING_RATIO = 8
_BGM_DUCKING_ATTACK_MS = 20
_BGM_DUCKING_RELEASE_MS = 300
_BGM_FALLBACK_DUCKING_FACTOR = 0.45
_AUDIO_PEAK_LIMITER_FILTER = "alimiter=limit=0.95:level=0:latency=1"


def _audio_limiter_temp_output_file(output_file: str) -> str:
    output_dir = os.path.dirname(output_file) or "."
    output_name = os.path.basename(output_file)
    output_stem, output_ext = os.path.splitext(output_name)
    return os.path.join(
        output_dir,
        f"{output_stem}.audiolimit.tmp{output_ext or '.mp4'}",
    )


def _limit_rendered_audio_peaks_with_ffmpeg(
    output_file: str,
    audio_bitrate: str,
) -> bool:
    """Limit a MoviePy-rendered audio track without re-encoding video frames."""
    if not os.path.isfile(output_file):
        return False

    temp_output_file = _audio_limiter_temp_output_file(output_file)
    _remove_file_quietly(temp_output_file)
    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-i",
        output_file,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-filter:a",
        _AUDIO_PEAK_LIMITER_FILTER,
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        str(audio_bitrate),
        *_ffmpeg_mp4_faststart_args(temp_output_file),
        temp_output_file,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"ffmpeg post-render audio limiter unavailable: {str(exc)}")
        return False

    if result.returncode == 0 and os.path.isfile(temp_output_file):
        os.replace(temp_output_file, output_file)
        return True

    reason = (result.stderr or result.stdout or "").strip()
    logger.debug(f"ffmpeg post-render audio limiter failed: {reason}")
    _remove_file_quietly(temp_output_file)
    return False


def _narration_peak_limiter_filter(voice_volume: float) -> str:
    """Apply gain first, then retain headroom before AAC encoding."""
    return (
        f"volume={voice_volume},{_AUDIO_PEAK_LIMITER_FILTER}"
        if voice_volume != 1.0
        else _AUDIO_PEAK_LIMITER_FILTER
    )


def _narration_ducked_bgm_volume(bgm_volume: float) -> float:
    try:
        return max(0.0, float(bgm_volume) * _BGM_FALLBACK_DUCKING_FACTOR)
    except (TypeError, ValueError):
        return 0.0


def _narration_relative_ducking_filter(voice_volume: float) -> str:
    try:
        voice_gain = float(voice_volume)
    except (TypeError, ValueError):
        voice_gain = 1.0

    if not math.isfinite(voice_gain):
        voice_gain = 1.0
    if voice_gain <= 0:
        threshold = 1.0
    else:
        # The sidechain already includes this gain, so scale its trigger too.
        threshold = min(0.99, max(0.001, _BGM_DUCKING_THRESHOLD * voice_gain))

    return (
        f"threshold={threshold:g}:ratio={_BGM_DUCKING_RATIO}:"
        f"attack={_BGM_DUCKING_ATTACK_MS}:release={_BGM_DUCKING_RELEASE_MS}"
    )


def _video_stream_matches_encoding_contract(video_path: str) -> bool:
    """Fail closed before stream-copying a video that may miss output settings."""
    try:
        # render_quality imports video helpers, so this remains local to avoid a
        # module-import cycle while still sharing the encoding-only probe.
        from app.services import render_quality

        return render_quality.video_stream_matches_encoding_contract(
            video_path,
            get_video_encoding_contract(),
        )
    except Exception as exc:
        logger.debug(f"could not verify video encoding contract for fast mux: {str(exc)}")
        return False


def _fast_mux_video_with_audio_and_bgm(
    video_path: str,
    audio_path: str,
    bgm_file: str,
    output_file: str,
    video_duration: float,
    audio_bitrate: str,
    bgm_volume: float,
    voice_volume: float = 1.0,
) -> bool:
    """Attach narration and looped BGM without re-encoding the video stream."""
    try:
        video_duration = float(video_duration)
        bgm_volume = float(bgm_volume)
        voice_volume = float(voice_volume)
    except (TypeError, ValueError):
        return False
    if (
        video_duration <= 0
        or not math.isfinite(video_duration)
        or not math.isfinite(bgm_volume)
        or not math.isfinite(voice_volume)
        or not os.path.isfile(video_path)
        or not os.path.isfile(audio_path)
        or not os.path.isfile(bgm_file)
    ):
        return False
    if not _video_stream_matches_encoding_contract(video_path):
        logger.debug(
            "ffmpeg BGM audio mux fast path skipped because source video does not meet the encoding contract"
        )
        return False

    fade_duration = min(3.0, video_duration)
    fade_start = max(0.0, video_duration - fade_duration)
    voice_filter = (
        f"[1:a]volume={voice_volume}[voice_input];"
        if voice_volume != 1.0
        else ""
    )
    voice_input_label = "voice_input" if voice_volume != 1.0 else "1:a"
    filter_graph = voice_filter + (
        f"[{voice_input_label}]asplit=2[voice][sidechain];"
        f"[2:a]volume={bgm_volume},"
        f"afade=t=out:st={fade_start}:d={fade_duration}[bgm];"
        f"[bgm][sidechain]sidechaincompress="
        f"{_narration_relative_ducking_filter(voice_volume)}[ducked];"
        f"[voice][ducked]amix=inputs=2:duration=longest:normalize=0,"
        f"{_AUDIO_PEAK_LIMITER_FILTER}[mixed]"
    )
    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-stream_loop",
        "-1",
        "-i",
        bgm_file,
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[mixed]",
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        str(audio_bitrate),
        "-t",
        str(video_duration),
        *_ffmpeg_mp4_faststart_args(output_file),
        output_file,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"ffmpeg BGM audio mux fast path unavailable: {str(exc)}")
        return False

    if result.returncode == 0:
        return True
    reason = (result.stderr or result.stdout or "").strip()
    logger.debug(f"ffmpeg BGM audio mux fast path failed: {reason}")
    return False


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    cue_end_times: list[float] | None = None,
    clip_speed: float = 1.0,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    try:
        # 这里只需要读取旁白音频时长来决定素材视频拼接长度；后续不会再使用
        # audio_clip。读取完成后立即关闭，避免早退或异常路径泄漏文件句柄。
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")
    required_video_duration = _get_required_video_duration(audio_duration)
    logger.info(
        f"required video duration: {required_video_duration:.2f} seconds "
        f"(audio duration + {_VIDEO_DURATION_SAFETY_MARGIN:.2f}s safety margin)"
    )

    # 兼容 API 直接调用时未传转场模式的情况，避免后续访问 .value 时崩溃。
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    normalized_clip_speed = utils.normalize_clip_speed(clip_speed)
    if normalized_clip_speed != 1.0:
        logger.info(f"clip playback speed: {normalized_clip_speed:.2f}x")
    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    is_single_openmontage_story = (
        len(video_paths) == 1
        and openmontage_materials.is_openmontage_output_path(video_paths[0])
    )
    if is_single_openmontage_story:
        # This source is already a composed explainer. Splitting it into
        # generic B-roll windows or fading within a scene makes it worse.
        transition_value = VideoTransitionMode.none.value
        logger.info("preserving the single OpenMontage story without transitions")
    output_dir = os.path.dirname(combined_video_path) or "."

    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    all_subclipped_items = []
    scene_change_times_by_source = {}
    video_duration = 0
    crossfade_enabled = transition_value == VideoTransitionMode.crossfade.value

    def append_processed_clip(processed_clip: SubClippedVideoClip):
        nonlocal video_duration
        previous_clip = processed_clips[-1] if processed_clips else None
        processed_clips.append(processed_clip)
        if crossfade_enabled and previous_clip is not None:
            overlap = _get_effective_crossfade_duration(
                previous_clip.duration,
                processed_clip.duration,
            )
            video_duration += processed_clip.duration - overlap
        else:
            video_duration += processed_clip.duration

    fast_path_available = transition_value in (
        None,
        VideoTransitionMode.none.value,
        VideoTransitionMode.crossfade.value,
    ) and normalized_clip_speed == 1.0
    for video_path in video_paths:
        clip = _open_video_clip_quietly(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        source_clip_duration_limit = (
            clip_duration
            if is_single_openmontage_story
            else max_clip_duration * normalized_clip_speed
        )
        detected_scene_change_times = []
        if (
            not is_single_openmontage_story
            and source_clip_duration_limit > 0
            and clip_duration > source_clip_duration_limit
        ):
            try:
                detected_scene_change_times = (
                    video_quality.detect_scene_change_timestamps(video_path) or []
                )
            except Exception as exc:
                logger.debug(f"skipping scene-change detection: {str(exc)}")
        scene_change_times_by_source[video_path] = detected_scene_change_times
        
        start_time = 0
        source_subclipped_items = []

        while start_time < clip_duration:
            end_time = min(start_time + source_clip_duration_limit, clip_duration)
            if end_time < clip_duration:
                end_time = _scene_aligned_subclip_end_time(
                    start_time=start_time,
                    planned_end_time=end_time,
                    scene_change_times=detected_scene_change_times,
                    minimum_duration=min(
                        end_time - start_time,
                        max(
                            _CUE_CUT_DYNAMIC_MIN_DURATION_SECONDS,
                            (end_time - start_time)
                            * _CUE_CUT_DYNAMIC_MIN_DURATION_RATIO,
                        ),
                    ),
                )

            # 保留所有有效分段。
            # 这样既不会丢掉“整段视频本身就短于 max_clip_duration”的素材，
            # 也不会吞掉长视频最后剩下的一小段尾部内容。
            if end_time > start_time:
                crop_x_ratio, crop_y_ratio = (
                    _clip_focal_crop_offset_ratios(
                        clip,
                        video_width,
                        video_height,
                        start_time=start_time,
                        end_time=end_time,
                    )
                    if fast_path_available
                    else (None, None)
                )
                source_subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        source_file_path=video_path,
                        crop_x_ratio=crop_x_ratio,
                        crop_y_ratio=crop_y_ratio,
                    )
                )

            start_time = end_time
            if (
                not is_single_openmontage_story
                and concat_mode_value == VideoConcatMode.sequential.value
                and len(video_paths) > 1
            ):
                break

        all_subclipped_items.extend(source_subclipped_items)

        quality_clip = clip
        try:
            detected_black_segments = (
                video_quality.detect_sustained_near_black_segments(video_path)
            )
            if detected_black_segments is None:
                filtered_items = _filter_near_black_subclips(
                    quality_clip,
                    source_subclipped_items,
                    preserve_when_empty=False,
                )
            else:
                filtered_items = _filter_subclips_with_detected_black_segments(
                    source_subclipped_items,
                    detected_black_segments,
                    preserve_when_empty=False,
                    trim_leading_prefix=not is_single_openmontage_story,
                )
        except Exception:
            filtered_items = source_subclipped_items
        try:
            detected_frozen_segments = (
                video_quality.detect_sustained_frozen_segments(video_path)
            )
            if detected_frozen_segments is not None:
                filtered_items = _filter_subclips_with_detected_segments(
                    filtered_items,
                    detected_frozen_segments,
                    preserve_when_empty=False,
                    trim_leading_prefix=not is_single_openmontage_story,
                )
        except Exception:
            pass
        finally:
            close_clip(quality_clip)
        subclipped_items.extend(filtered_items)

    if not subclipped_items:
        subclipped_items = all_subclipped_items

    subclipped_items = _prioritize_unique_source_clips(
        subclipped_items=subclipped_items,
        concat_mode=video_concat_mode,
    )

    brightness_adjustments = {}
    saturation_adjustments = {}
    warmth_adjustments = {}
    if fast_path_available:
        try:
            brightness_adjustments = _subclip_brightness_adjustments(subclipped_items)
        except Exception as exc:
            logger.debug(f"skipping brightness leveling: {str(exc)}")
        try:
            saturation_adjustments = _subclip_saturation_adjustments(subclipped_items)
        except Exception as exc:
            logger.debug(f"skipping saturation leveling: {str(exc)}")
        try:
            warmth_adjustments = _subclip_warmth_adjustments(subclipped_items)
        except Exception as exc:
            logger.debug(f"skipping warmth leveling: {str(exc)}")

    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration >= required_video_duration:
            break

        preferred_clip_duration = (
            min(
                subclipped_item.duration / normalized_clip_speed,
                required_video_duration,
            )
            if is_single_openmontage_story
            else min(
                subclipped_item.duration / normalized_clip_speed,
                max_clip_duration,
            )
        )
        minimum_cue_aligned_duration = min(
            preferred_clip_duration,
            max(
                _CUE_CUT_DYNAMIC_MIN_DURATION_SECONDS,
                preferred_clip_duration * _CUE_CUT_DYNAMIC_MIN_DURATION_RATIO,
            ),
        )
        if crossfade_enabled and processed_clips:
            planned_clip_duration = _crossfade_cue_aligned_clip_duration(
                timeline_duration=video_duration,
                previous_clip_duration=processed_clips[-1].duration,
                preferred_clip_duration=preferred_clip_duration,
                cue_end_times=cue_end_times,
                minimum_duration=minimum_cue_aligned_duration,
            )
        else:
            planned_clip_duration = _cue_aligned_clip_duration(
                timeline_start=video_duration,
                clip_duration=preferred_clip_duration,
                cue_end_times=cue_end_times,
                minimum_duration=minimum_cue_aligned_duration,
            )
        planned_source_duration = planned_clip_duration * normalized_clip_speed
        planned_clip_end_time = (
            subclipped_item.start_time + planned_source_duration
        )
        if (
            planned_clip_duration < preferred_clip_duration
            and normalized_clip_speed == 1.0
            and not crossfade_enabled
            and concat_mode_value != VideoConcatMode.sequential.value
        ):
            scene_aligned_end_time = _scene_aligned_subclip_end_time(
                start_time=subclipped_item.start_time,
                planned_end_time=planned_clip_end_time,
                scene_change_times=scene_change_times_by_source.get(
                    subclipped_item.source_file_path
                ),
                minimum_duration=minimum_cue_aligned_duration,
            )
            planned_clip_duration = (
                scene_aligned_end_time - subclipped_item.start_time
            )
            planned_source_duration = planned_clip_duration
            planned_clip_end_time = scene_aligned_end_time
        brightness_adjustment = brightness_adjustments.get(
            _subclip_identity(subclipped_item),
            0.0,
        )
        saturation_multiplier = saturation_adjustments.get(
            _subclip_identity(subclipped_item),
            1.0,
        )
        warmth_adjustment = warmth_adjustments.get(
            _subclip_identity(subclipped_item),
            0.0,
        )
        
        logger.debug(
            f"processing clip {i+1}: {subclipped_item.width}x{subclipped_item.height}, "
            f"source: {os.path.basename(subclipped_item.source_file_path)}, "
            f"current duration: {video_duration:.2f}s, "
            f"remaining: {required_video_duration - video_duration:.2f}s"
        )
        
        clip_file = os.path.join(output_dir, f"temp-clip-{i+1}.mp4")
        if fast_path_available:
            if _fast_render_clip_with_ffmpeg(
                input_file=subclipped_item.file_path,
                output_file=clip_file,
                start_time=subclipped_item.start_time,
                duration=planned_source_duration,
                target_width=video_width,
                target_height=video_height,
                threads=threads,
                brightness_adjustment=brightness_adjustment,
                saturation_multiplier=saturation_multiplier,
                warmth_adjustment=warmth_adjustment,
                crop_x_ratio=subclipped_item.crop_x_ratio,
                crop_y_ratio=subclipped_item.crop_y_ratio,
            ):
                append_processed_clip(
                    SubClippedVideoClip(
                        file_path=clip_file,
                        duration=planned_clip_duration,
                        width=subclipped_item.width,
                        height=subclipped_item.height,
                        source_file_path=subclipped_item.source_file_path,
                    )
                )
                continue
            fast_path_available = False

        clip = None
        try:
            clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                subclipped_item.start_time, planned_clip_end_time
            )
            if normalized_clip_speed != 1.0:
                clip = clip.with_speed_scaled(normalized_clip_speed)
            clip_duration = clip.duration
            # Not all videos are same size, so we need to resize them
            clip_w, clip_h = clip.size
            clip = _fit_clip_to_target_frame(clip, video_width, video_height)
                    
            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            transition_duration = _get_effective_transition_duration(clip.duration)
            if (
                transition_value in (None, VideoTransitionMode.none.value)
                or transition_duration <= 0
            ):
                clip = clip
            elif transition_value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, transition_duration)
            elif transition_value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, transition_duration)
            elif transition_value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, transition_duration, shuffle_side)
            elif transition_value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, transition_duration, shuffle_side)
            elif transition_value == VideoTransitionMode.zoom_in.value:
                clip = video_effects.zoomin_transition(clip, transition_duration)
            elif transition_value == VideoTransitionMode.zoom_out.value:
                clip = video_effects.zoomout_transition(clip, transition_duration)
            elif transition_value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, transition_duration),
                    lambda c: video_effects.fadeout_transition(c, transition_duration),
                    lambda c: video_effects.slidein_transition(
                        c, transition_duration, shuffle_side
                    ),
                    lambda c: video_effects.slideout_transition(
                        c, transition_duration, shuffle_side
                    ),
                    lambda c: video_effects.zoomin_transition(c, transition_duration),
                    lambda c: video_effects.zoomout_transition(c, transition_duration),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if not is_single_openmontage_story and clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)
                
            # wirte clip to temp file
            _write_videofile_with_codec_fallback(
                clip,
                clip_file,
                codec=_get_configured_video_codec(),
                logger=None,
                fps=_get_configured_video_fps(),
                threads=threads,
            )

            # Store clip duration before closing
            clip_duration_saved = clip.duration

            append_processed_clip(
                SubClippedVideoClip(
                    file_path=clip_file,
                    duration=clip_duration_saved,
                    width=clip_w,
                    height=clip_h,
                    source_file_path=subclipped_item.source_file_path,
                )
            )
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
        finally:
            if clip is not None:
                close_clip(clip)
    
    # loop processed clips until the video duration covers the audio duration and the small safety margin.
    if video_duration < required_video_duration:
        logger.warning(
            f"video duration ({video_duration:.2f}s) is shorter than required duration "
            f"({required_video_duration:.2f}s), looping clips to match audio length."
        )
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= required_video_duration:
                break
            append_processed_clip(clip)
        logger.info(
            f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, "
            f"required duration: {required_video_duration:.2f}s, "
            f"looped {len(processed_clips)-len(base_clips)} clips"
        )
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    # if there is only one clip, use it directly
    if len(processed_clips) == 1:
        logger.info("using single clip directly")
        shutil.copy(processed_clips[0].file_path, combined_video_path)
        delete_files([processed_clips[0].file_path])
        logger.info("video combining completed")
        return combined_video_path

    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    if crossfade_enabled:
        crossfade_video_clips_with_ffmpeg(
            clip_files=clip_files,
            clip_durations=[clip.duration for clip in processed_clips],
            output_file=combined_video_path,
            threads=threads,
            max_duration=required_video_duration,
        )
    else:
        concat_video_clips_with_ffmpeg(
            clip_files=clip_files,
            output_file=combined_video_path,
            threads=threads,
            output_dir=output_dir,
            max_duration=required_video_duration,
        )
    
    # clean temp files
    delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # 字幕换行必须在真正创建 TextClip 前完成，否则 MoviePy 只会按原始文本
    # 计算渲染区域。这里用 PIL 按当前字体和字号测量宽度，确保每一行都尽量
    # 控制在视频可用宽度内，避免大字号或中文长句直接溢出画面。
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, fontsize
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    def split_long_token(token):
        # 当一个 token 本身就超宽时（常见于中文无空格长句，或英文超长单词），
        # 退化为字符级拆分。关键点是：检测到 candidate 超宽时，先提交上一个
        # 仍然合法的 current，再把当前字符放入下一行，不能把超宽字符塞回上一行。
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # 中文长句按字符拆分时，最后一个句号、逗号等闭合标点可能被单独
        # 放到下一行，导致字幕背景被异常撑高，视觉上像一个小点掉在正文
        # 下方。这里在不重新设计换行算法的前提下，把上一行最后一个字
        # 移到标点行前面，让标点跟随文字显示，兼容中英文常见闭合标点。
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    height = len(lines) * height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # 字幕背景色来自 API/WebUI 参数，可能为空或格式不规范。这里统一只接受
    # #RRGGBB 形式，非法值回退为黑色，避免 PIL 渲染阶段抛出异常中断任务。
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # 新字幕背景仅在用户显式开启时使用：通过 RGBA 图片绘制圆角半透明底板，
    # 再交给 MoviePy 作为透明 ImageClip 参与合成。这样默认路径完全不变，
    # 同时可以低成本试验更柔和的字幕视觉效果。
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _get_visible_center_position(
    text_clip: TextClip,
    container_width: int,
    container_height: int,
) -> tuple[int, int]:
    """
    按文字真实可见像素把 TextClip 放到背景容器中心。

    MoviePy 的 TextClip 会按字体行高和 baseline 创建透明画布。很多字体的
    可见字形并不在这个画布的几何中心，直接 `with_position("center")`
    会把整块透明画布居中，导致字幕看起来偏上或偏下。这里读取 TextClip
    的透明 mask，只根据实际有像素的 bbox 计算偏移，让用户看到的文字
    在字幕背景里视觉居中。
    """
    x = int(round((container_width - text_clip.w) / 2))
    y = int(round((container_height - text_clip.h) / 2))

    try:
        if text_clip.mask is None:
            return x, y

        mask_frame = text_clip.mask.get_frame(0)
        ys, _ = np.where(mask_frame > 0.01)
        if len(ys) == 0:
            return x, y

        visible_top = int(ys.min())
        visible_bottom = int(ys.max())
        visible_height = visible_bottom - visible_top + 1
        y = int(round((container_height - visible_height) / 2 - visible_top))
    except Exception as exc:
        logger.debug(f"failed to center subtitle text by visible mask: {str(exc)}")

    return x, y


def get_subtitle_bottom_safe_margin_ratio(video_aspect) -> float:
    try:
        is_portrait = VideoAspect(video_aspect) == VideoAspect.portrait
    except (TypeError, ValueError):
        is_portrait = False
    return (
        _PORTRAIT_SUBTITLE_BOTTOM_SAFE_MARGIN_RATIO
        if is_portrait
        else _DEFAULT_SUBTITLE_BOTTOM_MARGIN_RATIO
    )


def _subtitle_y_within_frame(
    video_height, subtitle_height, requested_y, margin: float = 10.0
) -> float:
    """Keep a subtitle visible even when a caption grows unusually tall."""
    try:
        frame_height = float(video_height)
        caption_height = float(subtitle_height)
        requested_y = float(requested_y)
        margin = float(margin)
    except (TypeError, ValueError):
        return 0.0
    if not all(
        math.isfinite(value)
        for value in (frame_height, caption_height, requested_y, margin)
    ):
        return 0.0

    frame_height = max(0.0, frame_height)
    caption_height = max(0.0, caption_height)
    usable_margin = min(max(0.0, margin), max(0.0, (frame_height - caption_height) / 2))
    max_y = max(usable_margin, frame_height - caption_height - usable_margin)
    return max(usable_margin, min(requested_y, max_y))


def _subtitle_bottom_y(video_height, subtitle_height, video_aspect) -> float:
    margin_ratio = get_subtitle_bottom_safe_margin_ratio(video_aspect)
    return _subtitle_y_within_frame(
        video_height,
        subtitle_height,
        video_height * (1 - margin_ratio) - subtitle_height,
    )


def _scene_aligned_subclip_end_time(
    start_time: float,
    planned_end_time: float,
    scene_change_times: list[float] | None,
    tolerance_seconds: float = _SCENE_CUT_ALIGNMENT_TOLERANCE_SECONDS,
    minimum_duration: float | None = None,
) -> float:
    """Use the nearest safe source-scene boundary around a planned B-roll cut."""
    try:
        start_time = float(start_time)
        planned_end_time = float(planned_end_time)
        tolerance_seconds = float(tolerance_seconds)
    except (TypeError, ValueError):
        return planned_end_time
    if (
        planned_end_time <= start_time
        or tolerance_seconds <= 0
        or not scene_change_times
    ):
        return planned_end_time

    if minimum_duration is None:
        earliest_end_time = start_time
    else:
        try:
            minimum_duration = float(minimum_duration)
        except (TypeError, ValueError):
            return planned_end_time
        earliest_end_time = start_time + min(
            planned_end_time - start_time,
            max(0.0, minimum_duration),
        )

    matching_scene_changes = []
    for scene_change_time in scene_change_times:
        try:
            scene_change_time = float(scene_change_time)
        except (TypeError, ValueError):
            continue
        if (
            earliest_end_time <= scene_change_time <= planned_end_time
            and scene_change_time >= planned_end_time - tolerance_seconds
        ):
            matching_scene_changes.append(scene_change_time)
    if not matching_scene_changes:
        return planned_end_time
    return min(
        matching_scene_changes,
        key=lambda scene_change_time: (
            abs(scene_change_time - planned_end_time),
            scene_change_time,
        ),
    )


def _cue_aligned_clip_duration(
    timeline_start: float,
    clip_duration: float,
    cue_end_times: list[float] | None,
    tolerance_seconds: float = _CUE_CUT_ALIGNMENT_TOLERANCE_SECONDS,
    minimum_duration: float | None = None,
) -> float:
    """Align a B-roll cut to a nearby or safely earlier subtitle cue end."""
    try:
        timeline_start = float(timeline_start)
        clip_duration = float(clip_duration)
        tolerance_seconds = float(tolerance_seconds)
    except (TypeError, ValueError):
        return clip_duration
    if clip_duration <= 0 or tolerance_seconds <= 0 or not cue_end_times:
        return clip_duration

    planned_cut = timeline_start + clip_duration
    if minimum_duration is None:
        earliest_cut = planned_cut - tolerance_seconds
    else:
        try:
            minimum_duration = float(minimum_duration)
        except (TypeError, ValueError):
            return clip_duration
        earliest_cut = timeline_start + min(
            clip_duration,
            max(0.0, minimum_duration),
        )
    matching_cuts = []
    for cue_end_time in cue_end_times:
        try:
            cue_end_time = float(cue_end_time)
        except (TypeError, ValueError):
            continue
        if timeline_start < cue_end_time <= planned_cut and cue_end_time >= earliest_cut:
            matching_cuts.append(cue_end_time)
    if not matching_cuts:
        return clip_duration
    return max(matching_cuts) - timeline_start


def _crossfade_cue_aligned_clip_duration(
    timeline_duration: float,
    previous_clip_duration: float,
    preferred_clip_duration: float,
    cue_end_times: list[float] | None,
    minimum_duration: float | None = None,
) -> float:
    """Align a crossfaded clip end to a cue using its actual overlap."""
    try:
        timeline_duration = float(timeline_duration)
        previous_clip_duration = float(previous_clip_duration)
        preferred_clip_duration = float(preferred_clip_duration)
    except (TypeError, ValueError):
        return preferred_clip_duration
    if (
        not math.isfinite(timeline_duration)
        or not math.isfinite(previous_clip_duration)
        or not math.isfinite(preferred_clip_duration)
        or preferred_clip_duration <= 0
        or not cue_end_times
    ):
        return preferred_clip_duration

    if minimum_duration is None:
        minimum_duration = 0.0
    else:
        try:
            minimum_duration = float(minimum_duration)
        except (TypeError, ValueError):
            return preferred_clip_duration
    minimum_duration = min(
        preferred_clip_duration,
        max(0.0, minimum_duration),
    )
    max_overlap = min(
        _DEFAULT_CROSSFADE_DURATION,
        max(0.0, previous_clip_duration) / 2,
    )
    candidates = []
    for cue_end_time in cue_end_times:
        try:
            cue_end_time = float(cue_end_time)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cue_end_time):
            continue

        short_duration = 2 * (cue_end_time - timeline_duration)
        if (
            minimum_duration <= short_duration <= preferred_clip_duration
            and short_duration <= 2 * max_overlap
        ):
            candidates.append((cue_end_time, short_duration))

        long_duration = cue_end_time - timeline_duration + max_overlap
        if (
            minimum_duration <= long_duration <= preferred_clip_duration
            and long_duration >= 2 * max_overlap
        ):
            candidates.append((cue_end_time, long_duration))

    if not candidates:
        return preferred_clip_duration
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _video_clip_matches_target_resolution(
    video_clip,
    target_width: int,
    target_height: int,
) -> bool:
    """Require the source clip to have the requested frame before stream copy."""
    try:
        width, height = (int(value) for value in getattr(video_clip, "size", ()))
    except (TypeError, ValueError):
        return False
    return (width, height) == (int(target_width), int(target_height))


def subtitle_colors_are_indistinguishable(params: VideoParams) -> bool:
    """判断字幕文字和背景是否同色，提醒用户可能无法看清字幕。"""
    if not params.subtitle_enabled or not params.text_background_color:
        return False

    def normalize_color(value):
        if isinstance(value, bool):
            return "#000000" if value else ""
        return str(value or "").strip().lower()

    text_color = normalize_color(params.text_fore_color)
    background_color = normalize_color(params.text_background_color)
    return bool(text_color and text_color == background_color)


@lru_cache(maxsize=64)
def _subtitle_font_supports_sample(font_path: str, sample: str) -> bool:
    """检查字体是否包含样本文字需要的字形，并缓存重复检查结果。"""
    try:
        font = ImageFont.truetype(font_path, 30)
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (
            missing_mask.size,
            missing_mask.getbbox(),
            bytes(missing_mask),
        )
        for char in sample:
            char_mask = font.getmask(char)
            char_signature = (
                char_mask.size,
                char_mask.getbbox(),
                bytes(char_mask),
            )
            if char_mask.getbbox() is None or char_signature == missing_signature:
                return False
        return True
    except Exception as e:
        # 字体探测失败不应阻止用户生成；保留日志供环境兼容问题排查。
        logger.warning(f"failed to inspect subtitle font glyphs: {font_path}, {e}")
        return True


def subtitle_font_supports_text(font_path: str, text: str) -> bool:
    """检查字体能否绘制文本中的字母和数字，忽略空白及标点符号。"""
    sample = "".join(
        dict.fromkeys(
            char
            for char in str(text or "")
            if unicodedata.category(char)[0] in {"L", "N"}
        )
    )[:64]
    if not sample:
        return True
    return _subtitle_font_supports_sample(font_path, sample)


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    video_aspect: VideoAspect | None = None,
    return_encoder_result: bool = False,
    prefer_ffmpeg_srt_subtitles: bool = True,
    bgm_file_override: str | None = None,
):
    aspect = video_aspect or VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file) or "."
    ass_subtitle_path = ""
    ffmpeg_srt_subtitle_path = ""
    srt_fallback_subtitle_path = ""
    moviepy_subtitle_path = subtitle_path
    moviepy_output_file = output_file
    if subtitle_path and subtitle_path.lower().endswith(".ass"):
        ass_subtitle_path = subtitle_path
        moviepy_subtitle_path = ""
        candidate_srt_path = os.path.splitext(subtitle_path)[0] + ".srt"
        if os.path.exists(candidate_srt_path):
            srt_fallback_subtitle_path = candidate_srt_path
        output_root, output_ext = os.path.splitext(output_file)
        moviepy_output_file = f"{output_root}.nosub{output_ext or '.mp4'}"

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    if (
        prefer_ffmpeg_srt_subtitles
        and moviepy_subtitle_path
        and moviepy_subtitle_path.lower().endswith(".srt")
        and _srt_subtitle_ffmpeg_supported(params)
    ):
        ffmpeg_srt_subtitle_path = moviepy_subtitle_path
        moviepy_subtitle_path = ""
        output_root, output_ext = os.path.splitext(output_file)
        moviepy_output_file = f"{output_root}.nosub{output_ext or '.mp4'}"

    def resolve_subtitle_background_color():
        # 兼容历史参数：API 里 `text_background_color` 既可能是布尔值，
        # 也可能是实际颜色字符串。统一在这里归一化，避免把 True/False
        # 直接传给 TextClip 后出现不可预期的渲染结果。
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )
        has_subtitle_background = bool(bg_color)
        pad_x = int(params.font_size * 0.6) if has_subtitle_background else 0
        # 字幕背景需要给文字左右留出明确内边距。先从可用宽度中扣除
        # padding 再换行，避免长英文或大字号刚好撑满 90% 视频宽度后，
        # 文字贴到背景框边缘，看起来像被裁切。普通矩形背景和圆角背景
        # 都走这条逻辑；无背景字幕则保持原有最大宽度。
        text_max_width = max(1, int(max_width) - 2 * pad_x)
        wrapped_txt, txt_height = wrap_text(
            phrase,
            max_width=text_max_width,
            font=font_path,
            fontsize=params.font_size,
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        text_clip_margin_y = max(
            int(params.font_size * 0.3), int(params.stroke_width * 2)
        )
        # MoviePy 在 `method=label` 下会自动收缩文本框高度，遇到多行字幕、
        # 描边或背景色时，容易把最后一行的下半部分裁掉。这里显式传入
        # 一个更保守的高度，把行间距和额外上下留白一并算进去，保证字幕
        # 背景框与文字本身都能完整渲染出来。
        clip_h = int(txt_height + vertical_padding + (interline * line_count))

        if rounded_bg_enabled:
            # 圆角背景需要贴合文字宽度，而不是沿用 90% 视频宽度。这里先用
            # PIL 测量最长一行文字，再加水平内边距，避免短字幕出现过宽底板。
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            clip_h = max(clip_h, text_clip.h)
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            text_position = _get_visible_center_position(text_clip, box_w, clip_h)
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=(box_w, clip_h),
            )
        elif bg_color:
            size = (
                int(max_width),
                clip_h,
            )
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(int(max_width), None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            size = (size[0], max(size[1], text_clip.h))
            bg_clip = _rounded_subtitle_background_clip(
                width=size[0],
                height=size[1],
                color=bg_color,
                alpha=255,
                radius=0,
            )
            text_position = _get_visible_center_position(text_clip, size[0], size[1])
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=size,
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(
                ("center", _subtitle_bottom_y(video_height, _clip.h, aspect))
            )
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(
                ("center", _subtitle_y_within_frame(video_height, _clip.h, video_height * 0.05))
            )
        elif params.subtitle_position == "custom":
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            _clip = _clip.with_position(
                ("center", _subtitle_y_within_frame(video_height, _clip.h, custom_y))
            )
        else:  # center
            _clip = _clip.with_position(
                (
                    "center",
                    _subtitle_y_within_frame(
                        video_height,
                        _clip.h,
                        (video_height - _clip.h) / 2,
                    ),
                )
            )
        return _clip

    configured_codec = _get_configured_video_codec()
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    if not bgm_enabled and params.bgm_type:
        logger.info(
            "skipping background music because volume is not positive: "
            f"type={params.bgm_type}, volume={params.bgm_volume}"
        )
    bgm_file = ""
    if bgm_enabled:
        bgm_file = (
            bgm_file_override
            if bgm_file_override is not None
            else get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)
        )
    bgm_mix_succeeded = True

    def encoder_result(used_codec: str) -> dict:
        result = _video_encoder_result(configured_codec, used_codec)
        result["bgm_mix_succeeded"] = bgm_mix_succeeded
        return result
    try:
        # ASS and visually compatible classic SRT subtitles are burned in a
        # later FFmpeg pass. MoviePy remains the fallback for complex SRT styles.
        voice_volume = float(params.voice_volume)
        use_fast_audio_mux = (
            not moviepy_subtitle_path
            and math.isfinite(voice_volume)
        )
    except (TypeError, ValueError):
        voice_volume = 1.0
        use_fast_audio_mux = False
    used_codec = ""
    source_video_clip = None
    video_clip = None
    voice_source_clip = None
    bgm_source_clip = None
    composite_audio_clip = None
    subtitle_clip = None
    text_clips = []
    try:
        source_video_clip = _open_video_clip_quietly(video_path)
        video_clip = source_video_clip
        voice_source_clip = AudioFileClip(audio_path)
        audio_clip = voice_source_clip
        try:
            audio_duration = float(getattr(audio_clip, "duration", 0) or 0)
            video_duration = float(getattr(video_clip, "duration", 0) or 0)
        except (TypeError, ValueError):
            audio_duration = 0
            video_duration = 0
        fast_audio_muxed = False
        fast_audio_mux_candidate = use_fast_audio_mux and 0 < audio_duration <= video_duration
        if fast_audio_mux_candidate and not _video_clip_matches_target_resolution(
            video_clip,
            video_width,
            video_height,
        ):
            logger.debug(
                "ffmpeg audio mux fast path skipped because source resolution does not match the requested output"
            )
        elif fast_audio_mux_candidate:
            if bgm_file:
                fast_audio_muxed = _fast_mux_video_with_audio_and_bgm(
                    video_path=video_path,
                    audio_path=audio_path,
                    bgm_file=bgm_file,
                    output_file=moviepy_output_file,
                    video_duration=video_duration,
                    audio_bitrate=_get_configured_audio_bitrate(),
                    bgm_volume=params.bgm_volume,
                    voice_volume=voice_volume,
                )
            else:
                fast_audio_muxed = _fast_mux_video_with_audio(
                    video_path=video_path,
                    audio_path=audio_path,
                    output_file=moviepy_output_file,
                    video_duration=video_duration,
                    audio_bitrate=_get_configured_audio_bitrate(),
                    voice_volume=voice_volume,
                )
        if fast_audio_muxed:
            used_codec = _get_effective_video_codec(configured_codec)
            if not ass_subtitle_path and not ffmpeg_srt_subtitle_path:
                if return_encoder_result:
                    return encoder_result(used_codec)
                return bgm_mix_succeeded

        if not fast_audio_muxed:
            audio_clip = audio_clip.with_effects(
                [afx.MultiplyVolume(params.voice_volume)]
            )

        def make_textclip(text):
            return TextClip(
                text=text,
                font=font_path,
                font_size=params.font_size,
            )

        if moviepy_subtitle_path and os.path.exists(moviepy_subtitle_path):
            subtitle_clip = SubtitlesClip(
                subtitles=moviepy_subtitle_path,
                encoding="utf-8",
                make_textclip=make_textclip,
            )
            for item in subtitle_clip.subtitles:
                clip = create_text_clip(subtitle_item=item)
                text_clips.append(clip)
            video_clip = CompositeVideoClip([video_clip, *text_clips])

        if not fast_audio_muxed and bgm_file:
            try:
                bgm_effects = [
                    afx.MultiplyVolume(
                        _narration_ducked_bgm_volume(params.bgm_volume)
                    ),
                    afx.AudioFadeOut(3),
                ]
                if bgm_file_override is None:
                    bgm_effects.append(afx.AudioLoop(duration=video_clip.duration))
                bgm_source_clip = AudioFileClip(bgm_file)
                bgm_clip = bgm_source_clip.with_effects(bgm_effects)
                composite_audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
                audio_clip = composite_audio_clip
            except Exception:
                bgm_mix_succeeded = False
                logger.exception(
                    f"failed to mix background music: type={params.bgm_type}, "
                    f"file={bgm_file}"
                )

        if not fast_audio_muxed:
            video_clip = video_clip.with_audio(audio_clip)
        # 显式沿用输入音频的采样率；如果取不到，再回退到 MoviePy 默认的 44100Hz。
        # 这样可以减少不同运行环境，尤其是 Docker 环境中再次重采样带来的音质波动。
        if not fast_audio_muxed:
            output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
            output_audio_bitrate = _get_configured_audio_bitrate()
            used_codec = _write_videofile_with_codec_fallback(
                video_clip,
                output_file=moviepy_output_file,
                codec=configured_codec,
                audio_codec=audio_codec,
                audio_fps=output_audio_fps,
                audio_bitrate=output_audio_bitrate,
                temp_audiofile_path=_get_temp_audio_dir(output_dir),
                threads=params.n_threads or 2,
                logger=None,
                fps=_get_configured_video_fps(),
            )
            if not _limit_rendered_audio_peaks_with_ffmpeg(
                moviepy_output_file,
                audio_bitrate=output_audio_bitrate,
            ):
                logger.debug("kept MoviePy audio output without post-render limiting")
    finally:
        closed_clip_ids = set()
        for managed_clip in (
            video_clip,
            source_video_clip,
            composite_audio_clip,
            bgm_source_clip,
            voice_source_clip,
            subtitle_clip,
            *text_clips,
        ):
            if (
                managed_clip is not None
                and id(managed_clip) not in closed_clip_ids
            ):
                close_clip(managed_clip)
                closed_clip_ids.add(id(managed_clip))

    if ass_subtitle_path:
        burned_codec = _burn_ass_subtitles_with_ffmpeg(
            input_file=moviepy_output_file,
            subtitle_file=ass_subtitle_path,
            output_file=output_file,
            threads=params.n_threads,
        )
        if burned_codec:
            if moviepy_output_file != output_file and os.path.exists(moviepy_output_file):
                os.remove(moviepy_output_file)
            if return_encoder_result:
                return encoder_result(burned_codec)
            return bgm_mix_succeeded

        if srt_fallback_subtitle_path:
            logger.warning("ASS subtitle burn-in failed, fallback to SRT subtitles")
            if moviepy_output_file != output_file and os.path.exists(moviepy_output_file):
                os.remove(moviepy_output_file)
            return generate_video(
                video_path=video_path,
                audio_path=audio_path,
                subtitle_path=srt_fallback_subtitle_path,
                output_file=output_file,
                params=params,
                video_aspect=video_aspect,
                return_encoder_result=return_encoder_result,
                prefer_ffmpeg_srt_subtitles=False,
                bgm_file_override=bgm_file_override,
            )

        logger.warning("ASS subtitle burn-in failed, keeping video without subtitles")
        if moviepy_output_file != output_file and os.path.exists(moviepy_output_file):
            os.replace(moviepy_output_file, output_file)

    if ffmpeg_srt_subtitle_path:
        burned_codec = _burn_srt_subtitles_with_ffmpeg(
            input_file=moviepy_output_file,
            subtitle_file=ffmpeg_srt_subtitle_path,
            output_file=output_file,
            params=params,
            video_width=video_width,
            video_height=video_height,
            video_aspect=aspect,
            font_path=font_path,
            threads=params.n_threads,
        )
        if burned_codec:
            if moviepy_output_file != output_file and os.path.exists(moviepy_output_file):
                os.remove(moviepy_output_file)
            if return_encoder_result:
                return encoder_result(burned_codec)
            return bgm_mix_succeeded

        logger.warning("SRT subtitle burn-in failed, fallback to MoviePy subtitles")
        if moviepy_output_file != output_file and os.path.exists(moviepy_output_file):
            os.remove(moviepy_output_file)
        return generate_video(
            video_path=video_path,
            audio_path=audio_path,
            subtitle_path=ffmpeg_srt_subtitle_path,
            output_file=output_file,
            params=params,
            video_aspect=video_aspect,
            return_encoder_result=return_encoder_result,
            prefer_ffmpeg_srt_subtitles=False,
            bgm_file_override=bgm_file_override,
        )

    if return_encoder_result:
        return encoder_result(used_codec)
    return bgm_mix_succeeded


def preprocess_video(
    materials: List[MaterialInfo],
    clip_duration=4,
    video_aspect: VideoAspect | str | None = None,
):
    # WebUI 在某些二次生成场景下可能传入空素材列表，这里直接返回空结果，避免抛出 NoneType 异常。
    if not materials:
        return []

    # 仅返回通过预处理校验的素材，避免低分辨率图片继续进入后续的视频合成流程。
    valid_materials = []
    seen_material_paths = set()
    target_image_width = None
    target_image_height = None
    if video_aspect is not None:
        try:
            target_image_width, target_image_height = VideoAspect(
                video_aspect
            ).to_resolution()
        except (TypeError, ValueError):
            target_image_width = None
            target_image_height = None
    local_videos_dir = utils.storage_dir("local_videos", create=True)

    for material in materials:
        if not material.url:
            continue

        is_openmontage_output = openmontage_materials.is_openmontage_output_path(
            material.url
        )
        try:
            material_source_path = file_security.resolve_path_within_directory(
                local_videos_dir, material.url
            )
        except ValueError as exc:
            # local video_source 的素材路径来自 API 参数，必须限制在专用素材目录。
            # 允许用户传文件名，也兼容历史返回的绝对路径，但不允许逃逸到系统
            # 其他目录，避免任意文件读取或通过 MoviePy 探测本地敏感文件。
            if not is_openmontage_output:
                logger.warning(
                    f"skip unsafe local material: {material.url}, "
                    f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
                )
                continue
            material_source_path = os.path.realpath(material.url)
            logger.info(f"using bundled OpenMontage material: {material_source_path}")

        if is_openmontage_output and video_aspect is not None:
            output_report = openmontage_materials.validate_openmontage_output(
                material_source_path,
                video_aspect=video_aspect,
            )
            if not isinstance(output_report, dict) or not output_report.get("valid"):
                issue_codes = (
                    ", ".join(output_report.get("issues", []))
                    if isinstance(output_report, dict)
                    else "validation_unavailable"
                )
                logger.warning(
                    "skip OpenMontage material without a native {} render: {}",
                    getattr(video_aspect, "value", video_aspect),
                    issue_codes,
                )
                continue

        material_source_key = _source_file_key(material_source_path)
        if material_source_key in seen_material_paths:
            continue
        seen_material_paths.add(material_source_key)

        ext = utils.parse_extension(material_source_path)
        clip = None
        image_size = None
        image_focal_x_ratio = None
        image_focal_y_ratio = None
        try:
            # 图片素材直接按图片方式读取，避免先走 VideoFileClip 误判后触发不稳定的回退分支。
            if ext in const.FILE_TYPE_IMAGES:
                with Image.open(material_source_path) as image:
                    image_size = image.size
                    image_focal_x_ratio, image_focal_y_ratio = _image_focal_ratios(
                        image
                    )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception as exc:
            # 非标准扩展名或探测失败时再回退到图片模式，兼容历史上直接传本地图片路径的情况。
            logger.warning(
                f"failed to open local material as video, trying image fallback: "
                f"{material_source_path}, error: {str(exc)}"
            )
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            if image_size is not None:
                width, height = image_size
            else:
                width = clip.size[0]
                height = clip.size[1]
            if not is_material_resolution_acceptable(width, height):
                logger.warning(
                    f"low resolution material: {width}x{height}, minimum "
                    f"{_MIN_MATERIAL_DIMENSION}x{_MIN_MATERIAL_DIMENSION} required "
                    f"(tolerance {_MIN_DIMENSION_TOLERANCE}px)"
                )
                # 探测到低分辨率素材后立即关闭资源，并且不要把该素材返回给后续流程。
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image: {material_source_path}")
                # 探测尺寸时已经打开过一次素材，这里先释放探测句柄，再重新创建用于导出的图片 clip。
                close_clip(clip)
                clip = None
                final_clip = None
                video_file = f"{material_source_path}.mp4"
                if _fast_render_image_with_ffmpeg(
                    input_file=material_source_path,
                    output_file=video_file,
                    width=width,
                    height=height,
                    duration=clip_duration,
                    focal_x_ratio=image_focal_x_ratio,
                    focal_y_ratio=image_focal_y_ratio,
                    target_width=target_image_width,
                    target_height=target_image_height,
                ):
                    material.url = video_file
                    logger.success(f"image processed: {video_file}")
                    valid_materials.append(material)
                    continue
                try:
                    # Create an image clip and set its duration to 3 seconds
                    clip = (
                        ImageClip(material_source_path)
                        .with_duration(clip_duration)
                        .with_position("center")
                    )
                    # Apply a zoom effect using the resize method.
                    # A lambda function is used to make the zoom effect dynamic over time.
                    # The zoom effect starts from the original size and gradually scales up to 120%.
                    # t represents the current time, and clip.duration is the total duration of the clip (3 seconds).
                    # Note: 1 represents 100% size, so 1.2 represents 120% size.
                    zoom_clip = clip.resized(
                        lambda t: _image_zoom_scale(t, clip.duration)
                    )

                    # Optionally, create a composite video clip containing the zoomed clip.
                    # This is useful when you want to add other elements to the video.
                    final_clip = CompositeVideoClip(
                        [zoom_clip],
                        size=_even_video_size(clip.size),
                        bg_color=(0, 0, 0),
                    )

                    # Output the video to a file.
                    _write_videofile_with_codec_fallback(
                        final_clip,
                        output_file=video_file,
                        codec=_get_configured_video_codec(),
                        fps=_get_configured_video_fps(),
                        logger=None,
                    )
                    material.url = video_file
                    logger.success(f"image processed: {video_file}")
                finally:
                    close_clip(clip)
                    close_clip(final_clip)
                    clip = None
                    final_clip = None
            else:
                # 普通视频素材只需要读取尺寸做校验，校验完成后立即释放句柄即可。
                close_clip(clip)
                # Update url to the resolved absolute path so that downstream
                # stages (combine_videos) can open the file without re-resolving.
                material.url = material_source_path
        except Exception as exc:
            logger.warning(
                f"failed to validate local material, closing clip: "
                f"{material_source_path}, error: {str(exc)}"
            )
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
