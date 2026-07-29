import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.asgi import (
    TaskOutputStaticFiles,
    cors_configuration,
    should_protect_task_outputs,
    warn_if_api_unprotected,
)


class TestAsgiAuthWarning(unittest.TestCase):
    def test_warns_when_api_key_empty_on_non_loopback(self):
        self.assertIsNotNone(warn_if_api_unprotected("", "0.0.0.0"))
        self.assertIsNotNone(warn_if_api_unprotected("   ", "::"))

    def test_does_not_warn_for_loopback_without_api_key(self):
        self.assertIsNone(warn_if_api_unprotected("", "127.0.0.1"))
        self.assertIsNone(warn_if_api_unprotected("", "localhost"))
        self.assertIsNone(warn_if_api_unprotected("", "::1"))

    def test_does_not_warn_when_api_key_is_set(self):
        self.assertIsNone(warn_if_api_unprotected("secret", "0.0.0.0"))

    def test_local_cors_allows_local_tools_without_browser_credentials(self):
        origins, allow_credentials = cors_configuration("127.0.0.1", "")

        self.assertEqual(origins, ["*"])
        self.assertFalse(allow_credentials)

    def test_network_cors_requires_explicit_origins(self):
        origins, allow_credentials = cors_configuration("0.0.0.0", "")

        self.assertEqual(origins, [])
        self.assertFalse(allow_credentials)

    def test_explicit_cors_origins_can_use_credentials_but_wildcard_cannot(self):
        origins, allow_credentials = cors_configuration(
            "0.0.0.0", " https://editor.example.test , https://review.example.test "
        )
        self.assertEqual(
            origins,
            ["https://editor.example.test", "https://review.example.test"],
        )
        self.assertTrue(allow_credentials)

        wildcard_origins, wildcard_credentials = cors_configuration("0.0.0.0", "*")
        self.assertEqual(wildcard_origins, ["*"])
        self.assertFalse(wildcard_credentials)

    def test_task_files_are_only_protected_for_authenticated_network_hosts(self):
        self.assertFalse(should_protect_task_outputs("", "0.0.0.0"))
        self.assertFalse(should_protect_task_outputs("secret", "127.0.0.1"))
        self.assertTrue(should_protect_task_outputs("secret", "0.0.0.0"))


class TestTaskOutputStaticFiles(unittest.IsolatedAsyncioTestCase):
    async def test_network_task_output_rejects_a_missing_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "preview.txt").write_text("preview", encoding="utf-8")
            static_files = TaskOutputStaticFiles(directory=temp_dir)
            with patch("app.asgi.config.app", {"api_key": "secret"}), patch(
                "app.asgi.config.listen_host", "0.0.0.0"
            ):
                response = await static_files.get_response(
                    "preview.txt",
                    {"type": "http", "method": "GET", "headers": []},
                )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
