import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.controllers import base


class TestTaskStaticFiles(unittest.TestCase):
    def setUp(self):
        base.reset_auth_rate_limits()
        self.addCleanup(base.reset_auth_rate_limits)
        fixture_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task_root = fixture_root / "tasks"
        self.task_root.mkdir()
        self.outside = fixture_root / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("must not be served", encoding="utf-8")
        (self.task_root / "önizleme.mp4").write_bytes(b"0123456789")

        # Exercise the deployed mount configuration using synthetic files only.
        task_mount = next(route.app for route in asgi.app.routes if route.path == "/tasks")
        self.enterContext(patch.object(task_mount, "directory", str(self.task_root)))
        self.enterContext(patch.object(task_mount, "all_directories", [str(self.task_root)]))
        self.enterContext(patch.object(asgi.config, "app", {"api_key": ""}))
        self.enterContext(patch.object(asgi.config, "listen_host", "127.0.0.1"))
        self.client = TestClient(asgi.app)
        self.addCleanup(self.client.close)

    def _symlink(self, target: Path, name: str, *, directory: bool = False):
        try:
            (self.task_root / name).symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symbolic links are unavailable: {error}")

    def test_serves_unicode_filename_and_byte_ranges_without_a_key(self):
        full = self.client.get("/tasks/önizleme.mp4")
        partial = self.client.get("/tasks/önizleme.mp4", headers={"Range": "bytes=3-6"})

        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"0123456789")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"3456")
        self.assertEqual(partial.headers["content-range"], "bytes 3-6/10")

    def test_rejects_symlink_to_file_outside_task_root(self):
        self._symlink(self.outside / "secret.txt", "leak.txt")
        response = self.client.get("/tasks/leak.txt")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"must not be served", response.content)

    def test_rejects_symlink_to_directory_outside_task_root(self):
        self._symlink(self.outside, "linked-directory", directory=True)
        response = self.client.get("/tasks/linked-directory/secret.txt")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"must not be served", response.content)

    def test_preserves_symlinks_that_resolve_inside_task_root(self):
        self._symlink(self.task_root / "önizleme.mp4", "internal.mp4")
        response = self.client.get("/tasks/internal.mp4")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"0123456789")

    def test_rejects_encoded_parent_directory_traversal(self):
        response = self.client.get("/tasks/%2e%2e/outside/secret.txt")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"must not be served", response.content)

    def test_configured_key_protects_loopback_and_network_downloads(self):
        for listen_host in ("127.0.0.1", "::1", "0.0.0.0"):
            with self.subTest(listen_host=listen_host), (
                patch.object(asgi.config, "app", {"api_key": "secret"})
            ), patch.object(asgi.config, "listen_host", listen_host):
                missing = self.client.get("/tasks/önizleme.mp4")
                wrong = self.client.get("/tasks/önizleme.mp4", headers={"x-api-key": "wrong"})
                accepted = self.client.get(
                    "/tasks/önizleme.mp4", headers={"x-api-key": "secret", "Range": "bytes=3-6"}
                )

                self.assertEqual(missing.status_code, 401)
                self.assertEqual(wrong.status_code, 401)
                self.assertEqual(accepted.status_code, 206)
                self.assertEqual(accepted.content, b"3456")

    def test_forwarded_headers_do_not_exempt_loopback_proxy_requests(self):
        with patch.object(asgi.config, "app", {"api_key": "secret"}):
            response = self.client.get(
                "/tasks/önizleme.mp4",
                headers={"x-forwarded-for": "127.0.0.1", "x-forwarded-host": "localhost"},
            )

        self.assertEqual(response.status_code, 401)

    def test_static_and_api_requests_share_invalid_key_rate_limit(self):
        with patch.object(asgi.config, "app", {"api_key": "secret", "api_auth_max_failures": 1}):
            api_failure = self.client.get("/api/v1/tasks")
            blocked_file = self.client.get("/tasks/önizleme.mp4")
            accepted_file = self.client.get("/tasks/önizleme.mp4", headers={"x-api-key": "secret"})
            reset_failure = self.client.get("/api/v1/tasks")

        self.assertEqual(api_failure.status_code, 401)
        self.assertEqual(blocked_file.status_code, 429)
        self.assertEqual(accepted_file.status_code, 200)
        self.assertEqual(reset_failure.status_code, 401)

    def test_duplicate_header_is_rejected_for_task_files(self):
        with patch.object(asgi.config, "app", {"api_key": "secret"}):
            response = self.client.get(
                "/tasks/önizleme.mp4",
                headers=[("x-api-key", "secret"), ("x-api-key", "wrong")],
            )

        self.assertEqual(response.status_code, 401)

    def test_malformed_key_configuration_does_not_expose_task_files(self):
        with patch.object(asgi.config, "app", {"api_key": False}):
            response = self.client.get("/tasks/önizleme.mp4")

        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"0123456789", response.content)

    def test_docs_remain_public_but_untrusted_cors_preflight_is_rejected(self):
        with patch.object(asgi.config, "app", {"api_key": "secret"}):
            docs = self.client.get("/docs")
            preflight = self.client.options(
                "/tasks/önizleme.mp4",
                headers={
                    "Origin": "https://editor.example.test",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-api-key",
                },
            )

        self.assertEqual(docs.status_code, 200)
        self.assertEqual(preflight.status_code, 403)
        self.assertNotIn("access-control-allow-origin", preflight.headers)


if __name__ == "__main__":
    unittest.main()
