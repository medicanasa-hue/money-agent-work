import html
import json
import math
import os
import subprocess
import tempfile

import numpy as np
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image, ImageDraw

from app.models.schema import VideoAspect
from app.services import state as sm
from app.services.video import get_subtitle_bottom_safe_margin_ratio
from app.utils import utils, video_quality
from app.services.voice.naming import is_no_voice


_FRAME_SAMPLE_RATIOS = (0.2, 0.5, 0.8)
_RENDER_QUALITY_FRAME_SAMPLE_INTERVAL_SECONDS = 0.5
_NEAR_BLACK_LUMA_THRESHOLD = 25.5
_NEAR_BLACK_DARK_PIXEL_RATIO = 0.98
_CAPTION_OVER_BLACK_DARK_PIXEL_RATIO = 0.85
_CAPTION_OVER_BLACK_MIN_BRIGHT_PIXEL_RATIO = 0.002
_CAPTION_OVER_BLACK_MAX_BRIGHT_PIXEL_RATIO = 0.2
_CAPTION_OVER_BLACK_BRIGHT_LUMA_THRESHOLD = 160.0
_DURATION_MISMATCH_TOLERANCE_SECONDS = 1.0
_AUDIO_SAMPLE_WINDOW_SECONDS = 0.25
_AUDIO_SAMPLE_COUNT_PER_WINDOW = 25
# This is a non-blocking warning threshold (roughly -50 dBFS), intended to
# catch rendered tracks that exist but contain no usable narration.
_NEAR_SILENT_AUDIO_PEAK_THRESHOLD = 0.003
_ENCODING_FPS_TOLERANCE = 0.05
_KEYFRAME_GAP_TOLERANCE_SECONDS = 0.15
_VMAF_CAPABILITY_TIMEOUT_SECONDS = 15
_VMAF_MEASUREMENT_TIMEOUT_SECONDS = 120
_BLACKDETECT_TIMEOUT_SECONDS = 30
_BLACKDETECT_MIN_DURATION_SECONDS = 0.15
_BLACKDETECT_PIXEL_THRESHOLD = 0.10
_FREEZEDETECT_TIMEOUT_SECONDS = 30
_FREEZEDETECT_MIN_DURATION_SECONDS = 1.5
_FREEZEDETECT_NOISE_TOLERANCE = 0.001
_SUBTITLE_SAFE_ZONE_OVERLAY = (255, 176, 0, 96)
_SUBTITLE_SAFE_ZONE_BOUNDARY = (255, 224, 128, 255)
_COLOR_WARMTH_SPREAD_WARNING_THRESHOLD = 0.25
_COLOR_SATURATION_SPREAD_WARNING_THRESHOLD = 0.25


def _expected_resolution(video_aspect) -> tuple[int, int] | None:
    try:
        return VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        return None


def _as_non_negative_float(value) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _render_quality_frame_sample_times(duration: float) -> tuple[float, ...]:
    duration = _as_non_negative_float(duration)
    if duration <= 0:
        return ()
    sample_count = max(
        1,
        int(math.ceil(duration / _RENDER_QUALITY_FRAME_SAMPLE_INTERVAL_SECONDS)),
    )
    sample_interval = duration / sample_count
    return tuple(
        round(sample_interval * (sample_index + 0.5), 3)
        for sample_index in range(sample_count)
    )


def _is_near_black_frame(frame) -> bool:
    try:
        pixels = np.asarray(frame)
        if not pixels.size:
            return False
        if pixels.ndim >= 3 and pixels.shape[-1] >= 3:
            rgb = pixels[..., :3].astype(float)
            luma = (
                (rgb[..., 0] * 0.2126)
                + (rgb[..., 1] * 0.7152)
                + (rgb[..., 2] * 0.0722)
            )
        else:
            luma = pixels.astype(float)
        if not np.isfinite(luma).all():
            return False
        if float(luma.max()) <= 1.0:
            luma *= 255.0
        return bool(
            np.mean(luma <= _NEAR_BLACK_LUMA_THRESHOLD)
            >= _NEAR_BLACK_DARK_PIXEL_RATIO
        )
    except (TypeError, ValueError):
        try:
            return float(frame.mean()) <= _NEAR_BLACK_LUMA_THRESHOLD
        except (AttributeError, TypeError, ValueError):
            return False


