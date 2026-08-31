from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import shutil
import sys
from typing import TYPE_CHECKING, Sequence
from uuid import UUID, uuid4

from loguru import logger

if TYPE_CHECKING:
    from app.models.schema import MaterialInfo, VideoParams


DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"
_CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_VIDEO_SOURCE_CHOICES = (
    "pexels",
    "pixabay",
    "coverr",
    "vecteezy",
    "dvids",
    "nasa",
    "noaa_ocean",
    "wikimedia",
    "archive_org",
    "loc",
    "local",
    "multi",
)
_VIDEO_ASPECT_CHOICES = ("16:9", "9:16", "4:5", "1:1")
_REPURPOSE_RENDER_MODES = ("fast", "precise")
_REPURPOSE_RENDER_ASPECTS = ("source", "9:16")


class _LazyModule:
    """Delay heavy service imports until a command actually needs them."""

    def __init__(self, module_name: str):
        self._module_name = module_name
        self._module = None

    def _load(self):
        if self._module is None:
            self._module = importlib.import_module(self._module_name)
        return self._module

    def __getattr__(self, name):
        return getattr(self._load(), name)


class _LazyAttribute:
    def __init__(self, module_name: str, attribute_name: str):
        self._module = _LazyModule(module_name)
        self._attribute_name = attribute_name

    def _load(self):
        return getattr(self._module._load(), self._attribute_name)

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)


# Keep ``--help`` independent from task/upload initialization. These names stay
# module-level so existing integrations and tests can still patch ``cli.tm`` and
# ``cli.upload_post``.
config = _LazyModule("app.config.config")
const = _LazyModule("app.models.const")
cost_estimate = _LazyModule("app.services.cost_estimate")
content_quality = _LazyModule("app.services.content_quality")
encoder_calibration = _LazyModule("app.services.encoder_calibration")
history = _LazyModule("app.services.history")
material_benchmark = _LazyModule("app.services.material_benchmark")
metrics_sync = _LazyModule("app.services.metrics_sync")
output_cleanup = _LazyModule("app.services.output_cleanup")
publish_insights = _LazyModule("app.services.publish_insights")
quality_baseline = _LazyModule("app.services.quality_baseline")
render_quality = _LazyModule("app.services.render_quality")
repurpose = _LazyModule("app.services.repurpose")
rss_trend = _LazyModule("app.services.rss_trend")
state_backup = _LazyModule("app.services.state_backup")
scheduled_job_health = _LazyModule("app.services.scheduled_job_health")
scheduled_job_notifications = _LazyModule(
    "app.services.scheduled_job_notifications"
)
scheduled_jobs = _LazyModule("app.services.scheduled_jobs")
thumbnail = _LazyModule("app.services.thumbnail")
twelvelabs = _LazyModule("app.services.twelvelabs")
video_service = _LazyModule("app.services.video")
visual_duplicates = _LazyModule("app.services.visual_duplicates")
visual_policy = _LazyModule("app.services.visual_policy")
tm = _LazyModule("app.services.task")
upload_post = _LazyModule("app.services.upload_post")
state = _LazyAttribute("app.services.state", "state")
VideoParams = _LazyAttribute("app.models.schema", "VideoParams")
openmontage_materials = _LazyModule("app.utils.openmontage_materials")
utils = _LazyModule("app.utils.utils")


class _CliHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    def _get_help_string(self, action):
        help_text = action.help or ""
        if (
            "%(default)" not in help_text
            and action.default not in (None, "", argparse.SUPPRESS)
            and action.option_strings
            and "default:" not in help_text.lower()
        ):
            help_text += " (default: %(default)s)"
        return help_text


def _configured_video_source_default() -> str:
    candidate = str(config.app.get("video_source") or "").strip().lower()
    return candidate if candidate in _VIDEO_SOURCE_CHOICES else "pexels"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"value must be >= 1, got {parsed}")
    return parsed


def _task_id(value: str) -> str:
    """Accept only UUID task identifiers so they cannot become filesystem paths."""
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"task-id must be a valid UUID, got {value!r}"
        ) from exc


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError(f"value must be finite and > 0, got {value}")
    return parsed


