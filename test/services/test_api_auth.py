import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.controllers import base
from app.controllers.v1 import llm as llm_controller
from app.controllers.v1 import video as video_controller
from app.models.exception import HttpException


def _request(headers=None):
    encoded_headers = []
    header_items = headers.items() if isinstance(headers, dict) else headers or []
    for key, value in header_items:
        encoded_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/videos",
            "headers": encoded_headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def _uses_verify_token(router):
    return any(
        getattr(dependency, "dependency", None) is base.verify_token
        for dependency in router.dependencies
    )


class TestApiAuth(unittest.TestCase):
    def setUp(self):
        base.reset_auth_rate_limits()

    def tearDown(self):
        base.reset_auth_rate_limits()

    def test_v1_video_and_llm_routers_require_verify_token(self):
        self.assertTrue(_uses_verify_token(video_controller.router))
        self.assertTrue(_uses_verify_token(llm_controller.router))

    @patch("app.controllers.base.config.app", {"api_key": ""})
    def test_verify_token_allows_requests_when_api_key_is_not_configured(self):
        base.verify_token(_request())

    @patch("app.controllers.base.config.app", {"api_key": "secret"})
    def test_verify_token_accepts_matching_api_key(self):
        base.verify_token(_request({"x-api-key": "secret"}))

    @patch("app.controllers.base.config.app", {"api_key": "secret"})
    def test_verify_token_rejects_missing_api_key(self):
        with self.assertRaises(HttpException) as context:
            base.verify_token(_request())

        self.assertEqual(context.exception.status_code, 401)

    @patch("app.controllers.base.config.app", {"api_key": "secret"})
    def test_verify_token_rejects_duplicate_keys_in_either_order(self):
        for values in (("secret", "wrong"), ("wrong", "secret"), ("secret", "secret")):
            with self.subTest(values=values):
                base.reset_auth_rate_limits()
                with self.assertRaises(HttpException) as context:
                    base.verify_token(_request([("x-api-key", value) for value in values]))
                self.assertEqual(context.exception.status_code, 401)

    @patch("app.controllers.base.config.app", {"api_key": "secret"})
    def test_verify_token_rejects_non_ascii_header_without_crashing(self):
        with self.assertRaises(HttpException) as context:
            base.verify_token(_request({"x-api-key": "invalid-é"}))
        self.assertEqual(context.exception.status_code, 401)

    @patch("app.controllers.base.config.app", {"api_key": "sëcret"})
    def test_verify_token_accepts_matching_unicode_key(self):
        base.verify_token(_request({"x-api-key": "sëcret"}))

    @patch("app.controllers.base.config.app", {"api_key": "  secret  "})
    def test_verify_token_preserves_surrounding_whitespace_compatibility(self):
        base.verify_token(_request({"x-api-key": " secret "}))

    def test_verify_token_rejects_non_string_config_even_when_falsy(self):
        for configured_key in (False, 0, 123, [], {}, ["secret"]):
            with self.subTest(configured_key=configured_key):
                with patch.object(base.config, "app", {"api_key": configured_key}):
                    with self.assertRaises(HttpException) as context:
                        base.verify_token(_request({"x-api-key": "123"}))
                self.assertEqual(context.exception.status_code, 500)
                self.assertEqual(
                    context.exception.message, "API authentication is misconfigured"
                )

    def test_verify_token_preserves_empty_config_local_access(self):
        for configured_key in (None, "", "   "):
            with self.subTest(configured_key=configured_key):
                with patch.object(base.config, "app", {"api_key": configured_key}):
                    base.verify_token(_request())

    @patch("app.controllers.base.config.app", {"api_key": "secret", "api_auth_max_failures": 1})
    def test_duplicate_key_attempts_share_existing_rate_limit(self):
        request = _request([("x-api-key", "secret"), ("x-api-key", "wrong")])
        with self.assertRaises(HttpException) as first:
            base.verify_token(request)
        with self.assertRaises(HttpException) as blocked:
            base.verify_token(request)

        self.assertEqual(first.exception.status_code, 401)
        self.assertEqual(blocked.exception.status_code, 429)

    @patch("app.controllers.base.config.app", {"api_key": "secret", "api_auth_max_failures": 1})
    def test_untrusted_forwarded_header_does_not_reset_rate_limit(self):
        with self.assertRaises(HttpException) as first:
            base.verify_token(_request({"x-forwarded-for": "192.0.2.1"}))
        with self.assertRaises(HttpException) as blocked:
            base.verify_token(_request({"x-forwarded-for": "192.0.2.2"}))

        self.assertEqual(first.exception.status_code, 401)
        self.assertEqual(blocked.exception.status_code, 429)

    @patch(
        "app.controllers.base.config.app",
        {
            "api_key": "secret",
            "api_auth_max_failures": 2,
            "api_auth_failure_window_seconds": 60,
        },
    )
    @patch("app.controllers.base.time.monotonic", side_effect=[100.0, 101.0, 102.0])
    def test_verify_token_rate_limits_repeated_failed_attempts(self, _monotonic):
        with self.assertRaises(HttpException) as first:
            base.verify_token(_request({"x-api-key": "wrong"}))
        with self.assertRaises(HttpException) as second:
            base.verify_token(_request({"x-api-key": "wrong"}))
        with self.assertRaises(HttpException) as blocked:
            base.verify_token(_request({"x-api-key": "wrong"}))

        self.assertEqual(first.exception.status_code, 401)
        self.assertEqual(second.exception.status_code, 401)
        self.assertEqual(blocked.exception.status_code, 429)
        self.assertEqual(blocked.exception.message, "too many invalid API key attempts")

    @patch(
        "app.controllers.base.config.app",
        {
            "api_key": "secret",
            "api_auth_max_failures": 2,
            "api_auth_failure_window_seconds": 60,
        },
    )
    @patch("app.controllers.base.time.monotonic", side_effect=[100.0, 101.0, 102.0])
    def test_successful_api_key_clears_failed_attempt_counter(self, _monotonic):
        with self.assertRaises(HttpException):
            base.verify_token(_request({"x-api-key": "wrong"}))
        base.verify_token(_request({"x-api-key": "secret"}))
        with self.assertRaises(HttpException) as after_reset:
            base.verify_token(_request({"x-api-key": "wrong"}))

        self.assertEqual(after_reset.exception.status_code, 401)

    @patch(
        "app.controllers.base.config.app",
        {
            "api_key": "secret",
            "api_auth_max_failures": 5,
            "api_auth_failure_window_seconds": 60,
        },
    )
    @patch.object(base, "_MAX_AUTH_RATE_LIMIT_CLIENTS", 2)
    @patch("app.controllers.base.time.monotonic", return_value=100.0)
    def test_rate_limit_cache_evicts_oldest_client_at_capacity(self, _monotonic):
        base._auth_failure_attempts.update(
            {
                "oldest-client": [99.0],
                "newer-client": [99.5],
            }
        )

        with self.assertRaises(HttpException):
            base.verify_token(_request({"x-api-key": "wrong"}))

        self.assertNotIn("oldest-client", base._auth_failure_attempts)
        self.assertIn("testclient", base._auth_failure_attempts)
        self.assertLessEqual(
            len(base._auth_failure_attempts), base._MAX_AUTH_RATE_LIMIT_CLIENTS
        )


if __name__ == "__main__":
    unittest.main()
