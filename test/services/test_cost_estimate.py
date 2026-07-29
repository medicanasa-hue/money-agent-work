import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import cost_estimate


class TestCostEstimate(unittest.TestCase):
    def test_estimate_uses_configured_character_rates(self):
        entry = {
            "script": "abcd",
            "llm_provider": "openai",
            "voice_name": "elevenlabs:voice-id:Rachel",
        }

        with patch.object(
            cost_estimate.config,
            "app",
            {
                "cost_estimate_llm_usd_per_million_characters": 1_000_000,
                "cost_estimate_tts_usd_per_million_characters": 500_000,
            },
        ):
            estimate = cost_estimate.estimate_history_cost(entry)

        self.assertEqual(estimate["llm"]["provider"], "openai")
        self.assertEqual(estimate["llm"]["characters"], 4)
        self.assertEqual(estimate["llm"]["estimated_usd"], 4.0)
        self.assertEqual(estimate["tts"]["provider"], "elevenlabs")
        self.assertEqual(estimate["tts"]["characters"], 4)
        self.assertEqual(estimate["tts"]["estimated_usd"], 2.0)
        self.assertEqual(estimate["estimated_known_total_usd"], 6.0)
        self.assertEqual(estimate["unknown_components"], [])

    def test_estimate_keeps_missing_rates_unknown_instead_of_zero(self):
        entry = {
            "script": "abcd",
            "llm_provider": "openai",
            "voice_name": "elevenlabs:voice-id:Rachel",
        }

        with patch.object(cost_estimate.config, "app", {}):
            estimate = cost_estimate.estimate_history_cost(entry)

        self.assertIsNone(estimate["llm"]["estimated_usd"])
        self.assertIsNone(estimate["tts"]["estimated_usd"])
        self.assertEqual(estimate["estimated_known_total_usd"], 0.0)
        self.assertEqual(estimate["unknown_components"], ["llm", "tts"])

    def test_estimate_rejects_non_finite_rates(self):
        entry = {
            "script": "abcd",
            "llm_provider": "openai",
            "voice_name": "elevenlabs:voice-id:Rachel",
        }

        with patch.object(
            cost_estimate.config,
            "app",
            {
                "cost_estimate_llm_usd_per_million_characters": float("inf"),
                "cost_estimate_tts_usd_per_million_characters": float("nan"),
            },
        ):
            estimate = cost_estimate.estimate_history_cost(entry)

        self.assertIsNone(estimate["llm"]["rate_usd_per_million_characters"])
        self.assertIsNone(estimate["tts"]["rate_usd_per_million_characters"])
        self.assertEqual(estimate["unknown_components"], ["llm", "tts"])

    def test_custom_audio_has_no_tts_usage_to_estimate(self):
        entry = {
            "script": "abcd",
            "custom_audio_file": "C:/audio/narration.mp3",
            "voice_name": "elevenlabs:voice-id:Rachel",
        }

        with patch.object(cost_estimate.config, "app", {}):
            estimate = cost_estimate.estimate_history_cost(entry)

        self.assertEqual(estimate["tts"]["provider"], "custom_audio")
        self.assertEqual(estimate["tts"]["characters"], 0)
        self.assertEqual(estimate["tts"]["estimated_usd"], 0.0)
        self.assertNotIn("tts", estimate["unknown_components"])

    def test_monthly_summary_keeps_unknown_costs_out_of_known_total(self):
        summary = cost_estimate.summarize_monthly_history_costs(
            [
                {
                    "created_at": "2026-07-01T10:00:00+00:00",
                    "cost_estimate": {
                        "estimated_known_total_usd": 0.4,
                        "unknown_components": [],
                    },
                },
                {
                    "created_at": "2026-07-10T10:00:00+00:00",
                    "cost_estimate": {
                        "estimated_known_total_usd": 0.2,
                        "unknown_components": ["tts"],
                    },
                },
                {
                    "created_at": "2026-07-11T10:00:00+00:00",
                },
                {
                    "created_at": "2026-06-30T10:00:00+00:00",
                    "cost_estimate": {
                        "estimated_known_total_usd": 9.99,
                        "unknown_components": [],
                    },
                },
            ],
            now="2026-07-12T10:00:00+00:00",
        )

        self.assertEqual(summary["job_count"], 3)
        self.assertEqual(summary["estimated_job_count"], 2)
        self.assertEqual(summary["unknown_job_count"], 2)
        self.assertEqual(summary["known_total_usd"], 0.6)

    def test_monthly_warning_is_opt_in_and_uses_known_total_only(self):
        entries = [
            {
                "created_at": "2026-07-01T10:00:00+00:00",
                "cost_estimate": {
                    "estimated_known_total_usd": 0.4,
                    "unknown_components": [],
                },
            },
            {
                "created_at": "2026-07-10T10:00:00+00:00",
                "cost_estimate": {
                    "estimated_known_total_usd": 0.2,
                    "unknown_components": ["tts"],
                },
            },
        ]

        warning = cost_estimate.evaluate_monthly_cost_warning(
            entries,
            threshold_usd=0.5,
            now="2026-07-12T10:00:00+00:00",
        )
        disabled_warning = cost_estimate.evaluate_monthly_cost_warning(
            entries,
            threshold_usd=0,
            now="2026-07-12T10:00:00+00:00",
        )

        self.assertTrue(warning["enabled"])
        self.assertTrue(warning["warning"])
        self.assertEqual(warning["known_total_usd"], 0.6)
        self.assertEqual(warning["unknown_job_count"], 1)
        self.assertFalse(disabled_warning["enabled"])
        self.assertFalse(disabled_warning["warning"])

    def test_monthly_cost_cap_is_opt_in_and_includes_projected_known_cost(self):
        entries = [
            {
                "created_at": "2026-07-01T10:00:00+00:00",
                "cost_estimate": {
                    "estimated_known_total_usd": 0.4,
                    "unknown_components": [],
                },
            }
        ]

        cap = cost_estimate.evaluate_monthly_cost_cap(
            entries,
            cap_usd=0.5,
            projected_cost_estimate={
                "estimated_known_total_usd": 0.2,
                "unknown_components": ["tts"],
            },
            now="2026-07-12T10:00:00+00:00",
        )
        disabled_cap = cost_estimate.evaluate_monthly_cost_cap(
            entries,
            cap_usd=0,
            projected_cost_estimate={"estimated_known_total_usd": 9.0},
            now="2026-07-12T10:00:00+00:00",
        )

        self.assertTrue(cap["enabled"])
        self.assertFalse(cap["allowed"])
        self.assertEqual(cap["known_total_usd"], 0.4)
        self.assertEqual(cap["projected_known_cost_usd"], 0.2)
        self.assertEqual(cap["projected_known_total_usd"], 0.6)
        self.assertEqual(cap["projected_unknown_components"], ["tts"])
        self.assertFalse(disabled_cap["enabled"])
        self.assertTrue(disabled_cap["allowed"])


if __name__ == "__main__":
    unittest.main()
