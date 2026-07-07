import json
import re
from typing import Any

from loguru import logger

from app.services import llm


DEFAULT_TARGET_PLATFORMS = ["tiktok", "youtube_shorts", "instagram_reels"]
MAX_ANALYSIS_SUBJECT_LENGTH = 500
MAX_ANALYSIS_SCRIPT_LENGTH = 8000
MAX_ANALYSIS_TITLE_LENGTH = 200
MAX_ANALYSIS_LANGUAGE_LENGTH = 64


def _clamp_text(value: Any, max_length: int) -> str:
    text = ("" if value is None else str(value)).strip()
    if len(text) > max_length:
        return text[:max_length].rstrip()
    return text


def _clamp_score(value: Any, default: int = 50) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


def _normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    choice = str(value or "").strip().lower()
    return choice if choice in allowed else default


def _normalize_string_list(value: Any, limit: int = 5, max_length: int = 140) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[\n;]+", value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = []

    result: list[str] = []
    seen = set()
    for item in items:
        text = _clamp_text(item, max_length)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_platforms(platforms: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if isinstance(platforms, str):
        candidates = [platforms]
    elif isinstance(platforms, (list, tuple)):
        candidates = [str(item) for item in platforms]
    else:
        candidates = []

    result: list[str] = []
    for item in candidates:
        platform = item.strip().lower()
        if not platform or platform in result:
            continue
        result.append(platform)
    return result or list(DEFAULT_TARGET_PLATFORMS)


def _normalize_platform_fit(value: Any, platforms: list[str]) -> dict[str, float]:
    if not isinstance(value, dict):
        value = {}

    result: dict[str, float] = {}
    for platform in platforms:
        raw = value.get(platform, value.get(platform.replace("_", " ")))
        try:
            score = float(raw)
        except (TypeError, ValueError):
            score = 0.6
        if score > 1:
            score = score / 100
        result[platform] = round(max(0.0, min(1.0, score)), 2)
    return result


def _strip_code_fence(response: str) -> str:
    text = (response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(response: str) -> dict[str, Any]:
    text = _strip_code_fence(response)
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group())

    if not isinstance(data, dict):
        raise ValueError("viral analysis response is not a JSON object")
    return data


def _first_sentence(script: str, subject: str) -> str:
    text = (script or subject or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?。！？])\s+", text, maxsplit=1)
    return parts[0].strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


def build_viral_analysis_prompt(
    video_subject: str,
    video_script: str = "",
    title: str = "",
    video_duration_sec: int | float | None = None,
    target_platforms: list[str] | tuple[str, ...] | str | None = None,
    language: str = "auto",
) -> str:
    subject = _clamp_text(video_subject, MAX_ANALYSIS_SUBJECT_LENGTH)
    script = _clamp_text(video_script, MAX_ANALYSIS_SCRIPT_LENGTH)
    title = _clamp_text(title, MAX_ANALYSIS_TITLE_LENGTH)
    language = _clamp_text(language or "auto", MAX_ANALYSIS_LANGUAGE_LENGTH) or "auto"
    platforms = _normalize_platforms(target_platforms)
    duration = video_duration_sec or "unknown"

    return f"""
# Role: Short-Video Pre-Publish Analyst

Analyze this short-video script before publishing. This is a creative quality
review, not a promise of real-world virality.

## Constraints
1. Respond ONLY with one valid minified JSON object. No markdown or commentary.
2. Scores must be integers from 0 to 100.
3. Keep suggestions practical and short.
4. Use the same language as the script unless language is explicitly set.

## Required JSON keys
{{
  "overall_score": 0,
  "hook_score": 0,
  "pacing_score": 0,
  "retention_curve": "strong|moderate|weak",
  "emotional_arc": "flat|crescendo|roller_coaster|anti_climax",
  "summary": "...",
  "hook_suggestions": ["...", "...", "..."],
  "title_variants": ["...", "...", "..."],
  "thumbnail_concepts": ["...", "...", "..."],
  "warnings": ["..."],
  "platform_fit": {{"tiktok": 0.0, "youtube_shorts": 0.0, "instagram_reels": 0.0}}
}}

## Context
Subject: {subject}
Title: {title}
Duration seconds: {duration}
Target platforms: {", ".join(platforms)}
Language: {language}

Script:
{script}
""".strip()


def _parse_viral_analysis(
    response: str,
    platforms: list[str],
    fallback_subject: str,
    fallback_script: str,
) -> dict[str, Any]:
    data = _extract_json_object(response)

    result = {
        "overall_score": _clamp_score(data.get("overall_score")),
        "hook_score": _clamp_score(data.get("hook_score")),
        "pacing_score": _clamp_score(data.get("pacing_score")),
        "retention_curve": _normalize_choice(
            data.get("retention_curve"),
            {"strong", "moderate", "weak"},
            "moderate",
        ),
        "emotional_arc": _normalize_choice(
            data.get("emotional_arc"),
            {"flat", "crescendo", "roller_coaster", "anti_climax"},
            "flat",
        ),
        "summary": _clamp_text(data.get("summary"), 240),
        "hook_suggestions": _normalize_string_list(data.get("hook_suggestions"), 5),
        "title_variants": _normalize_string_list(data.get("title_variants"), 5),
        "thumbnail_concepts": _normalize_string_list(
            data.get("thumbnail_concepts"), 5, 180
        ),
        "warnings": _normalize_string_list(data.get("warnings"), 6, 180),
        "platform_fit": _normalize_platform_fit(data.get("platform_fit"), platforms),
    }

    fallback = _fallback_viral_analysis(
        video_subject=fallback_subject,
        video_script=fallback_script,
        title="",
        video_duration_sec=None,
        target_platforms=platforms,
    )
    for key in ("hook_suggestions", "title_variants", "thumbnail_concepts"):
        if not result[key]:
            result[key] = fallback[key]
    if not result["summary"]:
        result["summary"] = fallback["summary"]
    return result


