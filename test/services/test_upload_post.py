from requests.exceptions import ConnectionError, HTTPError
from app.services.upload_post import (
    UploadPostService,
    aggregate_post_metrics,
    extract_post_platform_metrics,
    extract_result_link,
    has_post_metrics,
    normalize_youtube_privacy_status,
)

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


_CONFIG_BASE = {
    "upload_post_enabled": True,
    "upload_post_api_key": "test-key",
    "upload_post_username": "testuser",
    "upload_post_platforms": ["tiktok", "instagram", "youtube"],
    "upload_post_auto_upload": True,
    "upload_post_youtube_privacy_status": "unlisted",
}


def _mock_response(success=True, payload=None):
    r = MagicMock()
    r.json.return_value = (
        payload
        if payload is not None
        else {"success": success, "request_id": "abc123"}
    )
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


class TestUploadPostService(unittest.TestCase):
    @patch(
        "app.services.upload_post.config.app",
        {**_CONFIG_BASE, "upload_post_enabled": False},
    )
    @patch("app.services.upload_post.requests.post")
    def test_unconfigured_service_skips_request(self, mock_post):
        """功能未启用时不能意外上传文件或消耗第三方 API 配额。"""
        result = UploadPostService().upload_video("/fake/v.mp4", "Title")

        self.assertFalse(result["success"])
        self.assertIn("not configured", result["error"])
        mock_post.assert_not_called()

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=False)
    @patch("app.services.upload_post.requests.post")
    def test_missing_video_skips_request(self, mock_post, _exists):
        """本地成片不存在时应在发起网络请求前返回明确错误。"""
        result = UploadPostService().upload_video("/missing/v.mp4", "Title")

        self.assertFalse(result["success"])
        self.assertIn("Video file not found", result["error"])
        mock_post.assert_not_called()

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_upload_request_error_returns_failure(self, mock_post, _exists):
        """网络异常需要转换为稳定结果，不能让发布失败中断视频生成任务。"""
        mock_post.side_effect = requests.exceptions.Timeout("upload timed out")

        result = UploadPostService().upload_video("/fake/v.mp4", "Title")

        self.assertFalse(result["success"])
        self.assertIn("upload timed out", result["error"])

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_check_status_returns_payload_or_network_failure(self, mock_get):
        """状态查询成功和失败应使用与上传接口一致的返回约定。"""
        response = _mock_response()
        response.json.return_value = {"success": True, "status": "processing"}
        mock_get.return_value = response
        service = UploadPostService()

        self.assertEqual(
            service.check_status("request-123"),
            {"success": True, "status": "processing"},
        )

        mock_get.side_effect = requests.exceptions.ConnectionError("offline")
        failed = service.check_status("request-123")
        self.assertFalse(failed["success"])
        self.assertIn("offline", failed["error"])


class TestUploadPostYouTubePayload(unittest.TestCase):
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


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

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
        self.assertEqual(_get(data, "is_aigc"), "false")
        self.assertFalse(_has_key(data, "youtube_title"))
        self.assertFalse(_has_key(data, "containsSyntheticMedia"))
        self.assertFalse(_has_key(data, "privacyStatus"))

    @patch("app.services.upload_post.config.app", {
        **_CONFIG_BASE,
        "upload_post_platforms": ["tiktok"],
        "upload_post_tiktok_is_aigc": True,
    })
    @patch("app.services.upload_post.os.path.exists", return_value=True)
    @patch("builtins.open", mock_open(read_data=b"fake"))
    @patch("app.services.upload_post.requests.post")
    def test_tiktok_aigc_label_can_be_enabled(self, mock_post, _exists):
        mock_post.return_value = _mock_response()
        svc = UploadPostService()
        svc.upload_video("/fake/v.mp4", "T")

        data = mock_post.call_args[1]["data"]
        self.assertEqual(_get(data, "is_aigc"), "true")

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

