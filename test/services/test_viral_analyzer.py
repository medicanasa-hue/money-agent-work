import unittest
from unittest.mock import patch

from app.services import viral_analyzer


class TestViralAnalyzer(unittest.TestCase):
    def test_analyze_viral_potential_uses_llm_response(self):
        payload = """
        Sure:
        {
          "overall_score": 92,
          "hook_score": 88,
          "pacing_score": 81,
          "retention_curve": "strong",
          "emotional_arc": "crescendo",
          "summary": "Strong opening and clear payoff.",
          "hook_suggestions": ["Open with the surprising number."],
          "title_variants": ["The coffee mistake everyone makes"],
          "thumbnail_concepts": ["Cup close-up with bold mistake text"],
          "warnings": [],
          "platform_fit": {"tiktok": 0.9, "youtube_shorts": 82}
        }
        """

        with patch.object(viral_analyzer.llm, "_generate_response", return_value=payload):
            result = viral_analyzer.analyze_viral_potential(
                video_subject="Coffee mistakes",
                video_script="Most people ruin coffee before the first sip.",
                title="Coffee mistakes",
                target_platforms=["tiktok", "youtube_shorts"],
            )

        self.assertEqual(result["overall_score"], 92)
        self.assertEqual(result["hook_score"], 88)
        self.assertEqual(result["retention_curve"], "strong")
        self.assertEqual(result["platform_fit"]["tiktok"], 0.9)
        self.assertEqual(result["platform_fit"]["youtube_shorts"], 0.82)
        self.assertEqual(
            result["title_variants"], ["The coffee mistake everyone makes"]
        )

    def test_analyze_viral_potential_falls_back_when_llm_unavailable(self):
        with patch.object(
            viral_analyzer.llm,
            "_generate_response",
            return_value="Error: api_key is not set",
        ):
            result = viral_analyzer.analyze_viral_potential(
                video_subject="Budget planning",
                video_script=(
                    "Did you know one tiny budget habit can save 20 percent? "
                    "Watch this and save it for later."
                ),
                target_platforms=["instagram_reels"],
            )

        self.assertGreaterEqual(result["overall_score"], 0)
        self.assertLessEqual(result["overall_score"], 100)
        self.assertTrue(result["hook_suggestions"])
        self.assertTrue(result["title_variants"])
        self.assertIn("instagram_reels", result["platform_fit"])

    def test_fallback_accepts_turkish_diacritic_cta(self):
        with patch.object(
            viral_analyzer.llm,
            "_generate_response",
            return_value="Error: api_key is not set",
        ):
            result = viral_analyzer.analyze_viral_potential(
                video_subject="Birikim hatalari",
                video_script=(
                    "Parani neden ay sonunda kaybettigini biliyor musun? "
                    "Bu tek aliskanlik fark yaratir. Paylaş."
                ),
            )

        self.assertNotIn(
            "No clear call to action was detected.",
            result["warnings"],
        )

    def test_build_prompt_clamps_long_inputs(self):
        prompt = viral_analyzer.build_viral_analysis_prompt(
            video_subject="x" * 600,
            video_script="y" * 9000,
            title="z" * 300,
            language="en-US",
        )

        self.assertIn("x" * viral_analyzer.MAX_ANALYSIS_SUBJECT_LENGTH, prompt)
        self.assertNotIn(
            "x" * (viral_analyzer.MAX_ANALYSIS_SUBJECT_LENGTH + 1), prompt
        )
        self.assertIn("y" * viral_analyzer.MAX_ANALYSIS_SCRIPT_LENGTH, prompt)
        self.assertNotIn(
            "y" * (viral_analyzer.MAX_ANALYSIS_SCRIPT_LENGTH + 1), prompt
        )
        self.assertIn("z" * viral_analyzer.MAX_ANALYSIS_TITLE_LENGTH, prompt)

    def test_build_prompt_includes_social_and_material_context(self):
        prompt = viral_analyzer.build_viral_analysis_prompt(
            video_subject="Budget planning",
            video_script="Save this checklist before payday.",
            title="Budget checklist",
            social_caption="Caption with a clear save CTA.",
            hashtags=["#budget", "#money"],
            material_attributions=[
                {
                    "provider": "wikimedia",
                    "title": "Budget image",
                    "attribution": "Example Creator",
                    "license": "CC BY-SA",
                }
            ],
        )

        self.assertIn("Social caption: Caption with a clear save CTA.", prompt)
        self.assertIn("Hashtags: #budget #money", prompt)
        self.assertIn("Budget image", prompt)
        self.assertIn("CC BY-SA", prompt)


if __name__ == "__main__":
    unittest.main()