def _frame_color_profile(frame) -> dict | None:
    """Return inexpensive color measurements from one sampled RGB frame."""
    try:
        pixels = np.asarray(frame)
        if pixels.ndim < 3 or pixels.shape[-1] < 3 or not pixels.size:
            return None
        rgb = pixels[..., :3].astype(float)
        if not np.isfinite(rgb).all():
            return None
        height, width = rgb.shape[:2]
        rgb = rgb[
            :: max(1, height // 64),
            :: max(1, width // 64),
        ]
        channel_means = rgb.mean(axis=(0, 1))
        red, _green, blue = (float(value) for value in channel_means)
        max_channel = rgb.max(axis=-1)
        min_channel = rgb.min(axis=-1)
        saturation = np.divide(
            max_channel - min_channel,
            max_channel,
            out=np.zeros_like(max_channel, dtype=float),
            where=max_channel > 0,
        )
        return {
            "warmth": (red - blue) / max(red + blue, 1e-6),
            "saturation": float(saturation.mean()),
        }
    except (TypeError, ValueError, IndexError):
        return None


def _frame_laplacian_variance(frame) -> float | None:
    """Return a cheap edge-detail proxy for one sampled frame."""
    try:
        pixels = np.asarray(frame)
        if pixels.ndim < 2 or not pixels.size:
            return None
        if pixels.ndim >= 3 and pixels.shape[-1] >= 3:
            rgb = pixels[..., :3].astype(float)
            luma = (
                (rgb[..., 0] * 0.2126)
                + (rgb[..., 1] * 0.7152)
                + (rgb[..., 2] * 0.0722)
            )
        else:
            luma = pixels.astype(float)
        if not np.isfinite(luma).all():
            return None
        if min(luma.shape[:2]) < 3:
            return None
        laplacian = (
            -4.0 * luma[1:-1, 1:-1]
            + luma[:-2, 1:-1]
            + luma[2:, 1:-1]
            + luma[1:-1, :-2]
            + luma[1:-1, 2:]
        )
        return float(np.var(laplacian))
    except (TypeError, ValueError, IndexError):
        return None


def build_visual_sharpness_report(frames) -> dict:
    """Summarize edge detail without deciding whether the video should fail."""
    measurements = []
    try:
        iterable_frames = list(frames or [])
    except TypeError:
        iterable_frames = []
    for frame in iterable_frames:
        score = _frame_laplacian_variance(frame)
        if score is not None:
            measurements.append(score)
    if not measurements:
        return {
            "sample_count": 0,
            "mean_laplacian_variance": None,
            "minimum_laplacian_variance": None,
            "maximum_laplacian_variance": None,
        }
    return {
        "sample_count": len(measurements),
        "mean_laplacian_variance": round(sum(measurements) / len(measurements), 4),
        "minimum_laplacian_variance": round(min(measurements), 4),
        "maximum_laplacian_variance": round(max(measurements), 4),
    }


def build_color_consistency_report(frames) -> dict:
    """Summarize color-character variation without modifying any video frames."""
    measurements = []
    try:
        iterable_frames = list(frames or [])
    except TypeError:
        iterable_frames = []
    for frame in iterable_frames:
        profile = _frame_color_profile(frame)
        if profile is not None:
            measurements.append(profile)
    if not measurements:
        return {
            "sample_count": 0,
            "status": "unavailable",
            "mean_warmth": None,
            "warmth_spread": None,
            "mean_saturation": None,
            "saturation_spread": None,
        }

    warmth_values = [profile["warmth"] for profile in measurements]
    saturation_values = [profile["saturation"] for profile in measurements]
    warmth_spread = max(warmth_values) - min(warmth_values)
    saturation_spread = max(saturation_values) - min(saturation_values)
    status = (
        "mixed"
        if (
            warmth_spread > _COLOR_WARMTH_SPREAD_WARNING_THRESHOLD
            or saturation_spread > _COLOR_SATURATION_SPREAD_WARNING_THRESHOLD
        )
        else "consistent"
    )
    return {
        "sample_count": len(measurements),
        "status": status,
        "mean_warmth": round(sum(warmth_values) / len(warmth_values), 4),
        "warmth_spread": round(warmth_spread, 4),
        "mean_saturation": round(sum(saturation_values) / len(saturation_values), 4),
        "saturation_spread": round(saturation_spread, 4),
    }


def _is_caption_over_black_frame(frame) -> bool:
    """Detect a mostly black frame whose limited bright area resembles an overlay."""
    try:
        pixels = np.asarray(frame)
        if not pixels.size:
            return False
        if pixels.ndim >= 3 and pixels.shape[-1] >= 3:
            pixels = pixels[..., :3].astype(float)
            luma = (
                (pixels[..., 0] * 0.2126)
                + (pixels[..., 1] * 0.7152)
                + (pixels[..., 2] * 0.0722)
            )
        else:
            luma = pixels.astype(float)
    except (TypeError, ValueError):
        return False

    luma = luma[np.isfinite(luma)]
    if not luma.size:
        return False
    if float(luma.max()) <= 1.0:
        luma *= 255.0

    dark_pixel_ratio = float(np.mean(luma <= _NEAR_BLACK_LUMA_THRESHOLD))
    bright_pixel_ratio = float(
        np.mean(luma >= _CAPTION_OVER_BLACK_BRIGHT_LUMA_THRESHOLD)
    )
    return (
        dark_pixel_ratio >= _CAPTION_OVER_BLACK_DARK_PIXEL_RATIO
        and _CAPTION_OVER_BLACK_MIN_BRIGHT_PIXEL_RATIO
        <= bright_pixel_ratio
        <= _CAPTION_OVER_BLACK_MAX_BRIGHT_PIXEL_RATIO
    )


def _sampled_audio_peak(audio, video_duration: float) -> float | None:
    get_frame = getattr(audio, "get_frame", None)
    to_soundarray = getattr(audio, "to_soundarray", None)
    if not callable(get_frame) and not callable(to_soundarray):
        return None

    audio_duration = _as_non_negative_float(getattr(audio, "duration", 0))
    sample_duration = min(video_duration, audio_duration) or video_duration
    if sample_duration <= 0:
        return None

    window_duration = min(_AUDIO_SAMPLE_WINDOW_SECONDS, sample_duration)
    sample_times = []
    for sample_ratio in _FRAME_SAMPLE_RATIOS:
        sample_center = sample_duration * sample_ratio
        window_start = min(
            max(0.0, sample_center - (window_duration / 2)),
            sample_duration - window_duration,
        )
        sample_times.extend(
            np.linspace(
                window_start,
                window_start + window_duration,
                _AUDIO_SAMPLE_COUNT_PER_WINDOW,
            )
        )

    try:
        if callable(get_frame):
            # MoviePy's vector ``tt`` reader can return a constant near-zero
            # value for otherwise audible AAC/WAV streams. Scalar reads use
            # the same decoder without that false-silence behaviour.
            samples = np.asarray(
                [get_frame(float(sample_time)) for sample_time in sample_times],
                dtype=float,
            )
        else:
            samples = np.asarray(
                to_soundarray(tt=np.asarray(sample_times)),
                dtype=float,
            )
    except Exception:
        return None
    magnitudes = np.abs(samples)
    finite_magnitudes = magnitudes[np.isfinite(magnitudes)]
    if not finite_magnitudes.size:
        return None
    return float(finite_magnitudes.max())


def _get_ffprobe_binary() -> str | None:
    return utils.get_ffprobe_binary()


def _detect_sustained_near_black_segments(video_path: str) -> list[tuple[float, float]] | None:
    return video_quality.detect_sustained_near_black_segments(
        video_path,
        min_duration_seconds=_BLACKDETECT_MIN_DURATION_SECONDS,
        pixel_threshold=_BLACKDETECT_PIXEL_THRESHOLD,
        timeout_seconds=_BLACKDETECT_TIMEOUT_SECONDS,
    )


def _detect_sustained_frozen_segments(video_path: str) -> list[tuple[float, float]] | None:
    return video_quality.detect_sustained_frozen_segments(
        video_path,
        min_duration_seconds=_FREEZEDETECT_MIN_DURATION_SECONDS,
        noise_tolerance=_FREEZEDETECT_NOISE_TOLERANCE,
        timeout_seconds=_FREEZEDETECT_TIMEOUT_SECONDS,
    )


def _as_finite_float(value) -> float | None:
    try:
        parsed_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed_value):
        return None
    return parsed_value


def _parse_frame_rate(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", maxsplit=1)
            parsed_denominator = float(denominator)
            if not parsed_denominator:
                return None
            frame_rate = float(numerator) / parsed_denominator
        else:
            frame_rate = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return frame_rate if math.isfinite(frame_rate) and frame_rate > 0 else None


def _normalized_encoding_value(value) -> str | None:
    normalized_value = str(value or "").strip().lower()
    return normalized_value or None


def _probe_video_encoding(video_path: str) -> dict | None:
    ffprobe_binary = _get_ffprobe_binary()
    if not ffprobe_binary:
        return None

    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_streams",
        "-show_frames",
        "-show_entries",
        (
            "stream=codec_name,pix_fmt,avg_frame_rate,r_frame_rate,"
            "color_space,color_transfer,color_primaries,color_range,"
            "sample_aspect_ratio,duration:"
            "frame=best_effort_timestamp_time"
        ),
        "-of",
        "json",
        video_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError):
        return None
    streams = payload.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return None

    stream = streams[0]
    frame_rate = _parse_frame_rate(
        stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    )
    keyframe_times = []
    for frame in payload.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        timestamp = _as_finite_float(frame.get("best_effort_timestamp_time"))
        if timestamp is not None and timestamp >= 0:
            keyframe_times.append(timestamp)
    keyframe_times.sort()
    keyframe_gaps = [
        later - earlier
        for earlier, later in zip(keyframe_times, keyframe_times[1:])
        if later >= earlier
    ]

    return {
        "codec": _normalized_encoding_value(stream.get("codec_name")),
        "pixel_format": _normalized_encoding_value(stream.get("pix_fmt")),
        "fps": frame_rate,
        "max_keyframe_gap_seconds": max(keyframe_gaps) if keyframe_gaps else None,
        "color_space": _normalized_encoding_value(stream.get("color_space")),
        "color_transfer": _normalized_encoding_value(stream.get("color_transfer")),
        "color_primaries": _normalized_encoding_value(stream.get("color_primaries")),
        "color_range": _normalized_encoding_value(stream.get("color_range")),
        "sample_aspect_ratio": _normalized_encoding_value(
            stream.get("sample_aspect_ratio")
        ),
        "duration": _as_finite_float(stream.get("duration")),
    }


def is_vmaf_available() -> bool:
    """Return whether the configured FFmpeg build exposes the libvmaf filter."""
    try:
        result = subprocess.run(
            [utils.get_ffmpeg_binary(), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_VMAF_CAPABILITY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any("libvmaf" in line.split() for line in (result.stdout or "").splitlines())


def _escape_ffmpeg_filter_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "/")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
    )


def _build_vmaf_filter_graph(log_path: str, frame_subsample: int) -> str:
    escaped_log_path = _escape_ffmpeg_filter_value(log_path)
    return (
        "[0:v]setpts=PTS-STARTPTS[candidate];"
        "[1:v]setpts=PTS-STARTPTS[reference];"
        "[candidate][reference]"
        f"libvmaf=log_path='{escaped_log_path}':log_fmt=json:"
        f"n_subsample={frame_subsample}:eof_action=endall[vmaf]"
    )


def _load_json_file(file_path: str) -> dict | None:
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_vmaf_log(log_path: str) -> dict | None:
    payload = _load_json_file(log_path)
    if not payload:
        return None
    pooled_metrics = payload.get("pooled_metrics") or {}
    vmaf_metrics = pooled_metrics.get("vmaf") if isinstance(pooled_metrics, dict) else None
    if not isinstance(vmaf_metrics, dict):
        return None

    mean_score = _as_finite_float(vmaf_metrics.get("mean"))
    if mean_score is None:
        return None
    frames = payload.get("frames") or []
    return {
        "mean": mean_score,
        "minimum": _as_finite_float(vmaf_metrics.get("min")),
        "maximum": _as_finite_float(vmaf_metrics.get("max")),
        "harmonic_mean": _as_finite_float(vmaf_metrics.get("harmonic_mean")),
        "sampled_frame_count": len(frames) if isinstance(frames, list) else 0,
    }


def calculate_vmaf(
    reference_video_path: str,
    candidate_video_path: str,
    *,
    frame_subsample: int = 1,
) -> dict | None:
    """Measure a candidate against a reference video without changing either file."""
    if isinstance(frame_subsample, bool):
        return None
    try:
        frame_subsample = int(frame_subsample)
    except (TypeError, ValueError):
        return None
    if frame_subsample < 1:
        return None

    reference_path = str(reference_video_path or "").strip()
    candidate_path = str(candidate_video_path or "").strip()
    if not (
        reference_path
        and candidate_path
        and os.path.isfile(reference_path)
        and os.path.isfile(candidate_path)
        and is_vmaf_available()
    ):
        return None

    log_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    log_path = log_file.name
    log_file.close()
    try:
        result = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-hide_banner",
                "-nostdin",
                "-i",
                candidate_path,
                "-i",
                reference_path,
                "-filter_complex",
                _build_vmaf_filter_graph(log_path, frame_subsample),
                "-map",
                "[vmaf]",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_VMAF_MEASUREMENT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        return _read_vmaf_log(log_path)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(log_path)
        except OSError:
            pass


def _normalize_gallery_sample_ratios(sample_ratios) -> tuple[float, ...]:
    if sample_ratios is None:
        return _FRAME_SAMPLE_RATIOS
    if isinstance(sample_ratios, (str, bytes)):
        return _FRAME_SAMPLE_RATIOS
    try:
        values = list(sample_ratios)
    except TypeError:
        return _FRAME_SAMPLE_RATIOS

    normalized_ratios = []
    for value in values:
        ratio = _as_finite_float(value)
        if ratio is None or ratio < 0 or ratio >= 1 or ratio in normalized_ratios:
            continue
        normalized_ratios.append(ratio)
    return tuple(normalized_ratios) or _FRAME_SAMPLE_RATIOS


def _safe_gallery_stem(video_path: str) -> str:
    stem = os.path.splitext(os.path.basename(video_path))[0]
    safe_stem = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in stem
    ).strip("-")
    return safe_stem or "video"


