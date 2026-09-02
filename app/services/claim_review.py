"""Lightweight advisory checks for claims that need a human source review."""

import re
from typing import Any


_CLAIM_RULES = (
    (
        "numeric_claim",
        re.compile(
            r"\b\d+(?:[.,]\d+)?\s*(?:%|percent\b|yüzde\b|million\b|milyon\b|billion\b|milyar\b)",
            re.IGNORECASE,
        ),
        "Check numerical or statistical claims against a reliable source.",
    ),
    (
        "absolute_claim",
        re.compile(
            r"\b(always|never|guarantee(?:s|d)?|everyone|no one|her zaman|asla|kesin|garanti)\b",
            re.IGNORECASE,
        ),
        "Review absolute or guarantee-style wording before publishing.",
    ),
    (
        "financial_guidance",
        re.compile(
            r"\b(invest(?:ment|ing)?|return|profit|stock|crypto|credit|interest rate|loan|yatırım|getiri|kâr|borsa|kripto|faiz|kredi)\b",
            re.IGNORECASE,
        ),
        "Review financial guidance and make its educational context clear.",
    ),
    (
        "health_claim",
        re.compile(
            r"\b(cure|treat(?:ment|s|ed)?|disease|diagnos(?:e|is)|tedavi|iyileştir|hastalık|teşhis)\b",
            re.IGNORECASE,
        ),
        "Review health-related claims against an appropriate source.",
    ),
)


def review_script_claims(script: Any) -> dict[str, Any]:
    """Return advisory review cues without judging truth or blocking generation."""
    text = str(script or "").strip()
    categories = []
    warnings = []
    for category, pattern, message in _CLAIM_RULES:
        if pattern.search(text):
            categories.append(category)
            warnings.append({"type": category, "message": message})

    return {
        "status": "review_recommended" if warnings else "clear",
        "claim_count": len(warnings),
        "categories": categories,
        "warnings": warnings,
        "automatic_block": False,
    }
