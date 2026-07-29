import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from loguru import logger

from app.services import llm, rss_trend

MAX_CONTENT_SUBJECT_LENGTH = 500
MAX_CONTENT_SCRIPT_LENGTH = 8000
MAX_CONTENT_CONTEXT_LENGTH = 500
MAX_CONTENT_TONE_LENGTH = 128
MAX_TREND_CONTEXT_LENGTH = 500
MAX_FALLBACK_ERROR_WARNING_LENGTH = 300
MAX_RAW_LLM_RESPONSE_LOG_LENGTH = 1200
TREND_SOURCE_NONE = "none"
TREND_SOURCE_STATIC = "static"
TREND_SOURCE_RSS = "rss"
ANGLE_HOOK_TEMPLATES = {
    "beginner guide": "New to {subject}? Here's where most people start wrong.",
    "common mistake": "This is the mistake almost everyone makes with {subject}.",
    "quick checklist": "Here's a fast checklist most people skip for {subject}.",
    "before and after": "See what changes when you fix this about {subject}.",
    "myth versus reality": "You've probably been told the wrong thing about {subject}.",
    "three practical tips": "Three things that actually work for {subject}.",
    "simple daily routine": "A simple routine most people never try for {subject}.",
}
NON_RETRYABLE_CONTENT_PLAN_ERROR_MARKERS = (
    "api_key is not set",
    "quota",
    "rate limit",
    "rate-limit",
    "authentication",
    "unauthorized",
    "forbidden",
)


