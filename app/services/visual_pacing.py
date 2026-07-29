"""Small, non-mutating visual pacing measurements for finished task results."""

import math


_CUE_ALIGNMENT_TOLERANCE_SECONDS = 0.75


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _scene_count(value) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value)) if math.isfinite(value) else 0
    if isinstance(value, (str, bytes)):
        return 1 if str(value).strip() else 0
    try:
        return max(0, len(value or []))
    except TypeError:
        return 0


def _cue_alignment_opportunities(
    planned_cut_times: list[float],
    cue_end_times,
) -> int:
    usable_cues = []
    for value in cue_end_times or []:
        try:
            cue_time = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(cue_time) and cue_time > 0:
            usable_cues.append(cue_time)
    return sum(
        any(
            abs(cue_time - cut_time) <= _CUE_ALIGNMENT_TOLERANCE_SECONDS
            for cue_time in usable_cues
        )
        for cut_time in planned_cut_times
    )


def build_visual_pacing_budget(
    audio_duration,
    max_clip_duration,
    *,
    scene_count=0,
    cue_end_times=None,
) -> dict:
    """Describe the current visual rhythm without altering render settings."""
    duration = _positive_float(audio_duration, 0.0)
    clip_duration = _positive_float(max_clip_duration, 5.0)
    if duration <= 0:
        return {
            "available": False,
            "reason": "invalid_audio_duration",
        }

    planned_visual_count = max(1, math.ceil(duration / clip_duration))
    seconds_per_visual = duration / planned_visual_count
    planned_cut_times = [
        round(index * seconds_per_visual, 3)
        for index in range(1, planned_visual_count)
    ]
    normalized_scene_count = _scene_count(scene_count)
    if normalized_scene_count <= 0:
        scene_coverage_status = "unavailable"
    elif normalized_scene_count >= planned_visual_count:
        scene_coverage_status = "sufficient"
    elif normalized_scene_count >= math.ceil(planned_visual_count * 0.6):
        scene_coverage_status = "partial"
    else:
        scene_coverage_status = "sparse"

    if seconds_per_visual < 2.0:
        pacing_status = "too_fast"
    elif seconds_per_visual > 7.0:
        pacing_status = "too_slow"
    else:
        pacing_status = "balanced"

    recommended_clip_duration = clip_duration
    if normalized_scene_count > 0:
        recommended_clip_duration = min(7.0, max(2.0, duration / normalized_scene_count))

    cue_alignment_count = _cue_alignment_opportunities(
        planned_cut_times,
        cue_end_times,
    )
    return {
        "available": True,
        "audio_duration_seconds": round(duration, 3),
        "configured_max_clip_duration_seconds": round(clip_duration, 3),
        "planned_visual_count": planned_visual_count,
        "planned_cut_count": len(planned_cut_times),
        "seconds_per_visual": round(seconds_per_visual, 3),
        "pacing_status": pacing_status,
        "scene_count": normalized_scene_count,
        "scene_coverage_status": scene_coverage_status,
        "recommended_clip_duration_seconds": round(recommended_clip_duration, 3),
        "cue_alignment_opportunity_count": cue_alignment_count,
        "cue_alignment_ratio": round(
            cue_alignment_count / len(planned_cut_times) if planned_cut_times else 1.0,
            3,
        ),
    }
