"""Read-only configuration health for registered video providers."""

from collections.abc import Iterable

from app.config import config
from app.services.providers import (
    FREE_PROVIDERS,
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_REGISTRY,
)


def _enabled_sources(value: object) -> set[str]:
    if isinstance(value, str):
        values: Iterable[object] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = ()
    return {
        str(source or "").strip()
        for source in values
        if str(source or "").strip() in PROVIDER_REGISTRY
    }


def build_video_source_health(enabled_sources: object | None = None) -> dict:
    """Report provider configuration without exposing keys or using the network."""
    if enabled_sources is None:
        enabled_sources = config.app.get("enabled_video_sources", [])
    enabled = _enabled_sources(enabled_sources)

    sources = []
    ready_count = 0
    needs_configuration_count = 0
    for source, provider_type in PROVIDER_REGISTRY.items():
        is_enabled = source in enabled
        try:
            is_available = bool(provider_type().is_available())
        except Exception:
            is_available = False

        if is_enabled and is_available:
            status = "ready"
            ready_count += 1
        elif is_enabled:
            status = "needs_configuration"
            needs_configuration_count += 1
        elif is_available:
            status = "available"
        else:
            status = "not_configured"

        sources.append(
            {
                "source": source,
                "label": PROVIDER_DISPLAY_NAMES.get(source, source),
                "enabled": is_enabled,
                "requires_api_key": source not in FREE_PROVIDERS,
                "status": status,
            }
        )

    return {
        "enabled_count": len(enabled),
        "ready_count": ready_count,
        "needs_configuration_count": needs_configuration_count,
        "sources": sources,
    }
