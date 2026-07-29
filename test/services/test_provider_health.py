import unittest
from unittest.mock import patch

from app.services import provider_health


class TestProviderHealth(unittest.TestCase):
    def test_build_video_source_health_reports_enabled_configuration_gaps(self):
        app_config = {
            "enabled_video_sources": ["pexels", "dvids", "nasa"],
            "pexels_api_keys": ["pexels-key"],
            "dvids_api_keys": [],
        }

        with patch.object(provider_health.config, "app", app_config):
            report = provider_health.build_video_source_health()

        self.assertEqual(report["enabled_count"], 3)
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual(report["needs_configuration_count"], 1)
        rows = {row["source"]: row for row in report["sources"]}
        self.assertEqual(rows["pexels"]["status"], "ready")
        self.assertEqual(rows["dvids"]["status"], "needs_configuration")
        self.assertEqual(rows["nasa"]["status"], "ready")
        self.assertNotIn("pexels-key", repr(report))

    def test_build_video_source_health_normalizes_string_source_list(self):
        app_config = {
            "enabled_video_sources": "nasa, wikimedia, unknown",
        }

        with patch.object(provider_health.config, "app", app_config):
            report = provider_health.build_video_source_health()

        enabled_rows = [
            row["source"] for row in report["sources"] if row["enabled"]
        ]
        self.assertEqual(enabled_rows, ["nasa", "wikimedia"])
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual(report["needs_configuration_count"], 0)


if __name__ == "__main__":
    unittest.main()
