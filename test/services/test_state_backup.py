import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services import state_backup


class TestStateBackup(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary_directory.name) / "storage"
        self.storage_root.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_file(self, relative_path, content):
        path = self.storage_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_export_includes_durable_state_without_media_or_config(self):
        self._write_file("history/production_history.json", "[]")
        self._write_file("history/metrics_sync_run.json", "{}")
        self._write_file("history/review_feedback.json", "[]")
        self._write_file("presets/shorts.json", "{}")
        self._write_file("local_videos/.material_catalog.json", "{}")
        self._write_file("render_quality_baseline.json", "{}")
        self._write_file("tasks/task-1/final.mp4", "media")
        self._write_file("cache_videos/cached.mp4", "cache")
        archive_path = Path(self.temporary_directory.name) / "state-backup.zip"

        summary = state_backup.export_state_backup(
            archive_path,
            storage_root=self.storage_root,
        )

        self.assertTrue(summary["ok"])
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            manifest = json.loads(
                archive.read(state_backup.BACKUP_MANIFEST_FILENAME).decode("utf-8")
            )

        self.assertIn("history/production_history.json", names)
        self.assertIn("history/review_feedback.json", names)
        self.assertIn("presets/shorts.json", names)
        self.assertIn("local_videos/.material_catalog.json", names)
        self.assertIn("render_quality_baseline.json", names)
        self.assertNotIn("tasks/task-1/final.mp4", names)
        self.assertNotIn("cache_videos/cached.mp4", names)
        self.assertIn("config.toml", manifest["excluded"])

    def test_export_does_not_overwrite_an_existing_archive(self):
        archive_path = Path(self.temporary_directory.name) / "state-backup.zip"
        archive_path.write_bytes(b"existing backup")

        summary = state_backup.export_state_backup(
            archive_path,
            storage_root=self.storage_root,
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(archive_path.read_bytes(), b"existing backup")
