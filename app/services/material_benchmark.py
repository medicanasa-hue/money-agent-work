"""Read-only material relevance measurements for local quality reviews."""

import math

from app.models.schema import VideoAspect
from app.services import material


def _as_material_items(materials) -> list:
    if isinstance(materials, (str, bytes)):
        return []
    try:
        return list(materials or [])
    except TypeError:
        return []


def build_material_relevance_report(
    materials,
    *,
    max_clip_duration: int = 5,
    video_aspect: VideoAspect = VideoAspect.portrait,
    substantive_threshold: float = material._SUBSTANTIVE_CONTENT_MATCH_THRESHOLD,
) -> dict:
    """Measure metadata relevance without downloading, ranking, or mutating materials."""
    try:
        substantive_threshold = min(1.0, max(0.0, float(substantive_threshold)))
    except (TypeError, ValueError):
        substantive_threshold = material._SUBSTANTIVE_CONTENT_MATCH_THRESHOLD

    entries = []
    query_entries = {}
    provider_entries = {}
    for item in _as_material_items(materials):
        try:
            content_match = float(material._score_content_match(item))
            material_score = float(
                material._score_material(item, max_clip_duration, video_aspect)
            )
        except Exception:
            continue

        query = str(getattr(item, "search_query", "") or "").strip()
        entry = {
            "provider": str(getattr(item, "provider", "") or "").strip(),
            "content_match": round(content_match, 3),
            "material_score": round(material_score, 3),
            "is_substantive": content_match >= substantive_threshold,
        }
        entries.append(entry)
        if query:
            query_entries.setdefault(query, []).append(entry)
        provider = entry["provider"] or "unknown"
        provider_entries.setdefault(provider, []).append((entry, query))

    queries = []
    for query in sorted(query_entries, key=str.casefold):
        candidates = query_entries[query]
        substantive_count = sum(
            1 for candidate in candidates if candidate["is_substantive"]
        )
        content_scores = [candidate["content_match"] for candidate in candidates]
        queries.append(
            {
                "query": query,
                "candidate_count": len(candidates),
                "best_content_match": round(max(content_scores), 3),
                "mean_content_match": round(sum(content_scores) / len(content_scores), 3),
                "substantive_candidate_count": substantive_count,
                "has_substantive_candidate": bool(substantive_count),
            }
        )

    measured_query_count = len(queries)
    covered_query_count = sum(
        1 for query in queries if query["has_substantive_candidate"]
    )
    providers = []
    for provider in sorted(provider_entries, key=str.casefold):
        provider_candidates = provider_entries[provider]
        provider_entries_only = [entry for entry, _query in provider_candidates]
        provider_queries = {
            query for _entry, query in provider_candidates if query
        }
        covered_provider_queries = {
            query
            for entry, query in provider_candidates
            if query and entry["is_substantive"]
        }
        substantive_candidate_count = sum(
            1 for entry in provider_entries_only if entry["is_substantive"]
        )
        candidate_count = len(provider_entries_only)
        measured_provider_query_count = len(provider_queries)
        providers.append(
            {
                "provider": provider,
                "candidate_count": candidate_count,
                "substantive_candidate_count": substantive_candidate_count,
                "substantive_candidate_rate": round(
                    substantive_candidate_count / candidate_count
                    if candidate_count
                    else 0.0,
                    3,
                ),
                "mean_content_match": round(
                    sum(entry["content_match"] for entry in provider_entries_only)
                    / candidate_count
                    if candidate_count
                    else 0.0,
                    3,
                ),
                "measured_query_count": measured_provider_query_count,
                "covered_query_count": len(covered_provider_queries),
                "query_coverage": round(
                    len(covered_provider_queries) / measured_provider_query_count
                    if measured_provider_query_count
                    else 0.0,
                    3,
                ),
            }
        )
    return {
        "candidate_count": len(entries),
        "measured_query_count": measured_query_count,
        "covered_query_count": covered_query_count,
        "query_coverage": round(
            covered_query_count / measured_query_count if measured_query_count else 0.0,
            3,
        ),
        "low_content_match_count": sum(
            1 for entry in entries if not entry["is_substantive"]
        ),
        "substantive_threshold": substantive_threshold,
        "unmatched_queries": [
            query["query"] for query in queries if not query["has_substantive_candidate"]
        ],
        "providers": providers,
        "queries": queries,
        "candidates": entries,
    }


def _safe_provider_name(value) -> str:
    provider = str(value or "").strip()
    safe_provider = "".join(
        character
        for character in provider
        if character.isalnum() or character in ("-", "_")
    )
    return safe_provider[:40] or "unknown"


def _positive_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _non_negative_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _aspect_fit(item, video_aspect: VideoAspect) -> float | None:
    width = _positive_number(getattr(item, "width", 0))
    height = _positive_number(getattr(item, "height", 0))
    if width is None or height is None:
        return None
    target_width, target_height = video_aspect.to_resolution()
    ratio_distance = abs(math.log((width / height) / (target_width / target_height)))
    return round(max(0.0, 1.0 - min(1.0, ratio_distance)), 3)


