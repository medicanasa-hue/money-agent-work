import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import history


class TestProductionHistory(unittest.TestCase):
    def test_add_list_and_clear_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-1",
                        "subject": "first",
                        "videos": ["/tmp/first.mp4"],
                    }
                )
                history.add_history(
                    {
                        "task_id": "task-2",
                        "subject": "second",
                        "status": "failed",
                        "error": "boom",
                    }
                )

                entries = history.list_history()

                self.assertEqual([entry["task_id"] for entry in entries], ["task-2", "task-1"])
                self.assertEqual(entries[0]["status"], "failed")
                self.assertEqual(entries[1]["videos"], ["/tmp/first.mp4"])
                self.assertIsNone(entries[1]["cooldown"])
                self.assertIsNone(entries[1]["viral_analysis"])
                self.assertEqual(entries[1]["language"], "")
                self.assertIn("created_at", entries[0])

                history.clear_history()

                self.assertEqual(history.list_history(), [])

    def test_backfill_render_quality_reports_previews_and_persists_missing_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            final_path = Path(temp_dir) / "final.mp4"
            existing_path = Path(temp_dir) / "existing.mp4"
            final_path.touch()
            existing_path.touch()
            existing_report = {"video_path": str(existing_path), "ok": True}
            inspected_report = {"ok": False, "warnings": ["sampled frames are near-black"]}
            inspect_video = Mock(return_value=inspected_report)

            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "task-render-quality",
                            "videos": [str(final_path), str(existing_path)],
                            "render_quality_reports": [existing_report],
                        }
                    ]
                )

                preview = history.backfill_render_quality_reports(inspect_video)
                after_preview = history.list_history()[0]

                persisted = history.backfill_render_quality_reports(
                    inspect_video,
                    persist=True,
                )
                after_persist = history.list_history()[0]

        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["jobs_with_new_reports"], 1)
        self.assertEqual(preview["inspected_videos"], 1)
        self.assertEqual(preview["skipped_videos"], 1)
        self.assertEqual(after_preview["render_quality_reports"], [existing_report])
        self.assertFalse(persisted["dry_run"])
        self.assertEqual(persisted["updated_jobs"], 1)
        self.assertEqual(after_persist["render_quality_reports"], [
            existing_report,
            {"video_path": str(final_path), **inspected_report},
        ])

    def test_list_history_returns_empty_for_invalid_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / history.HISTORY_FILENAME
            path.write_text("{bad-json", encoding="utf-8")

            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                self.assertEqual(history.list_history(), [])

    def test_save_history_replaces_file_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / history.HISTORY_FILENAME
            temp_path = Path(f"{history_path}.tmp")

            with patch("app.services.history.utils.storage_dir", return_value=temp_dir), patch(
                "app.services.history.os.replace",
                wraps=os.replace,
            ) as replace:
                saved_path = history.save_history(
                    [
                        {
                            "task_id": "task-atomic",
                            "subject": "atomic",
                        }
                    ]
                )

                entries = history.list_history()

        self.assertEqual(saved_path, str(history_path))
        replace.assert_called_once_with(str(temp_path), str(history_path))
        self.assertFalse(temp_path.exists())
        self.assertEqual(entries[0]["task_id"], "task-atomic")

    def test_history_preserves_cooldown_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-cooldown",
                        "subject": "cooldown",
                        "cooldown": {
                            "moved_recent_count": 2,
                            "days": 7,
                        },
                    }
                )

                entries = history.list_history()

        self.assertEqual(
            entries[0]["cooldown"],
            {
                "moved_recent_count": 2,
                "days": 7,
            },
        )

    def test_history_preserves_pending_uploads(self):
        pending_uploads = [
            {
                "video_path": "/tmp/final.mp4",
                "title": "Ready to upload",
                "platforms": ["youtube"],
                "status": "pending",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": pending_uploads,
                    }
                )

                entries = history.list_history()

        self.assertEqual(entries[0]["pending_uploads"], pending_uploads)

    def test_history_preserves_viral_analysis(self):
        viral_analysis = {
            "overall_score": 74,
            "warnings": ["Add a sharper CTA."],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-viral",
                        "subject": "viral",
                        "viral_analysis": viral_analysis,
                    }
                )

                entries = history.list_history()

        self.assertEqual(entries[0]["viral_analysis"], viral_analysis)

    def test_history_preserves_script_for_reuse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-script",
                        "subject": "Voyager signal",
                        "script": "A short reusable script.",
                    }
                )
                history.add_history(
                    {
                        "task_id": "task-no-script",
                        "subject": "Legacy record",
                    }
                )

                entries = history.list_history()

        self.assertEqual(entries[1]["script"], "A short reusable script.")
        self.assertEqual(entries[0]["script"], "")

    def test_new_history_entry_persists_cost_estimate_snapshot(self):
        expected_cost = {
            "version": 1,
            "estimated_known_total_usd": 0.42,
            "unknown_components": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir), patch(
                "app.services.history.cost_estimate.estimate_history_cost",
                return_value=expected_cost,
            ) as estimate_history_cost:
                history.add_history(
                    {
                        "task_id": "task-cost",
                        "subject": "cost estimate",
                        "script": "A short script.",
                        "voice_name": "elevenlabs:voice-id:Rachel",
                    }
                )
                entries = history.list_history()

        estimate_history_cost.assert_called_once()
        self.assertEqual(entries[0]["cost_estimate"], expected_cost)

    def test_history_preserves_thumbnail_candidates(self):
        thumbnail_candidates = [
            {
                "path": "/tmp/task/thumbnails/thumbnail-1.jpg",
                "timestamp_sec": 1.0,
                "concept": "Close-up with bold keyword",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-thumb",
                        "subject": "thumbnail",
                        "thumbnail_candidates": thumbnail_candidates,
                        "thumbnail_candidate_error": "",
                    }
                )

                entries = history.list_history()

        self.assertEqual(entries[0]["thumbnail_candidates"], thumbnail_candidates)
        self.assertEqual(entries[0]["thumbnail_candidate_error"], "")

    def test_update_publish_metrics_normalizes_manual_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-metrics",
                        "subject": "metrics",
                    }
                )

                updated = history.update_publish_metrics(
                    "task-metrics",
                    {
                        "views": "1200",
                        "likes": "90",
                        "comments": "10",
                        "shares": "5",
                        "saves": "7",
                        "captured_at": "2026-07-07T10:00:00+00:00",
                    },
                )
                entries = history.list_history()

        self.assertTrue(updated)
        self.assertEqual(
            entries[0]["publish_metrics"],
            {
                "views": 1200,
                "likes": 90,
                "comments": 10,
                "shares": 5,
                "saves": 7,
                "captured_at": "2026-07-07T10:00:00+00:00",
            },
        )

    def test_update_publish_metrics_keeps_platform_breakdown_and_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history({"task_id": "task-metrics", "subject": "metrics"})
                history.update_publish_metrics(
                    "task-metrics",
                    {
                        "views": 100,
                        "likes": 10,
                        "captured_at": "2026-07-07T10:00:00+00:00",
                        "platform_metrics": {
                            "youtube": {"views": 70, "likes": 8},
                            "tiktok": {"views": 30, "likes": 2},
                        },
                    },
                )
                history.update_publish_metrics(
                    "task-metrics",
                    {
                        "views": 140,
                        "likes": 14,
                        "captured_at": "2026-07-08T10:00:00+00:00",
                        "platform_metrics": {"youtube": {"views": 100, "likes": 11}},
                    },
                )
                entry = history.list_history()[0]

        self.assertEqual(entry["publish_metrics"]["platform_metrics"]["youtube"]["views"], 100)
        self.assertEqual(
            entry["publish_metric_snapshots"][0]["platform_metrics"]["tiktok"]["likes"],
            2,
        )
        self.assertEqual(
            [snapshot["views"] for snapshot in entry["publish_metric_snapshots"]],
            [100, 140],
        )

    def test_list_jobs_pending_metrics_sync_returns_empty_for_empty_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                candidates = history.list_jobs_pending_metrics_sync(
                    now="2026-07-10T12:00:00+00:00"
                )

        self.assertEqual(candidates, [])

    def test_list_jobs_pending_metrics_sync_skips_current_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "current",
                            "created_at": "2026-07-10T11:00:00+00:00",
                            "pending_uploads": [{"request_id": "current-request"}],
                            "publish_metrics": {
                                "captured_at": "2026-07-10T10:00:00+00:00"
                            },
                        }
                    ]
                )
                candidates = history.list_jobs_pending_metrics_sync(
                    recheck_after_hours=24,
                    now="2026-07-10T12:00:00+00:00",
                )

        self.assertEqual(candidates, [])

    def test_list_jobs_pending_metrics_sync_returns_stale_and_missing_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "stale",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "stale-request"}],
                            "publish_metrics": {
                                "captured_at": "2026-07-08T12:00:00+00:00"
                            },
                        },
                        {
                            "task_id": "missing",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "missing-request"}],
                            "publish_metrics": None,
                        },
                        {
                            "task_id": "current",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "current-request"}],
                            "publish_metrics": {
                                "captured_at": "2026-07-10T11:00:00+00:00"
                            },
                        },
                    ]
                )
                candidates = history.list_jobs_pending_metrics_sync(
                    max_age_hours=72,
                    recheck_after_hours=24,
                    now="2026-07-10T12:00:00+00:00",
                )

        self.assertEqual([job["task_id"] for job in candidates], ["stale", "missing"])

    def test_list_jobs_pending_metrics_sync_defers_recent_missing_metric_attempts(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "recent-no-data",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "recent-request"}],
                            "publish_metrics": None,
                            "metrics_sync": {
                                "outcome": "no_data",
                                "attempted_at": "2026-07-10T11:00:00+00:00",
                            },
                        },
                        {
                            "task_id": "old-no-data",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "old-request"}],
                            "publish_metrics": None,
                            "metrics_sync": {
                                "outcome": "no_data",
                                "attempted_at": "2026-07-09T11:00:00+00:00",
                            },
                        },
                        {
                            "task_id": "recent-transient-with-stale-metrics",
                            "created_at": "2026-07-09T12:00:00+00:00",
                            "pending_uploads": [{"request_id": "stale-request"}],
                            "publish_metrics": {
                                "captured_at": "2026-07-08T12:00:00+00:00"
                            },
                            "metrics_sync": {
                                "outcome": "transient_error",
                                "attempted_at": "2026-07-10T11:00:00+00:00",
                            },
                        },
                    ]
                )

                candidates = history.list_jobs_pending_metrics_sync(
                    recheck_after_hours=24,
                    now="2026-07-10T12:00:00+00:00",
                )

        self.assertEqual([job["task_id"] for job in candidates], ["old-no-data"])

    def test_update_metrics_sync_state_records_outcome_and_attempt_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "task-metrics-state",
                            "subject": "metrics",
                        }
                    ]
                )

                updated = history.update_metrics_sync_state(
                    "task-metrics-state",
                    "transient_error",
                    attempted_at="2026-07-10T12:00:00+00:00",
                )
                entries = history.list_history()

        self.assertTrue(updated)
        self.assertEqual(
            entries[0]["metrics_sync"],
            {
                "outcome": "transient_error",
                "attempted_at": "2026-07-10T12:00:00+00:00",
            },
        )

    def test_record_metrics_sync_run_persists_safe_summary(self):
        summary = {
            "eligible": 4,
            "synced": 1,
            "skipped": 1,
            "errors": ["task: secret-api-key"],
            "outcomes": {
                "synced": 1,
                "no_data": 1,
                "transient_error": 1,
                "permanent_error": 1,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                recorded = history.record_metrics_sync_run(
                    summary,
                    status="completed",
                    recorded_at="2026-07-10T12:00:00+00:00",
                )
                loaded = history.get_last_metrics_sync_run()

        expected = {
            "recorded_at": "2026-07-10T12:00:00+00:00",
            "status": "completed",
            "eligible": 4,
            "synced": 1,
            "skipped": 1,
            "errors": 1,
            "outcomes": {
                "synced": 1,
                "no_data": 1,
                "transient_error": 1,
                "permanent_error": 1,
            },
        }
        self.assertEqual(recorded, expected)
        self.assertEqual(loaded, expected)
        self.assertNotIn("secret-api-key", str(loaded))

    def test_update_pending_upload_result_marks_uploaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": [
                            {
                                "video_path": "/tmp/final.mp4",
                                "title": "Ready to upload",
                                "platforms": ["youtube"],
                                "status": "pending",
                            }
                        ],
                    }
                )

                updated = history.update_pending_upload_result(
                    "task-upload",
                    "/tmp/final.mp4",
                    {
                        "success": True,
                        "request_id": "abc123",
                        "post_url": "https://youtube.com/shorts/abc",
                    },
                )
                entries = history.list_history()

        self.assertTrue(updated)
        pending_upload = entries[0]["pending_uploads"][0]
        self.assertEqual(pending_upload["status"], "uploaded")
        self.assertEqual(
            pending_upload["result"],
            {
                "success": True,
                "request_id": "abc123",
                "post_url": "https://youtube.com/shorts/abc",
            },
        )
        self.assertIn("updated_at", pending_upload)

    def test_update_pending_upload_result_marks_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.add_history(
                    {
                        "task_id": "task-upload",
                        "subject": "upload",
                        "pending_uploads": [
                            {
                                "video_path": "/tmp/final.mp4",
                                "title": "Ready to upload",
                                "platforms": ["youtube"],
                                "status": "pending",
                            }
                        ],
                    }
                )

                updated = history.update_pending_upload_result(
                    "task-upload",
                    "/tmp/final.mp4",
                    {"success": False, "error": "quota exceeded"},
                )
                entries = history.list_history()

        self.assertTrue(updated)
        pending_upload = entries[0]["pending_uploads"][0]
        self.assertEqual(pending_upload["status"], "failed")
        self.assertEqual(
            pending_upload["result"],
            {"success": False, "error": "quota exceeded"},
        )

    def test_update_pending_upload_result_skips_malformed_pending_uploads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "task-upload",
                            "subject": "malformed duplicate",
                            "pending_uploads": "not-a-list",
                        },
                        {
                            "task_id": "task-upload",
                            "subject": "valid duplicate",
                            "pending_uploads": [
                                {
                                    "video_path": "/tmp/final.mp4",
                                    "title": "Ready to upload",
                                    "platforms": ["youtube"],
                                    "status": "pending",
                                }
                            ],
                        },
                    ]
                )

                updated = history.update_pending_upload_result(
                    "task-upload",
                    "/tmp/final.mp4",
                    {"success": True, "post_url": "https://youtube.com/shorts/abc"},
                )
                entries = history.list_history()

        self.assertTrue(updated)
        self.assertEqual(entries[0]["pending_uploads"], "not-a-list")
        self.assertEqual(entries[1]["pending_uploads"][0]["status"], "uploaded")

    def test_find_recent_similar_subjects_matches_recent_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "recent",
                            "subject": "Budget mistakes beginners make",
                            "created_at": "2026-07-04T10:00:00+00:00",
                        },
                        {
                            "task_id": "old",
                            "subject": "Budget mistakes beginners make",
                            "created_at": "2026-06-20T10:00:00+00:00",
                        },
                        {
                            "task_id": "different",
                            "subject": "Coffee brewing guide",
                            "created_at": "2026-07-05T10:00:00+00:00",
                        },
                    ]
                )

                matches = history.find_recent_similar_subjects(
                    "Beginner budget mistakes",
                    days=5,
                    now="2026-07-07T10:00:00+00:00",
                )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["task_id"], "recent")
        self.assertGreaterEqual(matches[0]["similarity"], 0.5)

    def test_find_recent_similar_subjects_ignores_blank_and_bad_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "blank",
                            "subject": "",
                            "created_at": "2026-07-06T10:00:00+00:00",
                        },
                        {
                            "task_id": "bad-date",
                            "subject": "Budget mistakes",
                            "created_at": "not-a-date",
                        },
                    ]
                )

                matches = history.find_recent_similar_subjects(
                    "Budget mistakes",
                    days=5,
                    now="2026-07-07T10:00:00+00:00",
                )

        self.assertEqual(matches, [])

    def test_find_recent_similar_subjects_can_use_bounded_semantic_callback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "recent",
                            "subject": "Reducing household expenses",
                            "created_at": "2026-07-05T10:00:00+00:00",
                        },
                        {
                            "task_id": "other",
                            "subject": "Coffee brewing guide",
                            "created_at": "2026-07-05T09:00:00+00:00",
                        },
                    ]
                )
                similarity = Mock(side_effect=[0.91, 0.12])

                matches = history.find_recent_similar_subjects(
                    "How to spend less on groceries",
                    days=5,
                    now="2026-07-07T10:00:00+00:00",
                    semantic_similarity=similarity,
                    semantic_threshold=0.8,
                    semantic_candidate_limit=1,
                )

        self.assertEqual([match["task_id"] for match in matches], ["recent"])
        self.assertEqual(similarity.call_count, 1)
        self.assertEqual(matches[0]["semantic_similarity"], 0.91)


if __name__ == "__main__":
    unittest.main()
