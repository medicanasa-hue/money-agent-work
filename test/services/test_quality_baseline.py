import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import quality_baseline


class TestQualityBaseline(unittest.TestCase):
    def test_build_render_quality_baseline_waits_for_five_real_videos(self):
        baseline = quality_baseline.build_render_quality_baseline(
            [
                {
                    "video_path": "first.mp4",
                    "ok": True,
                    "duration": 20.0,
                    "fps": 30.0,
                    "resolution": [1080, 1920],
                    "warnings": [],
                    "has_audio": True,
                },
                {
                    "video_path": "second.mp4",
                    "ok": False,
                    "duration": 24.0,
                    "fps": 30.0,
                    "resolution": [1080, 1920],
                    "warnings": ["sampled audio is near-silent"],
                    "has_audio": True,
                },
            ]
        )

        self.assertFalse(baseline["ready"])
        self.assertEqual(baseline["required_video_count"], 5)
        self.assertEqual(baseline["available_video_count"], 2)
        self.assertEqual(baseline["average_duration"], 22.0)
        self.assertEqual(baseline["near_silent_count"], 1)

    def test_build_render_quality_baseline_summarizes_five_reports(self):
        reports = [
            {
                "video_path": f"video-{index}.mp4",
                "ok": index != 3,
                "duration": 10.0 + index,
                "fps": 30.0,
                "resolution": [1080, 1920] if index < 4 else [1080, 1350],
                "warnings": ["video keyframe interval exceeds the encoding contract"]
                if index == 3
                else [],
                "has_audio": True,
            }
            for index in range(5)
        ]

        baseline = quality_baseline.build_render_quality_baseline(reports)

        self.assertTrue(baseline["ready"])
        self.assertEqual(baseline["quality_ok_count"], 4)
        self.assertEqual(baseline["resolution_counts"], {"1080x1920": 4, "1080x1350": 1})
        self.assertEqual(
            baseline["warning_counts"],
            {"video keyframe interval exceeds the encoding contract": 1},
        )

    def test_build_render_quality_baseline_summarizes_advisory_color_consistency(self):
        reports = [
            {
                "video_path": f"video-{index}.mp4",
                "ok": True,
                "duration": 12.0,
                "fps": 30.0,
                "resolution": [1080, 1920],
                "warnings": [],
                "has_audio": True,
                "color_consistency": color_consistency,
            }
            for index, color_consistency in enumerate(
                [
                    {
                        "sample_count": 3,
                        "status": "consistent",
                        "warmth_spread": 0.08,
                        "saturation_spread": 0.06,
                    },
                    {
                        "sample_count": 3,
                        "status": "mixed",
                        "warmth_spread": 0.72,
                        "saturation_spread": 0.55,
                    },
                    {"sample_count": 0, "status": "unavailable"},
                    {"sample_count": 3, "status": "consistent"},
                    {"sample_count": 3, "status": "consistent"},
                ]
            )
        ]

        baseline = quality_baseline.build_render_quality_baseline(reports)

        color_summary = baseline["color_consistency"]
        self.assertEqual(color_summary["status"], "mixed")
        self.assertEqual(color_summary["analyzed_video_count"], 4)
        self.assertEqual(color_summary["mixed_video_count"], 1)
        self.assertEqual(
            color_summary["status_counts"],
            {"consistent": 3, "mixed": 1, "unavailable": 1},
        )
        self.assertEqual(color_summary["average_warmth_spread"], 0.4)
        self.assertEqual(color_summary["average_saturation_spread"], 0.305)

    def test_build_render_quality_baseline_ignores_unknown_color_statuses(self):
        baseline = quality_baseline.build_render_quality_baseline(
            [
                {
                    "color_consistency": {"status": "unexpected"},
                    "resolution": [1080, 1920],
                    "warnings": ["existing warning"],
                }
            ]
        )

        self.assertEqual(baseline["color_consistency"]["status"], "unavailable")
        self.assertEqual(baseline["color_consistency"]["analyzed_video_count"], 0)
        self.assertEqual(baseline["color_consistency"]["status_counts"], {})
        self.assertEqual(baseline["resolution_counts"], {"1080x1920": 1})
        self.assertEqual(baseline["warning_counts"], {"existing warning": 1})

    def test_collect_render_quality_baseline_uses_latest_task_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_dir = Path(temp_dir) / "tasks" / "first"
            second_dir = Path(temp_dir) / "tasks" / "second"
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            first_video = first_dir / "final-1.mp4"
            second_video = second_dir / "final-1.mp4"
            first_video.touch()
            second_video.touch()
            os.utime(first_video, (1, 1))
            os.utime(second_video, (2, 2))

            with (
                patch.object(quality_baseline.utils, "storage_dir", return_value=temp_dir),
                patch.object(
                    quality_baseline.render_quality,
                    "inspect_rendered_video",
                    return_value={
                        "ok": True,
                        "duration": 15.0,
                        "fps": 30.0,
                        "resolution": [1080, 1920],
                        "warnings": [],
                        "has_audio": True,
                    },
                ) as inspect,
            ):
                baseline = quality_baseline.collect_render_quality_baseline(
                    max_videos=1
                )

        self.assertEqual(baseline["available_video_count"], 1)
        self.assertEqual(baseline["video_paths"], [str(second_video)])
        inspect.assert_called_once_with(str(second_video))

    def test_refresh_automatic_baseline_uses_saved_reports_and_notifies_once(self):
        history_entries = [
            {
                "render_quality_reports": [
                    {
                        "video_path": f"previous-{index}.mp4",
                        "ok": True,
                        "warnings": [],
                        "has_audio": True,
                        "color_consistency": {"status": "consistent"},
                    }
                ]
            }
            for index in range(4)
        ]
        current_report = {
            "video_path": "current.mp4",
            "ok": False,
            "warnings": ["sampled audio is near-silent"],
            "has_audio": True,
            "color_consistency": {"status": "mixed"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(quality_baseline.utils, "storage_dir", return_value=temp_dir),
                patch.object(
                    quality_baseline.history,
                    "list_history",
                    return_value=history_entries,
                ),
            ):
                first = quality_baseline.refresh_automatic_render_quality_baseline(
                    [current_report]
                )
                second = quality_baseline.refresh_automatic_render_quality_baseline(
                    [current_report]
                )
                saved = json.loads(
                    (
                        Path(temp_dir)
                        / quality_baseline.AUTOMATIC_BASELINE_FILENAME
                    ).read_text(encoding="utf-8")
                )

        self.assertTrue(first["baseline"]["ready"])
        self.assertIn("ses", first["notification_summary"])
        self.assertIsNone(second["notification_summary"])
        self.assertEqual(len(saved["reports"]), 5)

    def test_refresh_automatic_baseline_stays_silent_for_healthy_reports(self):
        reports = [
            {
                "video_path": f"video-{index}.mp4",
                "ok": True,
                "warnings": [],
                "has_audio": True,
                "color_consistency": {"status": "consistent"},
            }
            for index in range(5)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(quality_baseline.utils, "storage_dir", return_value=temp_dir),
                patch.object(quality_baseline.history, "list_history", return_value=[]),
            ):
                result = quality_baseline.refresh_automatic_render_quality_baseline(
                    reports
                )

        self.assertTrue(result["baseline"]["ready"])
        self.assertIsNone(result["notification_summary"])


if __name__ == "__main__":
    unittest.main()
