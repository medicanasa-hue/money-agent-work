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
        self.assertFalse(report.get("has_sufficient_samples", True))
        self.assertEqual(report.get("minimum_samples_per_bucket"), 5)
        self.assertIsNone(report["recommended_threshold"])
        self.assertIn("at least 5 strong", report["recommendation"].lower())

    def test_quality_gate_report_recommends_threshold_with_enough_samples(self):
        strong_sample = {
            "viral_analysis": {"overall_score": 82},
            "publish_metrics": {
                "views": 2000,
                "likes": 120,
                "comments": 20,
                "shares": 10,
                "saves": 10,
            },
        }
        weak_sample = {
            "viral_analysis": {"overall_score": 48},
            "publish_metrics": {
                "views": 200,
                "likes": 1,
                "comments": 0,
                "shares": 0,
                "saves": 0,
            },
        }

        report = quality_calibration.build_quality_gate_calibration_report(
            [strong_sample] * 5 + [weak_sample] * 5,
            current_threshold=60,
        )

        self.assertTrue(report.get("has_sufficient_samples", False))
        self.assertEqual(report["minimum_samples_per_bucket"], 5)
        self.assertEqual(report["recommended_threshold"], 65)

    def test_quality_gate_report_requires_minimum_in_each_performance_bucket(self):
        strong_sample = {
            "viral_analysis": {"overall_score": 82},
            "publish_metrics": {"views": 2000, "likes": 120},
        }
        weak_sample = {
            "viral_analysis": {"overall_score": 48},
            "publish_metrics": {"views": 200, "likes": 1},
        }

        report = quality_calibration.build_quality_gate_calibration_report(
            [strong_sample] * 5 + [weak_sample] * 4,
            current_threshold=60,
        )

        self.assertFalse(report["has_sufficient_samples"])
        self.assertIsNone(report["recommended_threshold"])

    def test_quality_gate_report_handles_invalid_current_threshold(self):
        report = quality_calibration.build_quality_gate_calibration_report(
            [],
            current_threshold="not-a-number",
        )

        self.assertIsNone(report["recommended_threshold"])
        self.assertFalse(report.get("has_sufficient_samples", True))
        self.assertIn("collect at least", report["recommendation"].lower())

    def test_quality_gate_report_groups_distinct_subjects_by_performance(self):
        report = quality_calibration.build_quality_gate_calibration_report(
            [
                {
                    "task_id": "strong-one",
                    "subject": "Solar battery breakthrough",
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
                    "task_id": "strong-duplicate",
                    "subject": "  Solar battery breakthrough ",
                    "viral_analysis": {"overall_score": 86},
                    "publish_metrics": {
                        "views": 2200,
                        "likes": 130,
                        "comments": 20,
                        "shares": 10,
                        "saves": 10,
                    },
                },
                {
                    "task_id": "weak-one",
                    "subject": "Generic productivity tip",
                    "viral_analysis": {"overall_score": 45},
                    "publish_metrics": {
                        "views": 200,
                        "likes": 1,
                        "comments": 0,
                        "shares": 0,
                        "saves": 0,
                    },
                },
            ]
        )

        self.assertEqual(report["strong_subjects"], ["Solar battery breakthrough"])
        self.assertEqual(report["weak_subjects"], ["Generic productivity tip"])

    def test_quality_gate_report_skips_invalid_analysis_records(self):
        report = quality_calibration.build_quality_gate_calibration_report(
            [
                {
                    "task_id": "invalid-analysis",
                    "viral_analysis": "not-a-dict",
                    "publish_metrics": {"views": 2000, "likes": 120},
                }
            ]
        )

        self.assertEqual(report["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