def _paragraph_count(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 10:
        raise argparse.ArgumentTypeError(
            f"paragraph-number must be between 1 and 10, got {parsed}"
        )
    return parsed


def _int_range(value: str, *, name: str, min_value: int, max_value: int) -> int:
    parsed = int(value)
    if parsed < min_value or parsed > max_value:
        raise argparse.ArgumentTypeError(
            f"{name} must be between {min_value} and {max_value}, got {parsed}"
        )
    return parsed


def _video_crf(value: str) -> int:
    return _int_range(value, name="video-crf", min_value=0, max_value=51)


def _video_fps(value: str) -> int:
    value = str(value).strip().lower()
    if value.endswith("fps"):
        value = value[:-3].strip()
    return _int_range(value, name="video-fps", min_value=1, max_value=120)


def _audio_bitrate(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized.endswith("kbps"):
        normalized = normalized[:-4].strip()
    elif normalized.endswith("k"):
        normalized = normalized[:-1]
    kbps = _int_range(
        normalized,
        name="audio-bitrate",
        min_value=32,
        max_value=512,
    )
    return f"{kbps}k"


def _video_codec(value: str) -> str:
    return str(value).strip().lower()


def _video_aspects(value: str) -> list[str]:
    aspects = []
    for raw_value in str(value or "").split(","):
        aspect = raw_value.strip()
        if aspect not in _VIDEO_ASPECT_CHOICES:
            raise argparse.ArgumentTypeError(
                f"unknown video aspect ratio: {raw_value!r}"
            )
        if aspect not in aspects:
            aspects.append(aspect)
    return aspects


def _libx264_preset(value: str) -> str:
    return str(value).strip().lower()


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            f"value must be a finite number >= 0, got {value!r}"
        )
    return parsed


def _percent_position(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError(
            "custom-position must be a finite number between 0 and 100, "
            f"got {value!r}"
        )
    return parsed


def _hex_color(value: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError(
            f"color must use #RRGGBB format, got {value!r}"
        )
    return value


_TRANSITION_MODE_VALUES = {
    "none": None,
    "shuffle": "Shuffle",
    "crossfade": "Crossfade",
    "fade-in": "FadeIn",
    "fade-out": "FadeOut",
    "slide-in": "SlideIn",
    "slide-out": "SlideOut",
    "zoom-in": "ZoomIn",
    "zoom-out": "ZoomOut",
}
_VIDEO_CODEC_CHOICES = [
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
]
_LIBX264_PRESET_CHOICES = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]


def _transition_mode(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized not in _TRANSITION_MODE_VALUES:
        allowed = ", ".join(_TRANSITION_MODE_VALUES)
        raise argparse.ArgumentTypeError(
            f"video-transition-mode must be one of: {allowed}"
        )
    return _TRANSITION_MODE_VALUES[normalized]


def _bgm_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "none":
        return ""
    if normalized in {"", "random", "custom", "sonilo"}:
        return normalized
    raise argparse.ArgumentTypeError(
        "bgm-type must be one of: none, random, custom, sonilo"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    help_requested = any(
        argument in {"-h", "--help"} for argument in raw_arguments
    )
    video_source_default = (
        "pexels" if help_requested else _configured_video_source_default()
    )
    parser = argparse.ArgumentParser(
        description=(
            "Generate MoneyPrinterTurbo videos without the WebUI.\n\n"
            "Provider settings and credentials are read from config.toml."
        ),
        epilog="""
Pipeline stages:
  script     Generate or return the script.
  terms      Generate material search terms.
  audio      Generate TTS, silent audio, or use --custom-audio-file.
  subtitle   Generate subtitles when enabled.
  materials  Download online materials or preprocess local files.
  video      Generate the final video.

Local material paths may be relative to the current working directory.
Task failures exit with 1; argument errors exit with 2.
""",
        formatter_class=_CliHelpFormatter,
    )
    parser.add_argument("--video-subject", default="", help="video subject")
    parser.add_argument(
        "--sync-metrics",
        action="store_true",
        help="refresh published-video metrics from Upload-Post",
    )
    parser.add_argument(
        "--sync-metrics-limit",
        type=_positive_int,
        default=None,
        help="maximum eligible jobs to refresh during --sync-metrics",
    )
    parser.add_argument(
        "--sync-metrics-dry-run",
        action="store_true",
        help="show eligible metrics jobs without calling Upload-Post",
    )
    parser.add_argument(
        "--scheduled-job",
        default="",
        help="named job from app.scheduled_jobs in config.toml",
    )
    parser.add_argument(
        "--scheduled-job-dry-run",
        action="store_true",
        help="validate a scheduled job without generating or publishing",
    )
    parser.add_argument(
        "--list-scheduled-jobs",
        action="store_true",
        help="list configured scheduled jobs without showing scripts",
    )
    parser.add_argument(
        "--check-video-encoder",
        action="store_true",
        help="run a short local video encoder check",
    )
    parser.add_argument(
        "--calibrate-amf",
        action="store_true",
        help="measure local AMF quality candidates without changing configuration",
    )
    parser.add_argument(
        "--publish-insights",
        action="store_true",
        help="show read-only suggestions from saved publish metrics",
    )
    parser.add_argument(
        "--cleanup-output",
        action="store_true",
        help="preview expired task-output cleanup",
    )
    parser.add_argument(
        "--apply-output-cleanup",
        action="store_true",
        help="delete expired task-output directories found by --cleanup-output",
    )
    parser.add_argument(
        "--cleanup-cache-videos",
        action="store_true",
        help="preview expired cached video cleanup",
    )
    parser.add_argument(
        "--apply-cache-video-cleanup",
        action="store_true",
        help="delete expired cached videos found by --cleanup-cache-videos",
    )
    parser.add_argument(
        "--cache-video-retention-days",
        type=_positive_int,
        default=30,
        help="keep cached videos modified within this many days",
    )
    parser.add_argument(
        "--output-retention-days",
        type=_positive_int,
        default=30,
        help="keep task-output directories modified within this many days",
    )
    parser.add_argument(
        "--export-state",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="export local history and presets without configuration, credentials, or media",
    )
    parser.add_argument(
        "--resume-task",
        default="",
        metavar="TASK_ID",
        help="manually resume an interrupted task from its saved script checkpoint",
    )
    parser.add_argument(
        "--validate-openmontage",
        action="store_true",
        help="validate local OpenMontage manifests and completed silent outputs",
    )
    parser.add_argument(
        "--backfill-render-quality",
        action="store_true",
        help="inspect local historical videos and preview missing quality reports",
    )
    parser.add_argument(
        "--apply-render-quality-backfill",
        action="store_true",
        help="save reports found by --backfill-render-quality",
    )
    parser.add_argument(
        "--visual-review-task",
        default="",
        help="build a local visual review package for a completed task id",
    )
    parser.add_argument(
        "--quality-baseline",
        action="store_true",
        help="summarize up to five recent local render-quality reports",
    )
    parser.add_argument(
        "--benchmark-material-topic",
        default=None,
        help="run one read-only material-provider comparison for a topic",
    )
    parser.add_argument(
        "--inspect-scene-materials",
        default=None,
        help="inspect read-only provider relevance for comma-separated scene queries",
    )
    parser.add_argument(
        "--visual-policy-topic",
        default=None,
        help="show a recommendation-only visual policy for a topic",
    )
    parser.add_argument(
        "--scan-visual-duplicates",
        action="store_true",
        help="scan recent local rendered videos for repeated visual frames",
    )
    parser.add_argument(
        "--repurpose-video",
        default="",
        help="plan short clips from a local video without rendering",
    )
    parser.add_argument(
        "--repurpose-clip-duration",
        type=_positive_float,
        default=None,
        help="requested short-clip duration in seconds",
    )
    parser.add_argument(
        "--repurpose-clip-count",
        type=_positive_int,
        default=None,
        help="requested number of short clips",
    )
    parser.add_argument(
        "--repurpose-output-dir",
        default="",
        help="explicit local directory for rendered short clips",
    )
    parser.add_argument(
        "--repurpose-render-mode",
        choices=_REPURPOSE_RENDER_MODES,
        default="fast",
        help="short-clip rendering mode when --repurpose-output-dir is set",
    )
    parser.add_argument(
        "--repurpose-aspect",
        choices=_REPURPOSE_RENDER_ASPECTS,
        default="source",
        help="optional output aspect for precise short-clip rendering",
    )
    parser.add_argument(
        "--repurpose-subtitle-file",
        default="",
        help="optional SRT file for subtitle-guided short-clip selection",
    )
    parser.add_argument("--video-script", default="", help="custom script")
    parser.add_argument("--video-terms", default=None, help="comma-separated terms")
    parser.add_argument(
        "--video-language",
        default=None,
        help="script generation language code (default: auto detect)",
    )
    parser.add_argument(
        "--paragraph-number",
        type=_paragraph_count,
        default=None,
        help="script paragraph count, 1-10",
    )
    parser.add_argument(
        "--video-script-prompt",
        default=None,
        help="custom script requirements prompt",
    )
    parser.add_argument(
        "--custom-system-prompt",
        default=None,
        help="custom system prompt for script generation",
    )
    parser.add_argument(
        "--video-source",
        default=video_source_default,
        choices=_VIDEO_SOURCE_CHOICES,
        help="video material source",
    )
    parser.add_argument(
        "--video-materials",
        default="",
        help="comma-separated local material paths",
    )
    parser.add_argument(
        "--stop-at",
        default="video",
        choices=["script", "terms", "audio", "subtitle", "materials", "video"],
        help="pipeline stop stage",
    )
    parser.add_argument(
        "--video-count", type=_positive_int, default=1, help="output video count (>=1)"
    )
    parser.add_argument(
        "--video-aspect",
        choices=_VIDEO_ASPECT_CHOICES,
        default="9:16",
        help="video aspect ratio",
    )
    parser.add_argument(
        "--video-aspects",
        type=_video_aspects,
        default=None,
        help="comma-separated additional output aspect ratios",
    )
    parser.add_argument(
        "--video-concat-mode",
        choices=["random", "sequential"],
        default=None,
        help="video concatenation mode",
    )
    parser.add_argument(
        "--video-transition-mode",
        type=_transition_mode,
        default=None,
        metavar="{none,shuffle,crossfade,fade-in,fade-out,slide-in,slide-out,zoom-in,zoom-out}",
        help="video transition mode",
    )
    parser.add_argument(
        "--video-clip-duration",
        type=_positive_int,
        default=None,
        help="maximum duration of each source clip in seconds",
    )
    parser.add_argument(
        "--video-codec",
        type=_video_codec,
        choices=_VIDEO_CODEC_CHOICES,
        default=None,
        help="video encoder codec",
    )
    parser.add_argument(
        "--video-crf",
        type=_video_crf,
        default=None,
        help="libx264 CRF quality level, 0-51; lower means higher quality",
    )
    parser.add_argument(
        "--video-encoder-preset",
        type=_libx264_preset,
        choices=_LIBX264_PRESET_CHOICES,
        default=None,
        help="libx264 encoder preset",
    )
    parser.add_argument(
        "--video-fps",
        type=_video_fps,
        default=None,
        help="output frame rate, 1-120",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=_audio_bitrate,
        default=None,
        help="final audio bitrate in kbps, 32-512; accepts 192, 192k, or 192kbps",
    )
    parser.add_argument(
        "--match-materials-to-script",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="match generated/search materials to script order",
    )
    parser.add_argument(
        "--n-threads",
        type=_positive_int,
        default=None,
        help="FFmpeg worker thread count",
    )
    parser.add_argument(
        "--smart-scene-queries",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="generate concrete scene-based B-roll search queries",
    )
    parser.add_argument(
        "--voice-name",
        default=DEFAULT_VOICE_NAME,
        help="tts voice name",
    )
    parser.add_argument(
        "--voice-volume",
        type=_non_negative_float,
        default=None,
        help="speech volume multiplier",
    )
    parser.add_argument(
        "--voice-rate",
        type=_positive_float,
        default=None,
        help="speech rate multiplier",
    )
    parser.add_argument(
        "--custom-audio-file",
        default=None,
        metavar="PATH",
        help="use a prepared narration audio file instead of TTS",
    )
    parser.add_argument(
        "--bgm-type",
        type=_bgm_type,
        default=None,
        metavar="{none,random,custom,sonilo}",
        help="background music mode",
    )
    parser.add_argument(
        "--sonilo-bgm-prompt",
        default=None,
        help="optional music style prompt for Sonilo",
    )
    parser.add_argument("--bgm-file", default=None, help="custom background music file")
    parser.add_argument(
        "--bgm-volume",
        type=_non_negative_float,
        default=None,
        help="background music volume multiplier",
    )
    parser.add_argument(
        "--subtitle-enabled",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="enable subtitles (default: enabled, use --no-subtitle-enabled to disable)",
    )
    parser.add_argument("--font-name", default=None, help="subtitle font file name")
    parser.add_argument(
        "--subtitle-style",
        choices=["classic", "karaoke"],
        default=None,
        help="subtitle rendering style",
    )
    parser.add_argument(
        "--subtitle-position",
        choices=["top", "center", "bottom", "custom"],
        default=None,
        help="subtitle position",
    )
    parser.add_argument(
        "--custom-position",
        type=_percent_position,
        default=None,
        help="custom subtitle position as percent from top, 0-100",
    )
    parser.add_argument(
        "--text-fore-color",
        type=_hex_color,
        default=None,
        help="subtitle text color in #RRGGBB format",
    )
    parser.add_argument(
        "--font-size", type=_positive_int, default=None, help="subtitle font size"
    )
    parser.add_argument(
        "--stroke-color",
        type=_hex_color,
        default=None,
        help="subtitle outline color in #RRGGBB format",
    )
    parser.add_argument(
        "--stroke-width",
        type=_non_negative_float,
        default=None,
        help="subtitle outline width",
    )
    parser.add_argument(
        "--subtitle-background-enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable subtitle background",
    )
    parser.add_argument(
        "--subtitle-background-color",
        type=_hex_color,
        default=None,
        help="subtitle background color in #RRGGBB format",
    )
    parser.add_argument(
        "--rounded-subtitle-background",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable rounded translucent subtitle background",
    )
    parser.add_argument(
        "--task-id",
        type=_task_id,
        default=None,
        help="custom UUID task id",
    )
    args = parser.parse_args(argv)

    has_scheduled_job = bool(args.scheduled_job.strip())
    has_repurpose_plan = bool(args.repurpose_video.strip())
    has_render_quality_backfill = args.backfill_render_quality
    has_visual_review_task = bool(args.visual_review_task.strip())
    has_quality_baseline = args.quality_baseline
    has_amf_calibration = args.calibrate_amf
    has_material_benchmark = args.benchmark_material_topic is not None
    has_visual_duplicate_scan = args.scan_visual_duplicates
    has_scene_material_inspection = args.inspect_scene_materials is not None
    has_visual_policy = args.visual_policy_topic is not None
    has_output_cleanup = args.cleanup_output
    has_cache_video_cleanup = args.cleanup_cache_videos
    has_cleanup_command = has_output_cleanup or has_cache_video_cleanup
    has_state_export = args.export_state is not None
    has_resume_task = bool(args.resume_task.strip())
    has_openmontage_validation = args.validate_openmontage
    if has_material_benchmark and not args.benchmark_material_topic.strip():
        parser.error("--benchmark-material-topic requires a non-empty topic")
    if has_scene_material_inspection and not args.inspect_scene_materials.strip():
        parser.error("--inspect-scene-materials requires one or more scene queries")
    if has_visual_policy and not args.visual_policy_topic.strip():
        parser.error("--visual-policy-topic requires a non-empty topic")
    if args.resume_task and not has_resume_task:
        parser.error("--resume-task requires a non-empty task id")
    has_other_job_command = (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or has_material_benchmark
        or has_scene_material_inspection
        or has_visual_policy
        or has_visual_duplicate_scan
        or args.publish_insights
        or has_render_quality_backfill
        or has_visual_review_task
        or has_quality_baseline
        or has_scheduled_job
        or has_cleanup_command
        or has_state_export
        or has_resume_task
        or has_openmontage_validation
    )
    special_job_command_count = sum(
        bool(command)
        for command in (
            args.sync_metrics,
            args.list_scheduled_jobs,
            args.check_video_encoder,
            has_amf_calibration,
            has_material_benchmark,
            has_scene_material_inspection,
            has_visual_policy,
            has_visual_duplicate_scan,
            args.publish_insights,
            has_render_quality_backfill,
            has_visual_review_task,
            has_quality_baseline,
            has_scheduled_job,
            has_output_cleanup,
            has_cache_video_cleanup,
            has_state_export,
            has_resume_task,
            has_openmontage_validation,
        )
    )
    if special_job_command_count > 1:
        parser.error("only one job command can be used at a time")
    if has_repurpose_plan and has_other_job_command:
        parser.error("--repurpose-video cannot be combined with another job command")
    if has_repurpose_plan and (
        args.repurpose_clip_duration is None or args.repurpose_clip_count is None
    ):
        parser.error(
            "--repurpose-video requires --repurpose-clip-duration and "
            "--repurpose-clip-count"
        )
    if not has_repurpose_plan and (
        args.repurpose_clip_duration is not None or args.repurpose_clip_count is not None
    ):
        parser.error(
            "--repurpose-clip-duration and --repurpose-clip-count require "
            "--repurpose-video"
        )
    if args.repurpose_output_dir.strip() and not has_repurpose_plan:
        parser.error("--repurpose-output-dir requires --repurpose-video")
    if args.repurpose_subtitle_file.strip() and not has_repurpose_plan:
        parser.error("--repurpose-subtitle-file requires --repurpose-video")
    if (
        args.repurpose_render_mode != repurpose.RENDER_MODE_FAST
        and not args.repurpose_output_dir.strip()
    ):
        parser.error("--repurpose-render-mode requires --repurpose-output-dir")
    if (
        args.repurpose_aspect != repurpose.RENDER_ASPECT_SOURCE
        and not args.repurpose_output_dir.strip()
    ):
        parser.error("--repurpose-aspect requires --repurpose-output-dir")
    if (
        args.repurpose_aspect != repurpose.RENDER_ASPECT_SOURCE
        and args.repurpose_render_mode != repurpose.RENDER_MODE_PRECISE
    ):
        parser.error("--repurpose-aspect requires --repurpose-render-mode precise")
    if args.sync_metrics and has_scheduled_job:
        parser.error("--scheduled-job cannot be combined with --sync-metrics")
    if args.list_scheduled_jobs and (args.sync_metrics or has_scheduled_job):
        parser.error("--list-scheduled-jobs cannot be combined with another job command")
    if args.check_video_encoder and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or has_amf_calibration
        or has_material_benchmark
        or has_scheduled_job
    ):
        parser.error("--check-video-encoder cannot be combined with another job command")
    if args.publish_insights and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or has_material_benchmark
        or has_scheduled_job
    ):
        parser.error("--publish-insights cannot be combined with another job command")
    if has_render_quality_backfill and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or has_material_benchmark
        or args.publish_insights
        or has_scheduled_job
    ):
        parser.error(
            "--backfill-render-quality cannot be combined with another job command"
        )
    if has_visual_review_task and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or has_material_benchmark
        or args.publish_insights
        or has_render_quality_backfill
        or has_scheduled_job
    ):
        parser.error(
            "--visual-review-task cannot be combined with another job command"
        )
    if has_quality_baseline and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or has_material_benchmark
        or args.publish_insights
        or has_render_quality_backfill
        or has_visual_review_task
        or has_scheduled_job
    ):
        parser.error("--quality-baseline cannot be combined with another job command")
    if has_amf_calibration and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or args.publish_insights
        or has_render_quality_backfill
        or has_visual_review_task
        or has_quality_baseline
        or has_scheduled_job
    ):
        parser.error("--calibrate-amf cannot be combined with another job command")
    if has_material_benchmark and (
        args.sync_metrics
        or args.list_scheduled_jobs
        or args.check_video_encoder
        or has_amf_calibration
        or args.publish_insights
        or has_render_quality_backfill
        or has_visual_review_task
        or has_quality_baseline
        or has_scheduled_job
    ):
        parser.error(
            "--benchmark-material-topic cannot be combined with another job command"
        )
    if args.apply_render_quality_backfill and not has_render_quality_backfill:
        parser.error(
            "--apply-render-quality-backfill requires --backfill-render-quality"
        )
    if args.apply_output_cleanup and not has_output_cleanup:
        parser.error("--apply-output-cleanup requires --cleanup-output")
    if args.apply_cache_video_cleanup and not has_cache_video_cleanup:
        parser.error(
            "--apply-cache-video-cleanup requires --cleanup-cache-videos"
        )

    if args.scheduled_job_dry_run and not has_scheduled_job:
        parser.error("--scheduled-job-dry-run requires --scheduled-job")

    if (
        has_scheduled_job
        and not args.scheduled_job_dry_run
        and args.stop_at != "video"
    ):
        parser.error("--scheduled-job only supports --stop-at video")

    if args.sync_metrics_dry_run and not args.sync_metrics:
        parser.error("--sync-metrics-dry-run requires --sync-metrics")

    is_video_command = not has_other_job_command and not has_repurpose_plan
    if is_video_command:
        if not args.video_subject.strip() and not args.video_script.strip():
            parser.error("one of --video-subject or --video-script is required")

        has_video_materials = bool((args.video_materials or "").strip())
        if args.video_source == "local" and args.stop_at == "terms":
            parser.error(
                "--stop-at terms has no effect with --video-source local "
                "(search terms are not generated for local sources)"
            )
        if (
            args.video_source == "local"
            and args.stop_at in {"materials", "video"}
            and not has_video_materials
        ):
            parser.error(
                "--video-materials is required with --video-source local when "
                "--stop-at is materials or video"
            )
        if args.video_source != "local" and has_video_materials:
            parser.error(
                "--video-materials can only be used with --video-source local"
            )

        if args.bgm_file:
            if args.bgm_type in (None, "custom"):
                args.bgm_type = "custom"
            else:
                parser.error(
                    "--bgm-file can only be combined with --bgm-type custom"
                )
        if args.sonilo_bgm_prompt:
            if args.bgm_type in (None, "sonilo"):
                args.bgm_type = "sonilo"
            else:
                parser.error(
                    "--sonilo-bgm-prompt can only be combined with "
                    "--bgm-type sonilo"
                )

        if (
            args.custom_position is not None
            and args.subtitle_position != "custom"
        ):
            parser.error("--custom-position requires --subtitle-position custom")
        if args.stop_at == "subtitle" and not args.subtitle_enabled:
            parser.error(
                "--stop-at subtitle cannot be combined with "
                "--no-subtitle-enabled"
            )
        if args.subtitle_background_enabled is False and (
            args.subtitle_background_color is not None
            or args.rounded_subtitle_background is True
        ):
            parser.error(
                "subtitle background color or rounding cannot be enabled "
                "together with --no-subtitle-background-enabled"
            )

    return args