def _write_visual_regression_gallery_html(gallery_path: str, entries: list[dict]):
    html_parts = [
        "<!doctype html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Visual regression gallery</title>",
        (
            "<style>body{font-family:system-ui,sans-serif;margin:24px;background:#111;color:#eee;}"
            "section{margin:0 0 28px;padding:16px;background:#1b1b1b;border-radius:10px;}"
            "h1{margin-top:0;}h2{margin:0 0 4px;font-size:18px;}p{color:#aaa;}"
            ".frames{display:flex;gap:12px;flex-wrap:wrap;}figure{margin:0;width:260px;}"
            "img{display:block;width:100%;height:auto;background:#000;border-radius:6px;}"
            "figcaption{margin-top:6px;color:#aaa;font-size:13px;}</style></head><body>"
        ),
        "<h1>Visual regression gallery</h1>",
    ]
    for entry in entries:
        title = html.escape(entry["title"])
        html_parts.extend(
            [
                "<section>",
                f"<h2>{title}</h2>",
                (
                    f"<p>{entry['width']}×{entry['height']} · "
                    f"{entry['duration']:.2f}s</p><div class=\"frames\">"
                ),
            ]
        )
        for frame in entry["frames"]:
            source = html.escape(frame["file_name"])
            label = html.escape(frame["label"])
            html_parts.append(
                f"<figure><img src=\"{source}\" alt=\"{title} at {label}\">"
                f"<figcaption>{label}</figcaption></figure>"
            )
        html_parts.extend(["</div>", "</section>"])
    html_parts.append("</body></html>")
    with open(gallery_path, "w", encoding="utf-8") as file:
        file.write("\n".join(html_parts))


