"""Stable review-decision vocabulary shared by manual quality workflows."""

import json
import os
import re
from datetime import datetime, timezone

from app.utils import utils


REVIEW_DECISIONS = ("approved", "rejected")
REJECTION_REASONS = (
    "unrelated_material",
    "repeated_visual",
    "poor_crop",
    "pacing",
    "subtitle_accuracy",
    "subtitle_placement",
    "audio_quality",
    "technical_quality",
    "other",
)
REVIEW_FEEDBACK_FILENAME = "review_feedback.json"
MAX_REVIEW_FEEDBACK_ENTRIES = 500
MATERIAL_REJECTION_REASONS = frozenset(
    {"unrelated_material", "repeated_visual", "poor_crop"}
)
MIN_PROVIDER_FEEDBACK_SAMPLE_COUNT = 5
MAX_PROVIDER_FEEDBACK_SCORE_PENALTY = 0.08
_provider_feedback_cache: tuple[tuple[str, float | None], dict] | None = None


def _normalized_code(value) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _normalized_material_provider(value) -> str | None:
    provider = _normalized_code(value)
    return provider if re.fullmatch(r"[a-z0-9_]{1,40}", provider) else None


def normalize_review_decision(decision, *, rejection_reason=None) -> dict:
    """Validate a manual approval or rejection without persisting it."""
    normalized_decision = _normalized_code(decision)
    if normalized_decision not in REVIEW_DECISIONS:
        return {"ok": False, "error": "invalid_review_decision"}

    if normalized_decision == "approved":
        return {
            "ok": True,
            "decision": normalized_decision,
            "rejection_reason": None,
        }

    normalized_reason = _normalized_code(rejection_reason)
    if not normalized_reason:
        return {"ok": False, "error": "rejection_reason_required"}
    if normalized_reason not in REJECTION_REASONS:
        return {"ok": False, "error": "invalid_rejection_reason"}
    return {
        "ok": True,
        "decision": normalized_decision,
        "rejection_reason": normalized_reason,
    }


def get_review_feedback_path(create: bool = False) -> str:
    return os.path.join(
        utils.storage_dir("history", create=create),
        REVIEW_FEEDBACK_FILENAME,
    )


def _recorded_at(value=None) -> str:
    if isinstance(value, datetime):
        parsed = value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        elif parsed is not None:
            parsed = parsed.astimezone(timezone.utc)
    else:
        parsed = None
    return (parsed or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _normalized_saved_entry(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    task_id = str(value.get("task_id") or "").strip()
    validation = normalize_review_decision(
        value.get("decision"),
        rejection_reason=value.get("rejection_reason"),
    )
    if not task_id or not validation.get("ok"):
        return None
    material_provider = _normalized_material_provider(value.get("material_provider"))
    if value.get("material_provider") and material_provider is None:
        return None
    return {
        "task_id": task_id,
        "decision": validation["decision"],
        "rejection_reason": validation["rejection_reason"],
        "material_provider": material_provider,
        "recorded_at": _recorded_at(value.get("recorded_at")),
    }


def list_review_decisions(limit: int | None = None) -> list[dict]:
    path = get_review_feedback_path(create=False)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    entries = [
        entry
        for raw_entry in payload
        if (entry := _normalized_saved_entry(raw_entry)) is not None
    ]
    if limit is None:
        return entries
    try:
        return entries[: max(0, int(limit))]
    except (TypeError, ValueError):
        return entries


def record_review_decision(
    task_id,
    decision,
    *,
    rejection_reason=None,
    material_provider=None,
    recorded_at=None,
) -> dict:
    """Persist one valid local review decision without touching task outputs."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return {"ok": False, "error": "task_id_required"}
    validation = normalize_review_decision(
        decision,
        rejection_reason=rejection_reason,
    )
    if not validation.get("ok"):
        return validation
    normalized_provider = _normalized_material_provider(material_provider)
    if material_provider not in (None, "") and normalized_provider is None:
        return {"ok": False, "error": "invalid_material_provider"}
    record = {
        "task_id": normalized_task_id,
        "decision": validation["decision"],
        "rejection_reason": validation["rejection_reason"],
        "material_provider": normalized_provider,
        "recorded_at": _recorded_at(recorded_at),
    }
    path = get_review_feedback_path(create=True)
    entries = [record, *list_review_decisions()][:MAX_REVIEW_FEEDBACK_ENTRIES]
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(entries, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, path)
    except OSError:
        return {"ok": False, "error": "review_feedback_unavailable"}
    finally:
        if os.path.isfile(temp_path):
            os.remove(temp_path)
    global _provider_feedback_cache
    _provider_feedback_cache = None
    return {"ok": True, "record": record}


def build_provider_feedback_adjustments(
    entries,
    *,
    minimum_sample_count: int = MIN_PROVIDER_FEEDBACK_SAMPLE_COUNT,
) -> dict[str, dict]:
    """Turn source-specific human reviews into bounded provider score adjustments."""
    try:
        minimum_sample_count = max(1, int(minimum_sample_count))
    except (TypeError, ValueError):
        minimum_sample_count = MIN_PROVIDER_FEEDBACK_SAMPLE_COUNT

    grouped = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        provider = _normalized_material_provider(entry.get("material_provider"))
        validation = normalize_review_decision(
            entry.get("decision"),
            rejection_reason=entry.get("rejection_reason"),
        )
        if provider is None or not validation.get("ok"):
            continue
        group = grouped.setdefault(
            provider,
            {"sample_count": 0, "material_rejection_count": 0},
        )
        group["sample_count"] += 1
        if (
            validation["decision"] == "rejected"
            and validation["rejection_reason"] in MATERIAL_REJECTION_REASONS
        ):
            group["material_rejection_count"] += 1

    adjustments = {}
    for provider, group in grouped.items():
        sample_count = group["sample_count"]
        rejection_count = group["material_rejection_count"]
        if sample_count < minimum_sample_count:
            adjustment = 0.0
            status = "insufficient_evidence"
        else:
            adjustment = -min(
                MAX_PROVIDER_FEEDBACK_SCORE_PENALTY,
                MAX_PROVIDER_FEEDBACK_SCORE_PENALTY
                * (rejection_count / sample_count),
            )
            status = "active"
        adjustments[provider] = {
            "sample_count": sample_count,
            "material_rejection_count": rejection_count,
            "status": status,
            "score_adjustment": round(adjustment, 4),
        }
    return adjustments


def get_provider_feedback_score_adjustment(provider) -> float:
    """Return the cached, bounded score adjustment for one material provider."""
    normalized_provider = _normalized_material_provider(provider)
    if normalized_provider is None:
        return 0.0
    path = get_review_feedback_path(create=False)
    try:
        modified_at = os.path.getmtime(path)
    except OSError:
        return 0.0

    global _provider_feedback_cache
    cache_key = (path, modified_at)
    if _provider_feedback_cache is None or _provider_feedback_cache[0] != cache_key:
        _provider_feedback_cache = (
            cache_key,
            build_provider_feedback_adjustments(list_review_decisions()),
        )
    adjustment = _provider_feedback_cache[1].get(normalized_provider, {}).get(
        "score_adjustment",
        0.0,
    )
    try:
        return float(adjustment)
    except (TypeError, ValueError):
        return 0.0