def build_video_params(args: argparse.Namespace) -> VideoParams:
    from app.models.schema import MaterialInfo, VideoParams

    video_terms = args.video_terms
    if video_terms:
        video_terms = [term.strip() for term in video_terms.split(",") if term.strip()]

    video_materials = None
    materials_arg = args.video_materials or ""
    if materials_arg.strip():
        video_materials = [
            # Actual duration will be detected during video processing; use 0 as placeholder.
            MaterialInfo(provider="local", url=item.strip(), duration=0)
            for item in materials_arg.split(",")
            if item.strip()
        ]

    params_kwargs = {
        "video_subject": args.video_subject,
        "video_script": args.video_script,
        "video_terms": video_terms,
        "video_source": args.video_source,
        "video_materials": video_materials,
        "video_count": args.video_count,
        "video_aspect": args.video_aspect,
        "voice_name": args.voice_name,
        "subtitle_enabled": args.subtitle_enabled,
        "outro_image_file": config.ui.get("outro_image_file", ""),
        "outro_duration": config.ui.get("outro_duration", 2.0),
    }
    additional_aspects = getattr(args, "video_aspects", None)
    if additional_aspects:
        selected_aspects = list(
            dict.fromkeys([args.video_aspect, *additional_aspects])
        )
        if len(selected_aspects) > 1:
            params_kwargs["video_aspects"] = selected_aspects

    optional_arg_names = [
        "video_language",
        "paragraph_number",
        "video_script_prompt",
        "custom_system_prompt",
        "video_concat_mode",
        "video_transition_mode",
        "video_clip_duration",
        "video_codec",
        "video_crf",
        "video_encoder_preset",
        "video_fps",
        "audio_bitrate",
        "match_materials_to_script",
        "n_threads",
        "smart_scene_queries",
        "voice_volume",
        "voice_rate",
        "custom_audio_file",
        "bgm_type",
        "bgm_file",
        "bgm_volume",
        "sonilo_bgm_prompt",
        "font_name",
        "subtitle_style",
        "subtitle_position",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "rounded_subtitle_background",
    ]
    for name in optional_arg_names:
        value = getattr(args, name)
        if value is not None:
            params_kwargs[name] = value

    if args.subtitle_background_enabled is False:
        params_kwargs["text_background_color"] = False
        params_kwargs["rounded_subtitle_background"] = False
    elif args.subtitle_background_color is not None:
        params_kwargs["text_background_color"] = args.subtitle_background_color
    elif args.subtitle_background_enabled is True:
        params_kwargs["text_background_color"] = True
    elif not config.ui.get("subtitle_background_enabled", True):
        params_kwargs["text_background_color"] = False
        params_kwargs["rounded_subtitle_background"] = False

    return VideoParams(**params_kwargs)


