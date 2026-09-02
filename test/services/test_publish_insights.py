import unittest

from app.services import publish_insights


class TestPublishInsights(unittest.TestCase):
    def test_build_publish_insights_identifies_quality_signal_without_actions(self):
        report = publish_insights.build_publish_performance_insights(
            [
                {
                    "publish_metrics": {
                        "views": 1000,
                        "likes": 40,
                        "comments": 5,
                        "shares": 3,
                        "saves": 2,
                    },
                    "viral_analysis": {"overall_score": 30},
                },
                {
                    "publish_metrics": {
                        "views": 800,
                        "likes": 25,
                        "comments": 4,
                        "shares": 2,
                        "saves": 1,
                    },
                    "viral_analysis": {"overall_score": 45},
                },
                {
                    "publish_metrics": {
                        "views": 1200,
                        "likes": 130,
                        "comments": 25,
                        "shares": 15,
                        "saves": 10,
                    },
                    "viral_analysis": {"overall_score": 80},
                },
                {
                    "publish_metrics": {
                        "views": 1500,
                        "likes": 160,
                        "comments": 35,
                        "shares": 25,
                        "saves": 20,
                    },
                    "viral_analysis": {"overall_score": 90},
                },
            ]
        )

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["sample_size"], 4)
        self.assertFalse(report["automatic_actions"])
        self.assertIn(
            "quality_gate_alignment",
            [suggestion["type"] for suggestion in report["suggestions"]],
        )

    def test_build_publish_insights_requests_metrics_when_sample_is_small(self):
        report = publish_insights.build_publish_performance_insights(
            [{"publish_metrics": {"views": 12}}]
        )

        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["sample_size"], 1)
        self.assertFalse(report["automatic_actions"])
        self.assertEqual(report["suggestions"][0]["type"], "collect_metrics")

    def test_publish_insights_segments_real_metrics_by_language_aspect_and_duration(self):
        entries = [
            {
                "language": "tr",
                "video_aspect": "9:16",
                "audio_duration": 24,
                "publish_metrics": {"views": 1000, "likes": 80},
            }
            for _ in range(3)
        ] + [
            {
                "language": "en",
                "video_aspect": "16:9",
                "audio_duration": 75,
                "publish_metrics": {"views": 400, "likes": 12},
            }
            for _ in range(3)
        ]

        report = publish_insights.build_publish_performance_insights(entries)

        self.assertEqual(report["segments"]["language"]["tr"]["sample_count"], 3)
        self.assertEqual(report["segments"]["language"]["tr"]["status"], "ready")
        self.assertEqual(
            report["segments"]["video_aspect"]["9:16"]["median_views"],
            1000,
        )
        self.assertEqual(
            report["segments"]["duration"]["long_over_60_seconds"]["sample_count"],
            3,
        )

    def test_publish_insights_segments_platform_and_production_context(self):
        entries = [
            {
                "language": "tr",
                "video_aspect": "9:16",
                "llm_provider": "gemini",
                "voice_name": "tr-TR-AhmetNeural",
                "video_source": "multi",
                "video_transition_mode": "crossfade",
                "publish_metrics": {
                    "views": 1000,
                    "likes": 50,
                    "platform_metrics": {
                        "youtube": {"views": 700, "likes": 42},
                        "tiktok": {"views": 300, "likes": 8},
                    },
                },
            }
            for _ in range(3)
        ]

        report = publish_insights.build_publish_performance_insights(entries)

        self.assertEqual(report["segments"]["platform"]["youtube"]["median_views"], 700)
        self.assertEqual(report["segments"]["platform"]["tiktok"]["sample_count"], 3)
        self.assertEqual(report["segments"]["llm_provider"]["gemini"]["status"], "ready")
        self.assertEqual(
            report["segments"]["voice"]["tr-tr-ahmetneural"]["sample_count"],
            3,
        )
        self.assertEqual(report["segments"]["video_source"]["multi"]["sample_count"], 3)
        self.assertEqual(
            report["segments"]["video_transition"]["crossfade"]["sample_count"],
            3,
        )

    def test_rank_subject_candidates_prefers_topics_with_stronger_history(self):
        entries = [
            {
                "subject": "Grocery prices explained",
                "publish_metrics": {"views": 300, "likes": 3},
            },
            {
                "subject": "Grocery prices explained",
                "publish_metrics": {"views": 280, "likes": 2},
            },
            {
                "subject": "Interest rates and rent",
                "publish_metrics": {"views": 1500, "likes": 180},
            },
            {
                "subject": "Interest rates and rent",
                "publish_metrics": {"views": 1300, "likes": 130},
            },
        ]

        ranked = publish_insights.rank_subject_candidates(
            ["Grocery prices explained", "Interest rates and rent"],
            entries,
        )

        self.assertEqual(ranked[0]["subject"], "Interest rates and rent")
        self.assertEqual(ranked[0]["ranking_status"], "performance_evidence")
        self.assertEqual(ranked[0]["evidence_sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