def _fallback_viral_analysis(
    video_subject: str,
    video_script: str = "",
    title: str = "",
    video_duration_sec: int | float | None = None,
    target_platforms: list[str] | tuple[str, ...] | str | None = None,
) -> dict[str, Any]:
    subject = _clamp_text(video_subject, MAX_ANALYSIS_SUBJECT_LENGTH) or "this topic"
    script = _clamp_text(video_script, MAX_ANALYSIS_SCRIPT_LENGTH)
    first = _first_sentence(script, subject)
    first_words = _word_count(first)
    total_words = _word_count(script)

    hook_score = 58
    if first_words and first_words <= 14:
        hook_score += 12
    if re.search(r"[?？]|\d|%|!", first):
        hook_score += 10
    if first_words > 22:
        hook_score -= 18

    duration = float(video_duration_sec or 0)
    if duration <= 0:
        duration = max(20.0, min(90.0, total_words / 2.4 if total_words else 45.0))
    words_per_minute = total_words / max(duration / 60.0, 0.1)
    if 115 <= words_per_minute <= 180:
        pacing_score = 78
    elif 90 <= words_per_minute < 115 or 180 < words_per_minute <= 220:
        pacing_score = 64
    else:
        pacing_score = 48

    warnings = []
    if first_words > 18:
        warnings.append("Opening sentence may be too slow for the first 3 seconds.")
    if total_words < 20:
        warnings.append("Script is very short; add one concrete payoff or detail.")
    if not re.search(
        r"\b(save|follow|comment|subscribe|share|watch|kaydet|takip|yorum|abone|paylas|paylaş)\b",
        script.lower(),
    ):
        warnings.append("No clear call to action was detected.")

    overall_score = round((hook_score * 0.45) + (pacing_score * 0.35) + 12)
    overall_score = _clamp_score(overall_score)
    hook_score = _clamp_score(hook_score)
    pacing_score = _clamp_score(pacing_score)

    platforms = _normalize_platforms(target_platforms)
    fit_base = round(max(0.35, min(0.92, overall_score / 100)), 2)

    clean_title = _clamp_text(title or subject, 80)
    return {
        "overall_score": overall_score,
        "hook_score": hook_score,
        "pacing_score": pacing_score,
        "retention_curve": "strong" if overall_score >= 75 else "moderate"
        if overall_score >= 55
        else "weak",
        "emotional_arc": "crescendo" if total_words >= 60 else "flat",
        "summary": "Rule-based review completed because AI analysis was unavailable.",
        "hook_suggestions": [
            f"What most people miss about {subject}",
            f"Before you ignore {subject}, watch this",
            f"The fastest way to understand {subject}",
        ],
        "title_variants": [
            clean_title,
            f"{clean_title}: what you need to know",
            f"Do not miss this about {clean_title}",
        ],
        "thumbnail_concepts": [
            "Large contrast text with one clear visual subject",
            "Before/after split showing the main change",
            "Close-up reaction plus one bold keyword",
        ],
        "warnings": warnings,
        "platform_fit": {platform: fit_base for platform in platforms},
    }


def analyze_viral_potential(
    video_subject: str,
    video_script: str = "",
    title: str = "",
    video_duration_sec: int | float | None = None,
    target_platforms: list[str] | tuple[str, ...] | str | None = None,
    language: str = "auto",
) -> dict[str, Any]:
    platforms = _normalize_platforms(target_platforms)
    subject = _clamp_text(video_subject, MAX_ANALYSIS_SUBJECT_LENGTH)
    script = _clamp_text(video_script, MAX_ANALYSIS_SCRIPT_LENGTH)
    title = _clamp_text(title, MAX_ANALYSIS_TITLE_LENGTH)

    prompt = build_viral_analysis_prompt(
        video_subject=subject,
        video_script=script,
        title=title,
        video_duration_sec=video_duration_sec,
        target_platforms=platforms,
        language=language,
    )

    response = ""
    try:
        response = llm._generate_response(prompt)
        if isinstance(response, str) and "Error: " in response:
            logger.warning(f"viral analysis LLM unavailable: {response}")
            raise ValueError(response)
        return _parse_viral_analysis(response, platforms, subject, script)
    except Exception as e:
        logger.warning(f"falling back to heuristic viral analysis: {str(e)}")
        return _fallback_viral_analysis(
            video_subject=subject,
            video_script=script,
            title=title,
            video_duration_sec=video_duration_sec,
            target_platforms=platforms,
        )
