import unittest
from unittest.mock import patch

from app.services import content_quality


class TestContentQuality(unittest.TestCase):
    def test_build_preflight_report_combines_plan_repeat_and_script_analysis(self):
        content_plan = {
            "ideas": [
                {"subject": "Budget hook", "hook": "Save this before payday."}
            ],
            "warnings": ["Planning note"],
            "source": "llm",
        }
        repeat_matches = [
            {
                "task_id": "recent",
                "subject": "Budget mistakes",
                "created_at": "2026-07-04T10:00:00+00:00",
                "similarity": 0.82,
            }
        ]
        script_analysis = {
            "overall_score": 72,
            "hook_score": 80,
            "pacing_score": 67,
            "warnings": ["Add a clearer CTA."],
        }

        with patch.object(
            content_quality.content_intelligence,
            "generate_content_plan",
            return_value=content_plan,
        ) as generate_plan, patch.object(
            content_quality.history,
            "find_recent_similar_subjects",
            return_value=repeat_matches,
        ) as find_subjects, patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
            return_value=script_analysis,
        ) as analyze:
            report = content_quality.build_preflight_report(
                video_subject="Budget mistakes",
                video_script="Do you know why budgets fail? Save this.",
                platform="tiktok",
                language="en",
            )

        generate_plan.assert_called_once()
        self.assertEqual(generate_plan.call_args.kwargs["idea_count"], 3)
        find_subjects.assert_called_once_with(
            "Budget mistakes",
            days=content_quality.DEFAULT_PREFLIGHT_LOOKBACK_DAYS,
        )
        analyze.assert_called_once()
        self.assertEqual(report["content_plan"], content_plan)
        self.assertEqual(report["repeat_matches"], repeat_matches)
        self.assertEqual(report["script_analysis"], script_analysis)
        self.assertEqual(report["fingerprint"]["subject"], "Budget mistakes")

    def test_build_preflight_report_skips_script_analysis_without_script(self):
        with patch.object(
            content_quality.content_intelligence,
            "generate_content_plan",
            return_value={"ideas": [], "warnings": [], "source": "fallback"},
        ), patch.object(
            content_quality.history,
            "find_recent_similar_subjects",
            return_value=[],
        ), patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
        ) as analyze:
            report = content_quality.build_preflight_report(
                video_subject="Budget mistakes",
                video_script="",
                platform="youtube_shorts",
                language="auto",
            )

        analyze.assert_not_called()
        self.assertIsNone(report["script_analysis"])

    def test_preflight_report_staleness_uses_fingerprint(self):
        report = {
            "fingerprint": content_quality.build_preflight_fingerprint(
                "Budget mistakes",
                "Save this.",
                "tiktok",
                "en",
            )
        }

        self.assertFalse(
            content_quality.is_preflight_report_stale(
                report,
                video_subject="Budget mistakes",
                video_script="Save this.",
                platform="tiktok",
                language="en",
            )
        )
        self.assertTrue(
            content_quality.is_preflight_report_stale(
                report,
                video_subject="Budget mistakes updated",
                video_script="Save this.",
                platform="tiktok",
                language="en",
            )
        )

    def test_quality_gate_warns_only_when_enabled_and_below_threshold(self):
        low_score_report = {"script_analysis": {"overall_score": 45}}
        high_score_report = {"script_analysis": {"overall_score": 75}}

        self.assertEqual(
            content_quality.evaluate_quality_gate(
                low_score_report,
                enabled=True,
                threshold=60,
            ),
            {
                "enabled": True,
                "threshold": 60,
                "score": 45,
                "warn": True,
            },
        )
        self.assertFalse(
            content_quality.evaluate_quality_gate(
                high_score_report,
                enabled=True,
                threshold=60,
            )["warn"]
        )
        self.assertFalse(
            content_quality.evaluate_quality_gate(
                low_score_report,
                enabled=False,
                threshold=60,
            )["warn"]
        )

    def test_script_improvement_prompt_includes_analysis_context(self):
        prompt = content_quality.build_script_improvement_prompt(
            video_subject="Coffee prices",
            video_script="Coffee is expensive. Follow for more.",
            viral_analysis={
                "overall_score": 42,
                "hook_score": 30,
                "pacing_score": 55,
                "warnings": ["No clear hook."],
                "hook_suggestions": ["Coffee changed overnight."],
            },
            platform="tiktok",
            language="en",
        )

        self.assertIn("Coffee prices", prompt)
        self.assertIn("Coffee is expensive", prompt)
        self.assertIn("No clear hook", prompt)
        self.assertIn("Coffee changed overnight", prompt)
        self.assertIn("Return ONLY the improved script text", prompt)

    def test_suggest_improved_script_preserves_original_and_returns_suggestion(self):
        original = "Coffee is expensive. Follow for more."
        improved = "Coffee prices did not jump by accident. Here is the simple chain. Save this before your next grocery run."

        with patch.object(
            content_quality.llm,
            "_generate_response",
            return_value=improved,
        ) as generate:
            suggestion = content_quality.suggest_improved_script(
                video_subject="Coffee prices",
                video_script=original,
                viral_analysis={"warnings": ["Weak hook."]},
                platform="tiktok",
                language="en",
            )

        generate.assert_called_once()
        self.assertEqual(suggestion["original_script"], original)
        self.assertEqual(suggestion["improved_script"], improved)
        self.assertEqual(suggestion["source"], "llm")
        self.assertEqual(suggestion["error"], "")

    def test_suggest_improved_script_rejects_empty_or_same_output(self):
        self.assertEqual(
            content_quality.suggest_improved_script(video_script="")["source"],
            "unavailable",
        )

        with patch.object(
            content_quality.llm,
            "_generate_response",
            return_value="Same script.",
        ):
            suggestion = content_quality.suggest_improved_script(
                video_script="Same script.",
                viral_analysis={"warnings": ["Weak hook."]},
            )

        self.assertEqual(suggestion["improved_script"], "")
        self.assertIn("No useful rewrite", suggestion["error"])


if __name__ == "__main__":
    unittest.main()
