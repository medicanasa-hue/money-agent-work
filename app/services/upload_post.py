"""
Upload-Post API integration for cross-posting videos to TikTok, Instagram and YouTube Shorts.

Docs: https://docs.upload-post.com
"""
import os
import time
from collections.abc import Callable
from typing import Any, Optional

import requests
from loguru import logger
from app.config import config

YOUTUBE_PRIVACY_PRIVATE = "private"
YOUTUBE_PRIVACY_UNLISTED = "unlisted"
YOUTUBE_PRIVACY_PUBLIC = "public"
YOUTUBE_PRIVACY_STATUSES = {
    YOUTUBE_PRIVACY_PRIVATE,
    YOUTUBE_PRIVACY_UNLISTED,
    YOUTUBE_PRIVACY_PUBLIC,
}
ANALYTICS_REQUEST_INTERVAL_SECONDS = 1.0
ANALYTICS_MAX_ATTEMPTS = 3
ANALYTICS_RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_ANALYTICS_STATUS_CODES = {429, 500, 502, 503, 504}
POST_METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves")


def _config_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def normalize_youtube_privacy_status(
    value,
    allow_public: bool = False,
) -> str:
    privacy_status = str(value or "").strip().lower()
    if privacy_status not in YOUTUBE_PRIVACY_STATUSES:
        logger.warning(
            "invalid YouTube privacy status; falling back to private upload"
        )
        return YOUTUBE_PRIVACY_PRIVATE
    if privacy_status == YOUTUBE_PRIVACY_PUBLIC and not allow_public:
        logger.warning(
            "public YouTube upload requested without explicit allow flag; "
            "falling back to private upload"
        )
        return YOUTUBE_PRIVACY_PRIVATE
    return privacy_status


def extract_result_link(result: dict) -> Optional[str]:
    if not isinstance(result, dict):
        return None

    preferred_keys = (
        "url",
        "video_url",
        "post_url",
        "youtube_url",
        "link",
        "permalink",
    )

    def is_url(value):
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    def find_link(value):
        if is_url(value):
            return value
        if isinstance(value, dict):
            for key in preferred_keys:
                link = value.get(key)
                if is_url(link):
                    return link
            for nested_value in value.values():
                link = find_link(nested_value)
                if link:
                    return link
        if isinstance(value, list):
            for item in value:
                link = find_link(item)
                if link:
                    return link
        return None

    return find_link(result)


def _metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def has_post_metrics(analytics_response: dict) -> bool:
    """Return whether an analytics response includes at least one metrics object."""
    if not isinstance(analytics_response, dict):
        return False

    platforms = analytics_response.get("platforms")
    if not isinstance(platforms, dict):
        return False

    return any(
        isinstance(payload, dict)
        and payload.get("success") is not False
        and isinstance(payload.get("post_metrics"), dict)
        for payload in platforms.values()
    )


def aggregate_post_metrics(analytics_response: dict) -> dict:
    totals = {field: 0 for field in POST_METRIC_FIELDS}
    for platform_metrics in extract_post_platform_metrics(analytics_response).values():
        for field in POST_METRIC_FIELDS:
            totals[field] += _metric_int(platform_metrics.get(field))
    return totals


def extract_post_platform_metrics(analytics_response: dict) -> dict[str, dict[str, int]]:
    """Return safe per-platform analytics without retaining raw API responses."""
    platform_totals: dict[str, dict[str, int]] = {}
    if not isinstance(analytics_response, dict):
        return platform_totals

    platforms = analytics_response.get("platforms")
    if not isinstance(platforms, dict):
        return platform_totals

    for platform, payload in platforms.items():
        if not isinstance(payload, dict):
            logger.warning(f"skipping Upload-Post analytics for {platform}: invalid payload")
            continue
        if payload.get("success") is False:
            logger.warning(
                f"skipping Upload-Post analytics for {platform}: platform reported failure"
            )
            continue

        metrics = payload.get("post_metrics")
        if not isinstance(metrics, dict):
            logger.warning(f"skipping Upload-Post analytics for {platform}: missing metrics")
            continue

        platform_name = str(platform or "").strip().casefold()
        if not platform_name:
            continue
        normalized_metrics = {
            field: _metric_int(metrics.get(field)) for field in POST_METRIC_FIELDS
        }
        if "saves" not in metrics:
            # Upload-Post may return favorites instead of saves for some platforms.
            normalized_metrics["saves"] = _metric_int(metrics.get("favorites"))
        platform_totals[platform_name] = normalized_metrics

    return platform_totals


def _is_retryable_analytics_error(error: requests.exceptions.RequestException) -> bool:
    if isinstance(
        error,
        (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
    ):
        return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) in RETRYABLE_ANALYTICS_STATUS_CODES


