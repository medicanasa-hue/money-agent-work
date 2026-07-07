from __future__ import annotations

from typing import Any

from app.services import content_intelligence, history, viral_analyzer

DEFAULT_PREFLIGHT_IDEA_COUNT = 3
DEFAULT_PREFLIGHT_DAYS = 7
DEFAULT_PREFLIGHT_DAILY_COUNT = 1
DEFAULT_PREFLIGHT_LOOKBACK_DAYS = history.DEFAULT_SUBJECT_LOOKBACK_DAYS
DEFAULT_QUALITY_GATE_THRESHOLD = 60


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def build_preflight_fingerprint(
    video_subject: str = "",
    video_script: str = "",
    platform: str = "tiktok",
    language: str = "auto",
) -> dict[str, str]:
    return {
        "subject": _clean_text(video_subject),
        "script": _clean_text(video_script),
        "platform": _clean_text(platform).lower() or "tiktok",
        "language": _clean_text(language) or "auto",
    }


def is_preflight_report_stale(
    report: dict[str, Any] | None,
    *,
    video_subject: str = "",
    video_script: str = "",
    platform: str = "tiktok",
    language: str = "auto",
) -> bool:
    if not isinstance(report, dict):
        return True
    return report.get("fingerprint") != build_preflight_fingerprint(
        video_subject=video_subject,
        video_script=video_script,
        platform=platform,
        language=language,
    )


def build_preflight_report(
    *,
    video_subject: str = "",
    video_script: str = "",
    platform: str = "tiktok",
    language: str = "auto",
    target_audience: str = "",
    tone: str = "",
    use_trend_context: bool = False,
    trend_source: str = content_intelligence.TREND_SOURCE_NONE,
    idea_count: int = DEFAULT_PREFLIGHT_IDEA_COUNT,
    days: int = DEFAULT_PREFLIGHT_DAYS,
    daily_count: int = DEFAULT_PREFLIGHT_DAILY_COUNT,
    lookback_days: int = DEFAULT_PREFLIGHT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    subject = _clean_text(video_subject)
    script = _clean_text(video_script)
    selected_platform = _clean_text(platform).lower() or "tiktok"
    selected_language = _clean_text(language) or "auto"

    content_subject = subject or script
    content_plan = content_intelligence.generate_content_plan(
        video_subject=content_subject,
        video_script=script,
        language=selected_language,
        platform=selected_platform,
        target_audience=target_audience,
        tone=tone,
        days=days,
        daily_count=daily_count,
        idea_count=idea_count,
        use_trend_context=use_trend_context,
        trend_source=(
            trend_source
            if use_trend_context
            else content_intelligence.TREND_SOURCE_NONE
        ),
    )

    repeat_matches = history.find_recent_similar_subjects(
        subject,
        days=lookback_days,
    )

    script_analysis = None
    if script:
        script_analysis = viral_analyzer.analyze_viral_potential(
            video_subject=subject,
            video_script=script,
            target_platforms=[selected_platform],
            language=selected_language,
        )

    return {
        "fingerprint": build_preflight_fingerprint(
            video_subject=subject,
            video_script=script,
            platform=selected_platform,
            language=selected_language,
        ),
        "content_plan": content_plan,
        "repeat_matches": repeat_matches,
        "script_analysis": script_analysis,
    }


def evaluate_quality_gate(
    report: dict[str, Any] | None,
    *,
    enabled: bool = False,
    threshold: int = DEFAULT_QUALITY_GATE_THRESHOLD,
) -> dict[str, Any]:
    try:
        normalized_threshold = max(0, min(100, int(threshold)))
    except (TypeError, ValueError):
        normalized_threshold = DEFAULT_QUALITY_GATE_THRESHOLD

    score = None
    if isinstance(report, dict):
        script_analysis = report.get("script_analysis")
        if isinstance(script_analysis, dict):
            try:
                score = int(script_analysis.get("overall_score"))
            except (TypeError, ValueError):
                score = None

    return {
        "enabled": bool(enabled),
        "threshold": normalized_threshold,
        "score": score,
        "warn": bool(enabled and score is not None and score < normalized_threshold),
    }
