from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from typing import Any

from loguru import logger

from app.services import content_intelligence, history, llm, viral_analyzer
from app.utils import utils

DEFAULT_PREFLIGHT_IDEA_COUNT = 3
DEFAULT_PREFLIGHT_DAYS = 7
DEFAULT_PREFLIGHT_DAILY_COUNT = 1
DEFAULT_PREFLIGHT_LOOKBACK_DAYS = history.DEFAULT_SUBJECT_LOOKBACK_DAYS
DEFAULT_QUALITY_GATE_THRESHOLD = 60
SCRIPT_REWRITE_REJECTION_LOG = "script_rewrite_rejections.jsonl"
MAX_REWRITE_SUBJECT_LENGTH = 500
MAX_REWRITE_SCRIPT_LENGTH = 8000
MAX_REWRITE_CONTEXT_LENGTH = 1200
MAX_REWRITE_LOG_ERROR_LENGTH = 500
MAX_REWRITE_LOG_WARNING_LENGTH = 180
SCRIPT_SCORE_KEYS = ("overall_score", "hook_score", "pacing_score")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clamp_text(value: Any, max_length: int) -> str:
    text = _clean_text(value)
    if len(text) > max_length:
        return text[:max_length].rstrip()
    return text


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


def _analysis_lines(analysis: dict[str, Any] | None) -> list[str]:
    if not isinstance(analysis, dict):
        return []

    lines: list[str] = []
    for key, label in (
        ("overall_score", "Overall score"),
        ("hook_score", "Hook score"),
        ("pacing_score", "Pacing score"),
    ):
        if analysis.get(key) is not None:
            lines.append(f"{label}: {analysis.get(key)}/100")

    for key, label in (
        ("summary", "Summary"),
        ("warnings", "Warnings"),
        ("hook_suggestions", "Hook suggestions"),
        ("title_variants", "Title variants"),
    ):
        value = analysis.get(key)
        if isinstance(value, (list, tuple)):
            text = "; ".join(_clean_text(item) for item in value if _clean_text(item))
        else:
            text = _clean_text(value)
        if text:
            lines.append(f"{label}: {text}")
    return lines


def _score_value(value: Any) -> int | None:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_script_rewrite_rejection_log_path(create: bool = False) -> str:
    return os.path.join(
        utils.storage_dir("logs", create=create),
        SCRIPT_REWRITE_REJECTION_LOG,
    )


def _analysis_score_summary(analysis: dict[str, Any] | None) -> dict[str, int | None]:
    source = analysis if isinstance(analysis, dict) else {}
    return {key: _score_value(source.get(key)) for key in SCRIPT_SCORE_KEYS}


def _analysis_warning_summary(analysis: dict[str, Any] | None) -> list[str]:
    if not isinstance(analysis, dict):
        return []
    warnings = analysis.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    if not isinstance(warnings, (list, tuple)):
        return []
    return [
        _clamp_text(warning, MAX_REWRITE_LOG_WARNING_LENGTH)
        for warning in warnings[:3]
        if _clean_text(warning)
    ]


def log_script_rewrite_rejection(
    *,
    reason: str,
    video_subject: str = "",
    viral_analysis: dict[str, Any] | None = None,
    platform: str = "tiktok",
    language: str = "auto",
    error: str = "",
) -> dict[str, Any]:
    event = {
        "created_at": _utc_now_iso(),
        "reason": _clamp_text(reason, 80),
        "video_subject": _clamp_text(video_subject, MAX_REWRITE_SUBJECT_LENGTH),
        "platform": _clamp_text(platform, 64) or "tiktok",
        "language": _clamp_text(language, 64) or "auto",
        "scores": _analysis_score_summary(viral_analysis),
        "warnings": _analysis_warning_summary(viral_analysis),
        "error": _clamp_text(error, MAX_REWRITE_LOG_ERROR_LENGTH),
    }

    try:
        log_path = get_script_rewrite_rejection_log_path(create=True)
        with open(log_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"failed to log script rewrite rejection: {exc}")
    return event


def build_script_score_comparison(
    original_analysis: dict[str, Any] | None,
    improved_analysis: dict[str, Any] | None,
) -> dict[str, dict[str, int | None]]:
    comparison: dict[str, dict[str, int | None]] = {}
    original = original_analysis if isinstance(original_analysis, dict) else {}
    improved = improved_analysis if isinstance(improved_analysis, dict) else {}

    for key in SCRIPT_SCORE_KEYS:
        before = _score_value(original.get(key))
        after = _score_value(improved.get(key))
        delta = after - before if before is not None and after is not None else None
        comparison[key] = {
            "before": before,
            "after": after,
            "delta": delta,
        }
    return comparison


def _script_improvement_result(
    *,
    original_script: str,
    improved_script: str = "",
    original_analysis: dict[str, Any] | None = None,
    improved_analysis: dict[str, Any] | None = None,
    source: str = "unavailable",
    error: str = "",
) -> dict[str, Any]:
    return {
        "original_script": original_script,
        "improved_script": improved_script,
        "original_analysis": (
            original_analysis if isinstance(original_analysis, dict) else None
        ),
        "improved_analysis": (
            improved_analysis if isinstance(improved_analysis, dict) else None
        ),
        "score_comparison": build_script_score_comparison(
            original_analysis,
            improved_analysis,
        ),
        "source": source,
        "error": error,
    }


