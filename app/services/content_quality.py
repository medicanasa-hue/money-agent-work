from __future__ import annotations

import json
import re
from typing import Any

from app.services import content_intelligence, history, llm, viral_analyzer

DEFAULT_PREFLIGHT_IDEA_COUNT = 3
DEFAULT_PREFLIGHT_DAYS = 7
DEFAULT_PREFLIGHT_DAILY_COUNT = 1
DEFAULT_PREFLIGHT_LOOKBACK_DAYS = history.DEFAULT_SUBJECT_LOOKBACK_DAYS
DEFAULT_QUALITY_GATE_THRESHOLD = 60
MAX_REWRITE_SUBJECT_LENGTH = 500
MAX_REWRITE_SCRIPT_LENGTH = 8000
MAX_REWRITE_CONTEXT_LENGTH = 1200


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
) -> dict[str, Any]:
    original_script = _clamp_text(video_script, MAX_REWRITE_SCRIPT_LENGTH)
    if not original_script:
        return {
            "original_script": "",
            "improved_script": "",
            "source": "unavailable",
            "error": "Original script is empty.",
        }

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
        return {
            "original_script": original_script,
            "improved_script": "",
            "source": "unavailable",
            "error": str(exc),
        }

    if not improved_script or improved_script.strip() == original_script.strip():
        return {
            "original_script": original_script,
            "improved_script": "",
            "source": "unavailable",
            "error": "No useful rewrite was generated.",
        }

    return {
        "original_script": original_script,
        "improved_script": improved_script,
        "source": "llm",
        "error": "",
    }
