import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import local_material_catalog


class TestLocalMaterialCatalog(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.library_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _touch(self, name, content=b"material"):
        path = self.library_dir / name
        path.write_bytes(content)
        return path

    def test_lists_only_supported_files_and_keeps_tags(self):
        self._touch("economy-clip.mp4")
        self._touch("chart.png")
        self._touch("notes.txt")
        (self.library_dir / "nested").mkdir()

        saved_tags = local_material_catalog.save_local_material_tags(
            "economy-clip.mp4",
            [" Economy ", "economy", "interest   rates"],
            storage_dir=self.library_dir,
        )
        entries = local_material_catalog.list_local_materials(
            storage_dir=self.library_dir
        )

        self.assertEqual(saved_tags, ["Economy", "interest rates"])
        self.assertEqual([entry["name"] for entry in entries], ["chart.png", "economy-clip.mp4"])
        clip_entry = next(entry for entry in entries if entry["name"] == "economy-clip.mp4")
        self.assertEqual(clip_entry["kind"], "video")
        self.assertEqual(clip_entry["tags"], ["Economy", "interest rates"])
        self.assertIsNone(clip_entry["health"])

    def test_rejects_tag_updates_outside_the_library(self):
        self._touch("approved.mp4")
        outside_file = self.library_dir.parent / "outside.mp4"
        outside_file.write_bytes(b"outside")
        self.addCleanup(outside_file.unlink, missing_ok=True)

        with self.assertRaises(ValueError):
            local_material_catalog.save_local_material_tags(
                "../outside.mp4",
                ["economy"],
                storage_dir=self.library_dir,
            )

    def test_recommends_manual_tag_overlap_without_selecting_everything(self):
        self._touch("rates.mp4")
        self._touch("travel.mp4")
        local_material_catalog.save_local_material_tags(
            "rates.mp4", ["interest rates", "inflation"], storage_dir=self.library_dir
        )
        local_material_catalog.save_local_material_tags(
            "travel.mp4", ["holiday"], storage_dir=self.library_dir
        )

        recommendations = local_material_catalog.recommend_local_materials(
            "How interest rates affect inflation", storage_dir=self.library_dir
        )

        self.assertEqual([entry["name"] for entry in recommendations], ["rates.mp4"])
        self.assertGreater(recommendations[0]["match_score"], 0)

    def test_marks_known_public_domain_source_without_losing_tags(self):
        self._touch("earthquake.mp4")
        local_material_catalog.save_local_material_tags(
            "earthquake.mp4", ["earthquake", "geology"], storage_dir=self.library_dir
        )

        source = local_material_catalog.save_local_material_source(
            "earthquake.mp4", "usgs", storage_dir=self.library_dir
        )
        entries = local_material_catalog.list_local_materials(
            storage_dir=self.library_dir
        )
        stored_catalog = json.loads(
            (self.library_dir / local_material_catalog.CATALOG_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(source["id"], "usgs")
        self.assertEqual(entries[0]["tags"], ["earthquake", "geology"])
        self.assertEqual(entries[0]["source_id"], "usgs")
        self.assertEqual(entries[0]["source_label"], "USGS")
        self.assertEqual(
            entries[0]["license"], "USGS public domain (unless otherwise indicated)"
        )
        self.assertEqual(stored_catalog["earthquake.mp4"]["source_id"], "usgs")

    def test_rejects_unknown_public_domain_source(self):
        self._touch("approved.mp4")

        with self.assertRaises(ValueError):
            local_material_catalog.save_local_material_source(
                "approved.mp4", "unknown-source", storage_dir=self.library_dir
            )

    def test_nps_source_requires_a_rights_check_on_the_original_page(self):
        source = next(
            item
            for item in local_material_catalog.list_public_domain_sources()
            if item["id"] == "nps_yellowstone"
        )

        self.assertEqual(
            source["license"],
            "NPS Yellowstone public domain (when verified on the source page)",
        )

    def test_video_health_check_reports_ffprobe_result(self):
        clip_path = self._touch("clip.mp4")

        with patch.object(
            local_material_catalog.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["ffprobe"], returncode=0, stdout="video\n", stderr=""
            ),
        ):
            healthy = local_material_catalog.check_local_material_health(clip_path)

        with patch.object(
            local_material_catalog.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["ffprobe"], returncode=1, stdout="", stderr="invalid file"
            ),
        ):
            unhealthy = local_material_catalog.check_local_material_health(clip_path)

        self.assertEqual(healthy, {"ok": True, "detail": "Video stream found"})
        self.assertEqual(unhealthy, {"ok": False, "detail": "Video stream could not be read"})


if __name__ == "__main__":
    unittest.main()
