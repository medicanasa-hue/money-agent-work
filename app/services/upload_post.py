"""
Upload-Post API integration for cross-posting videos to TikTok, Instagram and YouTube Shorts.

Docs: https://docs.upload-post.com
"""
import os
from typing import Optional

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


class UploadPostService:
    API_BASE = "https://api.upload-post.com"

    def __init__(self):
        self.reload_config()

    def reload_config(self):
        self.api_key = config.app.get("upload_post_api_key", "")
        self.username = config.app.get("upload_post_username", "")
        self.enabled = config.app.get("upload_post_enabled", False)
        self.platforms = config.app.get("upload_post_platforms", ["tiktok", "instagram"])
        self.auto_upload = config.app.get("upload_post_auto_upload", False)
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


# Singleton instance
upload_post_service = UploadPostService()


def cross_post_video(
    video_path: str,
    title: str,
    platforms: Optional[list] = None,
    youtube_extra: Optional[dict] = None,
) -> dict:
    return upload_post_service.upload_video(video_path, title, platforms, youtube_extra=youtube_extra)