def _resolve_cli_file(
    raw_path: str,
    *,
    description: str,
    fallback_dir: str | None = None,
) -> str:
    """
    将 CLI 文件参数按当前工作目录解析为绝对路径，
    并在任务开始前确认存在。

    本地素材旧版本始终相对 ``storage/local_videos`` 解析。为兼容已有脚本，
    当前目录找不到相对路径时允许回退该目录；绝对路径始终按用户输入
    直接解析。
    """
    expanded_path = os.path.expanduser(raw_path.strip())
    if not expanded_path:
        raise ValueError(f"{description} path cannot be empty")

    candidate = (
        expanded_path
        if os.path.isabs(expanded_path)
        else os.path.join(os.getcwd(), expanded_path)
    )
    resolved_path = os.path.realpath(candidate)
    if not os.path.isfile(resolved_path) and fallback_dir and not os.path.isabs(expanded_path):
        resolved_path = os.path.realpath(os.path.join(fallback_dir, expanded_path))

    if not os.path.isfile(resolved_path):
        raise ValueError(f"{description} file does not exist: {raw_path}")
    return resolved_path


def _path_is_within_directory(file_path: str, directory: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(directory), os.path.realpath(file_path)]
        ) == os.path.realpath(directory)
    except ValueError:
        # Windows 不同盘符无法计算 commonpath，此时文件显然不在目标目录内。
        return False


def _resolve_managed_resource_file(
    raw_path: str,
    *,
    resource_dir: str,
    description: str,
) -> str:
    """解析项目资源文件，并确保绝对路径仍位于对应资源目录内。"""
    from app.utils import utils

    expanded_path = os.path.expanduser(raw_path.strip())
    candidates = (
        [expanded_path]
        if os.path.isabs(expanded_path)
        else [
            os.path.join(resource_dir, expanded_path),
            os.path.join(utils.root_dir(), expanded_path),
        ]
    )
    for candidate in candidates:
        resolved_path = os.path.realpath(candidate)
        if os.path.isfile(resolved_path) and _path_is_within_directory(
            resolved_path, resource_dir
        ):
            return resolved_path
    raise ValueError(
        f"{description} file must exist inside {resource_dir}: {raw_path}"
    )


def prepare_cli_files(params: VideoParams, stop_at: str) -> None:
    """
    在调用 LLM/TTS 前准备 CLI 文件，避免长流程运行到后期才报告路径错误。

    服务层为了保护 API 请求，只允许读取 ``storage/local_videos`` 内的素材。
    CLI 是本地入口，接受当前目录相对路径和绝对路径。目录外素材会
    复制到受控目录，再把参数替换为服务层可安全使用的绝对路径。
    """
    from app.models import const
    from app.services import bgm as bgm_service
    from app.utils import utils

    local_material_extensions = {
        *(f".{extension}" for extension in const.FILE_TYPE_VIDEOS),
        *(f".{extension}" for extension in const.FILE_TYPE_IMAGES),
        ".avi",
        ".flv",
    }

    if params.custom_audio_file:
        params.custom_audio_file = _resolve_cli_file(
            params.custom_audio_file,
            description="custom audio",
        )
        audio_extension = os.path.splitext(params.custom_audio_file)[1].lower()
        if audio_extension not in _CUSTOM_AUDIO_EXTENSIONS:
            allowed = ", ".join(sorted(_CUSTOM_AUDIO_EXTENSIONS))
            raise ValueError(
                f"unsupported custom audio type {audio_extension or '<none>'}; "
                f"allowed extensions: {allowed}"
            )

    if params.bgm_type == "custom":
        if not bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
            # 0 音量时下游会统一跳过所有 BGM。这里同时清空文件参数，避免
            # CLI 为一个不会被读取的文件执行路径解析、存在性检查或格式
            # 校验。
            params.bgm_file = ""
        elif not params.bgm_file:
            # 缺少文件是否构成错误取决于通用 BGM 开关，不能在 argparse 阶段
            # 无条件拦截，否则 ``custom + 0%`` 会和 WebUI、服务层行为不一致。
            raise ValueError("--bgm-file is required when --bgm-type is custom")
        else:
            try:
                # CLI、WebUI 和任务服务必须共用同一个 BGM 文件边界。这里直接
                # 复用服务层解析，既支持用户上传目录和内置歌曲目录，也
                # 自动继承新增音频格式及路径安全规则，避免多个入口分别
                # 维护白名单。
                params.bgm_file = bgm_service.resolve_bgm_file(params.bgm_file)
            except ValueError as exc:
                supported_extensions = ", ".join(
                    bgm_service.SUPPORTED_BGM_EXTENSIONS
                )
                raise ValueError(
                    "background music must be a supported audio file inside "
                    f"storage/bgm or resource/songs ({supported_extensions}): "
                    f"{params.bgm_file}"
                ) from exc

    if params.subtitle_enabled and params.font_name and stop_at == "video":
        font_path = _resolve_managed_resource_file(
            params.font_name,
            resource_dir=utils.font_dir(),
            description="subtitle font",
        )
        if not font_path.lower().endswith((".ttf", ".ttc")):
            raise ValueError("subtitle font must use the .ttf or .ttc extension")
        # 下游根据 resource/fonts 内的文件名拼接路径，因此仍保留纯文件名。
        params.font_name = os.path.basename(font_path)

    if params.video_source != "local" or stop_at not in {"materials", "video"}:
        return

    local_videos_dir = utils.storage_dir("local_videos", create=True)
    resolved_materials: list[tuple[MaterialInfo, str, str]] = []
    for material in params.video_materials or []:
        source_path = _resolve_cli_file(
            material.url,
            description="local material",
            fallback_dir=local_videos_dir,
        )
        extension = os.path.splitext(source_path)[1].lower()
        if extension not in local_material_extensions:
            allowed = ", ".join(sorted(local_material_extensions))
            raise ValueError(
                f"unsupported local material type {extension or '<none>'}: "
                f"{material.url}; allowed extensions: {allowed}"
            )
        resolved_materials.append((material, source_path, extension))

    # 所有输入检查通过后再复制，避免第二个文件无效时留下第一个文件的
    # 孤儿副本。
    prepared_paths: dict[str, str] = {}
    for material, source_path, extension in resolved_materials:
        prepared_path = prepared_paths.get(source_path)
        if prepared_path is None:
            if _path_is_within_directory(source_path, local_videos_dir):
                prepared_path = source_path
            else:
                prepared_path = os.path.join(
                    local_videos_dir,
                    f"cli-material-{uuid4().hex}{extension}",
                )
                shutil.copy2(source_path, prepared_path)
                logger.info(
                    "copied CLI local material into managed storage: "
                    f"source={source_path}, target={prepared_path}"
                )
            prepared_paths[source_path] = prepared_path

        material.url = prepared_path


