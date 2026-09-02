import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import review_feedback


class TestReviewFeedback(unittest.TestCase):
    def test_normalize_rejected_review_requires_a_known_reason(self):
        result = review_feedback.normalize_review_decision(
            "rejected",
            rejection_reason="unrelated_material",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["rejection_reason"], "unrelated_material")

    def test_normalize_approved_review_clears_rejection_reason(self):
        result = review_feedback.normalize_review_decision(
            "approved",
            rejection_reason="unrelated_material",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "approved")
        self.assertIsNone(result["rejection_reason"])

    def test_normalize_rejected_review_rejects_missing_or_unknown_reason(self):
        missing_reason = review_feedback.normalize_review_decision("rejected")
        unknown_reason = review_feedback.normalize_review_decision(
            "rejected",
            rejection_reason="looks_bad",
        )

        self.assertEqual(missing_reason["error"], "rejection_reason_required")
        self.assertEqual(unknown_reason["error"], "invalid_rejection_reason")

    def test_record_review_decision_persists_a_valid_local_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            review_feedback.utils, "storage_dir", return_value=temp_dir
        ):
            recorded = review_feedback.record_review_decision(
                "task-123",
                "rejected",
                rejection_reason="repeated_visual",
                recorded_at="2026-07-14T09:00:00+00:00",
            )
            entries = review_feedback.list_review_decisions()

        self.assertTrue(recorded["ok"])
        self.assertEqual(entries, [recorded["record"]])
        self.assertEqual(entries[0]["task_id"], "task-123")
        self.assertEqual(entries[0]["rejection_reason"], "repeated_visual")

    def test_provider_feedback_adjustment_requires_enough_source_specific_reviews(self):
        entries = [
            {
                "task_id": f"task-{index}",
                "decision": "rejected",
                "rejection_reason": "unrelated_material",
                "material_provider": "pexels",
            }
            for index in range(4)
        ]
        entries.append(
            {
                "task_id": "task-approved",
                "decision": "approved",
                "rejection_reason": None,
                "material_provider": "pexels",
            }
        )

        adjustments = review_feedback.build_provider_feedback_adjustments(entries)

        self.assertEqual(adjustments["pexels"]["sample_count"], 5)
        self.assertEqual(adjustments["pexels"]["status"], "active")
        self.assertLess(adjustments["pexels"]["score_adjustment"], 0)


if __name__ == "__main__":
    unittest.main()