class TestUploadPostAnalytics(unittest.TestCase):
    @patch(
        "app.services.upload_post.config.app",
        {
            **_CONFIG_BASE,
            "upload_post_enabled": False,
        },
    )
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_skips_network_when_unconfigured(self, mock_get):
        svc = UploadPostService()

        result = svc.get_post_analytics("req-123")

        self.assertFalse(result["success"])
        self.assertIn("not configured", result["error"].lower())
        mock_get.assert_not_called()

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_skips_blank_request_ids(self, mock_get):
        svc = UploadPostService()

        result = svc.get_post_analytics("  ")

        self.assertFalse(result["success"])
        self.assertIn("request id", result["error"].lower())
        mock_get.assert_not_called()

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_uses_request_id_endpoint(self, mock_get):
        payload = {"success": True, "platforms": {}}
        mock_get.return_value = _mock_response(payload=payload)
        svc = UploadPostService()

        result = svc.get_post_analytics("req-123")

        self.assertEqual(result, payload)
        call_url = mock_get.call_args.args[0]
        self.assertTrue(
            call_url.endswith("/api/uploadposts/post-analytics/req-123"),
            f"Endpoint incorrecto: {call_url}",
        )
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Apikey test-key",
        )
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 30)

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_rejects_non_object_responses(self, mock_get):
        response = _mock_response()
        response.json.return_value = []
        mock_get.return_value = response
        svc = UploadPostService()

        result = svc.get_post_analytics("req-123")

        self.assertFalse(result["success"])
        self.assertIn("invalid analytics response", result["error"].lower())

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_handles_invalid_json(self, mock_get):
        response = _mock_response()
        response.json.side_effect = ValueError("invalid json")
        mock_get.return_value = response
        svc = UploadPostService()

        result = svc.get_post_analytics("req-123")

        self.assertFalse(result["success"])
        self.assertIn("invalid analytics response", result["error"].lower())

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.logger")
    @patch(
        "app.services.upload_post.requests.get",
        side_effect=ConnectionError("network down api_key=secret-value"),
    )
    def test_get_post_analytics_returns_controlled_failure(self, _mock_get, logger):
        svc = UploadPostService()

        result = svc.get_post_analytics("req-123")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Unable to fetch post analytics")
        self.assertTrue(result["retryable"])
        self.assertNotIn("secret-value", result["error"])
        logger.error.assert_called_once_with("Failed to fetch post analytics")

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_spaces_individual_requests(self, mock_get):
        mock_get.return_value = _mock_response(payload={"success": True, "platforms": {}})
        clock = _FakeClock()
        svc = UploadPostService(
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        svc.get_post_analytics("req-1")
        svc.get_post_analytics("req-2")

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(clock.sleeps, [1.0])

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_retries_transient_failures_with_backoff(
        self, mock_get
    ):
        mock_get.side_effect = [
            ConnectionError("temporary"),
            ConnectionError("temporary"),
            _mock_response(payload={"success": True, "platforms": {}}),
        ]
        clock = _FakeClock()
        svc = UploadPostService(
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        result = svc.get_post_analytics("req-1")

        self.assertTrue(result["success"])
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(clock.sleeps, [1.0, 2.0])

    @patch("app.services.upload_post.config.app", _CONFIG_BASE)
    @patch("app.services.upload_post.requests.get")
    def test_get_post_analytics_does_not_retry_permanent_http_errors(self, mock_get):
        mock_get.side_effect = HTTPError(
            "bad request",
            response=MagicMock(status_code=400),
        )
        clock = _FakeClock()
        svc = UploadPostService(
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        result = svc.get_post_analytics("req-1")

        self.assertFalse(result["success"])
        self.assertFalse(result["retryable"])
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_aggregate_post_metrics_single_platform(self):
        metrics = aggregate_post_metrics({
            "success": True,
            "platforms": {
                "youtube": {
                    "success": True,
                    "post_metrics": {
                        "views": 5200,
                        "likes": 120,
                        "comments": 8,
                        "shares": 4,
                        "saves": 7,
                    },
                }
            },
        })

        self.assertEqual(metrics, {
            "views": 5200,
            "likes": 120,
            "comments": 8,
            "shares": 4,
            "saves": 7,
        })

    def test_aggregate_post_metrics_sums_multiple_platforms(self):
        metrics = aggregate_post_metrics({
            "success": True,
            "platforms": {
                "youtube": {
                    "success": True,
                    "post_metrics": {
                        "views": 100,
                        "likes": 10,
                        "comments": 1,
                        "favorites": 3,
                    },
                },
                "tiktok": {
                    "success": True,
                    "post_metrics": {
                        "views": 200,
                        "likes": 20,
                        "comments": 2,
                        "shares": 5,
                        "saves": 6,
                    },
                },
            },
        })

        self.assertEqual(metrics, {
            "views": 300,
            "likes": 30,
            "comments": 3,
            "shares": 5,
            "saves": 9,
        })

    def test_extract_post_platform_metrics_keeps_platform_breakdown(self):
        metrics = extract_post_platform_metrics(
            {
                "success": True,
                "platforms": {
                    "youtube": {
                        "success": True,
                        "post_metrics": {"views": 100, "likes": 10, "favorites": 3},
                    },
                    "tiktok": {
                        "success": True,
                        "post_metrics": {"views": 200, "likes": 20, "shares": 5},
                    },
                },
            }
        )

        self.assertEqual(
            metrics,
            {
                "youtube": {
                    "views": 100,
                    "likes": 10,
                    "comments": 0,
                    "shares": 0,
                    "saves": 3,
                },
                "tiktok": {
                    "views": 200,
                    "likes": 20,
                    "comments": 0,
                    "shares": 5,
                    "saves": 0,
                },
            },
        )

    @patch("app.services.upload_post.logger")
    def test_aggregate_post_metrics_skips_failed_platforms(self, logger):
        metrics = aggregate_post_metrics({
            "success": True,
            "platforms": {
                "youtube": {
                    "success": False,
                    "post_metrics_error": "not ready api_key=secret-value",
                },
                "instagram": {
                    "success": True,
                    "post_metrics": {"views": 50, "likes": 4},
                },
            },
        })

        self.assertEqual(metrics, {
            "views": 50,
            "likes": 4,
            "comments": 0,
            "shares": 0,
            "saves": 0,
        })
        logger.warning.assert_called_once_with(
            "skipping Upload-Post analytics for youtube: platform reported failure"
        )

    def test_has_post_metrics_requires_a_platform_metric_object(self):
        self.assertFalse(has_post_metrics({"success": True, "platforms": {}}))
        self.assertTrue(
            has_post_metrics(
                {
                    "success": True,
                    "platforms": {
                        "youtube": {
                            "success": True,
                            "post_metrics": {"views": 0},
                        }
                    },
                }
            )
        )
