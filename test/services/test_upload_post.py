import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services.upload_post import (
    UploadPostService,
    extract_result_link,
    normalize_youtube_privacy_status,
)


_CONFIG_BASE = {
    "upload_post_enabled": True,
    "upload_post_api_key": "test-key",
    "upload_post_username": "testuser",
    "upload_post_platforms": ["tiktok", "instagram", "youtube"],
    "upload_post_auto_upload": True,
    "upload_post_youtube_privacy_status": "unlisted",
}


def _mock_response(success=True):
    r = MagicMock()
    r.json.return_value = {"success": success, "request_id": "abc123"}
    r.raise_for_status = MagicMock()
    return r


def _get(data, key):
    for k, v in data:
        if k == key:
            return v
    return None


def _get_all(data, key):
    return [v for k, v in data if k == key]


def _has_key(data, key):
    return any(k == key for k, v in data)


class TestUploadPostYouTube(unittest.TestCase):

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_youtube_fields_en_payload(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()

        svc.upload_video("/fake/v.mp4", "Título", youtube_extra={
            "youtube_title": "Mi Short",
            "youtube_description": "Descripción",
            "tags": ["ia", "shorts"],
            "privacyStatus": "unlisted",
        })

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "youtube_title"), "Mi Short")
        self.assertEqual(_get(data, "youtube_description"), "Descripción")
        self.assertEqual(_get_all(data, "tags[]"), ["ia", "shorts"])
        self.assertEqual(_get(data, "privacyStatus"), "unlisted")
        self.assertEqual(_get(data, "containsSyntheticMedia"), "true")

    def test_public_youtube_privacy_requires_explicit_allow_flag(self):
        self.assertEqual(
            normalize_youtube_privacy_status("public", allow_public=False),
            "private",
        )
        self.assertEqual(
            normalize_youtube_privacy_status("public", allow_public=True),
            "public",
        )
        self.assertEqual(
            normalize_youtube_privacy_status("invalid", allow_public=True),
            "private",
        )

    @patch("app.services.upload_post.config.app", {
        key: value
        for key, value in _CONFIG_BASE.items()
        if key != "upload_post_youtube_privacy_status"
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_missing_youtube_privacy_defaults_to_unlisted(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()

        svc.upload_video("/fake/v.mp4", "T", youtube_extra={"youtube_title": "T"})

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "privacyStatus"), "unlisted")

    @patch("app.services.upload_post.config.app", {
        **_CONFIG_BASE,
        "upload_post_youtube_privacy_status": "public",
        "upload_post_allow_public_youtube": False,
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_public_youtube_upload_falls_back_to_private_by_default(
        self,
        mock_post,
        _exists,
    ):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()

        svc.upload_video("/fake/v.mp4", "T", youtube_extra={"privacyStatus": "public"})

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "privacyStatus"), "private")

    @patch("app.services.upload_post.config.app", {
        **_CONFIG_BASE,
        "upload_post_youtube_privacy_status": "public",
        "upload_post_allow_public_youtube": True,
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_public_youtube_upload_is_allowed_with_explicit_flag(
        self,
        mock_post,
        _exists,
    ):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()

        svc.upload_video("/fake/v.mp4", "T", youtube_extra={"privacyStatus": "public"})

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "privacyStatus"), "public")

    def test_reload_config_reads_updated_upload_settings(self):
        app_config = {
            **_CONFIG_BASE,
            "upload_post_youtube_privacy_status": "unlisted",
            "upload_post_allow_public_youtube": False,
        }
        with patch("app.services.upload_post.config.app", app_config):
            svc = UploadPostService()
            self.assertEqual(svc.youtube_privacy_status, "unlisted")
            self.assertTrue(svc.enabled)

            app_config["upload_post_enabled"] = False
            app_config["upload_post_youtube_privacy_status"] = "public"
            app_config["upload_post_allow_public_youtube"] = True
            svc.reload_config()

            self.assertFalse(svc.enabled)
            self.assertEqual(svc.youtube_privacy_status, "public")

    def test_extract_result_link_reads_common_direct_fields(self):
        self.assertEqual(
            extract_result_link({"post_url": "https://youtube.com/shorts/abc"}),
            "https://youtube.com/shorts/abc",
        )
        self.assertEqual(
            extract_result_link({"youtube_url": "https://youtube.com/watch?v=abc"}),
            "https://youtube.com/watch?v=abc",
        )

    def test_extract_result_link_reads_nested_platform_fields(self):
        result = {
            "request_id": "abc123",
            "results": {
                "youtube": {
                    "url": "https://youtube.com/shorts/abc",
                }
            },
        }

        self.assertEqual(
            extract_result_link(result),
            "https://youtube.com/shorts/abc",
        )

    def test_extract_result_link_ignores_non_url_values(self):
        self.assertIsNone(extract_result_link({"request_id": "abc123"}))
        self.assertIsNone(extract_result_link({"url": "not-a-url"}))

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_contains_synthetic_media_siempre_true(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()

        svc.upload_video("/fake/v.mp4", "T", youtube_extra={"containsSyntheticMedia": False})

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "containsSyntheticMedia"), "true")

    @patch("app.services.upload_post.config.app", {
        **_CONFIG_BASE,
        "upload_post_platforms": ["tiktok", "instagram"],
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_tiktok_instagram_sin_youtube_fields(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()
        svc.upload_video("/fake/v.mp4", "T")

        data = mock_post.call_args[1]["data"]
        self.assertFalse(_has_key(data, "youtube_title"))
        self.assertFalse(_has_key(data, "containsSyntheticMedia"))
        self.assertFalse(_has_key(data, "privacyStatus"))

    @patch("app.services.upload_post.config.app", {
        **_CONFIG_BASE,
        "upload_post_platforms": ["tiktok"],
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_youtube_extra_ignorado_si_youtube_no_en_platforms(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()
        svc.upload_video("/fake/v.mp4", "T", youtube_extra={"youtube_title": "irrelevante"})

        data = mock_post.call_args[1]["data"]
        self.assertFalse(_has_key(data, "youtube_title"))

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", side_effect=OSError("file disappeared"))
    @patch("app.services.upload_post.requests.post")
    def test_file_open_error_returns_controlled_failure(
        self,
        mock_post,
        _open,
        _exists,
    ):
        svc = UploadPostService()

        result = svc.upload_video("/fake/v.mp4", "T")

        self.assertFalse(result["success"])
        self.assertIn("Could not read video file", result["error"])
        mock_post.assert_not_called()

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_endpoint_y_platform_format_correcto(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()
        svc.upload_video("/fake/v.mp4", "T")

        call_url = mock_post.call_args[0][0]
        self.assertTrue(call_url.endswith("/api/upload"), f"Endpoint incorrecto: {call_url}")

        data = mock_post.call_args[1]["data"]
        platforms = _get_all(data, "platform[]")
        self.assertIn("tiktok", platforms)
        self.assertIn("instagram", platforms)
        self.assertIn("youtube", platforms)


if __name__ == "__main__":
    unittest.main()
