import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import rss_trend


class TestRssTrend(unittest.TestCase):
    def test_fetch_rss_trend_returns_recent_titles(self):
        class Response:
            content = b"<rss></rss>"

            def raise_for_status(self):
                return None

        class Entry:
            def __init__(self, title):
                self.title = title

        fake_feedparser = types.SimpleNamespace(
            parse=lambda content: types.SimpleNamespace(
                entries=[
                    Entry("First finance headline"),
                    Entry("Second finance headline"),
                    Entry("Third finance headline"),
                    Entry("Ignored headline"),
                ]
            )
        )

        with patch.dict(sys.modules, {"feedparser": fake_feedparser}):
            with patch.object(rss_trend.requests, "get", return_value=Response()) as get:
                result = rss_trend.fetch_rss_trend("personal finance", limit=3)

        self.assertIn("First finance headline", result)
        self.assertIn("Third finance headline", result)
        self.assertNotIn("Ignored headline", result)
        get.assert_called_once()
        self.assertIn("personal+finance", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["timeout"], 3.0)

    def test_fetch_rss_trend_returns_empty_on_network_error(self):
        fake_feedparser = types.SimpleNamespace(parse=lambda content: None)

        with patch.dict(sys.modules, {"feedparser": fake_feedparser}):
            with patch.object(rss_trend.requests, "get", side_effect=RuntimeError("offline")):
                result = rss_trend.fetch_rss_trend("personal finance")

        self.assertEqual(result, "")

    def test_fetch_rss_trend_returns_empty_without_feedparser(self):
        with patch.dict(sys.modules, {"feedparser": None}):
            result = rss_trend.fetch_rss_trend("personal finance")

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
