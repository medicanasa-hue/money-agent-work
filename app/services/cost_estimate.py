from datetime import datetime, timezone
from typing import Any

from app.config import config


COST_ESTIMATE_VERSION = 1
_CHARACTERS_PER_MILLION = 1_000_000
_LLM_RATE_KEY = "cost_estimate_llm_usd_per_million_characters"
_TTS_RATE_KEY = "cost_estimate_tts_usd_per_million_characters"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_characters(value: Any) -> int:
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, dict):
        return sum(_text_characters(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(_text_characters(item) for item in value)
    return 0


def _configured_rate(key: str) -> float | None:
    value = config.app.get(key)
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if 0 <= rate < float("inf") else None


def _cost_for_characters(characters: int, rate: float | None) -> float | None:
    if characters <= 0:
        return 0.0
    if rate is None:
        return None
    return round(characters * rate / _CHARACTERS_PER_MILLION, 6)


def _tts_provider(entry: dict[str, Any]) -> str:
    if _text(entry.get("custom_audio_file")):
        return "custom_audio"

    voice_name = _text(entry.get("voice_name"))
    normalized_name = voice_name.casefold()
    if normalized_name in {"no-voice", "none"}:
        return "no_voice"
    if normalized_name.startswith("elevenlabs:"):
        return "elevenlabs"
    if normalized_name.startswith("gemini:"):
        return "gemini"
    if normalized_name.startswith("mimo:"):
        return "mimo"
    if normalized_name.startswith("siliconflow:"):
        return "siliconflow"
    if normalized_name.startswith("chatterbox:"):
        return "chatterbox"
    if "-v2" in normalized_name:
        return "azure_speech"
    return "edge_tts" if voice_name else "unknown"


def _as_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc
    )


def _non_negative_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if 0 <= parsed < float("inf") else 0.0


def estimate_history_cost(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Return a conservative, configurable cost estimate for one history record.

    History retains generated artifacts rather than raw provider usage.  This
    deliberately estimates from observable character counts and never invents
    an amount when a component rate is not configured.
    """
    record = entry if isinstance(entry, dict) else {}
    llm_characters = sum(
        _text_characters(record.get(key))
        for key in ("script", "terms", "metadata", "viral_analysis")
    )
    tts_provider = _tts_provider(record)
    tts_characters = (
        0
        if tts_provider in {"custom_audio", "no_voice"}
        else _text_characters(record.get("script"))
    )

    llm_rate = _configured_rate(_LLM_RATE_KEY)
    tts_rate = _configured_rate(_TTS_RATE_KEY)
    llm_cost = _cost_for_characters(llm_characters, llm_rate)
    tts_cost = _cost_for_characters(tts_characters, tts_rate)

    unknown_components = []
    if llm_cost is None:
        unknown_components.append("llm")
    if tts_cost is None:
        unknown_components.append("tts")

    return {
        "version": COST_ESTIMATE_VERSION,
        "currency": "USD",
        "basis": "stored_character_heuristic",
        "llm": {
            "provider": _text(record.get("llm_provider"))
            or _text(config.app.get("llm_provider"))
            or "unknown",
            "characters": llm_characters,
            "rate_usd_per_million_characters": llm_rate,
            "estimated_usd": llm_cost,
        },
        "tts": {
            "provider": tts_provider,
            "characters": tts_characters,
            "rate_usd_per_million_characters": tts_rate,
            "estimated_usd": tts_cost,
        },
        "estimated_known_total_usd": round(
            sum(cost for cost in (llm_cost, tts_cost) if cost is not None),
            6,
        ),
        "unknown_components": unknown_components,
    }


def summarize_monthly_history_costs(
    entries: Any,
    *,
    now: str | datetime | None = None,
) -> dict[str, int | float]:
    """Summarize the current UTC month's stored cost estimates safely."""
    current_time = _as_utc_datetime(now) or datetime.now(timezone.utc)
    job_count = 0
    estimated_job_count = 0
    unknown_job_count = 0
    known_total_usd = 0.0

    for entry in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        created_at = _as_utc_datetime(entry.get("created_at"))
        if (
            created_at is None
            or created_at.year != current_time.year
            or created_at.month != current_time.month
        ):
            continue

        job_count += 1
        estimate = entry.get("cost_estimate")
        if not isinstance(estimate, dict):
            unknown_job_count += 1
            continue

        estimated_job_count += 1
        known_total_usd += _non_negative_float(
            estimate.get("estimated_known_total_usd")
        )
        unknown_components = estimate.get("unknown_components")
        if not isinstance(unknown_components, (list, tuple, set)) or unknown_components:
            unknown_job_count += 1

    return {
        "job_count": job_count,
        "estimated_job_count": estimated_job_count,
        "unknown_job_count": unknown_job_count,
        "known_total_usd": round(known_total_usd, 6),
    }


def evaluate_monthly_cost_warning(
    entries: Any,
    *,
    threshold_usd: Any,
    now: str | datetime | None = None,
) -> dict[str, int | float | bool]:
    """Report a warning-only monthly known-cost threshold evaluation."""
    summary = summarize_monthly_history_costs(entries, now=now)
    threshold = _non_negative_float(threshold_usd)
    enabled = threshold > 0
    return {
        **summary,
        "threshold_usd": threshold,
        "enabled": enabled,
        "warning": bool(enabled and summary["known_total_usd"] >= threshold),
    }


def evaluate_monthly_cost_cap(
    entries: Any,
    *,
    cap_usd: Any,
    projected_cost_estimate: Any = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate an opt-in cap against known monthly costs and one job estimate.

    Provider billing data is not available locally, so unknown components stay
    visible instead of being treated as free.  Callers can use this result to
    stop a scheduled job before its remaining generation stages begin.
    """
    summary = summarize_monthly_history_costs(entries, now=now)
    cap = _non_negative_float(cap_usd)
    estimate = projected_cost_estimate if isinstance(projected_cost_estimate, dict) else {}
    projected_known_cost = _non_negative_float(
        estimate.get("estimated_known_total_usd")
    )
    unknown_components = estimate.get("unknown_components")
    projected_unknown_components = []
    if isinstance(unknown_components, (list, tuple, set)):
        for component in unknown_components:
            name = _text(component)
            if name and name not in projected_unknown_components:
                projected_unknown_components.append(name)

    projected_known_total = round(
        summary["known_total_usd"] + projected_known_cost,
        6,
    )
    enabled = cap > 0
    allowed = bool(
        not enabled
        or (
            summary["known_total_usd"] < cap
            and projected_known_total <= cap
        )
    )
    return {
        **summary,
        "cap_usd": cap,
        "enabled": enabled,
        "allowed": allowed,
        "projected_known_cost_usd": projected_known_cost,
        "projected_known_total_usd": projected_known_total,
        "projected_unknown_components": projected_unknown_components,
    }
