import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.asgi import (
    TaskOutputStaticFiles,
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

    def test_task_files_are_protected_on_all_hosts_when_a_key_is_configured(self):
        self.assertFalse(should_protect_task_outputs("", "0.0.0.0"))
        self.assertFalse(should_protect_task_outputs("   ", "127.0.0.1"))
        self.assertTrue(should_protect_task_outputs("secret", "127.0.0.1"))
        self.assertTrue(should_protect_task_outputs("secret", "0.0.0.0"))
        self.assertTrue(should_protect_task_outputs("secret", "::1"))

    def test_invalid_key_configuration_never_disables_task_protection(self):
        for invalid_key in (False, 0, [], {}):
            with self.subTest(invalid_key=invalid_key):
                self.assertTrue(should_protect_task_outputs(invalid_key, "127.0.0.1"))


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
