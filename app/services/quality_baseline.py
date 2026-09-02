import json
import os
from collections import Counter

from app.services import history, render_quality
from app.utils import utils


DEFAULT_REQUIRED_VIDEO_COUNT = 5
AUTOMATIC_BASELINE_FILENAME = "render_quality_baseline.json"


def _positive_count(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _non_negative_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _recent_final_video_paths(max_videos: int) -> list[str]:
    task_root = os.path.join(utils.storage_dir(), "tasks")
    if not os.path.isdir(task_root):
        return []

    video_paths = []
    for root, _, files in os.walk(task_root):
        for name in files:
            if not name.casefold().startswith("final-") or not name.casefold().endswith(
                ".mp4"
            ):
                continue
            video_path = os.path.join(root, name)
            if not os.path.isfile(video_path):
                continue
            try:
                modified_at = os.path.getmtime(video_path)
            except OSError:
                continue
            video_paths.append((modified_at, video_path))

    video_paths.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [video_path for _, video_path in video_paths[:max_videos]]


def build_render_quality_baseline(
    reports,
    *,
    required_video_count: int = DEFAULT_REQUIRED_VIDEO_COUNT,
) -> dict:
    """Summarize real render reports without treating a small sample as a baseline."""
    required_count = _positive_count(required_video_count, DEFAULT_REQUIRED_VIDEO_COUNT)
    normalized_reports = [report for report in reports or [] if isinstance(report, dict)]
    durations = []
    frame_rates = []
    warning_counts = Counter()
    resolution_counts = Counter()
    color_status_counts = Counter()
    quality_ok_count = 0
    audio_track_count = 0
    near_silent_count = 0
    color_analyzed_video_count = 0
    color_mixed_video_count = 0
    color_warmth_spreads = []
    color_saturation_spreads = []

    for report in normalized_reports:
        duration = _non_negative_number(report.get("duration"))
        if duration is not None:
            durations.append(duration)
        frame_rate = _non_negative_number(report.get("fps"))
        if frame_rate is not None:
            frame_rates.append(frame_rate)
        if report.get("ok"):
            quality_ok_count += 1
        if report.get("has_audio"):
            audio_track_count += 1

        color_consistency = report.get("color_consistency")
        if isinstance(color_consistency, dict):
            color_status = str(color_consistency.get("status") or "").strip().lower()
            if color_status in {"consistent", "mixed", "unavailable"}:
                color_status_counts[color_status] += 1
                if color_status != "unavailable":
                    color_analyzed_video_count += 1
                    if color_status == "mixed":
                        color_mixed_video_count += 1
                    warmth_spread = _non_negative_number(
                        color_consistency.get("warmth_spread")
                    )
                    if warmth_spread is not None:
                        color_warmth_spreads.append(warmth_spread)
                    saturation_spread = _non_negative_number(
                        color_consistency.get("saturation_spread")
                    )
                    if saturation_spread is not None:
                        color_saturation_spreads.append(saturation_spread)

        resolution = report.get("resolution")
        if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
            try:
                resolution_counts[f"{int(resolution[0])}x{int(resolution[1])}"] += 1
            except (TypeError, ValueError):
                pass

        warnings = report.get("warnings") or []
        if isinstance(warnings, str):
            warnings = [warnings]
        for warning in warnings:
            normalized_warning = str(warning or "").strip()
            if not normalized_warning:
                continue
            warning_counts[normalized_warning] += 1
            if "near-silent" in normalized_warning:
                near_silent_count += 1

    available_count = len(normalized_reports)
    ready = available_count >= required_count
    return {
        "ready": ready,
        "status": "ready" if ready else "needs_more_videos",
        "required_video_count": required_count,
        "available_video_count": available_count,
        "video_paths": [
            str(report.get("video_path") or "")
            for report in normalized_reports
            if str(report.get("video_path") or "")
        ],
        "quality_ok_count": quality_ok_count,
        "quality_warning_count": sum(warning_counts.values()),
        "near_silent_count": near_silent_count,
        "audio_track_count": audio_track_count,
        "average_duration": round(sum(durations) / len(durations), 2)
        if durations
        else None,
        "average_fps": round(sum(frame_rates) / len(frame_rates), 2)
        if frame_rates
        else None,
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "warning_counts": dict(sorted(warning_counts.items())),
        "color_consistency": {
            "status": (
                "mixed"
                if color_mixed_video_count
                else "consistent"
                if color_analyzed_video_count
                else "unavailable"
            ),
            "analyzed_video_count": color_analyzed_video_count,
            "mixed_video_count": color_mixed_video_count,
            "status_counts": dict(sorted(color_status_counts.items())),
            "average_warmth_spread": round(
                sum(color_warmth_spreads) / len(color_warmth_spreads), 4
            )
            if color_warmth_spreads
            else None,
            "average_saturation_spread": round(
                sum(color_saturation_spreads) / len(color_saturation_spreads), 4
            )
            if color_saturation_spreads
            else None,
        },
    }


def collect_render_quality_baseline(
    *,
    max_videos: int = DEFAULT_REQUIRED_VIDEO_COUNT,
) -> dict:
    requested_count = _positive_count(max_videos, DEFAULT_REQUIRED_VIDEO_COUNT)
    video_paths = _recent_final_video_paths(requested_count)
    reports = []
    for video_path in video_paths:
        try:
            report = render_quality.inspect_rendered_video(video_path)
        except Exception:
            report = {
                "ok": False,
                "warnings": ["rendered video could not be inspected"],
            }
        if not isinstance(report, dict):
            report = {
                "ok": False,
                "warnings": ["rendered video could not be inspected"],
            }
        reports.append({"video_path": video_path, **report})

    return build_render_quality_baseline(
        reports,
        required_video_count=requested_count,
    )


def _report_items(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(report) for report in value if isinstance(report, dict)]


def _report_key(report: dict) -> str:
    video_path = str(report.get("video_path") or "").strip()
    if video_path:
        return f"path:{video_path}"
    return "report:" + json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)


