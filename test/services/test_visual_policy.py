import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import visual_policy


class TestVisualPolicy(unittest.TestCase):
    def test_finance_topic_gets_economic_explainer_policy(self):
        policy = visual_policy.recommend_visual_policy(
            "Türkiye'de enflasyon ve market fiyatları neden arttı?"
        )

        self.assertEqual(policy["profile"], "economic_explainer")
        self.assertTrue(policy["recommended_params"]["match_materials_to_script"])
        self.assertTrue(policy["recommended_params"]["smart_scene_queries"])

    def test_unmatched_topic_gets_conservative_general_policy(self):
        policy = visual_policy.recommend_visual_policy("Kısa bir genel hikaye")

        self.assertEqual(policy["profile"], "general_explainer")
        self.assertFalse(policy["recommended_params"]["smart_scene_queries"])
