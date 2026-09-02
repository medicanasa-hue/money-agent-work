import unittest
from unittest.mock import patch

from app.services import scheduled_jobs


class TestScheduledJobs(unittest.TestCase):
    def test_list_scheduled_jobs_normalizes_enabled_and_disabled_jobs(self):
        jobs = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "morning-finance",
                    "enabled": True,
                    "video_subject": "Why prices rise",
                    "video_script": "A private approved script.",
                },
                {
                    "name": "weekend-history",
                    "enabled": False,
                    "video_subject": "A historical event",
                },
            ]
        )

        self.assertEqual([job["name"] for job in jobs], [
            "morning-finance",
            "weekend-history",
        ])
        self.assertTrue(jobs[0]["enabled"])
        self.assertFalse(jobs[1]["enabled"])
        self.assertEqual(jobs[1]["video_script"], "")
        self.assertFalse(jobs[0]["skip_if_recent_duplicate"])

    def test_list_scheduled_jobs_rejects_duplicate_names(self):
        with self.assertRaises(scheduled_jobs.ScheduledJobError):
            scheduled_jobs.list_scheduled_jobs(
                [
                    {"name": "daily", "video_subject": "First"},
                    {"name": "DAILY", "video_subject": "Second"},
                ]
            )

    def test_subject_pool_allows_pool_only_jobs_and_hides_candidates_in_summary(self):
        jobs = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject_pool": [
                        "Why grocery prices rise",
                        "why grocery prices rise",
                        "How interest rates affect rent",
                    ],
                    "video_script": "",
                }
            ]
        )

        self.assertEqual(jobs[0]["video_subject"], "")
        self.assertEqual(
            jobs[0]["video_subject_pool"],
            [
                "Why grocery prices rise",
                "How interest rates affect rent",
            ],
        )
        summary = scheduled_jobs.scheduled_job_summary(jobs[0])
        self.assertEqual(summary["subject_pool_size"], 2)
        self.assertNotIn("video_subject_pool", summary)

    def test_subject_pool_rejects_a_fixed_script(self):
        with self.assertRaises(scheduled_jobs.ScheduledJobError):
            scheduled_jobs.list_scheduled_jobs(
                [
                    {
                        "name": "weekday-finance",
                        "video_subject_pool": ["Why grocery prices rise"],
                        "video_script": "A fixed script for another topic.",
                    }
                ]
            )

    def test_rss_trend_query_allows_rss_only_jobs_and_hides_query_in_summary(self):
        jobs = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-news",
                    "rss_trend_query": "personal finance",
                    "video_script": "",
                }
            ]
        )

        self.assertEqual(jobs[0]["video_subject"], "")
        self.assertEqual(jobs[0]["rss_trend_query"], "personal finance")
        summary = scheduled_jobs.scheduled_job_summary(jobs[0])
        self.assertTrue(summary["has_rss_trend_query"])
        self.assertNotIn("rss_trend_query", summary)

    def test_rss_trend_query_keeps_its_optional_language(self):
        jobs = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-turkish-news",
                    "rss_trend_query": "ekonomi",
                    "rss_trend_language": "tr-TR",
                }
            ]
        )

        self.assertEqual(jobs[0]["rss_trend_language"], "tr-TR")

    def test_rss_trend_query_rejects_a_fixed_script(self):
        with self.assertRaises(scheduled_jobs.ScheduledJobError):
            scheduled_jobs.list_scheduled_jobs(
                [
                    {
                        "name": "weekday-news",
                        "rss_trend_query": "personal finance",
                        "video_script": "A fixed script for another topic.",
                    }
                ]
            )

    def test_get_scheduled_job_returns_enabled_named_job(self):
        app_config = {
            "scheduled_jobs": [
                {
                    "name": "morning-finance",
                    "enabled": True,
                    "video_subject": "Why prices rise",
                    "video_script": "Prices can rise for more than one reason.",
                }
            ]
        }

        with patch.object(scheduled_jobs.config, "app", app_config):
            job = scheduled_jobs.get_scheduled_job("Morning-Finance")

        self.assertEqual(job["name"], "morning-finance")
        self.assertEqual(job["video_subject"], "Why prices rise")
        self.assertTrue(job["enabled"])

    def test_static_job_can_opt_in_to_skip_recent_duplicates(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "skip_if_recent_duplicate": True,
                }
            ]
        )[0]

        self.assertTrue(job["skip_if_recent_duplicate"])
        self.assertTrue(
            scheduled_jobs.scheduled_job_summary(job)["skip_if_recent_duplicate"]
        )

    def test_scheduled_job_normalizes_a_transition_mode(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "video_transition_mode": "shuffle",
                }
            ]
        )[0]

        self.assertEqual(job["video_transition_mode"], "Shuffle")
        self.assertEqual(
            scheduled_jobs.scheduled_job_summary(job)["video_transition_mode"],
            "Shuffle",
        )

    def test_scheduled_job_preserves_an_optional_voice_name(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "voice_name": "no-voice",
                }
            ]
        )[0]

        self.assertEqual(job["voice_name"], "no-voice")

    def test_scheduled_job_preserves_an_optional_script_prompt(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "video_script_prompt": "Open with a concrete viewer benefit.",
                }
            ]
        )[0]

        self.assertEqual(
            job["video_script_prompt"], "Open with a concrete viewer benefit."
        )

    def test_scheduled_job_preserves_scene_matching_options(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "match_materials_to_script": True,
                    "smart_scene_queries": True,
                }
            ]
        )[0]

        self.assertTrue(job["match_materials_to_script"])
        self.assertTrue(job["smart_scene_queries"])
        summary = scheduled_jobs.scheduled_job_summary(job)
        self.assertTrue(summary["match_materials_to_script"])
        self.assertTrue(summary["smart_scene_queries"])

    def test_scheduled_job_can_opt_in_to_openmontage_materials(self):
        job = scheduled_jobs.list_scheduled_jobs(
            [
                {
                    "name": "weekday-finance",
                    "video_subject": "Why prices rise",
                    "openmontage_auto_materials": True,
                }
            ]
        )[0]

        self.assertTrue(job["openmontage_auto_materials"])
        self.assertTrue(
            scheduled_jobs.scheduled_job_summary(job)["openmontage_auto_materials"]
        )

    def test_scheduled_job_rejects_an_invalid_transition_mode(self):
        with self.assertRaises(scheduled_jobs.ScheduledJobError):
            scheduled_jobs.list_scheduled_jobs(
                [
                    {
                        "name": "weekday-finance",
                        "video_subject": "Why prices rise",
                        "video_transition_mode": "DiagonalSpin",
                    }
                ]
            )

    def test_get_scheduled_job_rejects_disabled_or_invalid_jobs(self):
        app_config = {
            "scheduled_jobs": [
                {
                    "name": "disabled-job",
                    "enabled": False,
                    "video_subject": "Do not run",
                },
                {
                    "name": "missing-subject",
                    "enabled": True,
                    "video_subject": "",
                },
            ]
        }

        with patch.object(scheduled_jobs.config, "app", app_config):
            with self.assertRaises(scheduled_jobs.ScheduledJobError):
                scheduled_jobs.get_scheduled_job("disabled-job")
            with self.assertRaises(scheduled_jobs.ScheduledJobError):
                scheduled_jobs.get_scheduled_job("missing-subject")


if __name__ == "__main__":
    unittest.main()