def build_video_quality_config(args: argparse.Namespace) -> dict[str, object]:
    quality_config_fields = {
        "video_codec": args.video_codec,
        "video_crf": args.video_crf,
        "video_encoder_preset": args.video_encoder_preset,
        "video_fps": args.video_fps,
        "audio_bitrate": args.audio_bitrate,
    }
    return {
        key: value
        for key, value in quality_config_fields.items()
        if value is not None
    }


def _upload_post_request_ids(job: dict) -> list[str]:
    request_ids = []
    for pending_upload in job.get("pending_uploads") or []:
        if not isinstance(pending_upload, dict):
            continue
        result = pending_upload.get("result")
        request_id = (
            result.get("request_id")
            if isinstance(result, dict)
            else pending_upload.get("request_id")
        )
        if isinstance(request_id, str) and request_id.strip():
            request_ids.append(request_id.strip())
    return list(dict.fromkeys(request_ids))


def _sync_upload_post_metrics_for_job(job: dict) -> metrics_sync.SyncJobResult:
    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0}
    platform_totals = {}
    has_metrics = False
    saw_transient_error = False
    saw_permanent_error = False

    for request_id in _upload_post_request_ids(job):
        analytics = upload_post.upload_post_service.get_post_analytics(request_id)
        if not isinstance(analytics, dict) or not analytics.get("success"):
            if isinstance(analytics, dict) and analytics.get("retryable"):
                saw_transient_error = True
            else:
                saw_permanent_error = True
            continue
        if not upload_post.has_post_metrics(analytics):
            continue

        metrics = upload_post.aggregate_post_metrics(analytics)
        has_metrics = True
        for field in totals:
            totals[field] += int(metrics.get(field, 0) or 0)
        for platform, platform_metrics in upload_post.extract_post_platform_metrics(
            analytics
        ).items():
            target = platform_totals.setdefault(
                platform,
                {field: 0 for field in totals},
            )
            for field in totals:
                target[field] += int(platform_metrics.get(field, 0) or 0)

    task_id = job.get("task_id")
    if has_metrics and isinstance(task_id, str) and task_id.strip():
        metrics_payload = {**totals}
        if platform_totals:
            metrics_payload["platform_metrics"] = platform_totals
        if history.update_publish_metrics(task_id, metrics_payload):
            return metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_SYNCED)
        return metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_PERMANENT_ERROR)
    if saw_permanent_error:
        return metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_PERMANENT_ERROR)
    if saw_transient_error:
        return metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_TRANSIENT_ERROR)
    return metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_NO_DATA)


def _run_metrics_sync(max_jobs: int | None = None, *, dry_run: bool = False) -> int:
    if dry_run:
        candidates = history.list_jobs_pending_metrics_sync()
        if max_jobs is not None:
            candidates = candidates[:max_jobs]
        print(json.dumps({"eligible": len(candidates), "dry_run": True}))
        return 0

    if not upload_post.upload_post_service.is_configured():
        history.record_metrics_sync_run(
            metrics_sync.empty_metrics_sync_summary(),
            status="not_configured",
        )
        print("Upload-Post metrics sync skipped: Upload-Post is not configured.")
        return 0

    summary = metrics_sync.sync_pending_publish_metrics(
        _sync_upload_post_metrics_for_job,
        max_jobs=max_jobs,
    )
    history.record_metrics_sync_run(summary)
    print(
        json.dumps(
            {
                "synced": int(summary.get("synced", 0) or 0),
                "skipped": int(summary.get("skipped", 0) or 0),
                "errors": len(summary.get("errors") or []),
            }
        )
    )
    return 1 if summary.get("errors") else 0


def _run_publish_insights() -> int:
    report = publish_insights.build_publish_performance_insights(
        history.list_history()
    )
    print(json.dumps(report))
    return 0


def _active_task_ids() -> set[str]:
    try:
        tasks = state.get_all_tasks()
    except Exception as error:
        logger.warning(f"Could not read active task state before cleanup: {error}")
        return set()
    if not isinstance(tasks, dict):
        return set()
    return {
        str(task_id).strip()
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and task.get("state") == const.TASK_STATE_PROCESSING
        and str(task_id).strip()
    }


def _run_output_cleanup(retention_days: int, *, apply: bool = False) -> int:
    summary = output_cleanup.cleanup_task_outputs(
        utils.storage_dir("tasks"),
        retention_days=retention_days,
        active_task_ids=_active_task_ids(),
        apply=apply,
    )
    print(
        json.dumps(
            {
                "dry_run": bool(summary.get("dry_run", True)),
                "retention_days": int(summary.get("retention_days", retention_days)),
                "scanned": int(summary.get("scanned", 0)),
                "eligible": len(summary.get("eligible") or []),
                "deleted": len(summary.get("deleted") or []),
                "skipped_active": int(summary.get("skipped_active", 0)),
                "errors": len(summary.get("errors") or []),
            }
        )
    )
    return 1 if summary.get("errors") else 0


def _run_cache_video_cleanup(retention_days: int, *, apply: bool = False) -> int:
    active_task_ids = _active_task_ids()
    summary = output_cleanup.cleanup_video_cache(
        utils.storage_dir("cache_videos"),
        retention_days=retention_days,
        active_tasks_present=bool(active_task_ids),
        apply=apply,
    )
    print(
        json.dumps(
            {
                "dry_run": bool(summary.get("dry_run", True)),
                "retention_days": int(summary.get("retention_days", retention_days)),
                "scanned": int(summary.get("scanned", 0)),
                "eligible": len(summary.get("eligible") or []),
                "eligible_bytes": int(summary.get("eligible_bytes", 0) or 0),
                "deleted": len(summary.get("deleted") or []),
                "deleted_bytes": int(summary.get("deleted_bytes", 0) or 0),
                "blocked_by_active_tasks": bool(
                    summary.get("blocked_by_active_tasks", False)
                ),
                "errors": len(summary.get("errors") or []),
            }
        )
    )
    return 1 if summary.get("errors") or summary.get("blocked_by_active_tasks") else 0


def _run_state_export(output_path: str | None) -> int:
    summary = state_backup.export_state_backup(output_path or None)
    print(
        json.dumps(
            {
                "ok": bool(summary.get("ok")),
                "archive": str(summary.get("archive") or ""),
                "files": len(summary.get("files") or []),
                "errors": len(summary.get("errors") or []),
            }
        )
    )
    return 0 if summary.get("ok") else 1


def _run_task_resume(task_id: str) -> int:
    result = tm.resume_interrupted_task(task_id)
    if not result:
        print(json.dumps({"resumed": False, "task_id": task_id}))
        return 1
    print(json.dumps({"resumed": True, "task_id": task_id}))
    return 0


def _run_openmontage_validation() -> int:
    report = openmontage_materials.validate_openmontage_library()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("valid") else 1


def _run_render_quality_backfill(persist: bool = False) -> int:
    summary = history.backfill_render_quality_reports(
        render_quality.inspect_rendered_video,
        persist=persist,
    )
    print(json.dumps(summary))
    return 1 if summary.get("inspection_errors", 0) else 0


def _run_visual_review_task(task_id: str) -> int:
    package = render_quality.build_task_visual_review_package(task_id)
    print(json.dumps(package, ensure_ascii=False))
    return 0 if package.get("ok") else 1


def _run_quality_baseline() -> int:
    baseline = quality_baseline.collect_render_quality_baseline(max_videos=5)
    print(json.dumps(baseline, ensure_ascii=False))
    return 0


def _run_amf_calibration(video_aspect: str) -> int:
    result = encoder_calibration.run_amf_calibration(video_aspect)
    print(json.dumps(result))
    return 0 if result.get("ok") else 2


