from __future__ import annotations

from typing import Any

from app.services import history

DEFAULT_STRONG_VIEW_MINIMUM = 1000
DEFAULT_STRONG_ENGAGEMENT_RATE = 0.05
DEFAULT_WEAK_VIEW_MAXIMUM = 300
DEFAULT_WEAK_ENGAGEMENT_RATE = 0.01
MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET = 5


def normalize_publish_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    return history.normalize_publish_metrics(metrics)


def engagement_rate(metrics: dict[str, Any] | None) -> float:
    normalized = normalize_publish_metrics(metrics)
    views = normalized.get("views", 0)
    if views <= 0:
        return 0.0
    engagements = (
        normalized.get("likes", 0)
        + normalized.get("comments", 0)
        + normalized.get("shares", 0)
        + normalized.get("saves", 0)
    )
    return round(engagements / views, 4)


def _score_value(value: Any) -> int | None:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _performance_bucket(metrics: dict[str, Any] | None) -> str:
    normalized = normalize_publish_metrics(metrics)
    views = normalized.get("views", 0)
    rate = engagement_rate(normalized)
    if views >= DEFAULT_STRONG_VIEW_MINIMUM and rate >= DEFAULT_STRONG_ENGAGEMENT_RATE:
        return "strong"
    if views <= DEFAULT_WEAK_VIEW_MAXIMUM or rate <= DEFAULT_WEAK_ENGAGEMENT_RATE:
        return "weak"
    return "moderate"


def _average(values: list[int]) -> int | None:
    if not values:
        return None
    return int(round(sum(values) / len(values)))


def _subject_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:120]


def _subjects_for(samples: list[dict[str, Any]], performance: str) -> list[str]:
    subjects = []
    seen = set()
    for sample in samples:
        if sample.get("performance") != performance:
            continue
        subject = sample.get("subject", "")
        subject_key = subject.casefold()
        if not subject or subject_key in seen:
            continue
        seen.add(subject_key)
        subjects.append(subject)
        if len(subjects) == 3:
            break
    return subjects


def _threshold_value(value: Any) -> int:
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        threshold = 60
    return max(0, min(100, threshold))


def build_quality_gate_calibration_report(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    current_threshold: int = 60,
) -> dict[str, Any]:
    samples = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        metrics = entry.get("publish_metrics")
        analysis = entry.get("viral_analysis") or {}
        if not isinstance(analysis, dict):
            continue
        score = _score_value(analysis.get("overall_score"))
        if not isinstance(metrics, dict) or score is None:
            continue
        samples.append(
            {
                "task_id": entry.get("task_id", ""),
                "subject": _subject_value(entry.get("subject")),
                "score": score,
                "engagement_rate": engagement_rate(metrics),
                "performance": _performance_bucket(metrics),
            }
        )

    strong_scores = [
        sample["score"] for sample in samples if sample["performance"] == "strong"
    ]
    weak_scores = [
        sample["score"] for sample in samples if sample["performance"] == "weak"
    ]
    strong_subjects = _subjects_for(samples, "strong")
    weak_subjects = _subjects_for(samples, "weak")
    strong_average = _average(strong_scores)
    weak_average = _average(weak_scores)

    has_sufficient_samples = (
        len(strong_scores) >= MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET
        and len(weak_scores) >= MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET
    )
    current_threshold = _threshold_value(current_threshold)
    recommended_threshold = None
    recommendation = (
        "Collect at least "
        f"{MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET} strong and "
        f"{MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET} weak publish metric samples "
        "before changing the threshold."
    )
    if has_sufficient_samples:
        midpoint = int(round((strong_average + weak_average) / 2))
        recommended_threshold = max(0, min(100, midpoint))
        if recommended_threshold > current_threshold:
            recommendation = (
                f"Consider raising the warning threshold toward {recommended_threshold}."
            )
        elif recommended_threshold < current_threshold:
            recommendation = (
                f"Consider lowering the warning threshold toward {recommended_threshold}."
            )
        else:
            recommendation = "Current warning threshold looks reasonable for now."

    return {
        "sample_count": len(samples),
        "strong_count": len(strong_scores),
        "weak_count": len(weak_scores),
        "minimum_samples_per_bucket": MINIMUM_SAMPLES_PER_PERFORMANCE_BUCKET,
        "has_sufficient_samples": has_sufficient_samples,
        "strong_average_score": strong_average,
        "weak_average_score": weak_average,
        "strong_subjects": strong_subjects,
        "weak_subjects": weak_subjects,
        "recommended_threshold": recommended_threshold,
        "recommendation": recommendation,
        "samples": samples,
    }
