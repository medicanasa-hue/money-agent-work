import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models.schema import VideoAspect
from app.utils import openmontage_materials


class FindOpenMontageOutputTest(unittest.TestCase):
    def test_probe_uses_ffprobe_beside_configured_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary_directory = Path(temporary_directory)
            ffmpeg_binary = binary_directory / "ffmpeg.exe"
            ffprobe_binary = binary_directory / "ffprobe.exe"
            video_path = binary_directory / "video.mp4"
            ffmpeg_binary.touch()
            ffprobe_binary.touch()
            video_path.touch()

            with (
                patch(
                    "app.utils.utils.get_ffmpeg_binary",
                    return_value=str(ffmpeg_binary),
                ),
                patch.object(
                    openmontage_materials.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=(
                            '{"streams": [{"width": 1080, "height": 1920}], '
                            '"format": {"duration": "8"}}'
                        ),
                    ),
                ) as run,
            ):
                metadata = openmontage_materials._probe_openmontage_video(video_path)

        self.assertEqual(metadata["width"], 1080)
        self.assertEqual(run.call_args.args[0][0], str(ffprobe_binary))

    def test_finds_matching_narrated_openmontage_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_720p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "inflation money"
                )

            self.assertEqual(result, str(output))

    def test_prefers_native_landscape_output_for_landscape_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            generic_output = project_dir / "final_silent_720p.mp4"
            native_output = project_dir / "final_silent_16x9_1080p.mp4"
            project_dir.mkdir(parents=True)
            generic_output.touch()
            native_output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "money inflation",
                    prefer_silent=True,
                    video_aspect=VideoAspect.landscape,
                )

            self.assertEqual(result, str(native_output))

    def test_returns_none_when_no_project_name_matches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_720p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output("ocean wildlife")

            self.assertIsNone(result)

    def test_recognizes_finished_output_inside_openmontage_projects(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_silent_9x16_1080p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.is_openmontage_output_path(output)

            self.assertTrue(result)

    def test_rejects_non_output_file_inside_openmontage_projects(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            source_file = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "script.py"
            )
            source_file.parent.mkdir(parents=True)
            source_file.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.is_openmontage_output_path(source_file)

            self.assertFalse(result)

    def test_prefers_native_portrait_output_for_portrait_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            landscape_output = project_dir / "final_silent_720p.mp4"
            portrait_output = project_dir / "final_silent_9x16_720p.mp4"
            project_dir.mkdir(parents=True)
            landscape_output.touch()
            portrait_output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "money inflation",
                    prefer_silent=True,
                    video_aspect=VideoAspect.portrait,
                )

            self.assertEqual(result, str(portrait_output))

    def test_prefers_1080p_native_portrait_output_when_both_exist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            lower_resolution_output = project_dir / "final_9x16_720p.mp4"
            higher_resolution_output = project_dir / "final_9x16_1080p.mp4"
            project_dir.mkdir(parents=True)
            lower_resolution_output.touch()
            higher_resolution_output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "money inflation", video_aspect=VideoAspect.portrait
                )

            self.assertEqual(result, str(higher_resolution_output))

    def test_finds_native_four_by_five_output_for_four_by_five_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            output = project_dir / "final_silent_tr_4x5_1080p.mp4"
            project_dir.mkdir(parents=True)
            output.touch()
            (project_dir / "openmontage.json").write_text(
                '{"keywords": ["para arzÄ± enflasyon"]}', encoding="utf-8"
            )

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "para arzÄ± enflasyon",
                    prefer_silent=True,
                    video_aspect=VideoAspect.portrait_4_5,
                    language="tr",
                )

            self.assertEqual(result, str(output))

    def test_rejects_landscape_only_output_for_portrait_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_720p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "money inflation",
                    video_aspect="9:16",
                )

            self.assertIsNone(result)

    def test_uses_next_best_project_when_best_match_lacks_requested_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            projects_dir = project_root / "OpenMontage" / "projects"
            best_match = projects_dir / "money-printing-inflation"
            available_match = projects_dir / "money-inflation-animation"
            best_match.mkdir(parents=True)
            available_match.mkdir(parents=True)
            output = available_match / "final_silent_9x16_1080p.mp4"
            output.touch()

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "money printing inflation",
                    prefer_silent=True,
                    video_aspect=VideoAspect.portrait,
                )

            self.assertEqual(result, str(output))

    def test_finds_portrait_output_from_project_keyword_alias(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            output = project_dir / "final_silent_9x16_1080p.mp4"
            project_dir.mkdir(parents=True)
            output.touch()
            (project_dir / "openmontage.json").write_text(
                '{"keywords": ["para arzı enflasyon"]}', encoding="utf-8"
            )

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "para arzı enflasyon",
                    prefer_silent=True,
                    video_aspect=VideoAspect.portrait,
                )

            self.assertEqual(result, str(output))

    def test_prefers_turkish_portrait_output_for_turkish_language(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            english_output = project_dir / "final_silent_9x16_1080p.mp4"
            turkish_output = project_dir / "final_silent_tr_9x16_1080p.mp4"
            project_dir.mkdir(parents=True)
            english_output.touch()
            turkish_output.touch()
            (project_dir / "openmontage.json").write_text(
                '{"keywords": ["para arzı enflasyon"]}', encoding="utf-8"
            )

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                result = openmontage_materials.find_openmontage_output(
                    "para arzı enflasyon",
                    prefer_silent=True,
                    video_aspect=VideoAspect.portrait,
                    language="tr-TR",
                )

            self.assertEqual(result, str(turkish_output))

    def test_validate_openmontage_output_rejects_a_landscape_file_for_portrait_use(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_silent_9x16_1080p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_output(
                    output,
                    video_aspect="9:16",
                    probe=lambda _path: {"width": 1920, "height": 1080, "duration": 12},
                )

        self.assertFalse(report["valid"])
        self.assertIn("aspect_mismatch", report["issues"])

    def test_validate_openmontage_output_accepts_a_native_portrait_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_silent_9x16_1080p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_output(
                    output,
                    video_aspect="9:16",
                    probe=lambda _path: {"width": 1080, "height": 1920, "duration": 12},
                )

        self.assertTrue(report["valid"])
        self.assertEqual(report["resolution"], "1080x1920")

    def test_validate_openmontage_output_reports_low_bitrate_without_rejecting_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_silent_9x16_1080p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_output(
                    output,
                    video_aspect="9:16",
                    probe=lambda _path: {
                        "width": 1080,
                        "height": 1920,
                        "duration": 12,
                        "avg_frame_rate": "30/1",
                        "bit_rate": "450000",
                    },
                )

        self.assertTrue(report["valid"])
        self.assertIn("low_bitrate_review_recommended", report["quality_warnings"])
        self.assertEqual(report["bitrate_kbps"], 450)
        self.assertGreater(report["quality_reference_kbps"], report["bitrate_kbps"])

    def test_validate_openmontage_output_keeps_high_bitrate_output_clean(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            output = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
                / "final_silent_9x16_1080p.mp4"
            )
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_output(
                    output,
                    video_aspect="9:16",
                    probe=lambda _path: {
                        "width": 1080,
                        "height": 1920,
                        "duration": 12,
                        "avg_frame_rate": "30/1",
                        "bit_rate": "3500000",
                    },
                )

        self.assertTrue(report["valid"])
        self.assertEqual(report["quality_warnings"], [])
        self.assertEqual(report["bitrate_kbps"], 3500)

    def test_validate_openmontage_library_checks_manifest_languages_and_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            project_dir.mkdir(parents=True)
            (project_dir / "openmontage.json").write_text(
                '{"languages": ["en", "tr"], "keywords": ["money inflation"]}',
                encoding="utf-8",
            )
            (project_dir / "final_silent_9x16_1080p.mp4").write_bytes(b"video")
            (project_dir / "final_silent_tr_9x16_1080p.mp4").write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_library(
                    probe=lambda _path: {
                        "width": 1080,
                        "height": 1920,
                        "duration": 12,
                    }
                )

        self.assertTrue(report["valid"])
        self.assertEqual(report["project_count"], 1)
        self.assertTrue(report["projects"][0]["valid"])

    def test_validate_openmontage_library_reports_missing_manifest_language_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            project_dir = (
                project_root
                / "OpenMontage"
                / "projects"
                / "money-printing-inflation"
            )
            project_dir.mkdir(parents=True)
            (project_dir / "openmontage.json").write_text(
                '{"languages": ["en", "tr"], "keywords": ["money inflation"]}',
                encoding="utf-8",
            )
            (project_dir / "final_silent_9x16_1080p.mp4").write_bytes(b"video")

            with patch.object(openmontage_materials, "PROJECT_ROOT", project_root):
                report = openmontage_materials.validate_openmontage_library(
                    probe=lambda _path: {
                        "width": 1080,
                        "height": 1920,
                        "duration": 12,
                    }
                )

        self.assertFalse(report["valid"])
        self.assertIn("missing_language_output:tr", report["projects"][0]["issues"])
