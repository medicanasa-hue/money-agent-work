import unittest

from app.services import quality_calibration


class TestQualityCalibration(unittest.TestCase):
    def test_normalize_publish_metrics_clamps_safe_integer_values(self):
        metrics = quality_calibration.normalize_publish_metrics(
            {
                "views": "1000",
                "likes": "80",
                "comments": "8",
                "shares": "4",
                "saves": "3",
                "captured_at": "2026-07-07T10:00:00+00:00",
            }
        )

        self.assertEqual(metrics["views"], 1000)
        self.assertEqual(metrics["likes"], 80)
        self.assertEqual(metrics["shares"], 4)
        self.assertEqual(metrics["captured_at"], "2026-07-07T10:00:00+00:00")

    def test_quality_gate_report_summarizes_publish_performance(self):
        report = quality_calibration.build_quality_gate_calibration_report(
            [
                {
                    "task_id": "strong",
                    "viral_analysis": {"overall_score": 82},
                    "publish_metrics": {
                        "views": 2000,
                        "likes": 120,
                        "comments": 20,
                        "shares": 10,
                        "saves": 10,
                    },
                },
                {
                    "task_id": "weak",
                    "viral_analysis": {"overall_score": 48},
                    "publish_metrics": {
                        "views": 200,
                        "likes": 1,
                        "comments": 0,
                        "shares": 0,
                        "saves": 0,
                    },
                },
            ],
            current_threshold=60,
        )

        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["strong_count"], 1)
        self.assertEqual(report["weak_count"], 1)
        self.assertEqual(report["recommended_threshold"], 65)
        self.assertIn("raising", report["recommendation"].lower())

    def test_quality_gate_report_handles_invalid_current_threshold(self):
        report = quality_calibration.build_quality_gate_calibration_report(
            [],
            current_threshold="not-a-number",
        )

        self.assertEqual(report["recommended_threshold"], 60)
        self.assertIn("collect more", report["recommendation"].lower())


if __name__ == "__main__":
    unittest.main()
