import tempfile
import unittest
from unittest.mock import patch

from app.services import history


class TestScriptRepetition(unittest.TestCase):
    def test_finds_recent_near_duplicate_without_exposing_prior_script(self):
        previous_script = (
            "When inflation rises, central banks increase interest rates. "
            "That makes borrowing more expensive and slows spending across the economy."
        )
        current_script = (
            "When inflation rises, central banks usually increase interest rates. "
            "That makes borrowing more expensive and slows spending across the economy."
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "recent-duplicate",
                            "subject": "Why interest rates rise",
                            "script": previous_script,
                            "created_at": "2026-07-10T10:00:00+00:00",
                        },
                        {
                            "task_id": "different",
                            "subject": "Coffee prices",
                            "script": (
                                "Coffee harvest weather affects bean supply and local prices. "
                                "Small cafes react differently to a changing market."
                            ),
                            "created_at": "2026-07-10T10:00:00+00:00",
                        },
                    ]
                )

                matches = history.find_recent_similar_scripts(
                    current_script,
                    now="2026-07-15T10:00:00+00:00",
                )

        self.assertEqual([match["task_id"] for match in matches], ["recent-duplicate"])
        self.assertGreaterEqual(matches[0]["similarity"], 0.78)
        self.assertNotIn("script", matches[0])

    def test_ignores_short_or_invalid_scripts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "short-script",
                            "subject": "Budget",
                            "script": "Save this now",
                            "created_at": "2026-07-10T10:00:00+00:00",
                        },
                        {
                            "task_id": "missing-script",
                            "subject": "Budget",
                            "created_at": "2026-07-10T10:00:00+00:00",
                        },
                    ]
                )

                matches = history.find_recent_similar_scripts(
                    "Save this now",
                    now="2026-07-15T10:00:00+00:00",
                )

        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
