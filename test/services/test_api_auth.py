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
    for key, value in (headers or {}).items():
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


if __name__ == "__main__":
    unittest.main()