class UploadPostService:
    API_BASE = "https://api.upload-post.com"

    def __init__(
        self,
        *,
        sleep_fn: Optional[Callable[[float], None]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
    ):
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic_fn or time.monotonic
        self._last_analytics_request_at: Optional[float] = None
        self.reload_config()

    def reload_config(self):
        self.api_key = config.app.get("upload_post_api_key", "")
        self.username = config.app.get("upload_post_username", "")
        self.enabled = config.app.get("upload_post_enabled", False)
        self.platforms = config.app.get("upload_post_platforms", ["tiktok", "instagram"])
        self.auto_upload = config.app.get("upload_post_auto_upload", False)
        self.tiktok_is_aigc = _config_bool(
            config.app.get("upload_post_tiktok_is_aigc", False)
        )
        self.allow_public_youtube_upload = _config_bool(
            config.app.get("upload_post_allow_public_youtube", False)
        )
        self.youtube_privacy_status = normalize_youtube_privacy_status(
            config.app.get("upload_post_youtube_privacy_status", YOUTUBE_PRIVACY_UNLISTED),
            allow_public=self.allow_public_youtube_upload,
        )

    def is_configured(self) -> bool:
        return bool(self.api_key and self.username and self.enabled)

    def upload_video(
        self,
        video_path: str,
        title: str,
        platforms: Optional[list] = None,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        youtube_extra: Optional[dict] = None,
    ) -> dict:
        if not self.is_configured():
            logger.warning("Upload-Post is not configured. Skipping cross-post.")
            return {"success": False, "error": "Upload-Post not configured"}

        if platforms is None:
            platforms = self.platforms

        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return {"success": False, "error": f"Video file not found: {video_path}"}

        logger.info(f"Cross-posting video to {', '.join(platforms)} via Upload-Post...")

        try:
            with open(video_path, 'rb') as video_file:
                files = {'video': video_file}

                data = [
                    ('user', self.username),
                    ('title', title[:2200]),
                    ('privacy_level', privacy_level),
                ]

                for platform in platforms:
                    data.append(('platform[]', platform))

                if any(
                    str(platform).strip().lower() == "tiktok"
                    for platform in platforms
                ):
                    data.append(('is_aigc', str(self.tiktok_is_aigc).lower()))

                if youtube_extra and any(p.startswith("youtube") for p in platforms):
                    if "youtube_title" in youtube_extra:
                        data.append(('youtube_title', youtube_extra["youtube_title"][:100]))
                    if "youtube_description" in youtube_extra:
                        data.append(('youtube_description', youtube_extra["youtube_description"]))
                    for tag in youtube_extra.get("tags", []):
                        data.append(('tags[]', tag))
                    data.append((
                        'privacyStatus',
                        normalize_youtube_privacy_status(
                            youtube_extra.get(
                                "privacyStatus",
                                self.youtube_privacy_status,
                            ),
                            allow_public=self.allow_public_youtube_upload,
                        ),
                    ))
                    data.append(('containsSyntheticMedia', "true"))

                headers = {'Authorization': f'Apikey {self.api_key}'}

                response = requests.post(
                    f"{self.API_BASE}/api/upload",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300,
                )

                response.raise_for_status()
                result = response.json()

                if result.get('success'):
                    logger.info(
                        f"Video cross-posted successfully. Request ID: {result.get('request_id')}"
                    )
                else:
                    logger.warning(f"Cross-post failed: {result.get('message', 'Unknown error')}")

                return result

        except OSError as e:
            logger.error(f"Could not read video file for cross-post: {e}")
            return {"success": False, "error": f"Could not read video file: {e}"}
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to cross-post video: {str(e)}")
            return {"success": False, "error": str(e)}

    def check_status(self, request_id: str) -> dict:
        """
        Check the status of an upload request.

        Args:
            request_id (str): The request ID from upload

        Returns:
            dict: Status information
        """
        try:
            headers = {
                'Authorization': f'Apikey {self.api_key}'
            }

            response = requests.get(
                f"{self.API_BASE}/api/uploadposts/status",
                params={'request_id': request_id},
                headers=headers,
                timeout=30
            )
            
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to check status: {str(e)}")
            return {"success": False, "error": str(e)}

    def _wait_for_analytics_request_slot(self, minimum_delay: float = 0.0):
        now = self._monotonic()
        last_request_at = self._last_analytics_request_at
        rate_delay = 0.0
        if last_request_at is not None:
            rate_delay = max(
                0.0,
                ANALYTICS_REQUEST_INTERVAL_SECONDS - (now - last_request_at),
            )
        delay = max(rate_delay, minimum_delay)
        if delay:
            self._sleep(delay)
        self._last_analytics_request_at = self._monotonic()

    def get_post_analytics(self, request_id: str) -> dict:
        """
        Fetch per-post analytics for an upload request.

        Args:
            request_id (str): The request ID from upload

        Returns:
            dict: Analytics information
        """
        if not self.is_configured():
            logger.warning("Upload-Post is not configured. Skipping analytics sync.")
            return {
                "success": False,
                "error": "Upload-Post not configured",
                "retryable": False,
            }

        request_id = str(request_id or "").strip()
        if not request_id:
            logger.warning("Upload-Post analytics request is missing a request ID.")
            return {
                "success": False,
                "error": "Upload-Post request ID is required",
                "retryable": False,
            }

        headers = {'Authorization': f'Apikey {self.api_key}'}
        last_error = None
        for attempt in range(ANALYTICS_MAX_ATTEMPTS):
            retry_delay = (
                0.0
                if attempt == 0
                else ANALYTICS_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            )
            self._wait_for_analytics_request_slot(retry_delay)

            try:
                response = requests.get(
                    f"{self.API_BASE}/api/uploadposts/post-analytics/{request_id}",
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    logger.error("Upload-Post returned an invalid analytics response")
                    return {
                        "success": False,
                        "error": "Invalid analytics response",
                        "retryable": False,
                    }
                return payload
            except ValueError:
                logger.error("Upload-Post returned invalid analytics JSON")
                return {
                    "success": False,
                    "error": "Invalid analytics response",
                    "retryable": False,
                }
            except requests.exceptions.RequestException as error:
                last_error = error
                if not _is_retryable_analytics_error(error):
                    break

        logger.error("Failed to fetch post analytics")
        return {
            "success": False,
            "error": "Unable to fetch post analytics",
            "retryable": bool(
                last_error and _is_retryable_analytics_error(last_error)
            ),
        }


# Singleton instance
upload_post_service = UploadPostService()


def cross_post_video(
    video_path: str,
    title: str,
    platforms: Optional[list] = None,
    youtube_extra: Optional[dict] = None,
) -> dict:
    return upload_post_service.upload_video(video_path, title, platforms, youtube_extra=youtube_extra)