def _unique_reports(*groups) -> list[dict]:
    reports = []
    seen = set()
    for group in groups:
        for report in _report_items(group):
            key = _report_key(report)
            if key in seen:
                continue
            seen.add(key)
            reports.append(report)
    return reports


def _recent_history_reports(max_videos: int) -> list[dict]:
    reports = []
    for entry in history.list_history():
        if not isinstance(entry, dict):
            continue
        reports.extend(_report_items(entry.get("render_quality_reports")))
        if len(reports) >= max_videos:
            break
    return reports[:max_videos]


def _automatic_baseline_path() -> str:
    return os.path.join(utils.storage_dir(), AUTOMATIC_BASELINE_FILENAME)


def _load_automatic_baseline_state() -> dict:
    try:
        with open(_automatic_baseline_path(), encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_automatic_baseline_state(state: dict) -> None:
    state_path = _automatic_baseline_path()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    temporary_path = f"{state_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, sort_keys=True)
    os.replace(temporary_path, state_path)


def _non_negative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _baseline_attention_summary(baseline: dict) -> tuple[str | None, str]:
    if not isinstance(baseline, dict) or not baseline.get("ready"):
        return None, ""

    available_count = _non_negative_int(baseline.get("available_video_count"))
    failed_count = max(
        0,
        available_count - _non_negative_int(baseline.get("quality_ok_count")),
    )
    warning_count = _non_negative_int(baseline.get("quality_warning_count"))
    near_silent_count = _non_negative_int(baseline.get("near_silent_count"))
    color_consistency = baseline.get("color_consistency")
    color_consistency = (
        color_consistency if isinstance(color_consistency, dict) else {}
    )
    mixed_color_count = _non_negative_int(color_consistency.get("mixed_video_count"))

    details = []
    if failed_count:
        details.append(f"{failed_count} video kalite kontrolünden geçemedi")
    if warning_count:
        details.append(f"{warning_count} kalite uyarısı var")
    if near_silent_count:
        details.append(f"{near_silent_count} videoda ses çok düşük")
    if mixed_color_count:
        details.append(f"{mixed_color_count} videoda renk tutarlılığı zayıf")
    if not details:
        return None, ""

    signature = json.dumps(
        {
            "failed": failed_count,
            "warnings": warning_count,
            "near_silent": near_silent_count,
            "mixed_color": mixed_color_count,
        },
        sort_keys=True,
    )
    return f"Son {available_count} videoda " + "; ".join(details) + ".", signature


def refresh_automatic_render_quality_baseline(
    reports,
    *,
    max_videos: int = DEFAULT_REQUIRED_VIDEO_COUNT,
) -> dict:
    """Persist a rolling baseline from reports already collected during rendering."""
    requested_count = _positive_count(max_videos, DEFAULT_REQUIRED_VIDEO_COUNT)
    state = _load_automatic_baseline_state()
    recent_reports = _unique_reports(reports, state.get("reports"))
    if len(recent_reports) < requested_count:
        recent_reports = _unique_reports(
            recent_reports,
            _recent_history_reports(requested_count),
        )
    recent_reports = recent_reports[:requested_count]
    baseline = build_render_quality_baseline(
        recent_reports,
        required_video_count=requested_count,
    )
    summary, signature = _baseline_attention_summary(baseline)
    notification_summary = (
        summary if summary and signature != state.get("attention_signature") else None
    )
    _save_automatic_baseline_state(
        {
            "reports": recent_reports,
            "baseline": baseline,
            "attention_signature": signature,
        }
    )
    return {"baseline": baseline, "notification_summary": notification_summary}
