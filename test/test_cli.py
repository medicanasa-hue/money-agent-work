import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from app.config import config
from app.models import const

import cli


class TestVideoSourceOptions(unittest.TestCase):
    def test_parse_args_uses_the_configured_video_source_by_default(self):
        with patch.dict(config.app, {"video_source": "multi"}, clear=False):
            args = cli.parse_args(["--video-subject", "Configured source check"])

        self.assertEqual(args.video_source, "multi")

    def test_parse_args_falls_back_when_the_configured_source_is_invalid(self):
        with patch.dict(config.app, {"video_source": "unknown-source"}, clear=False):
            args = cli.parse_args(["--video-subject", "Configured source check"])

        self.assertEqual(args.video_source, "pexels")

    def test_parse_args_accepts_supported_video_sources(self):
        for source in ("dvids", "vecteezy", "noaa_ocean", "loc"):
            with self.subTest(source=source):
                args = cli.parse_args(
                    ["--video-subject", "Video source check", "--video-source", source]
                )

                self.assertEqual(args.video_source, source)


class TestOutputCleanupCommand(unittest.TestCase):
    def test_parse_args_accepts_cleanup_output_without_video_subject(self):
        args = cli.parse_args(["--cleanup-output"])

        self.assertTrue(args.cleanup_output)
        self.assertEqual(args.video_subject, "")

    def test_parse_args_requires_cleanup_flag_before_apply(self):
        error = io.StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--apply-output-cleanup"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--apply-output-cleanup requires --cleanup-output",
            error.getvalue(),
        )

    def test_parse_args_accepts_cache_video_cleanup_without_video_subject(self):
        args = cli.parse_args(["--cleanup-cache-videos"])

        self.assertTrue(args.cleanup_cache_videos)
        self.assertEqual(args.video_subject, "")

    def test_parse_args_requires_cache_cleanup_flag_before_apply(self):
        error = io.StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--apply-cache-video-cleanup"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--apply-cache-video-cleanup requires --cleanup-cache-videos",
            error.getvalue(),
        )

    @patch("cli.state.get_all_tasks")
    @patch("cli.output_cleanup.cleanup_task_outputs")
    def test_cleanup_output_previews_expired_tasks_and_protects_active_tasks(
        self, cleanup_task_outputs, get_all_tasks
    ):
        get_all_tasks.return_value = {
            "running": {"state": const.TASK_STATE_PROCESSING},
            "finished": {"state": const.TASK_STATE_COMPLETE},
        }
        cleanup_task_outputs.return_value = {
            "dry_run": True,
            "retention_days": 30,
            "scanned": 3,
            "eligible": ["old-task"],
            "deleted": [],
            "skipped_active": 1,
            "errors": ["old-task: permission denied"],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--cleanup-output"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            cleanup_task_outputs.call_args.kwargs["active_task_ids"],
            {"running"},
        )
        self.assertFalse(cleanup_task_outputs.call_args.kwargs["apply"])
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertNotIn("permission denied", output.getvalue())

    @patch("cli.state.get_all_tasks")
    @patch("cli.output_cleanup.cleanup_video_cache")
    def test_cleanup_cache_video_preview_reports_reclaimable_space(
        self, cleanup_video_cache, get_all_tasks
    ):
        get_all_tasks.return_value = {}
        cleanup_video_cache.return_value = {
            "dry_run": True,
            "retention_days": 30,
            "scanned": 3,
            "eligible": ["old.mp4"],
            "eligible_bytes": 4096,
            "deleted": [],
            "deleted_bytes": 0,
            "blocked_by_active_tasks": False,
            "errors": [],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--cleanup-cache-videos"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(cleanup_video_cache.call_args.kwargs["apply"])
        self.assertFalse(cleanup_video_cache.call_args.kwargs["active_tasks_present"])
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["eligible_bytes"], 4096)


class TestStateExportCommand(unittest.TestCase):
    def test_parse_args_accepts_state_export_without_video_subject(self):
        args = cli.parse_args(["--export-state"])

        self.assertEqual(args.export_state, "")
        self.assertEqual(args.video_subject, "")

    @patch("cli.state_backup.export_state_backup")
    def test_state_export_reports_archive_without_printing_its_contents(
        self, export_state_backup
    ):
        export_state_backup.return_value = {
            "ok": True,
            "archive": "C:/backups/mpt-state.zip",
            "files": ["history/production_history.json"],
            "errors": [],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--export-state", "C:/backups/mpt-state.zip"])

        self.assertEqual(exit_code, 0)
        export_state_backup.assert_called_once_with("C:/backups/mpt-state.zip")
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["files"], 1)
        self.assertNotIn("production_history.json", output.getvalue())


