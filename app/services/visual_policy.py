"""Small, recommendation-only visual policies for common subject types."""

import re


_PROFILES = (
    {
        "name": "economic_explainer",
        "keywords": frozenset(
            {
                "economy",
                "economic",
                "economics",
                "finance",
                "financial",
                "inflation",
                "interest",
                "market",
                "prices",
                "budget",
                "investment",
                "stock",
                "ekonomi",
                "enflasyon",
                "faiz",
                "fiyat",
                "fiyatları",
                "market",
                "bütçe",
                "yatırım",
                "borsa",
                "kredi",
                "para",
            }
        ),
        "recommended_params": {
            "video_source": "multi",
            "match_materials_to_script": True,
            "smart_scene_queries": True,
            "video_clip_duration": 4,
        },
        "scene_guidance": [
            "Prefer concrete prices, payments, charts, shops, and work scenes.",
            "Avoid generic flags or skylines when a specific economic action is named.",
        ],
    },
    {
        "name": "science_explainer",
        "keywords": frozenset(
            {
                "science",
                "scientific",
                "technology",
                "space",
                "climate",
                "health",
                "bilim",
                "teknoloji",
                "uzay",
                "iklim",
                "sağlık",
            }
        ),
        "recommended_params": {
            "video_source": "multi",
            "match_materials_to_script": True,
            "smart_scene_queries": True,
            "video_clip_duration": 5,
        },
        "scene_guidance": [
            "Prefer observable processes, instruments, data, and environments.",
            "Use one visual concept per narration beat rather than generic abstract footage.",
        ],
    },
)

_GENERAL_PROFILE = {
    "name": "general_explainer",
    "recommended_params": {
        "video_source": "multi",
        "match_materials_to_script": False,
        "smart_scene_queries": False,
        "video_clip_duration": 5,
    },
    "scene_guidance": [
        "Use concrete, visible actions before broad atmosphere shots.",
    ],
}


def _subject_tokens(subject: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w]+", str(subject or "").casefold(), re.UNICODE)
        if len(token) > 2
    }


def recommend_visual_policy(subject: str) -> dict:
    """Return a non-mutating visual-policy recommendation for a subject."""
    tokens = _subject_tokens(subject)
    selected_profile = _GENERAL_PROFILE
    matched_keywords: set[str] = set()
    for profile in _PROFILES:
        profile_matches = tokens & profile["keywords"]
        if len(profile_matches) > len(matched_keywords):
            selected_profile = profile
            matched_keywords = profile_matches

    return {
        "profile": selected_profile["name"],
        "matched_keywords": sorted(matched_keywords),
        "recommended_params": dict(selected_profile["recommended_params"]),
        "scene_guidance": list(selected_profile["scene_guidance"]),
        "recommendation_only": True,
    }