def _run_material_benchmark(topic: str, video_aspect: str) -> int:
    result = material_benchmark.benchmark_material_providers(topic, video_aspect)
    print(json.dumps(result))
    return 0 if result.get("ok") else 2


def _run_visual_duplicate_scan() -> int:
    result = visual_duplicates.find_cross_task_visual_duplicates(max_videos=20)
    print(json.dumps(result))
    return 0 if result.get("ok") else 2


def _run_scene_material_inspection(scene_queries: str, video_aspect: str) -> int:
    result = material_benchmark.inspect_scene_material_relevance(
        scene_queries,
        video_aspect,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


def _run_visual_policy(topic: str) -> int:
    print(json.dumps(visual_policy.recommend_visual_policy(topic), ensure_ascii=False))
    return 0


def _run_video_encoder_check(
    video_aspect: str | Sequence[str] = "9:16",
) -> int:
    requested_aspects = (
        [video_aspect] if isinstance(video_aspect, str) else list(video_aspect)
    )
    video_aspects = []
    for requested_aspect in requested_aspects:
        aspect = str(requested_aspect or "").strip()
        if aspect and aspect not in video_aspects:
            video_aspects.append(aspect)
    if not video_aspects:
        video_aspects = ["9:16"]

    if len(video_aspects) == 1:
        try:
            result = video_service.check_video_encoder(video_aspects[0])
        except Exception:
            print(json.dumps({"ok": False, "error": "Video encoder check failed."}))
            return 1

        print(json.dumps(result))
        return 2 if result["fallback_used"] else 0

    checks = []
    has_errors = False
    for aspect in video_aspects:
        try:
            result = video_service.check_video_encoder(aspect)
        except Exception:
            checks.append(
                {
                    "video_aspect": aspect,
                    "ok": False,
                    "error": "Video encoder check failed.",
                }
            )
            has_errors = True
            continue
        checks.append({"ok": True, **result})

    fallback_used = any(bool(check.get("fallback_used")) for check in checks)
    print(json.dumps({"checks": checks, "fallback_used": fallback_used}))
    if has_errors:
        return 1
    return 2 if fallback_used else 0


def _run_short_clip_plan(
    video_path: str,
    clip_duration_seconds: float,
    clip_count: int,
    output_dir: str = "",
    render_mode: str = "fast",
    video_codec: str = "libx264",
    subtitle_path: str = "",
    target_aspect: str = "source",
) -> int:
    try:
        source_duration = video_service.get_video_duration(video_path)
    except Exception:
        source_duration = None
    if subtitle_path.strip():
        plan = repurpose.plan_subtitle_guided_short_clips(
            source_duration,
            clip_duration_seconds=clip_duration_seconds,
            clip_count=clip_count,
            subtitle_path=subtitle_path,
        )
    else:
        plan = repurpose.plan_short_clips(
            source_duration,
            clip_duration_seconds=clip_duration_seconds,
            clip_count=clip_count,
        )
    if plan is None:
        print(json.dumps({"ok": False, "error": "Video duration is unavailable."}))
        return 1

    summary = {"ok": True, **plan}
    if output_dir.strip():
        try:
            render_result = repurpose.render_short_clips(
                video_path,
                output_dir,
                plan["clips"],
                render_mode=render_mode,
                video_codec=video_codec,
                target_aspect=target_aspect,
            )
        except Exception:
            render_result = {
                "rendered_clip_count": 0,
                "error_count": len(plan["clips"]),
            }
        rendered_count = int(render_result.get("rendered_clip_count", 0) or 0)
        error_count = int(render_result.get("error_count", 0) or 0)
        summary["rendered_clip_count"] = rendered_count
        summary["render_error_count"] = error_count
        summary["render_mode"] = render_mode
        summary["target_aspect"] = target_aspect
        if render_mode == repurpose.RENDER_MODE_PRECISE:
            summary["video_codec"] = video_codec
        if error_count:
            summary["ok"] = False

    print(json.dumps(summary))
    return 1 if not summary["ok"] else 0


def _run_scheduled_job_dry_run(name: str) -> int:
    try:
        job = scheduled_jobs.get_scheduled_job(name)
    except scheduled_jobs.ScheduledJobError:
        print(json.dumps({"valid": False, "error": "Scheduled job is unavailable."}))
        return 2

    print(json.dumps({"valid": True, "job": scheduled_jobs.scheduled_job_summary(job)}))
    return 0


def _run_scheduled_job_list() -> int:
    try:
        jobs = scheduled_jobs.list_scheduled_jobs()
    except scheduled_jobs.ScheduledJobError:
        print(json.dumps({"jobs": [], "error": "Scheduled jobs are unavailable."}))
        return 2

    summaries = [scheduled_jobs.scheduled_job_summary(job) for job in jobs]
    try:
        history_entries = history.list_history()
    except Exception:
        history_entries = []
    health = scheduled_job_health.build_scheduled_job_health_summary(
        history_entries,
        jobs,
    )
    print(json.dumps({"count": len(summaries), "jobs": summaries, "health": health}))
    return 0


def _scheduled_job_args(args: argparse.Namespace, job: dict) -> argparse.Namespace:
    scheduled_args = argparse.Namespace(**vars(args))
    scheduled_args.video_subject = job["video_subject"]
    scheduled_args.video_script = job["video_script"]
    if job.get("video_transition_mode") is not None:
        scheduled_args.video_transition_mode = job["video_transition_mode"]
    if job.get("voice_name"):
        scheduled_args.voice_name = job["voice_name"]
    if job.get("video_script_prompt"):
        scheduled_args.video_script_prompt = job["video_script_prompt"]
    for option in ("match_materials_to_script", "smart_scene_queries"):
        if job.get(option) is not None:
            setattr(scheduled_args, option, job[option])
    if job.get("openmontage_auto_materials"):
        output_path = openmontage_materials.find_openmontage_output(
            scheduled_args.video_subject,
            prefer_silent=True,
            video_aspect=scheduled_args.video_aspect,
            language=scheduled_args.video_language,
        )
        if output_path:
            try:
                output_report = openmontage_materials.validate_openmontage_output(
                    output_path,
                    video_aspect=scheduled_args.video_aspect,
                )
            except Exception:
                output_report = {}
            if not isinstance(output_report, dict) or not output_report.get("valid"):
                logger.warning(
                    "Scheduled job '{}' skipped an invalid OpenMontage output.",
                    job.get("name", ""),
                )
                output_path = None
        if output_path:
            scheduled_args.video_source = "local"
            scheduled_args.video_materials = output_path
            logger.info(
                "Scheduled job '{}' selected a matching native OpenMontage output.",
                job.get("name", ""),
            )
    return scheduled_args


def _select_scheduled_job_subject(job: dict) -> str:
    def highest_ranked_subject(candidates: list[str]) -> str:
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0]
        try:
            ranked = publish_insights.rank_subject_candidates(
                candidates,
                history.list_history(),
            )
        except Exception:
            return candidates[0]
        for item in ranked:
            subject = str(item.get("subject") or "").strip()
            if subject:
                return subject
        return candidates[0]

    subject_pool = job.get("video_subject_pool")
    if isinstance(subject_pool, list) and subject_pool:
        fresh_subjects = []
        for subject in subject_pool:
            if not _find_recent_scheduled_subjects(subject):
                fresh_subjects.append(subject)
        ranked_subject = highest_ranked_subject(fresh_subjects)
        if ranked_subject:
            return ranked_subject

    rss_trend_query = str(job.get("rss_trend_query") or "").strip()
    if rss_trend_query:
        try:
            rss_trend_language = str(job.get("rss_trend_language") or "").strip()
            rss_kwargs = {"language": rss_trend_language} if rss_trend_language else {}
            rss_summary = rss_trend.fetch_rss_trend(rss_trend_query, **rss_kwargs)
        except Exception:
            logger.warning("Scheduled RSS trend lookup failed.")
            rss_summary = ""
        fresh_subjects = []
        for title in rss_summary.split(";"):
            subject = title.strip()
            if subject and not _find_recent_scheduled_subjects(subject):
                fresh_subjects.append(subject)
        ranked_subject = highest_ranked_subject(fresh_subjects)
        if ranked_subject:
            return ranked_subject

    static_subject = str(job.get("video_subject") or "").strip()
    if subject_pool or rss_trend_query:
        if static_subject and not _find_recent_scheduled_subjects(static_subject):
            return static_subject
        return ""
    return static_subject