class TestRenderQualityBackfillCommand(unittest.TestCase):
    def test_parse_args_requires_backfill_for_apply(self):
        error = io.StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--apply-render-quality-backfill"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--apply-render-quality-backfill requires --backfill-render-quality",
            error.getvalue(),
        )

    @patch("cli.history.backfill_render_quality_reports")
    def test_backfill_render_quality_previews_without_video_subject(self, backfill):
        backfill.return_value = {
            "dry_run": True,
            "jobs_with_new_reports": 2,
            "updated_jobs": 0,
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--backfill-render-quality"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), backfill.return_value)
        self.assertFalse(backfill.call_args.kwargs["persist"])

    @patch("cli.history.backfill_render_quality_reports")
    def test_backfill_render_quality_persists_only_with_explicit_apply(self, backfill):
        backfill.return_value = {
            "dry_run": False,
            "jobs_with_new_reports": 1,
            "updated_jobs": 1,
        }

        with redirect_stdout(io.StringIO()):
            exit_code = cli.run_cli(
                ["--backfill-render-quality", "--apply-render-quality-backfill"]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(backfill.call_args.kwargs["persist"])


class TestMetricsSyncCommand(unittest.TestCase):
    def test_parse_args_explains_sync_metrics_dry_run_requires_sync_metrics(self):
        error = io.StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--sync-metrics-dry-run"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--sync-metrics-dry-run requires --sync-metrics", error.getvalue()
        )

    def test_parse_args_accepts_sync_metrics_without_video_subject(self):
        args = cli.parse_args(["--sync-metrics"])

        self.assertTrue(args.sync_metrics)
        self.assertEqual(args.video_subject, "")

    def test_parse_args_accepts_metrics_sync_limit(self):
        args = cli.parse_args(["--sync-metrics", "--sync-metrics-limit", "2"])

        self.assertEqual(args.sync_metrics_limit, 2)

    @patch("cli.history.record_metrics_sync_run")
    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.history.list_jobs_pending_metrics_sync")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_dry_run_lists_limited_candidates_without_api_calls(
        self,
        upload_post_service,
        list_candidates,
        sync_pending_publish_metrics,
        record_metrics_sync_run,
    ):
        list_candidates.return_value = [{"task_id": "one"}, {"task_id": "two"}]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--sync-metrics",
                    "--sync-metrics-dry-run",
                    "--sync-metrics-limit",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn('"eligible": 1', output.getvalue())
        self.assertIn('"dry_run": true', output.getvalue())
        upload_post_service.is_configured.assert_not_called()
        sync_pending_publish_metrics.assert_not_called()
        record_metrics_sync_run.assert_not_called()

    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_exits_cleanly_when_upload_post_is_unconfigured(
        self, upload_post_service, sync_pending_publish_metrics
    ):
        upload_post_service.is_configured.return_value = False

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--sync-metrics"])

        self.assertEqual(exit_code, 0)
        self.assertIn("not configured", output.getvalue())
        sync_pending_publish_metrics.assert_not_called()

    @patch("cli.history.record_metrics_sync_run")
    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_records_unconfigured_run(
        self,
        upload_post_service,
        sync_pending_publish_metrics,
        record_metrics_sync_run,
    ):
        upload_post_service.is_configured.return_value = False

        exit_code = cli.run_cli(["--sync-metrics"])

        self.assertEqual(exit_code, 0)
        sync_pending_publish_metrics.assert_not_called()
        recorded_summary = record_metrics_sync_run.call_args.args[0]
        self.assertEqual(recorded_summary["eligible"], 0)
        self.assertEqual(recorded_summary["errors"], [])
        self.assertEqual(record_metrics_sync_run.call_args.kwargs["status"], "not_configured")

    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_prints_counts_without_error_details(
        self, upload_post_service, sync_pending_publish_metrics
    ):
        upload_post_service.is_configured.return_value = True
        sync_pending_publish_metrics.return_value = {
            "synced": 2,
            "skipped": 1,
            "errors": ["task: secret-api-key"],
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--sync-metrics"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"synced": 2', output.getvalue())
        self.assertIn('"skipped": 1', output.getvalue())
        self.assertIn('"errors": 1', output.getvalue())
        self.assertNotIn("secret-api-key", output.getvalue())
        self.assertTrue(sync_pending_publish_metrics.call_args.args[0])

    @patch("cli.history.record_metrics_sync_run")
    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_records_completed_run_summary(
        self,
        upload_post_service,
        sync_pending_publish_metrics,
        record_metrics_sync_run,
    ):
        summary = {
            "eligible": 2,
            "synced": 1,
            "skipped": 1,
            "errors": [],
            "outcomes": {
                "synced": 1,
                "no_data": 1,
                "transient_error": 0,
                "permanent_error": 0,
            },
        }
        upload_post_service.is_configured.return_value = True
        sync_pending_publish_metrics.return_value = summary

        exit_code = cli.run_cli(["--sync-metrics"])

        self.assertEqual(exit_code, 0)
        record_metrics_sync_run.assert_called_once_with(summary)

    @patch("cli.metrics_sync.sync_pending_publish_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_sync_metrics_passes_configured_job_limit(
        self, upload_post_service, sync_pending_publish_metrics
    ):
        upload_post_service.is_configured.return_value = True
        sync_pending_publish_metrics.return_value = {
            "synced": 0,
            "skipped": 0,
            "errors": [],
        }

        exit_code = cli.run_cli(["--sync-metrics", "--sync-metrics-limit", "2"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(sync_pending_publish_metrics.call_args.kwargs["max_jobs"], 2)

    @patch("cli.history.update_publish_metrics", return_value=True)
    @patch("cli.upload_post.aggregate_post_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_job_sync_uses_upload_post_analytics(
        self, upload_post_service, aggregate_post_metrics, update_publish_metrics
    ):
        upload_post_service.get_post_analytics.return_value = {
            "success": True,
            "platforms": {"youtube": {"post_metrics": {"views": 4}}},
        }
        aggregate_post_metrics.return_value = {
            "views": 4,
            "likes": 2,
            "comments": 1,
            "shares": 0,
            "saves": 3,
        }

        result = cli._sync_upload_post_metrics_for_job(
            {
                "task_id": "task-1",
                "pending_uploads": [
                    {"result": {"request_id": "request-1"}},
                ],
            }
        )

        self.assertEqual(result.outcome, cli.metrics_sync.SYNC_OUTCOME_SYNCED)
        upload_post_service.get_post_analytics.assert_called_once_with("request-1")
        aggregate_post_metrics.assert_called_once()
        self.assertEqual(update_publish_metrics.call_args.args[0], "task-1")
        self.assertEqual(update_publish_metrics.call_args.args[1]["views"], 4)

    @patch("cli.history.update_publish_metrics", return_value=True)
    @patch("cli.upload_post.aggregate_post_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_job_sync_does_not_capture_empty_analytics(
        self, upload_post_service, aggregate_post_metrics, update_publish_metrics
    ):
        upload_post_service.get_post_analytics.return_value = {
            "success": True,
            "platforms": {},
        }

        result = cli._sync_upload_post_metrics_for_job(
            {
                "task_id": "task-1",
                "pending_uploads": [
                    {"result": {"request_id": "request-1"}},
                ],
            }
        )

        self.assertEqual(result.outcome, cli.metrics_sync.SYNC_OUTCOME_NO_DATA)
        aggregate_post_metrics.assert_not_called()
        update_publish_metrics.assert_not_called()

    @patch("cli.history.update_publish_metrics")
    @patch("cli.upload_post.aggregate_post_metrics")
    @patch("cli.upload_post.upload_post_service")
    def test_job_sync_reports_transient_analytics_failures(
        self, upload_post_service, aggregate_post_metrics, update_publish_metrics
    ):
        upload_post_service.get_post_analytics.return_value = {
            "success": False,
            "retryable": True,
        }

        result = cli._sync_upload_post_metrics_for_job(
            {
                "task_id": "task-1",
                "pending_uploads": [
                    {"result": {"request_id": "request-1"}},
                ],
            }
        )

        self.assertEqual(
            result.outcome,
            cli.metrics_sync.SYNC_OUTCOME_TRANSIENT_ERROR,
        )
        aggregate_post_metrics.assert_not_called()
        update_publish_metrics.assert_not_called()


class TestPublishInsightsCommand(unittest.TestCase):
    def test_parse_args_accepts_publish_insights_without_video_subject(self):
        args = cli.parse_args(["--publish-insights"])

        self.assertTrue(args.publish_insights)
        self.assertEqual(args.video_subject, "")

    @patch("cli.tm.start")
    @patch(
        "cli.publish_insights.build_publish_performance_insights",
        return_value={
            "status": "ready",
            "sample_size": 3,
            "automatic_actions": False,
            "suggestions": [{"type": "manual_review"}],
        },
    )
    @patch("cli.history.list_history", return_value=[{"subject": "private subject"}])
    def test_publish_insights_prints_read_only_summary(
        self,
        list_history,
        build_publish_performance_insights,
        start,
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--publish-insights"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        self.assertNotIn("private subject", output.getvalue())
        list_history.assert_called_once_with()
        build_publish_performance_insights.assert_called_once_with(
            [{"subject": "private subject"}]
        )
        start.assert_not_called()


class TestVideoFormatCommand(unittest.TestCase):
    def test_build_video_params_keeps_primary_aspect_with_additional_outputs(self):
        args = cli.parse_args(
            [
                "--video-subject",
                "format test",
                "--video-aspect",
                "9:16",
                "--video-aspects",
                "4:5, 1:1, 4:5",
            ]
        )

        params = cli.build_video_params(args)

        self.assertEqual(params.video_aspect.value, "9:16")
        self.assertEqual(
            [aspect.value for aspect in params.video_aspects],
            ["9:16", "4:5", "1:1"],
        )


class TestVideoEncoderCheckCommand(unittest.TestCase):
    def test_parse_args_accepts_video_encoder_check_without_video_subject(self):
        args = cli.parse_args(["--check-video-encoder"])

        self.assertTrue(args.check_video_encoder)
        self.assertEqual(args.video_subject, "")

    @patch("cli.video_service.check_video_encoder")
    def test_video_encoder_check_prints_codec_summary(self, check_video_encoder):
        check_video_encoder.return_value = {
            "configured_codec": "h264_amf",
            "used_codec": "h264_amf",
            "fallback_used": False,
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--check-video-encoder"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"configured_codec": "h264_amf"', output.getvalue())
        self.assertIn('"fallback_used": false', output.getvalue())

    @patch("cli.video_service.check_video_encoder")
    def test_video_encoder_check_uses_requested_aspect(self, check_video_encoder):
        check_video_encoder.return_value = {
            "configured_codec": "h264_amf",
            "used_codec": "h264_amf",
            "fallback_used": False,
        }

        with redirect_stdout(io.StringIO()):
            exit_code = cli.run_cli(
                ["--check-video-encoder", "--video-aspect", "4:5"]
            )

        self.assertEqual(exit_code, 0)
        check_video_encoder.assert_called_once_with("4:5")

    @patch("cli.video_service.check_video_encoder")
    def test_video_encoder_check_covers_requested_additional_aspects(
        self, check_video_encoder
    ):
        check_video_encoder.side_effect = [
            {
                "configured_codec": "h264_amf",
                "used_codec": "h264_amf",
                "fallback_used": False,
                "video_aspect": "9:16",
            },
            {
                "configured_codec": "h264_amf",
                "used_codec": "h264_amf",
                "fallback_used": False,
                "video_aspect": "4:5",
            },
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--check-video-encoder",
                    "--video-aspect",
                    "9:16",
                    "--video-aspects",
                    "4:5",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [call.args[0] for call in check_video_encoder.call_args_list],
            ["9:16", "4:5"],
        )
        self.assertFalse(summary["fallback_used"])
        self.assertEqual(
            [item["video_aspect"] for item in summary["checks"]],
            ["9:16", "4:5"],
        )

    @patch("cli.video_service.check_video_encoder")
    def test_video_encoder_check_continues_after_an_aspect_error(
        self, check_video_encoder
    ):
        check_video_encoder.side_effect = [
            RuntimeError("private driver detail"),
            {
                "configured_codec": "h264_amf",
                "used_codec": "h264_amf",
                "fallback_used": False,
                "video_aspect": "4:5",
            },
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--check-video-encoder",
                    "--video-aspect",
                    "9:16",
                    "--video-aspects",
                    "4:5",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            [call.args[0] for call in check_video_encoder.call_args_list],
            ["9:16", "4:5"],
        )
        self.assertFalse(summary["checks"][0]["ok"])
        self.assertEqual(
            summary["checks"][0]["error"], "Video encoder check failed."
        )
        self.assertTrue(summary["checks"][1]["ok"])
        self.assertNotIn("private driver detail", output.getvalue())

    @patch("cli.video_service.check_video_encoder")
    def test_video_encoder_check_hides_internal_error_details(self, check_video_encoder):
        check_video_encoder.side_effect = RuntimeError("private driver detail")

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--check-video-encoder"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Video encoder check failed.", output.getvalue())
        self.assertNotIn("private driver detail", output.getvalue())


class TestShortClipPlanCommand(unittest.TestCase):
    def test_parse_args_accepts_short_clip_plan_without_video_subject(self):
        args = cli.parse_args(
            [
                "--repurpose-video",
                "C:/tmp/source.mp4",
                "--repurpose-clip-duration",
                "30",
                "--repurpose-clip-count",
                "3",
            ]
        )

        self.assertEqual(args.repurpose_video, "C:/tmp/source.mp4")
        self.assertEqual(args.repurpose_clip_duration, 30.0)
        self.assertEqual(args.repurpose_clip_count, 3)

    def test_parse_args_accepts_optional_subtitle_for_short_clip_selection(self):
        args = cli.parse_args(
            [
                "--repurpose-video",
                "C:/tmp/source.mp4",
                "--repurpose-clip-duration",
                "30",
                "--repurpose-clip-count",
                "3",
                "--repurpose-subtitle-file",
                "C:/tmp/subtitle.srt",
            ]
        )

        self.assertEqual(args.repurpose_subtitle_file, "C:/tmp/subtitle.srt")

    def test_parse_args_accepts_portrait_short_clip_rendering(self):
        args = cli.parse_args(
            [
                "--repurpose-video",
                "C:/tmp/source.mp4",
                "--repurpose-clip-duration",
                "30",
                "--repurpose-clip-count",
                "3",
                "--repurpose-output-dir",
                "C:/tmp/output",
                "--repurpose-render-mode",
                "precise",
                "--repurpose-aspect",
                "9:16",
            ]
        )

        self.assertEqual(args.repurpose_aspect, "9:16")

    def test_parse_args_rejects_portrait_short_clip_rendering_in_fast_mode(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-output-dir",
                    "C:/tmp/output",
                    "--repurpose-aspect",
                    "9:16",
                ]
            )

    @patch("cli.tm.start")
    @patch("cli.video_service.get_video_duration", return_value=95)
    @patch(
        "cli.repurpose.plan_subtitle_guided_short_clips",
        return_value={
            "source_duration_seconds": 95.0,
            "clip_duration_seconds": 30.0,
            "requested_clip_count": 3,
            "clip_count": 2,
            "selection_mode": "subtitle",
            "subtitle_segment_count": 3,
            "clips": [
                {"index": 1, "start_seconds": 10.0, "duration_seconds": 30.0},
                {"index": 2, "start_seconds": 60.0, "duration_seconds": 30.0},
            ],
        },
    )
    def test_short_clip_plan_uses_optional_subtitle_without_exposing_its_path(
        self, plan_subtitle_guided_short_clips, get_video_duration, start
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-subtitle-file",
                    "C:/tmp/subtitle.srt",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["selection_mode"], "subtitle")
        self.assertNotIn("C:/tmp/subtitle.srt", output.getvalue())
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        plan_subtitle_guided_short_clips.assert_called_once_with(
            95,
            clip_duration_seconds=30.0,
            clip_count=3,
            subtitle_path="C:/tmp/subtitle.srt",
        )
        start.assert_not_called()

    @patch("cli.repurpose.render_short_clips")
    @patch("cli.tm.start")
    @patch("cli.video_service.get_video_duration", return_value=95)
    def test_short_clip_plan_prints_windows_without_generating_video(
        self, get_video_duration, start, render_short_clips
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["clip_count"], 3)
        self.assertEqual(summary["clips"][1]["start_seconds"], 32.5)
        self.assertNotIn("C:/tmp/source.mp4", output.getvalue())
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        start.assert_not_called()
        render_short_clips.assert_not_called()

    @patch(
        "cli.repurpose.render_short_clips",
        return_value={"rendered_clip_count": 3, "error_count": 0},
    )
    @patch("cli.video_service.get_video_duration", return_value=95)
    def test_short_clip_plan_renders_only_with_explicit_output_dir(
        self, get_video_duration, render_short_clips
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-output-dir",
                    "C:/tmp/output",
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["rendered_clip_count"], 3)
        self.assertEqual(summary["render_error_count"], 0)
        self.assertNotIn("C:/tmp/output", output.getvalue())
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        render_short_clips.assert_called_once()
        self.assertEqual(
            render_short_clips.call_args.kwargs["render_mode"],
            "fast",
        )

    @patch(
        "cli.repurpose.render_short_clips",
        return_value={"rendered_clip_count": 3, "error_count": 0},
    )
    @patch("cli.video_service.get_video_duration", return_value=95)
    def test_short_clip_plan_passes_requested_precise_render_mode(
        self, get_video_duration, render_short_clips
    ):
        with redirect_stdout(io.StringIO()):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-output-dir",
                    "C:/tmp/output",
                    "--repurpose-render-mode",
                    "precise",
                ]
            )

        self.assertEqual(exit_code, 0)
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        self.assertEqual(
            render_short_clips.call_args.kwargs["render_mode"],
            "precise",
        )

    @patch(
        "cli.repurpose.render_short_clips",
        return_value={"rendered_clip_count": 3, "error_count": 0},
    )
    @patch("cli.video_service.get_video_duration", return_value=95)
    def test_short_clip_plan_passes_requested_portrait_aspect(
        self, get_video_duration, render_short_clips
    ):
        with redirect_stdout(io.StringIO()):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-output-dir",
                    "C:/tmp/output",
                    "--repurpose-render-mode",
                    "precise",
                    "--repurpose-aspect",
                    "9:16",
                ]
            )

        self.assertEqual(exit_code, 0)
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        self.assertEqual(
            render_short_clips.call_args.kwargs["target_aspect"],
            "9:16",
        )

    @patch(
        "cli.repurpose.render_short_clips",
        return_value={"rendered_clip_count": 3, "error_count": 0},
    )
    @patch("cli.video_service.get_video_duration", return_value=95)
    def test_short_clip_plan_passes_requested_video_codec_in_precise_mode(
        self, get_video_duration, render_short_clips
    ):
        with redirect_stdout(io.StringIO()):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/source.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                    "--repurpose-output-dir",
                    "C:/tmp/output",
                    "--repurpose-render-mode",
                    "precise",
                    "--video-codec",
                    "h264_amf",
                ]
            )

        self.assertEqual(exit_code, 0)
        get_video_duration.assert_called_once_with("C:/tmp/source.mp4")
        self.assertEqual(
            render_short_clips.call_args.kwargs["video_codec"],
            "h264_amf",
        )

    @patch("cli.tm.start")
    @patch("cli.video_service.get_video_duration", return_value=None)
    def test_short_clip_plan_fails_safely_when_duration_is_unavailable(
        self, get_video_duration, start
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                [
                    "--repurpose-video",
                    "C:/tmp/missing.mp4",
                    "--repurpose-clip-duration",
                    "30",
                    "--repurpose-clip-count",
                    "3",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("Video duration is unavailable.", output.getvalue())
        self.assertNotIn("C:/tmp/missing.mp4", output.getvalue())
        get_video_duration.assert_called_once_with("C:/tmp/missing.mp4")
        start.assert_not_called()


class TestScheduledJobCommand(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(cli.config.app)
        cli.config.app["twelvelabs_semantic_duplicate_check"] = False

    def tearDown(self):
        cli.config.app.clear()
        cli.config.app.update(self.original_app_config)

    def test_parse_args_accepts_scheduled_job_listing_without_video_subject(self):
        args = cli.parse_args(["--list-scheduled-jobs"])

        self.assertTrue(args.list_scheduled_jobs)
        self.assertEqual(args.video_subject, "")

    @patch("cli.history.list_history", return_value=[])
    @patch("cli.scheduled_jobs.list_scheduled_jobs")
    def test_scheduled_job_listing_prints_safe_summaries(
        self,
        list_scheduled_jobs,
        _list_history,
    ):
        list_scheduled_jobs.return_value = [
            {
                "name": "morning-finance",
                "enabled": True,
                "video_subject": "Why prices rise",
                "video_script": "Private approved script.",
            }
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--list-scheduled-jobs"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"count": 1', output.getvalue())
        self.assertIn('"name": "morning-finance"', output.getvalue())
        self.assertNotIn("Private approved script.", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["health"]["job_count"], 1)

    @patch(
        "cli.scheduled_jobs.list_scheduled_jobs",
        side_effect=cli.scheduled_jobs.ScheduledJobError("private script detail"),
    )
    def test_scheduled_job_listing_hides_configuration_error_details(
        self, list_scheduled_jobs
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--list-scheduled-jobs"])

        self.assertEqual(exit_code, 2)
        self.assertIn("Scheduled jobs are unavailable.", output.getvalue())
        self.assertNotIn("private script detail", output.getvalue())

    def test_parse_args_accepts_scheduled_job_dry_run_without_video_subject(self):
        args = cli.parse_args(
            ["--scheduled-job", "morning-finance", "--scheduled-job-dry-run"]
        )

        self.assertEqual(args.scheduled_job, "morning-finance")
        self.assertTrue(args.scheduled_job_dry_run)

    def test_scheduled_job_args_applies_a_configured_transition_mode(self):
        args = cli.parse_args(["--scheduled-job", "morning-finance"])

        scheduled_args = cli._scheduled_job_args(
            args,
            {
                "video_subject": "Why prices rise",
                "video_script": "",
                "video_transition_mode": "Shuffle",
                "voice_name": "no-voice",
                "video_script_prompt": "Open with a concrete viewer benefit.",
            },
        )

        self.assertEqual(scheduled_args.video_transition_mode, "Shuffle")
        self.assertEqual(scheduled_args.voice_name, "no-voice")
        self.assertEqual(
            scheduled_args.video_script_prompt,
            "Open with a concrete viewer benefit.",
        )
        self.assertEqual(
            cli.build_video_params(scheduled_args).video_transition_mode.value,
            "Shuffle",
        )

    @patch("cli.openmontage_materials.validate_openmontage_output")
    @patch("cli.openmontage_materials.find_openmontage_output")
    def test_scheduled_job_args_uses_matching_silent_openmontage_output(
        self, find_openmontage_output, validate_openmontage_output
    ):
        find_openmontage_output.return_value = (
            "C:/library/final_silent_tr_9x16_1080p.mp4"
        )
        validate_openmontage_output.return_value = {
            "valid": True,
            "quality_warnings": [],
        }
        args = cli.parse_args(["--scheduled-job", "morning-finance"])
        args.video_language = "tr-TR"

        scheduled_args = cli._scheduled_job_args(
            args,
            {
                "video_subject": "Para basma enflasyon",
                "video_script": "",
                "openmontage_auto_materials": True,
            },
        )

        self.assertEqual(scheduled_args.video_source, "local")
        self.assertEqual(
            scheduled_args.video_materials,
            "C:/library/final_silent_tr_9x16_1080p.mp4",
        )
        find_openmontage_output.assert_called_once_with(
            "Para basma enflasyon",
            prefer_silent=True,
            video_aspect=args.video_aspect,
            language="tr-TR",
        )
        validate_openmontage_output.assert_called_once_with(
            "C:/library/final_silent_tr_9x16_1080p.mp4",
            video_aspect=args.video_aspect,
        )

    @patch("cli.openmontage_materials.validate_openmontage_output")
    @patch("cli.openmontage_materials.find_openmontage_output")
    def test_scheduled_job_args_uses_low_bitrate_openmontage_output_when_native_render_is_valid(
        self, find_openmontage_output, validate_openmontage_output
    ):
        find_openmontage_output.return_value = (
            "C:/library/final_silent_tr_9x16_1080p.mp4"
        )
        validate_openmontage_output.return_value = {
            "valid": True,
            "quality_warnings": ["low_bitrate_review_recommended"],
        }
        args = cli.parse_args(["--scheduled-job", "morning-finance"])
        scheduled_args = cli._scheduled_job_args(
            args,
            {
                "video_subject": "Para basma enflasyon",
                "video_script": "",
                "openmontage_auto_materials": True,
            },
        )

        self.assertEqual(scheduled_args.video_source, "local")
        self.assertEqual(
            scheduled_args.video_materials,
            "C:/library/final_silent_tr_9x16_1080p.mp4",
        )
        validate_openmontage_output.assert_called_once_with(
            "C:/library/final_silent_tr_9x16_1080p.mp4",
            video_aspect=args.video_aspect,
        )

    @patch("cli.openmontage_materials.validate_openmontage_output")
    @patch("cli.openmontage_materials.find_openmontage_output")
    def test_scheduled_job_args_keeps_stock_source_when_openmontage_validation_fails(
        self, find_openmontage_output, validate_openmontage_output
    ):
        find_openmontage_output.return_value = (
            "C:/library/final_silent_tr_9x16_1080p.mp4"
        )
        validate_openmontage_output.return_value = {
            "valid": False,
            "quality_warnings": [],
        }
        args = cli.parse_args(["--scheduled-job", "morning-finance"])
        args.video_source = "multi"

        scheduled_args = cli._scheduled_job_args(
            args,
            {
                "video_subject": "Para basma enflasyon",
                "video_script": "",
                "openmontage_auto_materials": True,
            },
        )

        self.assertEqual(scheduled_args.video_source, "multi")
        self.assertEqual(scheduled_args.video_materials, args.video_materials)

    @patch("cli.openmontage_materials.find_openmontage_output")
    def test_scheduled_job_args_keeps_stock_source_when_no_openmontage_output(
        self, find_openmontage_output
    ):
        find_openmontage_output.return_value = None
        args = cli.parse_args(["--scheduled-job", "morning-finance"])
        args.video_source = "multi"

        scheduled_args = cli._scheduled_job_args(
            args,
            {
                "video_subject": "Unmatched topic",
                "video_script": "",
                "openmontage_auto_materials": True,
            },
        )

        self.assertEqual(scheduled_args.video_source, "multi")
        self.assertEqual(scheduled_args.video_materials, args.video_materials)

    @patch("cli.history.find_recent_similar_subjects", return_value=[])
    @patch("cli.twelvelabs.is_enabled", return_value=True)
    def test_scheduled_subject_check_can_opt_in_to_semantic_similarity(
        self, is_enabled, find_recent_similar_subjects
    ):
        with patch.dict(
            cli.config.app,
            {
                "twelvelabs_semantic_duplicate_check": True,
                "twelvelabs_semantic_duplicate_max_candidates": 7,
                "twelvelabs_semantic_duplicate_threshold": 0.82,
            },
        ):
            matches = cli._find_recent_scheduled_subjects("Fresh finance topic")

        self.assertEqual(matches, [])
        is_enabled.assert_called_once_with()
        self.assertIs(
            find_recent_similar_subjects.call_args.kwargs["semantic_similarity"],
            cli.twelvelabs.semantic_text_similarity,
        )
        self.assertEqual(
            find_recent_similar_subjects.call_args.kwargs["semantic_candidate_limit"],
            7,
        )

    def test_parse_args_accepts_crossfade_transition_mode(self):
        args = cli.parse_args(
            ["--video-subject", "Why prices rise", "--video-transition-mode", "crossfade"]
        )

        self.assertEqual(args.video_transition_mode, "Crossfade")
        self.assertEqual(
            cli.build_video_params(args).video_transition_mode.value,
            "Crossfade",
        )

    @patch("cli.history.find_recent_similar_subjects")
    def test_scheduled_subject_pool_skips_recent_topics(self, find_recent_similar_subjects):
        find_recent_similar_subjects.side_effect = [
            [{"subject": "Why grocery prices rise"}],
            [],
        ]

        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "",
                "video_subject_pool": [
                    "Why grocery prices rise",
                    "How interest rates affect rent",
                ],
            }
        )

        self.assertEqual(subject, "How interest rates affect rent")
        self.assertEqual(find_recent_similar_subjects.call_count, 2)

    @patch("cli.history.list_history")
    @patch("cli.history.find_recent_similar_subjects", return_value=[])
    def test_scheduled_subject_pool_prefers_stronger_historical_topic(
        self,
        _find_recent_similar_subjects,
        list_history,
    ):
        list_history.return_value = [
            {
                "subject": "Grocery prices explained",
                "publish_metrics": {"views": 250, "likes": 2},
            },
            {
                "subject": "Grocery prices explained",
                "publish_metrics": {"views": 260, "likes": 2},
            },
            {
                "subject": "Interest rates and rent",
                "publish_metrics": {"views": 1400, "likes": 160},
            },
            {
                "subject": "Interest rates and rent",
                "publish_metrics": {"views": 1300, "likes": 130},
            },
        ]

        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "",
                "video_subject_pool": [
                    "Grocery prices explained",
                    "Interest rates and rent",
                ],
            }
        )

        self.assertEqual(subject, "Interest rates and rent")

    @patch(
        "cli.history.find_recent_similar_subjects",
        return_value=[{"subject": "Recently used"}],
    )
    def test_scheduled_subject_pool_returns_empty_when_exhausted(
        self,
        find_recent_similar_subjects,
    ):
        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "",
                "video_subject_pool": ["Recently used"],
            }
        )

        self.assertEqual(subject, "")
        find_recent_similar_subjects.assert_called_once_with("Recently used")

    @patch("cli.history.find_recent_similar_subjects")
    @patch(
        "cli.rss_trend.fetch_rss_trend",
        return_value="Recently used headline; Fresh headline",
    )
    def test_scheduled_rss_trend_skips_recent_headlines(
        self,
        fetch_rss_trend,
        find_recent_similar_subjects,
    ):
        find_recent_similar_subjects.side_effect = [
            [{"subject": "Recently used headline"}],
            [],
        ]

        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "",
                "video_subject_pool": [],
                "rss_trend_query": "personal finance",
            }
        )

        self.assertEqual(subject, "Fresh headline")
        fetch_rss_trend.assert_called_once_with("personal finance")
        self.assertEqual(find_recent_similar_subjects.call_count, 2)

    @patch("cli.history.find_recent_similar_subjects", return_value=[])
    @patch("cli.rss_trend.fetch_rss_trend", return_value="Fresh headline")
    def test_scheduled_rss_trend_uses_configured_language(
        self,
        fetch_rss_trend,
        _find_recent_similar_subjects,
    ):
        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "",
                "video_subject_pool": [],
                "rss_trend_query": "economy",
                "rss_trend_language": "tr-TR",
            }
        )

        self.assertEqual(subject, "Fresh headline")
        fetch_rss_trend.assert_called_once_with("economy", language="tr-TR")

    @patch("cli.history.find_recent_similar_subjects", return_value=[])
    @patch("cli.rss_trend.fetch_rss_trend", return_value="")
    def test_scheduled_rss_trend_falls_back_to_fresh_static_subject(
        self,
        fetch_rss_trend,
        find_recent_similar_subjects,
    ):
        subject = cli._select_scheduled_job_subject(
            {
                "video_subject": "Fallback topic",
                "video_subject_pool": [],
                "rss_trend_query": "personal finance",
            }
        )

        self.assertEqual(subject, "Fallback topic")
        fetch_rss_trend.assert_called_once_with("personal finance")
        find_recent_similar_subjects.assert_called_once_with("Fallback topic")

    @patch("cli.tm.generate_script")
    @patch(
        "cli.history.find_recent_similar_subjects",
        return_value=[{"subject": "Recently used"}],
    )
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_stops_when_subject_pool_is_exhausted(
        self,
        get_scheduled_job,
        find_recent_similar_subjects,
        generate_script,
    ):
        get_scheduled_job.return_value = {
            "name": "weekday-finance",
            "enabled": True,
            "video_subject": "",
            "video_subject_pool": ["Recently used"],
            "video_script": "",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "weekday-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "no_fresh_subject"', output.getvalue())
        find_recent_similar_subjects.assert_called_once_with("Recently used")
        generate_script.assert_not_called()

    def test_scheduled_static_job_skips_recent_duplicate_when_configured(self):
        output = io.StringIO()
        with (
            patch("cli.scheduled_jobs.get_scheduled_job") as get_scheduled_job,
            patch(
                "cli.history.find_recent_similar_subjects",
                return_value=[{"subject": "Why prices rise"}],
            ) as find_recent_similar_subjects,
            patch("cli.logger.warning") as logger_warning,
            patch("cli.tm.generate_script") as generate_script,
            redirect_stdout(output),
        ):
            get_scheduled_job.return_value = {
                "name": "weekday-finance",
                "enabled": True,
                "video_subject": "Why prices rise",
                "video_script": "",
                "skip_if_recent_duplicate": True,
            }
            generate_script.side_effect = AssertionError("script generation must not run")

            exit_code = cli.run_cli(["--scheduled-job", "weekday-finance"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"skipped": true', output.getvalue())
        self.assertIn('"reason": "recent_duplicate"', output.getvalue())
        find_recent_similar_subjects.assert_called_once_with("Why prices rise")
        logger_warning.assert_called_once()
        generate_script.assert_not_called()

    @patch("cli.history.add_history")
    def test_scheduled_history_records_partial_format_result(self, add_history):
        cli._record_scheduled_job_history(
            task_id="scheduled-partial-task",
            job={"name": "weekday-finance"},
            params=cli.VideoParams(
                video_subject="Why prices rise",
                video_language="tr-TR",
            ),
            result={
                "videos": ["final-1-9x16.mp4"],
                "partial_success": True,
                "failed_aspects": ["4:5"],
                "audio_duration": 28.5,
                "render_quality_reports": [{"video_path": "final-1-9x16.mp4"}],
            },
        )

        entry = add_history.call_args.args[0]
        self.assertTrue(entry["partial_success"])
        self.assertEqual(entry["failed_aspects"], ["4:5"])
        self.assertEqual(entry["language"], "tr-TR")
        self.assertEqual(entry["video_aspect"], "9:16")
        self.assertEqual(entry["video_aspects"], ["9:16"])
        self.assertEqual(entry["audio_duration"], 28.5)
        self.assertEqual(
            entry["render_quality_reports"],
            [{"video_path": "final-1-9x16.mp4"}],
        )

    @patch("cli.scheduled_job_notifications.notify_scheduled_job_attention")
    @patch("cli.history.add_history")
    def test_scheduled_history_notifies_when_result_is_partial_success(
        self, add_history, notify_scheduled_job_attention
    ):
        cli._record_scheduled_job_history(
            task_id="scheduled-partial-task",
            job={"name": "weekday-finance"},
            params=cli.VideoParams(video_subject="Why prices rise"),
            result={"partial_success": True},
        )

        add_history.assert_called_once()
        notify_scheduled_job_attention.assert_called_once_with(
            "weekday-finance", "partial_success"
        )

    @patch("cli.tm.start")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_dry_run_prints_safe_summary_without_starting_task(
        self, get_scheduled_job, start
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "Prices can rise for more than one reason.",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(
                ["--scheduled-job", "morning-finance", "--scheduled-job-dry-run"]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn('"valid": true', output.getvalue())
        self.assertIn('"name": "morning-finance"', output.getvalue())
        self.assertNotIn("Prices can rise for more than one reason.", output.getvalue())
        start.assert_not_called()

    @patch(
        "cli.cost_estimate.evaluate_monthly_cost_warning",
        return_value={
            "enabled": True,
            "warning": True,
            "threshold_usd": 0.5,
            "known_total_usd": 0.6,
            "unknown_job_count": 1,
        },
    )
    @patch("cli.history.list_history", return_value=[])
    @patch(
        "app.services.cost_estimate.estimate_history_cost",
        return_value={
            "estimated_known_total_usd": 0.0142,
            "unknown_components": ["tts"],
        },
    )
    @patch("cli.history.find_recent_similar_subjects", return_value=[])
    @patch("cli.history.add_history")
    @patch("cli.thumbnail.generate_thumbnail_candidates")
    @patch("cli.tm.start")
    @patch("cli.content_quality.evaluate_quality_gate")
    @patch("cli.content_quality.build_preflight_report")
    @patch("cli.tm.generate_script")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_runs_after_quality_check_and_records_review_queue(
        self,
        get_scheduled_job,
        generate_script,
        build_preflight_report,
        evaluate_quality_gate,
        start,
        generate_thumbnail_candidates,
        add_history,
        find_recent_similar_subjects,
        estimate_history_cost,
        list_history,
        evaluate_monthly_cost_warning,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_subject_pool": ["Why prices rise"],
            "video_script": "",
        }
        generate_script.return_value = "A generated script."
        build_preflight_report.return_value = {
            "script_analysis": {
                "overall_score": 84,
                "thumbnail_concepts": ["Bold first-frame text"],
                "thumbnail_timestamps": [0.75, 3.5],
            }
        }
        evaluate_quality_gate.return_value = {
            "enabled": True,
            "threshold": 60,
            "score": 84,
            "warn": False,
        }
        start.return_value = {
            "videos": ["/tmp/video.mp4"],
            "pending_uploads": [{"video_path": "/tmp/video.mp4"}],
            "partial_success": True,
            "failed_aspects": ["4:5", "4:5", "unsupported"],
        }
        generate_thumbnail_candidates.return_value = {
            "candidates": [{"path": "/tmp/thumbnail.jpg"}],
            "error": "",
        }

        output = io.StringIO()
        with patch.dict(
            cli.config.app,
            {
                "llm_provider": "gemini",
                "scheduled_llm_fallback_providers": ["openai", "gemini", "openai"],
            },
            clear=False,
        ):
            with redirect_stdout(output):
                exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"started": true', output.getvalue())
        self.assertNotIn("A generated script.", output.getvalue())
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["partial_success"])
        self.assertEqual(summary["failed_aspects"], ["4:5"])
        self.assertEqual(summary["estimated_known_cost_usd"], 0.0142)
        self.assertEqual(summary["cost_unknown_components"], ["tts"])
        self.assertTrue(summary["monthly_cost_warning"])
        self.assertEqual(summary["monthly_known_cost_usd"], 0.6)
        self.assertEqual(summary["monthly_cost_warning_threshold_usd"], 0.5)
        self.assertEqual(summary["monthly_cost_unknown_jobs"], 1)
        self.assertEqual(
            generate_script.call_args.kwargs["fallback_providers"],
            ["openai", "gemini", "openai"],
        )
        params = start.call_args.kwargs["params"]
        self.assertEqual(params.video_subject, "Why prices rise")
        find_recent_similar_subjects.assert_called_once_with("Why prices rise")
        self.assertEqual(params.video_script, "A generated script.")
        self.assertTrue(start.call_args.kwargs["require_upload_review"])
        self.assertEqual(start.call_args.kwargs["stop_at"], "video")
        self.assertTrue(evaluate_quality_gate.call_args.kwargs["enabled"])
        self.assertEqual(add_history.call_args.args[0]["status"], "completed")
        self.assertEqual(add_history.call_args.args[0]["scheduled_job"], "morning-finance")
        self.assertEqual(add_history.call_args.args[0]["failed_aspects"], ["4:5"])
        self.assertEqual(
            add_history.call_args.args[0]["thumbnail_candidates"],
            [{"path": "/tmp/thumbnail.jpg"}],
        )
        self.assertEqual(
            add_history.call_args.args[0]["pending_uploads"],
            [{"video_path": "/tmp/video.mp4"}],
        )
        generate_thumbnail_candidates.assert_called_once_with(
            task_id=start.call_args.kwargs["task_id"],
            video_paths=["/tmp/video.mp4"],
            thumbnail_concepts=["Bold first-frame text"],
            hook_timestamps=[0.75, 3.5],
        )
        self.assertEqual(estimate_history_cost.call_count, 2)
        self.assertEqual(list_history.call_count, 2)
        evaluate_monthly_cost_warning.assert_called_once()

    @patch("cli.history.add_history")
    @patch("cli.tm.start")
    @patch("cli.content_quality.evaluate_quality_gate")
    @patch("cli.content_quality.build_preflight_report")
    @patch("cli.tm.generate_script")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_blocks_when_quality_score_is_unavailable(
        self,
        get_scheduled_job,
        generate_script,
        build_preflight_report,
        evaluate_quality_gate,
        start,
        add_history,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "",
        }
        generate_script.return_value = "A generated script."
        build_preflight_report.return_value = {"script_analysis": {}}
        evaluate_quality_gate.return_value = {
            "enabled": True,
            "threshold": 60,
            "score": None,
            "warn": False,
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "quality_gate"', output.getvalue())
        start.assert_not_called()
        self.assertEqual(add_history.call_args.args[0]["status"], "blocked")

    @patch("cli.history.add_history")
    @patch("cli.tm.start")
    @patch(
        "cli.content_quality.build_preflight_report",
        side_effect=RuntimeError("secret-api-key"),
    )
    @patch("cli.tm.generate_script", return_value="A generated script.")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_fails_safely_when_quality_preflight_raises(
        self,
        get_scheduled_job,
        generate_script,
        build_preflight_report,
        start,
        add_history,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "quality_preflight"', output.getvalue())
        self.assertNotIn("secret-api-key", output.getvalue())
        start.assert_not_called()
        self.assertEqual(add_history.call_args.args[0]["status"], "failed")

    @patch("cli.history.add_history")
    @patch("cli.tm.start", side_effect=RuntimeError("upload-api-key"))
    @patch(
        "app.services.state.state.get_task",
        return_value={"failed_aspects": ["4:5", "4:5"]},
    )
    @patch(
        "cli.content_quality.evaluate_quality_gate",
        return_value={"enabled": True, "threshold": 60, "score": 84, "warn": False},
    )
    @patch(
        "cli.content_quality.build_preflight_report",
        return_value={"script_analysis": {"overall_score": 84}},
    )
    @patch("cli.tm.generate_script", return_value="A generated script.")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_fails_safely_when_generation_raises(
        self,
        get_scheduled_job,
        generate_script,
        build_preflight_report,
        evaluate_quality_gate,
        get_task,
        start,
        add_history,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "generation"', output.getvalue())
        self.assertNotIn("upload-api-key", output.getvalue())
        self.assertEqual(add_history.call_args.args[0]["status"], "failed")
        self.assertEqual(add_history.call_args.args[0]["failed_aspects"], ["4:5"])
        get_task.assert_called_once()

    @patch("cli.history.add_history")
    @patch("cli.tm.start", return_value=None)
    @patch(
        "app.services.state.state.get_task",
        return_value={"failed_aspects": ["4:5", "4:5"]},
    )
    @patch(
        "cli.content_quality.evaluate_quality_gate",
        return_value={"enabled": True, "threshold": 60, "score": 84, "warn": False},
    )
    @patch(
        "cli.content_quality.build_preflight_report",
        return_value={"script_analysis": {"overall_score": 84}},
    )
    @patch("cli.tm.generate_script", return_value="A generated script.")
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_records_failed_formats_when_generation_returns_no_result(
        self,
        get_scheduled_job,
        generate_script,
        build_preflight_report,
        evaluate_quality_gate,
        get_task,
        start,
        add_history,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "generation"', output.getvalue())
        self.assertEqual(add_history.call_args.args[0]["status"], "failed")
        self.assertEqual(add_history.call_args.args[0]["failed_aspects"], ["4:5"])

    @patch("cli.history.add_history")
    @patch("cli.tm.generate_script", side_effect=RuntimeError("llm-api-key"))
    @patch("cli.scheduled_jobs.get_scheduled_job")
    def test_scheduled_job_fails_safely_when_script_generation_raises(
        self,
        get_scheduled_job,
        generate_script,
        add_history,
    ):
        get_scheduled_job.return_value = {
            "name": "morning-finance",
            "enabled": True,
            "video_subject": "Why prices rise",
            "video_script": "",
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.run_cli(["--scheduled-job", "morning-finance"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"reason": "script_generation"', output.getvalue())
        self.assertNotIn("llm-api-key", output.getvalue())
        self.assertEqual(add_history.call_args.args[0]["status"], "failed")


class TestTaskResumeCommand(unittest.TestCase):
    def test_parse_args_accepts_resume_task_without_video_subject(self):
        args = cli.parse_args(["--resume-task", "interrupted-task"])

        self.assertEqual(args.resume_task, "interrupted-task")
        self.assertEqual(args.video_subject, "")

    @patch("cli.tm.resume_interrupted_task", return_value={"videos": ["final.mp4"]})
    def test_run_cli_resumes_task_without_printing_task_artifacts(self, resume):
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--resume-task", "interrupted-task"])

        self.assertEqual(exit_code, 0)
        resume.assert_called_once_with("interrupted-task")
        self.assertEqual(
            json.loads(output.getvalue()),
            {"resumed": True, "task_id": "interrupted-task"},
        )


class TestOpenMontageValidationCommand(unittest.TestCase):
    def test_parse_args_accepts_openmontage_validation_without_video_subject(self):
        args = cli.parse_args(["--validate-openmontage"])

        self.assertTrue(args.validate_openmontage)
        self.assertEqual(args.video_subject, "")

    @patch("cli.openmontage_materials.validate_openmontage_library")
    def test_run_cli_prints_read_only_openmontage_validation(self, validate):
        validate.return_value = {
            "valid": True,
            "project_count": 1,
            "projects": [],
            "issues": [],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli.run_cli(["--validate-openmontage"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["project_count"], 1)
        validate.assert_called_once_with()
