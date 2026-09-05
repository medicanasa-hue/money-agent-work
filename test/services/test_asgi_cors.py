import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from app import asgi


class TestASGICORS(unittest.TestCase):
    @staticmethod
    def _create_client(allowed_origins: list[str]) -> TestClient:
        application = FastAPI()

        @application.get("/probe")
        def probe():
            return {"status": "ok"}

        asgi.configure_browser_access(application, allowed_origins)
        return TestClient(application)

    def test_origin_parser_trims_values_and_ignores_empty_items(self):
        origins = asgi.parse_cors_allowed_origins(
            " https://a.example,https://b.example, ,"
        )

        self.assertEqual(
            origins,
            ["https://a.example", "https://b.example"],
        )
        self.assertEqual(asgi.parse_cors_allowed_origins(""), [])
        self.assertEqual(asgi.parse_cors_allowed_origins(None), [])

    def test_empty_configuration_rejects_cross_origin_browser_requests(self):
        with self._create_client([]) as client:
            origin = "https://evil.attacker.example"
            response = client.get("/probe", headers={"Origin": origin})
            preflight = client.options(
                "/probe",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertEqual(preflight.status_code, 403)
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_same_origin_and_server_clients_remain_compatible(self):
        with self._create_client([]) as client:
            same_origin = client.get(
                "/probe",
                headers={"Origin": "http://testserver"},
            )
            server_client = client.get("/probe")

        self.assertEqual(same_origin.status_code, 200)
        self.assertEqual(server_client.status_code, 200)

    def test_explicit_origin_allows_only_the_trusted_frontend(self):
        trusted_origin = "https://frontend.example"
        untrusted_origin = "https://evil.attacker.example"
        with self._create_client([trusted_origin]) as client:
            trusted = client.get("/probe", headers={"Origin": trusted_origin})
            untrusted = client.get("/probe", headers={"Origin": untrusted_origin})
            trusted_preflight = client.options(
                "/probe",
                headers={
                    "Origin": trusted_origin,
                    "Access-Control-Request-Method": "GET",
                },
            )
            untrusted_preflight = client.options(
                "/probe",
                headers={
                    "Origin": untrusted_origin,
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(
            trusted.headers["access-control-allow-origin"], trusted_origin
        )
        self.assertEqual(trusted.headers["access-control-allow-credentials"], "true")
        self.assertEqual(untrusted.status_code, 403)
        self.assertNotIn("access-control-allow-origin", untrusted.headers)
        self.assertEqual(trusted_preflight.status_code, 200)
        self.assertEqual(untrusted_preflight.status_code, 400)

    def test_trusted_origin_can_request_private_network_access(self):
        trusted_origin = "https://frontend.example"
        with self._create_client([trusted_origin]) as client:
            preflight = client.options(
                "/probe",
                headers={
                    "Origin": trusted_origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Private-Network": "true",
                },
            )

        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(
            preflight.headers["access-control-allow-private-network"],
            "true",
        )

    def test_explicit_wildcard_does_not_enable_credentials(self):
        origin = "https://frontend.example"
        with self._create_client(["*"]) as client:
            response = client.get("/probe", headers={"Origin": origin})
            preflight = client.options(
                "/probe",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", response.headers)
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", preflight.headers)

    def test_untrusted_multipart_request_is_rejected_before_side_effect(self):
        application = FastAPI()
        save_upload = Mock(return_value="stored.mp3")

        @application.post("/upload")
        def upload(file: UploadFile = File(...)):
            return {"file": save_upload(file.filename)}

        asgi.configure_browser_access(application, [])
        with TestClient(application) as client:
            response = client.post(
                "/upload",
                headers={"Origin": "https://evil.attacker.example"},
                files={
                    "file": (
                        "attack.mp3",
                        b"attacker-controlled",
                        "audio/mpeg",
                    )
                },
            )

        self.assertEqual(response.status_code, 403)
        save_upload.assert_not_called()

    def test_rejection_log_does_not_include_client_controlled_text(self):
        with self._create_client([]) as client, patch.object(
            asgi.logger, "warning"
        ) as warning:
            response = client.get(
                "/%1b%5b2Jprivate-marker",
                headers={"Origin": "https://private-origin.example"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(warning.call_count, 1)
        logged_arguments = str(warning.call_args)
        self.assertNotIn("private-marker", logged_arguments)
        self.assertNotIn("private-origin", logged_arguments)


if __name__ == "__main__":
    unittest.main()
