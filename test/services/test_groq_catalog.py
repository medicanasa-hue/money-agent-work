import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import groq_catalog


class TestGroqCatalogSecurity(unittest.TestCase):
    def test_custom_or_private_base_url_never_receives_api_key(self):
        rejected_urls = (
            "http://127.0.0.1:8000/v1",
            "http://169.254.169.254/latest",
            "https://proxy.example/v1",
            "https://user:password@api.groq.com/openai/v1",
            "file:///etc/passwd",
        )

        for base_url in rejected_urls:
            with self.subTest(base_url=base_url), patch.object(
                groq_catalog.requests, "get"
            ) as get:
                result = groq_catalog.get_model_ids("catalog-test-credential", base_url)

            self.assertEqual(result, [])
            get.assert_not_called()

    def test_official_catalog_uses_constant_url_and_disables_redirects(self):
        payload = json.dumps(
            {"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "a-model"}]}
        ).encode("utf-8")
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Length": str(len(payload))},
            iter_content=lambda chunk_size: iter((payload,)),
            close=Mock(),
        )
        with (
            patch.object(
                groq_catalog,
                "public_http_url_addresses",
                return_value=frozenset({"93.184.216.34"}),
            ),
            patch.object(
                groq_catalog.requests, "get", return_value=response
            ) as get,
        ):
            result = groq_catalog.get_model_ids(
                "catalog-test-credential", "https://api.groq.com/openai/v1/"
            )

        self.assertEqual(result, ["a-model", "z-model"])
        get.assert_called_once_with(
            groq_catalog.GROQ_MODELS_URL,
            headers={"Authorization": "Bearer catalog-test-credential"},
            timeout=(5, 10),
            stream=True,
            allow_redirects=False,
        )
        response.close.assert_called_once_with()

    def test_official_catalog_rejects_redirect_without_forwarding_key(self):
        response = SimpleNamespace(status_code=302, headers={}, close=Mock())
        with (
            patch.object(
                groq_catalog,
                "public_http_url_addresses",
                return_value=frozenset({"93.184.216.34"}),
            ),
            patch.object(groq_catalog.requests, "get", return_value=response) as get,
        ):
            result = groq_catalog.get_model_ids(
                "catalog-test-credential", "https://api.groq.com/openai/v1"
            )

        self.assertEqual(result, [])
        self.assertEqual(get.call_count, 1)
        response.close.assert_called_once_with()

    def test_official_catalog_rejects_private_dns_before_request(self):
        with (
            patch.object(groq_catalog, "public_http_url_addresses", return_value=None),
            patch.object(groq_catalog.requests, "get") as get,
        ):
            result = groq_catalog.get_model_ids(
                "catalog-test-credential", "https://api.groq.com/openai/v1"
            )

        self.assertEqual(result, [])
        get.assert_not_called()

    def test_official_catalog_rejects_oversized_response(self):
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Length": str(groq_catalog.MAX_CATALOG_BYTES + 1)},
            iter_content=Mock(),
            close=Mock(),
        )
        with (
            patch.object(
                groq_catalog,
                "public_http_url_addresses",
                return_value=frozenset({"93.184.216.34"}),
            ),
            patch.object(groq_catalog.requests, "get", return_value=response),
        ):
            result = groq_catalog.get_model_ids(
                "catalog-test-credential", "https://api.groq.com/openai/v1"
            )

        self.assertEqual(result, [])
        response.iter_content.assert_not_called()
        response.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