def build_visual_regression_gallery(
    video_paths,
    output_dir: str,
    *,
    sample_ratios=None,
) -> dict:
    """Save normalized video frames and a local HTML gallery for manual comparison."""
    gallery = {"html_path": None, "frame_paths": [], "video_count": 0}
    normalized_output_dir = str(output_dir or "").strip()
    if not normalized_output_dir:
        return gallery
    try:
        os.makedirs(normalized_output_dir, exist_ok=True)
    except OSError:
        return gallery

    if isinstance(video_paths, (str, bytes)):
        source_paths = [video_paths]
    else:
        try:
            source_paths = list(video_paths or [])
        except TypeError:
            source_paths = []
    ratios = _normalize_gallery_sample_ratios(sample_ratios)
    entries = []

    for index, video_path in enumerate(source_paths, start=1):
        normalized_path = str(video_path or "").strip()
        if not normalized_path or not os.path.isfile(normalized_path):
            continue

        clip = None
        try:
            clip = VideoFileClip(normalized_path)
            duration = _as_non_negative_float(getattr(clip, "duration", 0))
            if duration <= 0:
                continue
            width, height = (int(value) for value in clip.size)
            entry = {
                "title": os.path.basename(normalized_path),
                "duration": duration,
                "width": width,
                "height": height,
                "frames": [],
            }
            for ratio in ratios:
                sample_time = min(duration * ratio, max(0.0, duration - 0.001))
                file_name = (
                    f"{index:02d}-{_safe_gallery_stem(normalized_path)}-"
                    f"{int(round(ratio * 100)):02d}.jpg"
                )
                frame_path = os.path.join(normalized_output_dir, file_name)
                try:
                    clip.save_frame(frame_path, t=sample_time)
                except Exception:
                    continue
                if not os.path.isfile(frame_path):
                    continue
                gallery["frame_paths"].append(frame_path)
                entry["frames"].append(
                    {"file_name": file_name, "label": f"{int(round(ratio * 100))}%"}
                )
            if entry["frames"]:
                entries.append(entry)
                gallery["video_count"] += 1
        except Exception:
            continue
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception:
                    pass

    gallery_path = os.path.join(normalized_output_dir, "index.html")
    try:
        _write_visual_regression_gallery_html(gallery_path, entries)
    except OSError:
        return gallery
    gallery["html_path"] = gallery_path
    return gallery


