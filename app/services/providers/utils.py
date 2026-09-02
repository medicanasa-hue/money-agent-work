import threading
import random

import requests
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect

_api_key_counter = 0
_api_key_lock = threading.Lock()
MAX_RANDOM_SEARCH_PAGE = 50


def safe_error_details(error: Exception) -> str:
    error_name = type(error).__name__
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return f"{error_name} status={status_code}"

    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int) and error_number >= 0:
        return f"{error_name} errno={error_number}"
    return error_name


def raise_for_http_error(response) -> None:
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        error = requests.HTTPError(f"HTTP {status_code}")
        error.response = response
        raise error


def get_api_key(cfg_key: str) -> str:
    """Config'den API key döndürür; birden fazla key varsa rotation uygular."""
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"'{cfg_key}' config.toml dosyasında tanımlı değil."
        )
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def get_tls_verify() -> bool:
    """Config'e göre TLS doğrulama ayarını döndürür."""
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")
    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled (config.app.tls_verify=false). "
            "Only use in trusted proxy environments."
        )
    return bool(tls_verify)


def get_search_page(source: str = "") -> int:
    source_key = f"{source}_search_max_page" if source else ""
    raw_value = config.app.get(source_key) if source_key else None
    config_key = source_key
    if raw_value is None:
        raw_value = config.app.get("material_search_max_page", 1)
        config_key = "material_search_max_page"

    try:
        max_page = int(raw_value or 1)
    except (TypeError, ValueError):
        logger.warning(
            f"[providers] invalid {config_key} value: {raw_value}"
        )
        return 1

    max_page = max(1, min(MAX_RANDOM_SEARCH_PAGE, max_page))
    if max_page <= 1:
        return 1
    return random.randint(1, max_page)


def select_best_video_variant(variants, *, video_aspect: VideoAspect, url_key: str):
    """Prefer a native output aspect before a larger but crop-prone file."""
    try:
        target_width, target_height = VideoAspect(video_aspect).to_resolution()
    except (TypeError, ValueError):
        target_width, target_height = VideoAspect.portrait.to_resolution()
    target_aspect = target_width / target_height

    selected = None
    selected_score = None
    for index, variant in enumerate(variants or []):
        if not isinstance(variant, dict):
            continue
        source_url = variant.get(url_key)
        if not isinstance(source_url, str) or not source_url.strip():
            continue
        try:
            width = int(variant.get("width") or 0)
            height = int(variant.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if width < target_width or height < target_height:
            continue

        source_aspect = width / height
        aspect_fit = min(source_aspect, target_aspect) / max(
            source_aspect, target_aspect
        )
        score = (aspect_fit, width * height, -index)
        if selected_score is None or score > selected_score:
            selected = (variant, width, height)
            selected_score = score
    return selected


# Ortak User-Agent
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/115.0.0.0 Safari/537.36"
)
