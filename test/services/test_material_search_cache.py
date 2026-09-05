"""Offline contracts for the bounded, process-local provider search cache."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from app.config import config
from app.services import material_cache


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class TestMaterialSearchCache(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(config.app, {}, clear=True)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        material_cache.clear_material_search_cache()
        self.addCleanup(material_cache.clear_material_search_cache)
        self.payload = {"videos": [{"id": 10, "files": [{"url": "https://example.test/v.mp4"}]}]}
        self.response = Response(self.payload)
        self.request = Mock(return_value=self.response)
        self.url = "https://example.test/search?page=1"

    def search(self, url=None, **kwargs):
        return material_cache.get_search_json(
            url or self.url, items_key="videos", request_get=self.request, **kwargs
        )

    def test_repeated_search_uses_one_request_and_closes_response(self):
        self.assertEqual(self.search(), self.payload)
        self.assertEqual(self.search(), self.payload)
        self.assertEqual(self.request.call_count, 1)
        self.assertTrue(self.response.closed)

    def test_nested_mutation_never_changes_cached_payload(self):
        first = self.search()
        first["videos"][0]["files"][0]["url"] = "changed first result"
        second = self.search()
        self.assertEqual(second["videos"][0]["files"][0]["url"], "https://example.test/v.mp4")
        second["videos"][0]["files"].clear()
        self.assertEqual(len(self.search()["videos"][0]["files"]), 1)
        self.assertEqual(self.request.call_count, 1)

    def test_canonical_mapping_order_uses_same_cache_entry(self):
        self.search(headers={"Authorization": "account", "Accept": "json"}, verify=True)
        self.search(verify=True, headers={"Accept": "json", "Authorization": "account"})
        self.assertEqual(self.request.call_count, 1)

    def test_account_page_proxy_tls_and_timeout_are_separate_requests(self):
        variants = [
            (self.url, {"headers": {"Authorization": "account-a"}}),
            (self.url, {"headers": {"Authorization": "account-b"}}),
            (self.url + "&key=account-a", {}),
            (self.url + "&key=account-b", {}),
            (self.url.replace("page=1", "page=2"), {}),
            (self.url, {"proxies": {"https": "http://proxy-a.test"}}),
            (self.url, {"proxies": {"https": "http://proxy-b.test"}}),
            (self.url, {"verify": False}),
            (self.url, {"verify": True}),
            (self.url, {"verify": "custom-ca.pem"}),
            (self.url, {"timeout": (30, 60)}),
            (self.url, {"timeout": (10, 60)}),
        ]
        for url, kwargs in variants:
            self.search(url, **kwargs)
            self.search(url, **kwargs)
        self.assertEqual(self.request.call_count, len(variants))

    def test_expected_items_key_separates_cache_entries(self):
        self.request.return_value = Response({"videos": [{"id": 1}], "hits": [{"id": 2}]})
        self.search()
        material_cache.get_search_json(self.url, items_key="hits", request_get=self.request)
        self.assertEqual(self.request.call_count, 2)

    def test_expiry_uses_monotonic_time_and_does_not_slide_on_hit(self):
        with patch.object(material_cache.time, "monotonic", return_value=100) as clock:
            self.search()
            clock.return_value = 100 + material_cache.MATERIAL_SEARCH_CACHE_TTL_SECONDS - 1
            self.search()
            self.assertEqual(self.request.call_count, 1)
            clock.return_value += 1
            self.search()
        self.assertEqual(self.request.call_count, 2)

    def test_lru_evicts_least_recently_used_entry(self):
        with patch.object(material_cache, "_MAX_CACHE_ENTRIES", 2):
            self.search("https://example.test/a")
            self.search("https://example.test/b")
            self.search("https://example.test/a")
            self.search("https://example.test/c")
            self.search("https://example.test/a")
            self.assertEqual(self.request.call_count, 3)
            self.search("https://example.test/b")
        self.assertEqual(self.request.call_count, 4)

    def test_clear_forces_a_new_request(self):
        self.search()
        material_cache.clear_material_search_cache()
        self.search()
        self.assertEqual(self.request.call_count, 2)

    def test_disabled_and_invalid_settings_bypass_cache(self):
        for value in (False, "false", " FALSE ", "off", "no", "0", "", "typo", None, 1, []):
            with self.subTest(value=value), patch.dict(config.app, {"material_search_cache_enabled": value}):
                material_cache.clear_material_search_cache()
                self.request.reset_mock()
                self.search()
                self.search()
                self.assertEqual(self.request.call_count, 2)

    def test_explicit_true_settings_enable_cache(self):
        for value in (True, "true", " TRUE ", "on", "yes", "1"):
            with self.subTest(value=value), patch.dict(config.app, {"material_search_cache_enabled": value}):
                material_cache.clear_material_search_cache()
                self.request.reset_mock()
                self.search()
                self.search()
                self.assertEqual(self.request.call_count, 1)

    def test_disabled_cache_bypasses_existing_entries_and_does_not_write(self):
        self.search()
        with patch.dict(config.app, {"material_search_cache_enabled": False}):
            self.search()
            self.search("https://example.test/uncached")
        self.search("https://example.test/uncached")
        self.assertEqual(self.request.call_count, 4)

    def test_empty_malformed_and_unserializable_payloads_are_not_retained(self):
        recursive = {}
        recursive["self"] = recursive
        payloads = [
            {}, {"videos": []}, {"videos": "invalid"}, {"videos": [1]},
            {"videos": [{"id": 1}, None]}, {"hits": [{"id": 1}]}, [], None,
            {"videos": [{"id": object()}]}, {"videos": [{"nested": recursive}]},
            {"videos": [{"score": float("nan")}]},
        ]
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                material_cache.clear_material_search_cache()
                self.request.reset_mock()
                self.request.return_value = Response(payload)
                self.assertIs(self.search(), payload)
                self.assertIs(self.search(), payload)
                self.assertEqual(self.request.call_count, 2)

    def test_oversized_payload_is_returned_but_not_retained(self):
        with patch.object(material_cache, "_MAX_PAYLOAD_BYTES", 8):
            self.assertEqual(self.search(), self.payload)
            self.assertEqual(self.search(), self.payload)
        self.assertEqual(self.request.call_count, 2)

    def test_nonserializable_request_key_falls_back_without_dropping_kwargs(self):
        marker = object()
        self.search(verify=marker)
        self.search(verify=marker)
        self.assertEqual(self.request.call_count, 2)
        self.request.assert_called_with(self.url, verify=marker)

    def test_http_errors_are_closed_and_retried(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                material_cache.clear_material_search_cache()
                rejected = Response(self.payload, status)
                self.request.reset_mock()
                self.request.side_effect = [rejected, self.response]
                with self.assertRaises(requests.HTTPError):
                    self.search()
                self.assertTrue(rejected.closed)
                self.assertEqual(self.search(), self.payload)
                self.assertEqual(self.request.call_count, 2)

    def test_non_success_statuses_are_not_cached(self):
        for status in (199, 301, 304, None, "200"):
            with self.subTest(status=status):
                material_cache.clear_material_search_cache()
                self.request.reset_mock()
                self.request.return_value = Response(self.payload, status)
                self.search()
                self.search()
                self.assertEqual(self.request.call_count, 2)

    def test_json_errors_close_response_and_are_retried(self):
        invalid = Response(ValueError("invalid JSON"))
        self.request.side_effect = [invalid, self.response]
        with self.assertRaises(ValueError):
            self.search()
        self.assertTrue(invalid.closed)
        self.assertEqual(self.search(), self.payload)
        self.assertEqual(self.request.call_count, 2)

    def test_network_errors_are_retried(self):
        self.request.side_effect = [requests.Timeout("timeout"), self.response]
        with self.assertRaises(requests.Timeout):
            self.search()
        self.assertEqual(self.search(), self.payload)
        self.assertEqual(self.request.call_count, 2)

    def test_simultaneous_identical_requests_share_one_success(self):
        start = threading.Barrier(8)
        all_missed = threading.Event()
        seen_threads = set()
        seen_lock = threading.Lock()
        load = material_cache._load

        def observe_miss(key):
            result = load(key)
            if result[0] is None:
                with seen_lock:
                    seen_threads.add(threading.get_ident())
                    if len(seen_threads) == 8:
                        all_missed.set()
            return result

        def delayed_response(*args, **kwargs):
            # Keep the first HTTP call open until all eight callers have missed.
            # A barrier alone could allow an immediate response to warm the cache.
            self.assertTrue(all_missed.wait(timeout=5))
            return self.response

        self.request.side_effect = delayed_response

        def search():
            start.wait(timeout=5)
            return self.search()

        with patch.object(material_cache, "_load", side_effect=observe_miss), ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: search(), range(8)))
        self.assertTrue(all(result == self.payload for result in results))
        self.assertEqual(self.request.call_count, 1)

    def test_clear_during_request_prevents_stale_response_repopulation(self):
        entered = threading.Event()
        finish = threading.Event()

        def delayed_request(*args, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(timeout=5))
            return self.response

        self.request.side_effect = delayed_request
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.search)
            try:
                self.assertTrue(entered.wait(timeout=5))
                material_cache.clear_material_search_cache()
            finally:
                finish.set()
            self.assertEqual(future.result(timeout=5), self.payload)
        self.request.side_effect = None
        self.search()
        self.assertEqual(self.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