def _review_task_directory(task_id: str) -> str:
    normalized_task_id = str(task_id or "").strip()
    if (
        not normalized_task_id
        or normalized_task_id in {".", ".."}
        or os.path.dirname(normalized_task_id)
    ):
        return ""

    task_root = os.path.realpath(os.path.join(utils.storage_dir(), "tasks"))
    task_directory = os.path.realpath(os.path.join(task_root, normalized_task_id))
    try:
        if os.path.commonpath([task_root, task_directory]) != task_root:
            return ""
    except ValueError:
        return ""
    return task_directory if os.path.isdir(task_directory) else ""


def _video_aspect_from_resolution(resolution) -> VideoAspect | None:
    if not isinstance(resolution, (list, tuple)) or len(resolution) != 2:
        return None
    try:
        dimensions = (int(resolution[0]), int(resolution[1]))
    except (TypeError, ValueError):
        return None
    for video_aspect in VideoAspect:
        if video_aspect.to_resolution() == dimensions:
            return video_aspect
    return None


def _saved_review_params(task_directory: str) -> dict:
    try:
        with open(os.path.join(task_directory, "script.json"), encoding="utf-8") as file:
            script_data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(script_data, dict):
        return {}
    params = script_data.get("params")
    return params if isinstance(params, dict) else {}


def _expected_aspect_for_review_video(video_path: str, params: dict) -> VideoAspect | None:
    filename_stem = os.path.splitext(os.path.basename(video_path))[0]
    filename_aspect = filename_stem.rsplit("-", 1)[-1].replace("x", ":")
    try:
        return VideoAspect(filename_aspect)
    except ValueError:
        pass

    try:
        return VideoAspect(params.get("video_aspect"))
    except (AttributeError, TypeError, ValueError):
        return None


def _expected_review_duration(task_id: str) -> float | None:
    try:
        task = sm.state.get_task(task_id)
    except Exception:
        return None
    if not isinstance(task, dict):
        return None
    duration = _as_non_negative_float(task.get("audio_duration"))
    return duration if duration > 0 else None


