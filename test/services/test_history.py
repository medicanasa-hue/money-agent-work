import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import history


class TestProductionHistory(unittest.TestCase):
    def test_add_list_and_clear_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-1",
                        "subject": "first",
                        "videos": ["/tmp/first.mp4"],
                    }
                )
                history.add_history(
                    {
                        "task_id": "task-2",
                        "subject": "second",
                        "status": "failed",
                        "error": "boom",
                    }
                )

                entries = history.list_history()

                self.assertEqual([entry["task_id"] for entry in entries], ["task-2", "task-1"])
                self.assertEqual(entries[0]["status"], "failed")
                self.assertEqual(entries[1]["videos"], ["/tmp/first.mp4"])
                self.assertIsNone(entries[1]["cooldown"])
                self.assertIn("created_at", entries[0])

                history.clear_history()

                self.assertEqual(history.list_history(), [])

    def test_list_history_returns_empty_for_invalid_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / history.HISTORY_FILENAME
            path.write_text("{bad-json", encoding="utf-8")

            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                self.assertEqual(history.list_history(), [])

    def test_save_history_replaces_file_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / history.HISTORY_FILENAME
            temp_path = Path(f"{history_path}.tmp")

            with patch("app.services.history.utils.storage_dir", return_value=temp_dir), patch(
                "app.services.history.os.replace",
                wraps=os.replace,
            ) as replace:
                saved_path = history.save_history(
                    [
                        {
                            "task_id": "task-atomic",
                            "subject": "atomic",
                        }
                    ]
                )

                entries = history.list_history()

        self.assertEqual(saved_path, str(history_path))
        replace.assert_called_once_with(str(temp_path), str(history_path))
        self.assertFalse(temp_path.exists())
        self.assertEqual(entries[0]["task_id"], "task-atomic")

    def test_history_preserves_cooldown_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-cooldown",
                        "subject": "cooldown",
                        "cooldown": {
                            "moved_recent_count": 2,
                            "days": 7,
                        },
                    }
                )

                entries = history.list_history()

        self.assertEqual(
            entries[0]["cooldown"],
            {
                "moved_recent_count": 2,
                "days": 7,
            },
        )

    def test_history_preserves_pending_uploads(self):
        pending_uploads = [
            {
                "video_path": "/tmp/final.mp4",
                "title": "Ready to upload",
                "platforms": ["youtube"],
                "status": "pending",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": pending_uploads,
                    }
                )

                entries = history.list_history()

        self.assertEqual(entries[0]["pending_uploads"], pending_uploads)

    def test_update_pending_upload_result_marks_uploaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": [
                            {
                                "video_path": "/tmp/final.mp4",
                                "title": "Ready to upload",
                                "platforms": ["youtube"],
                                "status": "pending",
                            }
                        ],
                    }
                )

                updated = history.update_pending_upload_result(
                    "task-upload",
                    "/tmp/final.mp4",
                    {
                        "success": True,
                        "request_id": "abc123",
                        "post_url": "https://youtube.com/shorts/abc",
                    },
                )
                entries = history.list_history()

        self.assertTrue(updated)
        pending_upload = entries[0]["pending_uploads"][0]
        self.assertEqual(pending_upload["status"], "uploaded")
        self.assertEqual(
            pending_upload["result"],
            {
                "success": True,
                "request_id": "abc123",
                "post_url": "https://youtube.com/shorts/abc",
            },
        )
        self.assertIn("updated_at", pending_upload)

    def test_update_pending_upload_result_marks_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": [
                            {
                                "video_path": "/tmp/final.mp4",
                                "title": "Ready to upload",
                                "platforms": ["youtube"],
                                "status": "pending",
                            }
                        ],
                    }
                )

                updated = history.update_pending_upload_result(
                    "task-upload",
                    "/tmp/final.mp4",
                    {"success": False, "error": "quota exceeded"},
                )
                entries = history.list_history()

        self.assertTrue(updated)
        pending_upload = entries[0]["pending_uploads"][0]
        self.assertEqual(pending_upload["status"], "failed")
        self.assertEqual(
            pending_upload["result"],
            {"success": False, "error": "quota exceeded"},
        )

    def test_find_recent_similar_subjects_matches_recent_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "recent",
                            "subject": "Budget mistakes beginners make",
                            "created_at": "2026-07-04T10:00:00+00:00",
                        },
                        {
                            "task_id": "old",
                            "subject": "Budget mistakes beginners make",
                            "created_at": "2026-06-20T10:00:00+00:00",
                        },
                        {
                            "task_id": "different",
                            "subject": "Coffee brewing guide",
                            "created_at": "2026-07-05T10:00:00+00:00",
                        },
                    ]
                )

                matches = history.find_recent_similar_subjects(
                    "Beginner budget mistakes",
                    days=5,
                    now="2026-07-07T10:00:00+00:00",
                )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["task_id"], "recent")
        self.assertGreaterEqual(matches[0]["similarity"], 0.5)

    def test_find_recent_similar_subjects_ignores_blank_and_bad_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "blank",
                            "subject": "",
                            "created_at": "2026-07-06T10:00:00+00:00",
                        },
                        {
                            "task_id": "bad-date",
                            "subject": "Budget mistakes",
                            "created_at": "not-a-date",
                        },
                    ]
                )

                matches = history.find_recent_similar_subjects(
                    "Budget mistakes",
                    days=5,
                    now="2026-07-07T10:00:00+00:00",
                )

        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
