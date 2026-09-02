import unittest

from app.services import claim_review


class TestClaimReview(unittest.TestCase):
    def test_flags_numeric_absolute_and_financial_claims_for_human_review(self):
        report = claim_review.review_script_claims(
            "This investment always guarantees a 20% return."
        )

        self.assertEqual(report["status"], "review_recommended")
        self.assertIn("numeric_claim", report["categories"])
        self.assertIn("absolute_claim", report["categories"])
        self.assertIn("financial_guidance", report["categories"])
        self.assertFalse(report["automatic_block"])

    def test_plain_explanatory_script_does_not_create_a_claim_warning(self):
        report = claim_review.review_script_claims(
            "Here is a simple way to organize your monthly budget."
        )

        self.assertEqual(report["status"], "clear")
        self.assertEqual(report["claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
