"""Read-only performance suggestions from aggregated publish metrics."""

import math
import re
from statistics import median
from typing import Any

from app.services import history


DEFAULT_MINIMUM_SAMPLE_SIZE = 3
DEFAULT_SUBJECT_RANKING_MINIMUM_SAMPLE_SIZE = 2
_ENGAGEMENT_FIELDS = ("likes", "comments", "shares", "saves")


def _history_entries(entries) -> list[dict[str, Any]]:
    if isinstance(entries, (str, bytes)):
        return []
    try:
        return [entry for entry in (entries or []) if isinstance(entry, dict)]
    except TypeError:
        return []


def _quality_score(entry: dict[str, Any]) -> float | None:
    analysis = entry.get("viral_analysis")
    if not isinstance(analysis, dict):
        return None
    value = analysis.get("overall_score")
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return score


def _normalized_segment_value(value, *, limit: int = 64) -> str | None:
    normalized = " ".join(str(value or "").casefold().split())
    return normalized[:limit] or None


def _voice_segment(entry: dict[str, Any]) -> str | None:
    if entry.get("custom_audio_file"):
        return "custom_audio"
    return _normalized_segment_value(entry.get("voice_name"))


def _metric_sample_from_metrics(
    entry: dict[str, Any], metrics: dict[str, Any]
) -> dict[str, Any]:
    views = float(metrics["views"])
    engagements = float(sum(metrics[field] for field in _ENGAGEMENT_FIELDS))
    return {
        "views": views,
        "engagement_rate": engagements / views if views > 0 else None,
        "quality_score": _quality_score(entry),
        "language": _normalized_language(entry.get("language")),
        "video_aspects": _entry_video_aspects(entry),
        "duration_bucket": _entry_duration_bucket(entry),
        "llm_provider": _normalized_segment_value(entry.get("llm_provider")),
        "voice": _voice_segment(entry),
        "video_source": _normalized_segment_value(entry.get("video_source")),
        "video_transition": _normalized_segment_value(
            entry.get("video_transition_mode")
        ),
        "platform_metrics": metrics.get("platform_metrics"),
    }


def _metric_sample(entry: dict[str, Any]) -> dict[str, Any] | None:
    raw_metrics = entry.get("publish_metrics")
    if not isinstance(raw_metrics, dict):
        return None
    return _metric_sample_from_metrics(entry, history.normalize_publish_metrics(raw_metrics))


def _normalized_language(value) -> str | None:
    language = str(value or "").strip().casefold()
    return language[:32] if language else None


def _entry_video_aspects(entry: dict[str, Any]) -> tuple[str, ...]:
    raw_aspects = entry.get("video_aspects")
    if isinstance(raw_aspects, (str, bytes)):
        raw_aspects = [raw_aspects]
    if not isinstance(raw_aspects, (list, tuple, set)):
        raw_aspects = []
    raw_aspects = [*raw_aspects, entry.get("video_aspect")]
    aspects = []
    for value in raw_aspects:
        aspect = str(getattr(value, "value", value) or "").strip()
        if aspect and aspect not in aspects:
            aspects.append(aspect[:16])
    return tuple(aspects)


def _entry_duration_bucket(entry: dict[str, Any]) -> str | None:
    for field in ("audio_duration", "duration", "video_duration"):
        try:
            duration = float(entry.get(field))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(duration) or duration <= 0:
            continue
        if duration <= 30:
            return "short_up_to_30_seconds"
        if duration <= 60:
            return "medium_31_to_60_seconds"
        return "long_over_60_seconds"
    return None


