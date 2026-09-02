import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import visual_pacing


class TestVisualPacing(unittest.TestCase):
    def test_pacing_budget_reports_balanced_plan_and_cue_alignment(self):
        report = visual_pacing.build_visual_pacing_budget(
            audio_duration=30,
            max_clip_duration=5,
            scene_count=7,
            cue_end_times=[4.8, 10.2, 15.1, 19.9, 25.0],
        )

        self.assertEqual(report["planned_visual_count"], 6)
        self.assertEqual(report["pacing_status"], "balanced")
        self.assertEqual(report["scene_coverage_status"], "sufficient")
        self.assertEqual(report["cue_alignment_opportunity_count"], 5)

    def test_pacing_budget_flags_sparse_visual_coverage(self):
        report = visual_pacing.build_visual_pacing_budget(
            audio_duration=30,
            max_clip_duration=3,
            scene_count=2,
        )

        self.assertEqual(report["planned_visual_count"], 10)
        self.assertEqual(report["scene_coverage_status"], "sparse")
        self.assertEqual(report["recommended_clip_duration_seconds"], 7.0)