def summarize_material_candidates(
    materials,
    video_aspect: VideoAspect | str = VideoAspect.portrait,
) -> dict:
    """Summarize candidate quality metadata without exposing source URLs."""
    aspect = VideoAspect(video_aspect)
    provider_entries: dict[str, list[dict]] = {}
    for item in _as_material_items(materials):
        provider = _safe_provider_name(getattr(item, "provider", ""))
        width = _positive_number(getattr(item, "width", 0))
        height = _positive_number(getattr(item, "height", 0))
        preview_quality = _non_negative_number(
            getattr(item, "preview_quality_score", None)
        )
        if preview_quality is not None:
            preview_quality = min(1.0, preview_quality)
        provider_entries.setdefault(provider, []).append(
            {
                "pixels": width * height if width is not None and height is not None else None,
                "duration": _positive_number(getattr(item, "duration", 0)),
                "aspect_fit": _aspect_fit(item, aspect),
                "preview_quality": preview_quality,
            }
        )

    providers = []
    for provider, entries in provider_entries.items():
        pixels = [entry["pixels"] for entry in entries if entry["pixels"] is not None]
        durations = [entry["duration"] for entry in entries if entry["duration"] is not None]
        aspect_fits = [
            entry["aspect_fit"] for entry in entries if entry["aspect_fit"] is not None
        ]
        preview_scores = [
            entry["preview_quality"]
            for entry in entries
            if entry["preview_quality"] is not None
        ]
        providers.append(
            {
                "provider": provider,
                "candidate_count": len(entries),
                "average_pixels": round(sum(pixels) / len(pixels)) if pixels else None,
                "average_duration_seconds": _average(durations),
                "average_aspect_fit": _average(aspect_fits),
                "average_preview_quality": _average(preview_scores),
            }
        )
    providers.sort(key=lambda entry: (-entry["candidate_count"], entry["provider"]))
    return {
        "video_aspect": aspect.value,
        "candidate_count": sum(entry["candidate_count"] for entry in providers),
        "provider_count": len(providers),
        "providers": providers,
    }


def benchmark_material_providers(
    topic: str,
    video_aspect: VideoAspect | str = VideoAspect.portrait,
) -> dict:
    """Run one read-only provider search and return aggregated candidate metadata."""
    normalized_topic = str(topic or "").strip()
    if not normalized_topic:
        return {"ok": False, "status": "invalid_topic"}
    try:
        aspect = VideoAspect(video_aspect)
    except ValueError:
        return {"ok": False, "status": "invalid_video_aspect"}

    try:
        candidates = material.search_video_candidates(
            [normalized_topic],
            source="multi",
            video_aspect=aspect,
            max_clip_duration=5,
            limit=24,
        )
    except Exception:
        return {"ok": False, "status": "provider_search_failed", "video_aspect": aspect.value}

    summary = summarize_material_candidates(candidates, aspect)
    return {
        "ok": bool(summary["candidate_count"]),
        "status": "completed" if summary["candidate_count"] else "no_candidates",
        **summary,
    }


def _normalized_scene_queries(scene_queries) -> list[str]:
    if isinstance(scene_queries, str):
        values = scene_queries.replace("\n", ",").split(",")
    else:
        try:
            values = list(scene_queries or [])
        except TypeError:
            return []
    queries = []
    seen = set()
    for value in values:
        query = str(value or "").strip()
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        queries.append(query)
    return queries


def inspect_scene_material_relevance(
    scene_queries,
    video_aspect: VideoAspect | str = VideoAspect.portrait,
) -> dict:
    """Inspect scene-query coverage using read-only provider candidate searches."""
    queries = _normalized_scene_queries(scene_queries)
    if not queries:
        return {"ok": False, "status": "invalid_scene_queries"}
    try:
        aspect = VideoAspect(video_aspect)
    except ValueError:
        return {"ok": False, "status": "invalid_video_aspect"}

    candidates = []
    search_error_count = 0
    for query in queries:
        try:
            candidates.extend(
                material.search_video_candidates(
                    [query],
                    source="multi",
                    video_aspect=aspect,
                    max_clip_duration=5,
                    limit=8,
                )
            )
        except Exception:
            search_error_count += 1

    if search_error_count == len(queries):
        return {
            "ok": False,
            "status": "provider_search_failed",
            "video_aspect": aspect.value,
            "scene_count": len(queries),
            "search_error_count": search_error_count,
        }

    relevance = build_material_relevance_report(
        candidates,
        video_aspect=aspect,
    )
    covered_queries = {
        str(entry.get("query") or "").casefold()
        for entry in relevance.get("queries") or []
        if entry.get("has_substantive_candidate")
    }
    uncovered_queries = [
        query for query in queries if query.casefold() not in covered_queries
    ]
    covered_scene_count = len(queries) - len(uncovered_queries)
    scene_coverage_ratio = covered_scene_count / len(queries)
    if scene_coverage_ratio >= 0.8:
        scene_coverage_status = "sufficient"
    elif scene_coverage_ratio >= 0.5:
        scene_coverage_status = "partial"
    else:
        scene_coverage_status = "sparse"
    return {
        "ok": True,
        "status": "completed" if not search_error_count else "partial",
        "video_aspect": aspect.value,
        "scene_count": len(queries),
        "covered_scene_count": covered_scene_count,
        "scene_coverage_ratio": round(scene_coverage_ratio, 3),
        "scene_coverage_status": scene_coverage_status,
        "search_error_count": search_error_count,
        "uncovered_scene_queries": uncovered_queries,
        "relevance": relevance,
    }