def _median_or_none(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _quality_score_suggestion(
    quality_samples: list[dict[str, float]],
    minimum_sample_size: int,
) -> dict[str, Any] | None:
    if len(quality_samples) < minimum_sample_size:
        return None

    ranked_samples = sorted(quality_samples, key=lambda sample: sample["quality_score"])
    midpoint = len(ranked_samples) // 2
    lower_rate = _median_or_none(
        [sample["engagement_rate"] for sample in ranked_samples[:midpoint]]
    )
    upper_rate = _median_or_none(
        [sample["engagement_rate"] for sample in ranked_samples[midpoint:]]
    )
    if lower_rate is None or upper_rate is None:
        return None

    common_fields = {
        "lower_median_engagement_rate_percent": round(lower_rate * 100, 2),
        "upper_median_engagement_rate_percent": round(upper_rate * 100, 2),
    }
    if upper_rate > lower_rate * 1.15:
        return {
            "type": "quality_gate_alignment",
            "message": (
                "Higher script-quality scores have a stronger median engagement "
                "rate in this sample; keep the quality gate as a manual review signal."
            ),
            **common_fields,
        }
    if lower_rate > upper_rate * 1.15:
        return {
            "type": "quality_gate_recheck",
            "message": (
                "Higher script-quality scores do not yet show a stronger median "
                "engagement rate in this sample; review the quality gate manually."
            ),
            **common_fields,
        }
    return {
        "type": "quality_gate_inconclusive",
        "message": (
            "Script-quality scores and engagement are too close in this sample "
            "to support a workflow change."
        ),
        **common_fields,
    }


def _segment_summary(samples: list[dict[str, Any]], minimum_sample_size: int) -> dict:
    views = [sample["views"] for sample in samples]
    engagement_rates = [
        sample["engagement_rate"]
        for sample in samples
        if sample["engagement_rate"] is not None
    ]
    median_views = _median_or_none(views)
    median_engagement_rate = _median_or_none(engagement_rates)
    return {
        "sample_count": len(samples),
        "status": "ready"
        if len(samples) >= minimum_sample_size
        else "insufficient_data",
        "median_views": int(round(median_views)) if median_views is not None else 0,
        "median_engagement_rate_percent": round(median_engagement_rate * 100, 2)
        if median_engagement_rate is not None
        else None,
    }


def _platform_metric_samples(sample: dict[str, Any]):
    raw_platform_metrics = sample.get("platform_metrics")
    if not isinstance(raw_platform_metrics, dict):
        return

    for raw_platform, raw_metrics in raw_platform_metrics.items():
        platform = _normalized_segment_value(raw_platform)
        if not platform or not isinstance(raw_metrics, dict):
            continue
        platform_metrics = history.normalize_publish_metrics(raw_metrics)
        platform_sample = dict(sample)
        views = float(platform_metrics["views"])
        engagements = float(
            sum(platform_metrics[field] for field in _ENGAGEMENT_FIELDS)
        )
        platform_sample["views"] = views
        platform_sample["engagement_rate"] = engagements / views if views > 0 else None
        yield platform, platform_sample


def _performance_segments(
    samples: list[dict[str, Any]],
    minimum_sample_size: int,
) -> dict[str, dict[str, dict]]:
    grouped = {
        "language": {},
        "video_aspect": {},
        "duration": {},
        "platform": {},
        "llm_provider": {},
        "voice": {},
        "video_source": {},
        "video_transition": {},
    }

    def add(group_name: str, key: str | None, sample: dict[str, Any]) -> None:
        if key:
            grouped[group_name].setdefault(key, []).append(sample)

    for sample in samples:
        add("language", sample.get("language"), sample)
        for aspect in sample.get("video_aspects") or ():
            add("video_aspect", aspect, sample)
        add("duration", sample.get("duration_bucket"), sample)
        add("llm_provider", sample.get("llm_provider"), sample)
        add("voice", sample.get("voice"), sample)
        add("video_source", sample.get("video_source"), sample)
        add("video_transition", sample.get("video_transition"), sample)
        for platform, platform_sample in _platform_metric_samples(sample):
            add("platform", platform, platform_sample)

    return {
        group_name: {
            key: _segment_summary(group_samples, minimum_sample_size)
            for key, group_samples in sorted(group.items())
        }
        for group_name, group in grouped.items()
    }


def _subject_tokens(value) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", str(value or "").casefold())
        if len(token) > 2
    }


def _subject_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _candidate_subjects(candidates) -> list[str]:
    if isinstance(candidates, (str, bytes)):
        candidates = [candidates]
    try:
        values = list(candidates or [])
    except TypeError:
        return []
    subjects = []
    seen = set()
    for value in values:
        subject = " ".join(str(value or "").split())[:160]
        key = subject.casefold()
        if subject and key not in seen:
            seen.add(key)
            subjects.append(subject)
    return subjects