def _find_recent_scheduled_subjects(subject: str) -> list[dict]:
    enabled = config.app.get("twelvelabs_semantic_duplicate_check", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled or not twelvelabs.is_enabled():
        return history.find_recent_similar_subjects(subject)

    try:
        candidate_limit = min(
            20,
            max(0, int(config.app.get("twelvelabs_semantic_duplicate_max_candidates", 12))),
        )
    except (TypeError, ValueError):
        candidate_limit = 12
    try:
        semantic_threshold = float(
            config.app.get("twelvelabs_semantic_duplicate_threshold", 0.82)
        )
    except (TypeError, ValueError):
        semantic_threshold = 0.82
    return history.find_recent_similar_subjects(
        subject,
        semantic_similarity=twelvelabs.semantic_text_similarity,
        semantic_threshold=min(1.0, max(0.0, semantic_threshold)),
        semantic_candidate_limit=candidate_limit,
    )


def _normalize_scheduled_failed_aspects(result: dict | None) -> list[str] | None:
    if not isinstance(result, dict):
        return None

    raw_failed_aspects = result.get("failed_aspects")
    if raw_failed_aspects is None:
        return None
    if isinstance(raw_failed_aspects, str):
        raw_failed_aspects = [raw_failed_aspects]
    if not isinstance(raw_failed_aspects, (list, tuple)):
        return []

    supported_aspects = set(_VIDEO_ASPECT_CHOICES)
    return list(
        dict.fromkeys(
            str(aspect).strip()
            for aspect in raw_failed_aspects
            if str(aspect).strip() in supported_aspects
        )
    )


def _scheduled_failure_result(task_id: str) -> dict | None:
    try:
        task_state = state.get_task(task_id)
    except Exception:
        logger.warning("Scheduled task failure details are unavailable.")
        return None

    failed_aspects = _normalize_scheduled_failed_aspects(task_state)
    if failed_aspects is None:
        return None
    return {"failed_aspects": failed_aspects}


def _record_scheduled_job_history(
    *,
    task_id: str,
    job: dict,
    params: VideoParams,
    result: dict | None = None,
    report: dict | None = None,
    status: str = "completed",
    error: str = "",
) -> dict:
    safe_result = result if isinstance(result, dict) else {}
    safe_report = report if isinstance(report, dict) else {}
    raw_video_aspects = [getattr(params, "video_aspect", None)]
    additional_video_aspects = getattr(params, "video_aspects", None)
    if isinstance(additional_video_aspects, (list, tuple, set)):
        raw_video_aspects.extend(additional_video_aspects)
    elif additional_video_aspects:
        raw_video_aspects.append(additional_video_aspects)
    video_aspects = []
    for raw_aspect in raw_video_aspects:
        aspect = str(getattr(raw_aspect, "value", raw_aspect) or "").strip()
        if aspect and aspect not in video_aspects:
            video_aspects.append(aspect)
    entry = {
        "task_id": task_id,
        "subject": params.video_subject,
        "script": params.video_script,
        "language": params.video_language or "",
        "video_aspect": video_aspects[0] if video_aspects else "",
        "video_aspects": video_aspects,
        "audio_duration": safe_result.get("audio_duration"),
        "llm_provider": config.app.get("llm_provider", ""),
        "voice_name": params.voice_name,
        "custom_audio_file": params.custom_audio_file,
        "video_source": str(getattr(params, "video_source", "") or ""),
        "video_transition_mode": str(
            getattr(
                getattr(params, "video_transition_mode", None),
                "value",
                getattr(params, "video_transition_mode", ""),
            )
            or ""
        ),
        "status": status,
        "videos": safe_result.get("videos", []),
        "materials": safe_result.get("materials", []),
        "material_attributions": safe_result.get("material_attributions"),
        "terms": safe_result.get("terms") or params.video_terms,
        "viral_analysis": safe_report.get("script_analysis"),
        "thumbnail_candidates": safe_result.get("thumbnail_candidates"),
        "thumbnail_candidate_error": safe_result.get("thumbnail_candidate_error", ""),
        "cooldown": safe_result.get("cooldown"),
        "render_quality_reports": safe_result.get("render_quality_reports"),
        "pending_uploads": safe_result.get("pending_uploads"),
        "partial_success": bool(safe_result.get("partial_success")),
        "failed_aspects": _normalize_scheduled_failed_aspects(safe_result),
        "scheduled_job": job["name"],
        "error": error,
    }
    try:
        cost_snapshot = cost_estimate.estimate_history_cost(entry)
    except Exception:
        logger.warning("Scheduled cost estimate is unavailable.")
        cost_snapshot = {}
    if not isinstance(cost_snapshot, dict):
        logger.warning("Scheduled cost estimate has an unexpected format.")
        cost_snapshot = {}
    entry["cost_estimate"] = cost_snapshot
    history.add_history(entry)
    notification_status = "partial_success" if entry["partial_success"] else status
    scheduled_job_notifications.notify_scheduled_job_attention(
        entry["scheduled_job"], notification_status
    )
    return cost_snapshot


def _attach_scheduled_thumbnail_candidates(
    task_id: str,
    result: dict | None,
    report: dict | None,
) -> None:
    if not isinstance(result, dict) or not isinstance(report, dict):
        return
    viral_analysis = report.get("script_analysis")
    if not isinstance(viral_analysis, dict):
        return

    try:
        thumbnail_result = thumbnail.generate_thumbnail_candidates(
            task_id=task_id,
            video_paths=result.get("videos", []),
            thumbnail_concepts=viral_analysis.get("thumbnail_concepts"),
            hook_timestamps=viral_analysis.get("thumbnail_timestamps"),
        )
    except Exception:
        logger.error("Scheduled thumbnail generation failed.")
        result["thumbnail_candidate_error"] = "Scheduled thumbnail generation failed."
        return

    candidates = thumbnail_result.get("candidates") or []
    error = thumbnail_result.get("error") or ""
    if candidates:
        result["thumbnail_candidates"] = candidates
    if error:
        result["thumbnail_candidate_error"] = error


def _run_scheduled_job(args: argparse.Namespace) -> int:
    try:
        job = scheduled_jobs.get_scheduled_job(args.scheduled_job)
    except scheduled_jobs.ScheduledJobError:
        print(json.dumps({"started": False, "error": "Scheduled job is unavailable."}))
        return 2

    static_subject = str(job.get("video_subject") or "").strip()
    has_dynamic_subjects = bool(
        job.get("video_subject_pool")
        or str(job.get("rss_trend_query") or "").strip()
    )
    if static_subject and not has_dynamic_subjects:
        recent_subjects = _find_recent_scheduled_subjects(static_subject)
        if recent_subjects:
            logger.warning(
                "Scheduled job '{}' has a recent duplicate subject.", job["name"]
            )
            if job.get("skip_if_recent_duplicate"):
                print(
                    json.dumps(
                        {
                            "started": False,
                            "skipped": True,
                            "job": job["name"],
                            "reason": "recent_duplicate",
                        }
                    )
                )
                return 0

    selected_subject = _select_scheduled_job_subject(job)
    if not selected_subject:
        print(
            json.dumps(
                {
                    "started": False,
                    "job": job["name"],
                    "reason": "no_fresh_subject",
                }
            )
        )
        return 1
    job = {**job, "video_subject": selected_subject}

    scheduled_args = _scheduled_job_args(args, job)
    params = build_video_params(scheduled_args)
    task_id = args.task_id or utils.get_uuid()
    cost_history_entries = history.list_history()
    try:
        monthly_cost_cap = cost_estimate.evaluate_monthly_cost_cap(
            cost_history_entries,
            cap_usd=config.app.get("cost_estimate_monthly_cap_usd", 0),
        )
    except Exception:
        logger.warning("Scheduled cost limit check is unavailable; continuing.")
        monthly_cost_cap = {"enabled": False, "allowed": True}
    if monthly_cost_cap.get("enabled") and not monthly_cost_cap.get("allowed"):
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            result=_scheduled_failure_result(task_id),
            status="blocked",
            error="Scheduled cost limit blocked generation.",
        )
        print(
            json.dumps(
                {
                    "started": False,
                    "job": job["name"],
                    "reason": "cost_limit",
                    "monthly_known_cost_usd": monthly_cost_cap.get(
                        "known_total_usd", 0.0
                    ),
                    "monthly_cost_cap_usd": monthly_cost_cap.get("cap_usd", 0.0),
                }
            )
        )
        return 1
    try:
        fallback_providers = config.app.get("scheduled_llm_fallback_providers", [])
        script_kwargs = (
            {"fallback_providers": fallback_providers} if fallback_providers else {}
        )
        video_script = tm.generate_script(task_id, params, **script_kwargs)
    except Exception:
        logger.error("Scheduled script generation failed.")
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            status="failed",
            error="Scheduled script generation failed.",
        )
        print(
            json.dumps(
                {"started": False, "job": job["name"], "reason": "script_generation"}
            )
        )
        return 1
    if not video_script or "Error: " in video_script:
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            status="failed",
            error="Scheduled script generation failed.",
        )
        print(
            json.dumps(
                {"started": False, "job": job["name"], "reason": "script_generation"}
            )
        )
        return 1

    params.video_script = video_script
    try:
        projected_cost_estimate = cost_estimate.estimate_history_cost(
            {
                "script": params.video_script,
                "llm_provider": config.app.get("llm_provider", ""),
                "voice_name": params.voice_name,
                "custom_audio_file": params.custom_audio_file,
            }
        )
        monthly_cost_cap = cost_estimate.evaluate_monthly_cost_cap(
            cost_history_entries,
            cap_usd=config.app.get("cost_estimate_monthly_cap_usd", 0),
            projected_cost_estimate=projected_cost_estimate,
        )
    except Exception:
        logger.warning("Scheduled projected cost check is unavailable; continuing.")
        monthly_cost_cap = {"enabled": False, "allowed": True}
    if monthly_cost_cap.get("enabled") and not monthly_cost_cap.get("allowed"):
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            result=_scheduled_failure_result(task_id),
            status="blocked",
            error="Scheduled cost limit blocked generation.",
        )
        print(
            json.dumps(
                {
                    "started": False,
                    "job": job["name"],
                    "reason": "cost_limit",
                    "monthly_known_cost_usd": monthly_cost_cap.get(
                        "known_total_usd", 0.0
                    ),
                    "projected_known_total_usd": monthly_cost_cap.get(
                        "projected_known_total_usd", 0.0
                    ),
                    "monthly_cost_cap_usd": monthly_cost_cap.get("cap_usd", 0.0),
                    "projected_unknown_components": monthly_cost_cap.get(
                        "projected_unknown_components", []
                    ),
                }
            )
        )
        return 1
    try:
        report = content_quality.build_preflight_report(
            video_subject=params.video_subject,
            video_script=params.video_script,
            platform="tiktok",
            language=params.video_language or "auto",
        )
        quality_gate = content_quality.evaluate_quality_gate(
            report,
            enabled=True,
            threshold=config.app.get(
                "viral_quality_gate_threshold",
                content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
            ),
        )
    except Exception:
        logger.error("Scheduled quality preflight failed.")
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            status="failed",
            error="Scheduled quality preflight failed.",
        )
        print(
            json.dumps(
                {"started": False, "job": job["name"], "reason": "quality_preflight"}
            )
        )
        return 1
    if quality_gate.get("warn") or quality_gate.get("score") is None:
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            report=report,
            status="blocked",
            error="Scheduled quality gate blocked generation.",
        )
        print(
            json.dumps(
                {
                    "started": False,
                    "job": job["name"],
                    "reason": "quality_gate",
                    "score": quality_gate.get("score"),
                    "threshold": quality_gate.get("threshold"),
                }
            )
        )
        return 1

    quality_config = build_video_quality_config(scheduled_args)
    logger.info(f"start scheduled cli task: {task_id}, job: {job['name']}")
    try:
        with video_service.video_quality_config(quality_config):
            result = tm.start(
                task_id=task_id,
                params=params,
                stop_at="video",
                require_upload_review=True,
                fallback_providers=fallback_providers or None,
            )
    except Exception:
        logger.error("Scheduled video generation failed.")
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            result=_scheduled_failure_result(task_id),
            report=report,
            status="failed",
            error="Scheduled video generation failed.",
        )
        print(
            json.dumps(
                {"started": False, "job": job["name"], "reason": "generation"}
            )
        )
        return 1
    if not result:
        _record_scheduled_job_history(
            task_id=task_id,
            job=job,
            params=params,
            result=_scheduled_failure_result(task_id),
            report=report,
            status="failed",
            error="Scheduled video generation failed.",
        )
        print(
            json.dumps(
                {"started": False, "job": job["name"], "reason": "generation"}
            )
        )
        return 1

    _attach_scheduled_thumbnail_candidates(task_id, result, report)
    cost_snapshot = _record_scheduled_job_history(
        task_id=task_id,
        job=job,
        params=params,
        result=result,
        report=report,
    )
    monthly_cost_warning = cost_estimate.evaluate_monthly_cost_warning(
        history.list_history(),
        threshold_usd=config.app.get("cost_estimate_monthly_warning_usd", 0),
    )
    pending_uploads = result.get("pending_uploads") if isinstance(result, dict) else []
    summary = {
        "started": True,
        "job": job["name"],
        "task_id": task_id,
        "pending_uploads": len(pending_uploads or []),
        "estimated_known_cost_usd": cost_snapshot.get(
            "estimated_known_total_usd", 0.0
        ),
        "cost_unknown_components": cost_snapshot.get("unknown_components") or [],
    }
    if monthly_cost_warning["enabled"]:
        summary.update(
            {
                "monthly_cost_warning": monthly_cost_warning["warning"],
                "monthly_known_cost_usd": monthly_cost_warning["known_total_usd"],
                "monthly_cost_warning_threshold_usd": monthly_cost_warning[
                    "threshold_usd"
                ],
                "monthly_cost_unknown_jobs": monthly_cost_warning[
                    "unknown_job_count"
                ],
            }
        )
    if monthly_cost_cap.get("enabled"):
        summary.update(
            {
                "monthly_cost_cap_usd": monthly_cost_cap.get("cap_usd", 0.0),
                "monthly_projected_known_total_usd": monthly_cost_cap.get(
                    "projected_known_total_usd", 0.0
                ),
                "monthly_projected_unknown_components": monthly_cost_cap.get(
                    "projected_unknown_components", []
                ),
            }
        )
    if isinstance(result, dict) and result.get("partial_success"):
        summary["partial_success"] = True
        summary["failed_aspects"] = _normalize_scheduled_failed_aspects(result) or []
    print(
        json.dumps(summary)
    )
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sync_metrics:
        return _run_metrics_sync(
            args.sync_metrics_limit,
            dry_run=args.sync_metrics_dry_run,
        )
    if args.publish_insights:
        return _run_publish_insights()
    if args.cleanup_output:
        return _run_output_cleanup(
            args.output_retention_days,
            apply=args.apply_output_cleanup,
        )
    if args.cleanup_cache_videos:
        return _run_cache_video_cleanup(
            args.cache_video_retention_days,
            apply=args.apply_cache_video_cleanup,
        )
    if args.export_state is not None:
        return _run_state_export(args.export_state)
    if args.resume_task:
        return _run_task_resume(args.resume_task)
    if args.validate_openmontage:
        return _run_openmontage_validation()
    if args.backfill_render_quality:
        return _run_render_quality_backfill(args.apply_render_quality_backfill)
    if args.visual_review_task:
        return _run_visual_review_task(args.visual_review_task)
    if args.quality_baseline:
        return _run_quality_baseline()
    if args.calibrate_amf:
        return _run_amf_calibration(args.video_aspect)
    if args.benchmark_material_topic is not None:
        return _run_material_benchmark(
            args.benchmark_material_topic,
            args.video_aspect,
        )
    if args.inspect_scene_materials is not None:
        return _run_scene_material_inspection(
            args.inspect_scene_materials,
            args.video_aspect,
        )
    if args.visual_policy_topic is not None:
        return _run_visual_policy(args.visual_policy_topic)
    if args.scan_visual_duplicates:
        return _run_visual_duplicate_scan()
    if args.check_video_encoder:
        return _run_video_encoder_check(
            [args.video_aspect, *(args.video_aspects or [])]
        )
    if args.repurpose_video:
        return _run_short_clip_plan(
            args.repurpose_video,
            args.repurpose_clip_duration,
            args.repurpose_clip_count,
            args.repurpose_output_dir,
            args.repurpose_render_mode,
            args.video_codec or repurpose.DEFAULT_PRECISE_VIDEO_CODEC,
            args.repurpose_subtitle_file,
            args.repurpose_aspect,
        )
    if args.list_scheduled_jobs:
        return _run_scheduled_job_list()
    if args.scheduled_job_dry_run:
        return _run_scheduled_job_dry_run(args.scheduled_job)
    if args.scheduled_job:
        return _run_scheduled_job(args)

    try:
        params = build_video_params(args)
        prepare_cli_files(params, stop_at=args.stop_at)
        quality_config = build_video_quality_config(args)
    except (ValueError, OSError) as exc:
        logger.error(f"invalid CLI input: {exc}")
        return 2

    task_id = args.task_id or utils.get_uuid()
    logger.info(f"start cli task: {task_id}, stop_at: {args.stop_at}")
    try:
        with video_service.video_quality_config(quality_config):
            result = tm.start(task_id=task_id, params=params, stop_at=args.stop_at)
    except Exception as exc:
        logger.exception(
            f"CLI task failed with an unexpected error: task_id={task_id}, error={exc}"
        )
        return 1
    if not result or result.get("state") == const.TASK_STATE_FAILED:
        failed_stage = result.get("failed_stage", "unknown") if result else "unknown"
        error = result.get("error", "unknown task error") if result else "empty result"
        logger.error(
            f"CLI task failed: task_id={task_id}, stop_at={args.stop_at}, "
            f"stage={failed_stage}, error={error}"
        )
        return 1

    print(json.dumps({"task_id": task_id, "result": result}, ensure_ascii=False))
    return 0


def _force_utf8_console() -> None:
    """Keep Unicode results printable on Windows legacy console code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


if __name__ == "__main__":
    _force_utf8_console()
    raise SystemExit(run_cli())