def build_task_visual_review_package(task_id: str) -> dict:
    """Build a local, on-demand visual review bundle for one completed task."""
    normalized_task_id = str(task_id or "").strip()
    package = {
        "ok": False,
        "task_id": normalized_task_id,
        "video_paths": [],
        "gallery": {"html_path": None, "frame_paths": [], "video_count": 0},
        "quality_reports": [],
        "safe_zone_snapshots": [],
        "manifest_path": None,
        "error": None,
    }
    task_directory = _review_task_directory(normalized_task_id)
    if not task_directory:
        package["error"] = "no_final_videos"
        return package

    try:
        final_video_paths = sorted(
            (
                os.path.join(task_directory, name)
                for name in os.listdir(task_directory)
                if name.casefold().startswith("final-")
                and name.casefold().endswith(".mp4")
                and os.path.isfile(os.path.join(task_directory, name))
            ),
            key=lambda video_path: (os.path.basename(video_path).count("-"), video_path),
        )
    except OSError:
        package["error"] = "no_final_videos"
        return package
    if not final_video_paths:
        package["error"] = "no_final_videos"
        return package

    review_directory = os.path.join(task_directory, "visual-review")
    try:
        os.makedirs(review_directory, exist_ok=True)
    except OSError:
        package["error"] = "review_directory_unavailable"
        return package

    package["video_paths"] = final_video_paths
    saved_params = _saved_review_params(task_directory)
    expected_duration = _expected_review_duration(normalized_task_id)
    for index, video_path in enumerate(final_video_paths, start=1):
        expected_aspect = _expected_aspect_for_review_video(video_path, saved_params)
        try:
            inspection_kwargs = {
                "expected_aspect": expected_aspect,
                "expected_duration": expected_duration,
            }
            if (
                is_no_voice(saved_params.get("voice_name"))
                and not saved_params.get("custom_audio_file")
            ):
                inspection_kwargs["allow_silent_audio"] = True
            quality_report = inspect_rendered_video(
                video_path,
                **inspection_kwargs,
            )
        except Exception:
            quality_report = {
                "ok": False,
                "warnings": ["rendered video could not be inspected"],
            }
        if not isinstance(quality_report, dict):
            quality_report = {
                "ok": False,
                "warnings": ["rendered video could not be inspected"],
            }
        package["quality_reports"].append(
            {"video_path": video_path, **quality_report}
        )

        video_aspect = expected_aspect or _video_aspect_from_resolution(
            quality_report.get("resolution")
        )
        if video_aspect is None:
            package["safe_zone_snapshots"].append(
                {
                    "video_path": video_path,
                    "snapshot_path": None,
                    "reason": "unknown_aspect",
                }
            )
            continue
        try:
            snapshot = create_subtitle_safe_zone_snapshot(
                video_path,
                os.path.join(review_directory, f"subtitle-safe-zone-{index}.png"),
                video_aspect=video_aspect,
            )
        except Exception:
            snapshot = {"snapshot_path": None, "reason": "snapshot_unavailable"}
        package["safe_zone_snapshots"].append({"video_path": video_path, **snapshot})

    try:
        package["gallery"] = build_visual_regression_gallery(
            final_video_paths,
            os.path.join(review_directory, "frames"),
        )
    except Exception:
        package["gallery"] = {"html_path": None, "frame_paths": [], "video_count": 0}
    package["ok"] = bool(
        package["gallery"].get("html_path")
        or any(
            snapshot.get("snapshot_path")
            for snapshot in package["safe_zone_snapshots"]
            if isinstance(snapshot, dict)
        )
    )
    if not package["ok"]:
        package["error"] = "review_package_incomplete"

    manifest_path = os.path.join(review_directory, "manifest.json")
    package["manifest_path"] = manifest_path
    try:
        with open(manifest_path, "w", encoding="utf-8") as file:
            json.dump(package, file, ensure_ascii=False, indent=2)
    except OSError:
        package["ok"] = False
        package["error"] = "review_manifest_unavailable"
        package["manifest_path"] = None
        return package
    return package


def _as_snapshot_image(frame) -> Image.Image | None:
    pixels = np.asarray(frame)
    if pixels.ndim != 3 or pixels.shape[2] < 3:
        return None

    rgb_pixels = np.asarray(pixels[..., :3], dtype=float)
    if not np.isfinite(rgb_pixels).all():
        return None
    if rgb_pixels.size and rgb_pixels.max() <= 1.0:
        rgb_pixels *= 255.0
    return Image.fromarray(np.clip(rgb_pixels, 0, 255).astype(np.uint8))