def rank_subject_candidates(
    candidates,
    entries,
    *,
    minimum_sample_size: int = DEFAULT_SUBJECT_RANKING_MINIMUM_SAMPLE_SIZE,
) -> list[dict[str, Any]]:
    """Rank equivalent scheduled-topic candidates from prior publish outcomes."""
    try:
        minimum_sample_size = max(1, int(minimum_sample_size))
    except (TypeError, ValueError):
        minimum_sample_size = DEFAULT_SUBJECT_RANKING_MINIMUM_SAMPLE_SIZE

    historical_samples = []
    for entry in _history_entries(entries):
        subject_tokens = _subject_tokens(entry.get("subject"))
        metric = _metric_sample(entry)
        if not subject_tokens or metric is None or metric["engagement_rate"] is None:
            continue
        historical_samples.append((subject_tokens, metric))

    ranked = []
    for index, subject in enumerate(_candidate_subjects(candidates)):
        candidate_tokens = _subject_tokens(subject)
        matches = [
            metric
            for historical_tokens, metric in historical_samples
            if _subject_similarity(candidate_tokens, historical_tokens) >= 0.5
        ]
        evidence_count = len(matches)
        if evidence_count >= minimum_sample_size:
            median_views = _median_or_none([match["views"] for match in matches])
            median_engagement_rate = _median_or_none(
                [match["engagement_rate"] for match in matches]
            )
            performance_score = (
                (median_engagement_rate or 0.0) * 100
                + math.log10(max(1.0, median_views or 0.0))
            )
            ranking_status = "performance_evidence"
        else:
            median_views = None
            median_engagement_rate = None
            performance_score = None
            ranking_status = "insufficient_evidence"
        ranked.append(
            {
                "subject": subject,
                "evidence_sample_count": evidence_count,
                "ranking_status": ranking_status,
                "historical_median_views": int(round(median_views))
                if median_views is not None
                else None,
                "historical_median_engagement_rate_percent": round(
                    median_engagement_rate * 100,
                    2,
                )
                if median_engagement_rate is not None
                else None,
                "performance_score": round(performance_score, 4)
                if performance_score is not None
                else None,
                "_original_index": index,
            }
        )
    ranked.sort(
        key=lambda item: (
            item["ranking_status"] != "performance_evidence",
            -(item["performance_score"] or 0.0),
            item["_original_index"],
        )
    )
    for item in ranked:
        item.pop("_original_index", None)
    return ranked


def build_publish_performance_insights(
    entries,
    *,
    minimum_sample_size: int = DEFAULT_MINIMUM_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Summarize publish outcomes without changing generation or publishing state."""
    try:
        minimum_sample_size = max(1, int(minimum_sample_size))
    except (TypeError, ValueError):
        minimum_sample_size = DEFAULT_MINIMUM_SAMPLE_SIZE

    samples = [
        sample
        for entry in _history_entries(entries)
        if (sample := _metric_sample(entry)) is not None
    ]
    engagement_rates = [
        sample["engagement_rate"]
        for sample in samples
        if sample["engagement_rate"] is not None
    ]
    quality_samples = [
        {
            "quality_score": sample["quality_score"],
            "engagement_rate": sample["engagement_rate"],
        }
        for sample in samples
        if sample["quality_score"] is not None
        and sample["engagement_rate"] is not None
    ]
    suggestions: list[dict[str, Any]] = []
    if len(samples) < minimum_sample_size:
        suggestions.append(
            {
                "type": "collect_metrics",
                "message": (
                    "Capture metrics for more published videos before changing "
                    "the production workflow."
                ),
            }
        )
    else:
        suggestions.append(
            {
                "type": "manual_review",
                "message": (
                    "Use these aggregated results as a review aid; generation and "
                    "publishing settings are not changed automatically."
                ),
            }
        )
        quality_suggestion = _quality_score_suggestion(
            quality_samples,
            minimum_sample_size,
        )
        if quality_suggestion is not None:
            suggestions.append(quality_suggestion)
        else:
            suggestions.append(
                {
                    "type": "collect_quality_scores",
                    "message": (
                        "Generate and retain more script-quality scores before "
                        "evaluating their relationship to engagement."
                    ),
                }
            )

    median_views = _median_or_none([sample["views"] for sample in samples])
    median_engagement_rate = _median_or_none(engagement_rates)
    return {
        "status": "ready" if len(samples) >= minimum_sample_size else "insufficient_data",
        "sample_size": len(samples),
        "minimum_sample_size": minimum_sample_size,
        "quality_score_sample_size": len(quality_samples),
        "median_views": int(round(median_views)) if median_views is not None else 0,
        "median_engagement_rate_percent": round(median_engagement_rate * 100, 2)
        if median_engagement_rate is not None
        else None,
        "automatic_actions": False,
        "suggestions": suggestions,
        "segments": _performance_segments(samples, minimum_sample_size),
    }