def build_script_improvement_prompt(
    *,
    video_subject: str = "",
    video_script: str = "",
    viral_analysis: dict[str, Any] | None = None,
    platform: str = "tiktok",
    language: str = "auto",
) -> str:
    subject = _clamp_text(video_subject, MAX_REWRITE_SUBJECT_LENGTH)
    script = _clamp_text(video_script, MAX_REWRITE_SCRIPT_LENGTH)
    analysis_context = _clamp_text(
        "\n".join(_analysis_lines(viral_analysis)),
        MAX_REWRITE_CONTEXT_LENGTH,
    )
    platform = _clamp_text(platform, 64) or "tiktok"
    language = _clamp_text(language, 64) or "auto"

    return f"""
# Role: Short-Video Script Rewrite Assistant

Improve the script below using the viral analysis notes. Preserve the same core
meaning and factual claims. Do not invent statistics, sources, or guarantees.

## Requirements
1. Return ONLY the improved script text. No markdown, labels, JSON, or commentary.
2. Keep the language as {language}; if language is auto, use the script language.
3. Improve the first 3 seconds hook, pacing, clarity, and CTA when weak.
4. Keep it suitable for {platform}.
5. Do not overwrite the original script; this is only a suggestion.

## Subject
{subject}

## Viral analysis notes
{analysis_context or "No analysis notes were provided."}

## Original script
{script}
""".strip()


def _extract_rewritten_script(response: str) -> str:
    text = _clean_text(response)
    if text.startswith("```"):
        text = re.sub(r"^```(?:text|markdown|json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    if text.startswith("{"):
        try:
            data = json.loads(text)
            for key in ("improved_script", "script", "rewrite"):
                if _clean_text(data.get(key)):
                    return _clamp_text(data.get(key), MAX_REWRITE_SCRIPT_LENGTH)
        except Exception:
            pass

    return _clamp_text(text, MAX_REWRITE_SCRIPT_LENGTH)


def suggest_improved_script(
    *,
    video_subject: str = "",
    video_script: str = "",
    viral_analysis: dict[str, Any] | None = None,
    platform: str = "tiktok",
    language: str = "auto",
    title: str = "",
    video_duration_sec: int | float | None = None,
    social_caption: str = "",
    hashtags: list[str] | tuple[str, ...] | str | None = None,
    material_attributions: (
        list[dict[str, Any]] | tuple[dict[str, Any], ...] | None
    ) = None,
) -> dict[str, Any]:
    original_script = _clamp_text(video_script, MAX_REWRITE_SCRIPT_LENGTH)
    if not original_script:
        return _script_improvement_result(
            original_script="",
            original_analysis=viral_analysis,
            error="Original script is empty.",
        )

    prompt = build_script_improvement_prompt(
        video_subject=video_subject,
        video_script=original_script,
        viral_analysis=viral_analysis,
        platform=platform,
        language=language,
    )

    try:
        response = llm._generate_response(prompt)
        if isinstance(response, str) and "Error: " in response:
            raise ValueError(response)
        improved_script = _extract_rewritten_script(response)
    except Exception as exc:
        log_script_rewrite_rejection(
            reason="llm_error",
            video_subject=video_subject,
            viral_analysis=viral_analysis,
            platform=platform,
            language=language,
            error=str(exc),
        )
        return _script_improvement_result(
            original_script=original_script,
            original_analysis=viral_analysis,
            error=str(exc),
        )

    if not improved_script:
        log_script_rewrite_rejection(
            reason="empty_output",
            video_subject=video_subject,
            viral_analysis=viral_analysis,
            platform=platform,
            language=language,
        )
        return _script_improvement_result(
            original_script=original_script,
            original_analysis=viral_analysis,
            error="No useful rewrite was generated.",
        )

    if improved_script.strip() == original_script.strip():
        log_script_rewrite_rejection(
            reason="same_output",
            video_subject=video_subject,
            viral_analysis=viral_analysis,
            platform=platform,
            language=language,
        )
        return _script_improvement_result(
            original_script=original_script,
            original_analysis=viral_analysis,
            error="No useful rewrite was generated.",
        )

    selected_platform = _clamp_text(platform, 64) or "tiktok"
    selected_language = _clamp_text(language, 64) or "auto"
    improved_analysis = viral_analyzer.analyze_viral_potential(
        video_subject=video_subject,
        video_script=improved_script,
        title=title,
        video_duration_sec=video_duration_sec,
        target_platforms=[selected_platform],
        language=selected_language,
        social_caption=social_caption,
        hashtags=hashtags,
        material_attributions=material_attributions,
    )

    return _script_improvement_result(
        original_script=original_script,
        improved_script=improved_script,
        original_analysis=viral_analysis,
        improved_analysis=improved_analysis,
        source="llm",
        error="",
    )