def create_subtitle_safe_zone_snapshot(
    video_path: str,
    output_path: str,
    *,
    video_aspect=VideoAspect.portrait,
    sample_ratio: float = 0.5,
) -> dict:
    """Save one rendered frame with the actual subtitle bottom-safe area marked."""
    snapshot = {
        "snapshot_path": None,
        "sample_time": None,
        "safe_bottom_margin_ratio": get_subtitle_bottom_safe_margin_ratio(video_aspect),
        "safe_zone_top": None,
        "width": None,
        "height": None,
    }
    normalized_video_path = str(video_path or "").strip()
    normalized_output_path = str(output_path or "").strip()
    if (
        not normalized_video_path
        or not normalized_output_path
        or not os.path.isfile(normalized_video_path)
    ):
        return snapshot

    clip = None
    try:
        clip = VideoFileClip(normalized_video_path)
        duration = _as_non_negative_float(getattr(clip, "duration", 0))
        if duration <= 0:
            return snapshot
        normalized_ratio = _as_finite_float(sample_ratio)
        if normalized_ratio is None:
            normalized_ratio = 0.5
        normalized_ratio = min(max(normalized_ratio, 0.0), 0.999)
        sample_time = min(duration * normalized_ratio, max(0.0, duration - 0.001))
        image = _as_snapshot_image(clip.get_frame(sample_time))
        if image is None:
            return snapshot

        safe_zone_top = int(
            round(image.height * (1 - snapshot["safe_bottom_margin_ratio"]))
        )
        safe_zone_top = min(max(safe_zone_top, 0), image.height)
        drawer = ImageDraw.Draw(image, "RGBA")
        drawer.rectangle(
            (0, safe_zone_top, image.width - 1, image.height - 1),
            fill=_SUBTITLE_SAFE_ZONE_OVERLAY,
        )
        drawer.line(
            (0, safe_zone_top, image.width - 1, safe_zone_top),
            fill=_SUBTITLE_SAFE_ZONE_BOUNDARY,
            width=max(1, image.height // 720),
        )

        output_dir = os.path.dirname(normalized_output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        image.save(normalized_output_path)
        if not os.path.isfile(normalized_output_path):
            return snapshot
        snapshot.update(
            {
                "snapshot_path": normalized_output_path,
                "sample_time": round(sample_time, 3),
                "safe_zone_top": safe_zone_top,
                "width": image.width,
                "height": image.height,
            }
        )
        return snapshot
    except Exception:
        return snapshot
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def _encoding_contract_warnings(
    encoding: dict | None,
    expected_encoding: dict,
) -> list[str]:
    """Return encoding-only contract mismatches for a probed video stream."""
    if not isinstance(encoding, dict) or not encoding:
        return ["encoding contract could not be inspected"]

    warnings = []
    for key, warning in (
        ("codec", "video codec does not match the encoding contract"),
        ("pixel_format", "video pixel format does not match the encoding contract"),
        ("color_space", "video color space does not match the encoding contract"),
        ("color_transfer", "video color transfer does not match the encoding contract"),
        ("color_primaries", "video color primaries do not match the encoding contract"),
        ("color_range", "video color range does not match the encoding contract"),
        (
            "sample_aspect_ratio",
            "video sample aspect ratio does not match the encoding contract",
        ),
    ):
        expected_value = _normalized_encoding_value(expected_encoding.get(key))
        if expected_value and encoding.get(key) != expected_value:
            warnings.append(warning)

    expected_fps = _as_finite_float(expected_encoding.get("fps"))
    actual_fps = _as_finite_float(encoding.get("fps"))
    if expected_fps and (
        actual_fps is None or abs(actual_fps - expected_fps) > _ENCODING_FPS_TOLERANCE
    ):
        warnings.append("video frame rate does not match the encoding contract")

    expected_gap = _as_finite_float(expected_encoding.get("max_keyframe_gap_seconds"))
    actual_gap = _as_finite_float(encoding.get("max_keyframe_gap_seconds"))
    if expected_gap:
        if actual_gap is None:
            duration = _as_finite_float(encoding.get("duration"))
            if (
                duration is None
                or duration > expected_gap + _KEYFRAME_GAP_TOLERANCE_SECONDS
            ):
                warnings.append(
                    "video keyframe interval could not be verified against the encoding contract"
                )
        elif actual_gap > expected_gap + _KEYFRAME_GAP_TOLERANCE_SECONDS:
            warnings.append("video keyframe interval exceeds the encoding contract")
    return warnings


def video_stream_matches_encoding_contract(
    video_path: str,
    expected_encoding: dict,
) -> bool:
    """Return whether stream-copy would preserve the requested video contract."""
    if not isinstance(expected_encoding, dict) or not expected_encoding:
        return False
    encoding = _probe_video_encoding(video_path)
    return not _encoding_contract_warnings(encoding, expected_encoding)


def _validate_encoding_contract(report: dict, video_path: str, expected_encoding: dict):
    encoding = _probe_video_encoding(video_path)
    report["encoding"] = encoding
    report["warnings"].extend(
        _encoding_contract_warnings(encoding, expected_encoding)
    )


def inspect_rendered_video(
    video_path: str,
    expected_aspect=None,
    expected_duration=None,
    expected_encoding: dict | None = None,
    allow_silent_audio: bool = False,
) -> dict:
    """Return a non-blocking technical quality report for a rendered video."""
    report = {
        "ok": False,
        "resolution": None,
        "duration": 0.0,
        "expected_duration": None,
        "duration_delta": None,
        "fps": 0.0,
        "has_audio": False,
        "sampled_audio_peak": None,
        "near_black_frame_count": 0,
        "near_black_sample_times": [],
        "frozen_segment_count": 0,
        "frozen_segment_start_times": [],
        "caption_over_black_frame_count": 0,
        "caption_over_black_sample_times": [],
        "color_consistency": {
            "sample_count": 0,
            "status": "unavailable",
            "mean_warmth": None,
            "warmth_spread": None,
            "mean_saturation": None,
            "saturation_spread": None,
        },
        "visual_sharpness": {
            "sample_count": 0,
            "mean_laplacian_variance": None,
            "minimum_laplacian_variance": None,
            "maximum_laplacian_variance": None,
        },
        "encoding": None,
        "warnings": [],
    }
    normalized_path = str(video_path or "").strip()
    if not normalized_path or not os.path.isfile(normalized_path):
        report["warnings"].append("rendered video file is missing")
        return report

    clip = None
    try:
        clip = VideoFileClip(normalized_path)
        width, height = (int(value) for value in clip.size)
        duration = _as_non_negative_float(getattr(clip, "duration", 0))
        fps = _as_non_negative_float(getattr(clip, "fps", 0))
        has_audio = getattr(clip, "audio", None) is not None
        report.update(
            {
                "resolution": [width, height],
                "duration": duration,
                "fps": fps,
                "has_audio": has_audio,
            }
        )

        if width <= 0 or height <= 0:
            report["warnings"].append("video resolution is invalid")
        expected_resolution = _expected_resolution(expected_aspect)
        if expected_resolution and (width, height) != expected_resolution:
            report["warnings"].append("video resolution does not match the selected aspect")
        if duration <= 0:
            report["warnings"].append("video duration is invalid")
        normalized_expected_duration = _as_non_negative_float(expected_duration)
        if normalized_expected_duration > 0:
            report["expected_duration"] = normalized_expected_duration
            duration_delta = abs(duration - normalized_expected_duration)
            report["duration_delta"] = duration_delta
            if (
                duration > 0
                and duration_delta > _DURATION_MISMATCH_TOLERANCE_SECONDS
            ):
                report["warnings"].append(
                    "video duration differs from expected audio duration"
                )
        if fps <= 0:
            report["warnings"].append("video frame rate is invalid")
        if not has_audio and not allow_silent_audio:
            report["warnings"].append("audio track is missing")
        else:
            sampled_audio_peak = _sampled_audio_peak(clip.audio, duration)
            report["sampled_audio_peak"] = sampled_audio_peak
            if (
                sampled_audio_peak is not None
                and sampled_audio_peak <= _NEAR_SILENT_AUDIO_PEAK_THRESHOLD
                and not allow_silent_audio
            ):
                report["warnings"].append("sampled audio is near-silent")

        detected_near_black_segments = _detect_sustained_near_black_segments(
            normalized_path
        )
        detected_frozen_segments = _detect_sustained_frozen_segments(
            normalized_path
        )
        near_black_frame_count = 0
        near_black_sample_times = []
        if detected_near_black_segments is not None:
            near_black_frame_count = len(detected_near_black_segments)
            near_black_sample_times = [
                start_time for start_time, _ in detected_near_black_segments
            ]
        frozen_segment_count = 0
        frozen_segment_start_times = []
        if detected_frozen_segments is not None:
            frozen_segment_count = len(detected_frozen_segments)
            frozen_segment_start_times = [
                start_time for start_time, _ in detected_frozen_segments
            ]
        caption_over_black_frame_count = 0
        caption_over_black_sample_times = []
        color_frames = []
        sampled_frame_count = 0
        frame_sample_times = (
            _render_quality_frame_sample_times(duration)
            if detected_near_black_segments is None
            else tuple(round(duration * ratio, 3) for ratio in _FRAME_SAMPLE_RATIOS)
        )
        for sample_time in frame_sample_times:
            try:
                frame = clip.get_frame(sample_time)
            except Exception:
                continue
            sampled_frame_count += 1
            color_frames.append(frame)
            if (
                detected_near_black_segments is None
                and _is_near_black_frame(frame)
            ):
                near_black_frame_count += 1
                near_black_sample_times.append(sample_time)
            if _is_caption_over_black_frame(frame):
                caption_over_black_frame_count += 1
                caption_over_black_sample_times.append(sample_time)
        report["near_black_frame_count"] = near_black_frame_count
        report["near_black_sample_times"] = near_black_sample_times
        report["frozen_segment_count"] = frozen_segment_count
        report["frozen_segment_start_times"] = frozen_segment_start_times
        report["caption_over_black_frame_count"] = caption_over_black_frame_count
        report["caption_over_black_sample_times"] = caption_over_black_sample_times
        report["color_consistency"] = build_color_consistency_report(color_frames)
        report["visual_sharpness"] = build_visual_sharpness_report(color_frames)
        if frame_sample_times and not sampled_frame_count:
            report["warnings"].append("rendered video frames could not be sampled")
        if detected_near_black_segments is not None and near_black_frame_count:
            report["warnings"].append("some sampled frames are near-black")
        elif sampled_frame_count and near_black_frame_count == sampled_frame_count:
            report["warnings"].append("sampled frames are near-black")
        elif near_black_frame_count:
            report["warnings"].append("some sampled frames are near-black")
        if frozen_segment_count:
            report["warnings"].append("video contains a sustained frozen visual")
        if (
            sampled_frame_count
            and caption_over_black_frame_count == sampled_frame_count
        ):
            report["warnings"].append(
                "sampled frames appear to contain captions over a black visual"
            )
        elif caption_over_black_frame_count:
            report["warnings"].append(
                "some sampled frames appear to contain captions over a black visual"
            )
    except Exception:
        report["warnings"].append("rendered video could not be inspected")
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass

    if isinstance(expected_encoding, dict) and expected_encoding:
        _validate_encoding_contract(report, normalized_path, expected_encoding)

    report["ok"] = not report["warnings"]
    return report
