import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services import output_cleanup


class TestOutputCleanup(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.task_root = Path(self.temporary_directory.name) / "tasks"
        self.task_root.mkdir()
        self.cache_root = Path(self.temporary_directory.name) / "cache_videos"
        self.cache_root.mkdir()
        self.now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _task_directory(self, name, age_days):
        directory = self.task_root / name
        directory.mkdir()
        timestamp = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(directory, (timestamp, timestamp))
        return directory

    def _cache_file(self, name, age_days, content=b"video"):
        file_path = self.cache_root / name
        file_path.write_bytes(content)
        timestamp = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(file_path, (timestamp, timestamp))
        return file_path

    def test_preview_lists_only_expired_inactive_task_directories(self):
        expired = self._task_directory("expired", age_days=31)
        self._task_directory("recent", age_days=2)
        self._task_directory("active", age_days=31)
        (self.task_root / "readme.txt").write_text("keep", encoding="utf-8")

        summary = output_cleanup.cleanup_task_outputs(
            self.task_root,
            retention_days=30,
            active_task_ids={"active"},
            now=self.now,
        )

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["eligible"], ["expired"])
        self.assertEqual(summary["deleted"], [])
        self.assertEqual(summary["skipped_active"], 1)
        self.assertTrue(expired.is_dir())

    def test_apply_deletes_only_expired_inactive_task_directories(self):
        expired = self._task_directory("expired", age_days=31)
        active = self._task_directory("active", age_days=31)

        summary = output_cleanup.cleanup_task_outputs(
            self.task_root,
            retention_days=30,
            active_task_ids={"active"},
            apply=True,
            now=self.now,
        )

        self.assertFalse(summary["dry_run"])
        self.assertEqual(summary["deleted"], ["expired"])
        self.assertFalse(expired.exists())
        self.assertTrue(active.is_dir())

    def test_rejects_non_positive_retention_days(self):
        with self.assertRaises(ValueError):
            output_cleanup.cleanup_task_outputs(
                self.task_root,
                retention_days=0,
                now=self.now,
            )

    def test_cache_preview_lists_only_expired_video_files(self):
        expired = self._cache_file("expired.mp4", age_days=31, content=b"expired")
        self._cache_file("recent.mp4", age_days=2)
        self._cache_file("readme.txt", age_days=31)

        summary = output_cleanup.cleanup_video_cache(
            self.cache_root,
            retention_days=30,
            now=self.now,
        )

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["eligible"], ["expired.mp4"])
        self.assertEqual(summary["eligible_bytes"], expired.stat().st_size)
        self.assertEqual(summary["deleted"], [])
        self.assertTrue(expired.is_file())

    def test_cache_cleanup_refuses_to_delete_while_tasks_are_active(self):
        expired = self._cache_file("expired.mp4", age_days=31)

        summary = output_cleanup.cleanup_video_cache(
            self.cache_root,
            retention_days=30,
            apply=True,
            active_tasks_present=True,
            now=self.now,
        )

        self.assertTrue(summary["blocked_by_active_tasks"])
        self.assertEqual(summary["deleted"], [])
        self.assertTrue(expired.is_file())