def _clean_text(value: Any, max_length: int, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    if len(text) > max_length:
        logger.warning("content intelligence input was truncated")
        text = text[:max_length]
    return text


def _clean_source_url(value: Any) -> str:
    url = _clean_text(value, 2048)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _normalize_days(days: int) -> int:
    return 7 if int(days or 7) <= 7 else 14


def _normalize_daily_count(daily_count: int) -> int:
    return max(1, min(3, int(daily_count or 1)))


def _normalize_idea_count(idea_count: int) -> int:
    return max(1, min(30, int(idea_count or 7)))


def _normalize_trend_source(source: str) -> str:
    source = _clean_text(source, 32, TREND_SOURCE_NONE).lower()
    if source in {TREND_SOURCE_STATIC, TREND_SOURCE_RSS}:
        return source
    return TREND_SOURCE_NONE


@dataclass
class TrendContextItem:
    title: str
    insight: str
    search_terms: list[str]
    source_url: str = ""
    publisher: str = ""


@dataclass(frozen=True)
class TrendContextSource:
    title: str
    url: str
    publisher: str = ""


@dataclass(frozen=True)
class TrendContextResult:
    text: str = ""
    warnings: tuple[str, ...] = ()
    source: str = TREND_SOURCE_NONE
    sources: tuple[TrendContextSource, ...] = ()


class TrendContextAdapter(Protocol):
    def fetch(
        self, query: str, platform: str = "tiktok", limit: int = 3
    ) -> list[TrendContextItem]:
        ...


class StaticTrendContextAdapter:
    source = TREND_SOURCE_STATIC

    _items = [
        (
            ("ai", "automation", "robot", "chatgpt", "yapay zeka"),
            TrendContextItem(
                title="AI workflow explainers",
                insight=(
                    "Short, practical AI workflow ideas are useful when framed as "
                    "specific before-and-after examples."
                ),
                search_terms=["AI workflow", "automation", "productivity"],
            ),
        ),
        (
            ("money", "finance", "budget", "crypto", "investment", "para"),
            TrendContextItem(
                title="Personal finance clarity",
                insight=(
                    "Finance topics work best when a single mistake, checklist, or "
                    "simple habit is explained without performance promises."
                ),
                search_terms=["budgeting", "saving money", "finance tips"],
            ),
        ),
        (
            ("health", "fitness", "wellness", "sleep", "sağlık"),
            TrendContextItem(
                title="Everyday wellness routines",
                insight=(
                    "Health content should stay educational and practical, focusing "
                    "on routines, myths, and safe general guidance."
                ),
                search_terms=["wellness routine", "healthy habits", "sleep tips"],
            ),
        ),
        (
            ("travel", "trip", "city", "food", "coffee", "seyahat"),
            TrendContextItem(
                title="Save-worthy local guides",
                insight=(
                    "Travel and food plans are stronger when they offer a compact "
                    "route, checklist, or hidden-detail angle."
                ),
                search_terms=["local guide", "travel tips", "coffee shops"],
            ),
        ),
    ]

    _generic = TrendContextItem(
        title="Practical short-form framing",
        insight=(
            "Use concrete hooks, one clear promise, and searchable B-roll terms. "
            "Avoid claiming that the idea is currently trending."
        ),
        search_terms=["short video", "how to", "quick tips"],
    )

    def fetch(
        self, query: str, platform: str = "tiktok", limit: int = 3
    ) -> list[TrendContextItem]:
        query_normalized = (query or "").lower()
        matches = []
        for keywords, item in self._items:
            if any(keyword in query_normalized for keyword in keywords):
                matches.append(item)
            if len(matches) >= limit:
                break

        if not matches:
            matches = [self._generic]
        return matches[:limit]


class RssTrendContextAdapter:
    source = TREND_SOURCE_RSS

    def fetch(
        self, query: str, platform: str = "tiktok", limit: int = 3
    ) -> list[TrendContextItem]:
        source_items = rss_trend.fetch_rss_trend_items(query, limit=limit)
        items = []
        for source_item in source_items:
            if not isinstance(source_item, dict):
                continue
            title = _clean_text(source_item.get("title"), 120)
            if not title:
                continue
            items.append(
                TrendContextItem(
                    title=title,
                    insight=(
                        "Recent RSS headline for planning context only; do not "
                        "treat it as popularity, ranking, or virality data."
                    ),
                    search_terms=[query, title],
                    source_url=_clean_source_url(source_item.get("url")),
                    publisher=_clean_text(source_item.get("publisher"), 120),
                )
            )
        return items[:limit]


def _get_trend_adapter(source: str) -> TrendContextAdapter | None:
    if source == TREND_SOURCE_STATIC:
        return StaticTrendContextAdapter()
    if source == TREND_SOURCE_RSS:
        return RssTrendContextAdapter()
    return None


def get_trend_context(
    video_subject: str = "",
    platform: str = "tiktok",
    enabled: bool = False,
    source: str = TREND_SOURCE_NONE,
    adapter: TrendContextAdapter | None = None,
) -> TrendContextResult:
    normalized_source = _normalize_trend_source(source)
    if not enabled or normalized_source == TREND_SOURCE_NONE:
        return TrendContextResult(source=TREND_SOURCE_NONE)

    adapter = adapter or _get_trend_adapter(normalized_source)
    if adapter is None:
        return TrendContextResult(
            warnings=("Trend context source is unavailable; planning continued without it.",),
            source=normalized_source,
        )

    try:
        items = adapter.fetch(video_subject, platform=platform, limit=3)
    except Exception as exc:
        logger.warning(f"failed to fetch trend context: {str(exc)}")
        return TrendContextResult(
            warnings=("Trend context was unavailable; planning continued without it.",),
            source=normalized_source,
        )

    lines = []
    sources = []
    for item in items:
        title = _clean_text(getattr(item, "title", ""), 120)
        insight = _clean_text(getattr(item, "insight", ""), 260)
        terms = ", ".join(_normalize_search_terms(getattr(item, "search_terms", []), "short video"))
        if title and insight:
            lines.append(f"- {title}: {insight} Search terms: {terms}.")
            source_url = _clean_source_url(getattr(item, "source_url", ""))
            if source_url:
                sources.append(
                    TrendContextSource(
                        title=title,
                        url=source_url,
                        publisher=_clean_text(getattr(item, "publisher", ""), 120),
                    )
                )

    context = _clean_text("\n".join(lines), MAX_TREND_CONTEXT_LENGTH)
    if not context:
        warning = (
            "RSS trend context returned no usable items; planning continued without it."
            if normalized_source == TREND_SOURCE_RSS
            else "Trend context had no usable items; planning continued without it."
        )
        return TrendContextResult(
            warnings=(warning,),
            source=normalized_source,
        )

    warning = (
        "RSS trend context was used as planning input; it is not ranking, popularity, or virality data."
        if normalized_source == TREND_SOURCE_RSS
        else "Static trend context was used as planning input; it is not live trend or popularity data."
    )
    return TrendContextResult(
        text=context,
        warnings=(warning,),
        source=normalized_source,
        sources=tuple(sources),
    )


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def _json_object_from_response(response: str) -> dict:
    data = None
    try:
        data = json.loads(_strip_code_fence(response))
    except Exception as exc:
        logger.warning(f"content intelligence response was not plain JSON: {exc}")
        match = re.search(r"\{.*\}", response or "", re.DOTALL)
        if match:
            data = json.loads(match.group())

    if not isinstance(data, dict):
        raise ValueError("content intelligence response is not a JSON object")
    return data


def _normalize_search_terms(value: Any, fallback_subject: str) -> list[str]:
    if isinstance(value, str):
        raw_terms = re.split(r"[,，\n]", value)
    elif isinstance(value, list):
        raw_terms = value
    else:
        raw_terms = []

    terms = []
    for term in raw_terms:
        clean = _clean_text(term, 80)
        if clean and clean not in terms:
            terms.append(clean)
        if len(terms) >= 5:
            break

    if not terms:
        terms = [fallback_subject or "short video idea"]
    return terms


def _normalize_warning_items(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_warnings = [value]
    elif isinstance(value, list):
        raw_warnings = value
    else:
        raw_warnings = []
    return [
        _clean_text(item, 300)
        for item in raw_warnings
        if _clean_text(item, 300)
    ]


def _fallback_error_warning(last_error: str = "") -> str:
    clean_error = _clean_text(
        last_error,
        MAX_FALLBACK_ERROR_WARNING_LENGTH,
    )
    return f"LLM planning failed: {clean_error}" if clean_error else ""


def _is_non_retryable_content_plan_error(error_message: str) -> bool:
    text = (error_message or "").lower()
    if any(marker in text for marker in NON_RETRYABLE_CONTENT_PLAN_ERROR_MARKERS):
        return True
    return bool(
        re.search(r"\berror:\s*(401|403|429)\b", text)
        or re.search(r"\b(401|403|429)\s+post\b", text)
    )


def _log_raw_llm_response_excerpt(response: str) -> None:
    if not response or str(response).lstrip().startswith("Error: "):
        return
    excerpt = _clean_text(response, MAX_RAW_LLM_RESPONSE_LOG_LENGTH)
    if excerpt:
        logger.warning(f"content plan raw LLM response excerpt: {excerpt}")


def _trend_disclaimer(extra_warnings: Sequence[str] | None = None) -> str:
    warnings_text = " ".join(extra_warnings or [])
    if "RSS trend context" in warnings_text:
        return (
            "RSS headlines were used as editorial planning context only; these are "
            "not popularity, ranking, or virality claims."
        )
    return (
        "No live trend data was used; these are planning suggestions, not current "
        "popularity or virality claims."
    )


def _trend_source_payload(
    sources: Sequence[TrendContextSource] | None = None,
) -> list[dict[str, str]]:
    payload = []
    seen_urls = set()
    for source in sources or ():
        url = _clean_source_url(getattr(source, "url", ""))
        title = _clean_text(getattr(source, "title", ""), 120)
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        payload.append(
            {
                "title": title,
                "url": url,
                "publisher": _clean_text(getattr(source, "publisher", ""), 120),
            }
        )
    return payload


def _normalize_idea(raw: Any, index: int, platform: str) -> dict | None:
    if not isinstance(raw, dict):
        return None

    subject = _clean_text(raw.get("subject"), 160)
    if not subject:
        return None

    angle = _clean_text(raw.get("angle"), 220, default=f"Part {index + 1}")
    hook = _clean_text(raw.get("hook"), 220, default=f"Why {subject} matters")
    script_prompt = _clean_text(
        raw.get("script_prompt"),
        600,
        default=f"Write a concise short-form video script about {subject}.",
    )
    rationale = _clean_text(
        raw.get("rationale"),
        300,
        default="Generated as a planning suggestion, not a live trend claim.",
    )

    return {
        "subject": subject,
        "angle": angle,
        "hook": hook,
        "script_prompt": script_prompt,
        "search_terms": _normalize_search_terms(raw.get("search_terms"), subject),
        "platform": _clean_text(raw.get("platform"), 64, default=platform),
        "rationale": rationale,
    }


def _build_calendar_from_ideas(
    ideas: list[dict],
    days: int,
    daily_count: int,
    start_date: date | None = None,
) -> list[dict]:
    if not ideas:
        return []

    start = start_date or date.today()
    calendar = []
    total_items = days * daily_count
    for index in range(total_items):
        idea = ideas[index % len(ideas)]
        day_number = (index // daily_count) + 1
        item_date = start + timedelta(days=day_number - 1)
        calendar.append(
            {
                "day": day_number,
                "date": item_date.isoformat(),
                "subject": idea["subject"],
                "format": "short_video",
                "goal": idea["angle"],
                "script_prompt": idea["script_prompt"],
            }
        )
    return calendar


def _normalize_calendar_item(raw: Any, index: int, ideas: list[dict]) -> dict | None:
    if not isinstance(raw, dict):
        return None

    fallback = ideas[index % len(ideas)] if ideas else {}
    subject = _clean_text(raw.get("subject"), 160, default=fallback.get("subject", ""))
    if not subject:
        return None

    return {
        "day": max(1, int(raw.get("day") or 1)),
        "date": _clean_text(raw.get("date"), 32),
        "subject": subject,
        "format": _clean_text(raw.get("format"), 80, default="short_video"),
        "goal": _clean_text(raw.get("goal"), 220, default=fallback.get("angle", "")),
        "script_prompt": _clean_text(
            raw.get("script_prompt"),
            600,
            default=fallback.get(
                "script_prompt",
                f"Write a concise short-form video script about {subject}.",
            ),
        ),
    }


def _normalize_plan_payload(
    payload: dict,
    platform: str,
    days: int,
    daily_count: int,
    idea_count: int,
    source: str,
    extra_warnings: Sequence[str] | None = None,
    trend_sources: Sequence[TrendContextSource] | None = None,
) -> dict:
    ideas = []
    for raw in payload.get("ideas") or []:
        idea = _normalize_idea(raw, len(ideas), platform)
        if idea:
            ideas.append(idea)
        if len(ideas) >= idea_count:
            break

    if not ideas:
        raise ValueError("content intelligence response has no usable ideas")

    calendar = []
    total_calendar_items = days * daily_count
    for raw in payload.get("calendar") or []:
        item = _normalize_calendar_item(raw, len(calendar), ideas)
        if item:
            calendar.append(item)
        if len(calendar) >= total_calendar_items:
            break

    if len(calendar) < total_calendar_items:
        calendar = _build_calendar_from_ideas(ideas, days, daily_count)

    warnings = _normalize_warning_items(payload.get("warnings"))
    disclaimer = _trend_disclaimer(extra_warnings)
    if disclaimer not in warnings:
        warnings.insert(0, disclaimer)
    for warning in extra_warnings or []:
        clean_warning = _clean_text(warning, 300)
        if clean_warning and clean_warning not in warnings:
            warnings.append(clean_warning)

    return {
        "ideas": ideas,
        "calendar": calendar,
        "warnings": warnings,
        "source": source,
        "trend_sources": _trend_source_payload(trend_sources),
    }


def _fallback_ideas(
    video_subject: str,
    target_audience: str,
    platform: str,
    idea_count: int,
) -> list[dict]:
    base_subject = video_subject or target_audience or "short video idea"
    angles = [
        "beginner guide",
        "common mistake",
        "quick checklist",
        "before and after",
        "myth versus reality",
        "three practical tips",
        "simple daily routine",
    ]

    ideas = []
    for index in range(idea_count):
        angle = angles[index % len(angles)]
        subject = f"{base_subject}: {angle}"
        hook_template = ANGLE_HOOK_TEMPLATES.get(
            angle,
            "Most people miss this about {subject}.",
        )
        ideas.append(
            {
                "subject": subject,
                "angle": angle,
                "hook": hook_template.format(subject=base_subject),
                "script_prompt": (
                    f"Write a concise short-form video script about {subject}. "
                    "Keep it practical and easy to follow."
                ),
                "search_terms": [base_subject, angle, "short video"],
                "platform": platform,
                "rationale": "Fallback planning idea generated without live trend data.",
            }
        )
    return ideas


def fallback_content_plan(
    video_subject: str = "",
    target_audience: str = "",
    platform: str = "tiktok",
    days: int = 7,
    daily_count: int = 1,
    idea_count: int = 7,
    start_date: date | None = None,
    extra_warnings: Sequence[str] | None = None,
    trend_sources: Sequence[TrendContextSource] | None = None,
    last_error: str = "",
) -> dict:
    days = _normalize_days(days)
    daily_count = _normalize_daily_count(daily_count)
    idea_count = _normalize_idea_count(idea_count)
    video_subject = _clean_text(video_subject, MAX_CONTENT_SUBJECT_LENGTH, "short video")
    target_audience = _clean_text(target_audience, MAX_CONTENT_CONTEXT_LENGTH)
    platform = _clean_text(platform, 64, "tiktok")

    ideas = _fallback_ideas(
        video_subject=video_subject,
        target_audience=target_audience,
        platform=platform,
        idea_count=idea_count,
    )
    warnings = [
        _trend_disclaimer(extra_warnings),
        "Fallback plan used because the LLM response was unavailable or invalid.",
    ]
    error_warning = _fallback_error_warning(last_error)
    if error_warning:
        warnings.append(error_warning)
    for warning in extra_warnings or []:
        clean_warning = _clean_text(warning, 300)
        if clean_warning and clean_warning not in warnings:
            warnings.append(clean_warning)

    return {
        "ideas": ideas,
        "calendar": _build_calendar_from_ideas(ideas, days, daily_count, start_date),
        "warnings": warnings,
        "source": "fallback",
        "trend_sources": _trend_source_payload(trend_sources),
    }


def build_content_plan_prompt(
    video_subject: str = "",
    video_script: str = "",
    language: str = "auto",
    platform: str = "tiktok",
    target_audience: str = "",
    tone: str = "",
    days: int = 7,
    daily_count: int = 1,
    idea_count: int = 7,
    trend_context: str = "",
) -> str:
    video_subject = _clean_text(video_subject, MAX_CONTENT_SUBJECT_LENGTH)
    video_script = _clean_text(video_script, MAX_CONTENT_SCRIPT_LENGTH)
    language = _clean_text(language, 64, "auto")
    platform = _clean_text(platform, 64, "tiktok")
    target_audience = _clean_text(target_audience, MAX_CONTENT_CONTEXT_LENGTH)
    tone = _clean_text(tone, MAX_CONTENT_TONE_LENGTH)
    days = _normalize_days(days)
    daily_count = _normalize_daily_count(daily_count)
    idea_count = _normalize_idea_count(idea_count)
    trend_context = _clean_text(trend_context, MAX_TREND_CONTEXT_LENGTH)
    language_instruction = (
        "Use the same language as the video subject and script."
        if language == "auto"
        else f"Write all user-facing text in this language: {language}."
    )

    return f"""
# Role: Short-Video Content Planning Assistant

## Goal
Create practical short-form video ideas and a production calendar for {platform}.

## Safety and truthfulness constraints
1. Respond ONLY with a single valid minified JSON object. No markdown, no code fences, no commentary.
2. Do not claim live trends, current popularity, or guaranteed virality.
3. Do not imply that you used web, YouTube, TikTok, Google Trends, RSS, or real-time data.
4. Treat this as an editorial planning assistant using only the context below.
5. The response must parse with Python json.loads without repairs:
   escape quotes and newlines inside string values, and do not use trailing commas.
6. Keep every string value concise, preferably under 120 characters.
7. {language_instruction}

## JSON shape
Return exactly these top-level keys: "ideas", "calendar", "warnings".

"ideas" must contain {idea_count} objects. Each object must include:
- "subject": short video topic
- "angle": specific editorial angle
- "hook": opening hook
- "script_prompt": prompt that can be used to generate the script later
- "search_terms": 3 to 5 concrete B-roll/search terms
- "platform": target platform
- "rationale": why this is useful without making trend claims

"calendar" must be an empty array. The application will expand the ideas into
{days} days and {daily_count} item(s) per day after parsing.

"warnings" must include at least one sentence saying no live trend data was used.

## Context
Subject or niche: {video_subject}
Existing script or notes: {video_script}
Target audience: {target_audience}
Tone: {tone}
Platform: {platform}
Optional external planning context, not popularity data: {trend_context}
""".strip()


def generate_content_plan(
    video_subject: str = "",
    video_script: str = "",
    language: str = "auto",
    platform: str = "tiktok",
    target_audience: str = "",
    tone: str = "",
    days: int = 7,
    daily_count: int = 1,
    idea_count: int = 7,
    use_trend_context: bool = False,
    trend_source: str = TREND_SOURCE_NONE,
    trend_adapter=None,
) -> dict:
    days = _normalize_days(days)
    daily_count = _normalize_daily_count(daily_count)
    idea_count = _normalize_idea_count(idea_count)
    trend_context = get_trend_context(
        video_subject=video_subject,
        platform=platform,
        enabled=use_trend_context,
        source=trend_source,
        adapter=trend_adapter,
    )
    prompt = build_content_plan_prompt(
        video_subject=video_subject,
        video_script=video_script,
        language=language,
        platform=platform,
        target_audience=target_audience,
        tone=tone,
        days=days,
        daily_count=daily_count,
        idea_count=idea_count,
        trend_context=trend_context.text,
    )

    response = ""
    last_error = ""
    for index in range(getattr(llm, "_max_retries", 3)):
        try:
            response = llm._generate_response(prompt)
            if isinstance(response, str) and "Error: " in response:
                raise ValueError(response)
            payload = _json_object_from_response(response)
            return _normalize_plan_payload(
                payload,
                platform=platform,
                days=days,
                daily_count=daily_count,
                idea_count=idea_count,
                source="llm",
                extra_warnings=trend_context.warnings,
                trend_sources=trend_context.sources,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"failed to generate content plan: {last_error}")
            _log_raw_llm_response_excerpt(response)
            if _is_non_retryable_content_plan_error(last_error):
                logger.warning(
                    "content plan generation stopped because the error is not retryable"
                )
                break
            if index < getattr(llm, "_max_retries", 3) - 1:
                logger.warning(
                    f"failed to generate content plan, trying again... {index + 1}"
                )

    logger.warning("falling back to heuristic content plan")
    return fallback_content_plan(
        video_subject=video_subject,
        target_audience=target_audience,
        platform=platform,
        days=days,
        daily_count=daily_count,
        idea_count=idea_count,
        extra_warnings=trend_context.warnings,
        trend_sources=trend_context.sources,
        last_error=last_error,
    )
