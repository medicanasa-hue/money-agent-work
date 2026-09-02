import json
import tempfile
import unittest
from pathlib import Path
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
        script_repeat_matches = [
            {
                "task_id": "recent-script",
                "subject": "Why interest rates rise",
                "created_at": "2026-07-04T10:00:00+00:00",
                "similarity": 0.91,
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
            content_quality.history,
            "find_recent_similar_scripts",
            return_value=script_repeat_matches,
        ) as find_scripts, patch.object(
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
        find_scripts.assert_called_once_with(
            "Do you know why budgets fail? Save this.",
            days=content_quality.DEFAULT_PREFLIGHT_LOOKBACK_DAYS,
        )
        analyze.assert_called_once()
        self.assertEqual(report["content_plan"], content_plan)
        self.assertEqual(report["repeat_matches"], repeat_matches)
        self.assertEqual(report["script_repeat_matches"], script_repeat_matches)
        self.assertEqual(report["script_analysis"], script_analysis)
        self.assertEqual(report["claim_review"]["status"], "clear")
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
        original_analysis = {
            "overall_score": 42,
            "hook_score": 45,
            "pacing_score": 55,
            "warnings": ["Weak hook."],
        }
        improved_analysis = {
            "overall_score": 82,
            "hook_score": 78,
            "pacing_score": 74,
            "warnings": [],
        }

        with patch.object(
            content_quality.llm,
            "_generate_response",
            return_value=improved,
        ) as generate, patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
            return_value=improved_analysis,
        ) as analyze:
            suggestion = content_quality.suggest_improved_script(
                video_subject="Coffee prices",
                video_script=original,
                viral_analysis=original_analysis,
                platform="tiktok",
                language="en",
                title="Coffee shock",
                video_duration_sec=35,
                social_caption="Save this before shopping.",
                hashtags=["#coffee"],
                material_attributions=[{"title": "Coffee beans", "license": "CC-BY"}],
            )

        generate.assert_called_once()
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.kwargs["video_script"], improved)
        self.assertEqual(analyze.call_args.kwargs["target_platforms"], ["tiktok"])
        self.assertEqual(analyze.call_args.kwargs["title"], "Coffee shock")
        self.assertEqual(analyze.call_args.kwargs["video_duration_sec"], 35)
        self.assertEqual(
            analyze.call_args.kwargs["social_caption"],
            "Save this before shopping.",
        )
        self.assertEqual(analyze.call_args.kwargs["hashtags"], ["#coffee"])
        self.assertEqual(
            analyze.call_args.kwargs["material_attributions"],
            [{"title": "Coffee beans", "license": "CC-BY"}],
        )
        self.assertEqual(suggestion["original_script"], original)
        self.assertEqual(suggestion["improved_script"], improved)
        self.assertEqual(suggestion["original_analysis"], original_analysis)
        self.assertEqual(suggestion["improved_analysis"], improved_analysis)
        self.assertEqual(
            suggestion["score_comparison"]["hook_score"],
            {"before": 45, "after": 78, "delta": 33},
        )
        self.assertEqual(suggestion["source"], "llm")
        self.assertEqual(suggestion["error"], "")

    def test_suggest_improved_script_handles_missing_original_analysis(self):
        improved = "Coffee changed overnight. Here is what it means. Follow for more."
        improved_analysis = {
            "overall_score": 80,
            "hook_score": 78,
            "pacing_score": 70,
        }

        with patch.object(
            content_quality.llm,
            "_generate_response",
            return_value=improved,
        ), patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
            return_value=improved_analysis,
        ):
            suggestion = content_quality.suggest_improved_script(
                video_subject="Coffee prices",
                video_script="Coffee is expensive. Follow for more.",
                viral_analysis=None,
                platform="tiktok",
                language="en",
            )

        self.assertIsNone(suggestion["score_comparison"]["hook_score"]["before"])
        self.assertEqual(suggestion["score_comparison"]["hook_score"]["after"], 78)
        self.assertIsNone(suggestion["score_comparison"]["hook_score"]["delta"])

    def test_suggest_improved_script_rejects_empty_or_same_output(self):
        with patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
        ) as analyze:
            self.assertEqual(
                content_quality.suggest_improved_script(video_script="")["source"],
                "unavailable",
            )
            analyze.assert_not_called()

        with patch.object(
            content_quality.llm,
            "_generate_response",
            return_value="Same script.",
        ), patch.object(
            content_quality.viral_analyzer,
            "analyze_viral_potential",
        ) as analyze:
            suggestion = content_quality.suggest_improved_script(
                video_script="Same script.",
                viral_analysis={"warnings": ["Weak hook."]},
            )

        self.assertEqual(suggestion["improved_script"], "")
        self.assertIn("No useful rewrite", suggestion["error"])
        analyze.assert_not_called()

    def test_suggest_improved_script_logs_rejected_outputs_without_script_text(self):
        sensitive_script = "Sensitive script text that should not be logged."
        viral_analysis = {
            "overall_score": 40,
            "hook_score": 35,
            "pacing_score": 55,
            "warnings": ["Weak hook.", "Missing CTA."],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                content_quality.utils,
                "storage_dir",
                return_value=temp_dir,
            ), patch.object(
                content_quality.llm,
                "_generate_response",
                side_effect=[
                    "",
                    sensitive_script,
                    ValueError("provider unavailable"),
                ],
            ), patch.object(
                content_quality.viral_analyzer,
                "analyze_viral_potential",
            ) as analyze:
                content_quality.suggest_improved_script(
                    video_subject="Coffee prices",
                    video_script=sensitive_script,
                    viral_analysis=viral_analysis,
                    platform="tiktok",
                    language="en",
                )
                content_quality.suggest_improved_script(
                    video_subject="Coffee prices",
                    video_script=sensitive_script,
                    viral_analysis=viral_analysis,
                    platform="tiktok",
                    language="en",
                )
                content_quality.suggest_improved_script(
                    video_subject="Coffee prices",
                    video_script=sensitive_script,
                    viral_analysis=viral_analysis,
                    platform="tiktok",
                    language="en",
                )

            log_path = Path(temp_dir) / content_quality.SCRIPT_REWRITE_REJECTION_LOG
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        analyze.assert_not_called()
        self.assertEqual(
            [event["reason"] for event in events],
            ["empty_output", "same_output", "llm_error"],
        )
        self.assertEqual(events[0]["video_subject"], "Coffee prices")
        self.assertEqual(events[0]["platform"], "tiktok")
        self.assertEqual(events[0]["language"], "en")
        self.assertEqual(events[0]["scores"]["hook_score"], 35)
        self.assertEqual(events[0]["warnings"], ["Weak hook.", "Missing CTA."])
        self.assertNotIn(sensitive_script, json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
