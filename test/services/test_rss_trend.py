import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import rss_trend


class TestRssTrend(unittest.TestCase):
    def setUp(self):
        rss_trend.clear_rss_trend_cache()

    def tearDown(self):
        rss_trend.clear_rss_trend_cache()

    def test_fetch_rss_trend_returns_recent_titles(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel>
                <item><title>First finance &amp; markets headline</title></item>
                <item><title>Second finance headline</title></item>
                <item><title>Third finance headline</title></item>
                <item><title>Ignored headline</title></item>
              </channel>
            </rss>"""

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()) as get:
            result = rss_trend.fetch_rss_trend("personal finance", limit=3)

        self.assertIn("First finance & markets headline", result)
        self.assertIn("Third finance headline", result)
        self.assertNotIn("Ignored headline", result)
        get.assert_called_once()
        self.assertIn("personal+finance", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["timeout"], 3.0)

    def test_fetch_rss_trend_uses_turkish_google_news_locale(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel><item><title>Turkish economy headline</title></item></channel></rss>"""

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()) as get:
            result = rss_trend.fetch_rss_trend(
                "ekonomi",
                language="tr-TR",
            )

        self.assertEqual(result, "Turkish economy headline")
        self.assertIn("hl=tr", get.call_args.args[0])
        self.assertIn("gl=TR", get.call_args.args[0])
        self.assertIn("ceid=TR:tr", get.call_args.args[0])

    def test_fetch_rss_trend_keeps_localized_results_separate_in_cache(self):
        class Response:
            def __init__(self, title):
                self.content = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<rss><channel><item><title>{title}</title></item></channel></rss>"
                ).encode("utf-8")

            def raise_for_status(self):
                return None

        with patch.object(
            rss_trend.requests,
            "get",
            side_effect=[Response("Turkish headline"), Response("Global headline")],
        ) as get:
            turkish = rss_trend.fetch_rss_trend(
                "economy",
                language="tr",
                now=100.0,
            )
            global_result = rss_trend.fetch_rss_trend("economy", now=101.0)

        self.assertEqual(turkish, "Turkish headline")
        self.assertEqual(global_result, "Global headline")
        self.assertEqual(get.call_count, 2)

    def test_fetch_rss_trend_items_keeps_safe_source_provenance(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0"><channel>
              <item>
                <title>Finance source headline</title>
                <link>https://news.example.test/finance</link>
                <source url="https://publisher.example.test">Example Publisher</source>
              </item>
              <item>
                <title>Unsafe source headline</title>
                <link>javascript:alert(1)</link>
              </item>
            </channel></rss>"""

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()):
            items = rss_trend.fetch_rss_trend_items("personal finance", limit=2)

        self.assertEqual(items[0]["title"], "Finance source headline")
        self.assertEqual(items[0]["url"], "https://news.example.test/finance")
        self.assertEqual(items[0]["publisher"], "Example Publisher")
        self.assertEqual(items[1]["url"], "")

    def test_fetch_rss_trend_items_uses_turkish_google_news_locale(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel><item><title>Turkish headline</title></item></channel></rss>"""

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()) as get:
            items = rss_trend.fetch_rss_trend_items("ekonomi", language="tr")

        self.assertEqual(items[0]["title"], "Turkish headline")
        self.assertIn("hl=tr", get.call_args.args[0])

    def test_fetch_rss_trend_returns_empty_on_network_error(self):
        with patch.object(rss_trend.requests, "get", side_effect=RuntimeError("offline")):
            result = rss_trend.fetch_rss_trend("personal finance")

        self.assertEqual(result, "")

    def test_fetch_rss_trend_returns_empty_on_invalid_xml(self):
        class Response:
            content = b"<rss><channel><item>"

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()):
            result = rss_trend.fetch_rss_trend("personal finance")

        self.assertEqual(result, "")

    def test_fetch_rss_trend_reuses_a_recent_successful_result(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel><item><title>Cached headline</title></item></channel></rss>"""

            def raise_for_status(self):
                return None

        with patch.object(rss_trend.requests, "get", return_value=Response()) as get:
            first = rss_trend.fetch_rss_trend(
                "personal finance",
                now=100.0,
            )
            second = rss_trend.fetch_rss_trend(
                "Personal   Finance",
                now=101.0,
            )

        self.assertEqual(first, "Cached headline")
        self.assertEqual(second, "Cached headline")
        get.assert_called_once()

    def test_fetch_rss_trend_does_not_cache_a_failed_request(self):
        class Response:
            content = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel><item><title>Recovered headline</title></item></channel></rss>"""

            def raise_for_status(self):
                return None

        with patch.object(
            rss_trend.requests,
            "get",
            side_effect=[RuntimeError("offline"), Response()],
        ) as get:
            self.assertEqual(
                rss_trend.fetch_rss_trend("personal finance", now=100.0),
                "",
            )
            self.assertEqual(
                rss_trend.fetch_rss_trend("personal finance", now=101.0),
                "Recovered headline",
            )

        self.assertEqual(get.call_count, 2)

    def test_fetch_rss_trend_refreshes_after_cache_expiry(self):
        class Response:
            def __init__(self, title):
                self.content = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<rss><channel><item><title>{title}</title></item></channel></rss>"
                ).encode("utf-8")

            def raise_for_status(self):
                return None

        with patch.object(
            rss_trend.requests,
            "get",
            side_effect=[Response("First headline"), Response("Fresh headline")],
        ) as get:
            self.assertEqual(
                rss_trend.fetch_rss_trend(
                    "personal finance",
                    cache_ttl_seconds=10,
                    now=100.0,
                ),
                "First headline",
            )
            self.assertEqual(
                rss_trend.fetch_rss_trend(
                    "personal finance",
                    cache_ttl_seconds=10,
                    now=111.0,
                ),
                "Fresh headline",
            )

        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
