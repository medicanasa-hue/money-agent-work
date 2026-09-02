import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import repurpose


class TestShortClipPlanning(unittest.TestCase):
    def test_plan_short_clips_spreads_non_overlapping_windows(self):
        plan = repurpose.plan_short_clips(
            95,
            clip_duration_seconds=30,
            clip_count=3,
        )

        self.assertEqual(plan["clip_count"], 3)
        self.assertEqual(
            plan["clips"],
            [
                {"index": 1, "start_seconds": 0.0, "duration_seconds": 30.0},
                {"index": 2, "start_seconds": 32.5, "duration_seconds": 30.0},
                {"index": 3, "start_seconds": 65.0, "duration_seconds": 30.0},
            ],
        )

    def test_plan_short_clips_reduces_count_when_source_is_too_short(self):
        plan = repurpose.plan_short_clips(
            65,
            clip_duration_seconds=30,
            clip_count=3,
        )

        self.assertEqual(plan["requested_clip_count"], 3)
        self.assertEqual(plan["clip_count"], 2)
        self.assertEqual(
            plan["clips"],
            [
                {"index": 1, "start_seconds": 0.0, "duration_seconds": 30.0},
                {"index": 2, "start_seconds": 35.0, "duration_seconds": 30.0},
            ],
        )

    def test_plan_short_clips_rejects_unusable_durations(self):
        self.assertIsNone(
            repurpose.plan_short_clips(
                None,
                clip_duration_seconds=30,
                clip_count=3,
            )
        )
        self.assertIsNone(
            repurpose.plan_short_clips(
                60,
                clip_duration_seconds=0,
                clip_count=3,
            )
        )


class TestSubtitleGuidedShortClipPlanning(unittest.TestCase):
    def test_plan_subtitle_guided_short_clips_prefers_high_signal_segments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle_path = Path(temp_dir) / "subtitle.srt"
            subtitle_path.write_text(
                """1
00:00:05,000 --> 00:00:08,000
Welcome to this weekly market update.

2
00:00:25,000 --> 00:00:29,000
Why does inflation make your savings lose value?

3
00:00:55,000 --> 00:00:59,000
Bu hata paranizi enflasyon karsisinda eritir!

4
00:01:15,000 --> 00:01:18,000
We will close with a short summary.
""",
                encoding="utf-8",
            )

            plan = repurpose.plan_subtitle_guided_short_clips(
                100,
                clip_duration_seconds=20,
                clip_count=2,
                subtitle_path=str(subtitle_path),
            )

        self.assertEqual(plan["selection_mode"], "subtitle")
        self.assertEqual(plan["subtitle_segment_count"], 4)
        self.assertEqual(
            [clip["start_seconds"] for clip in plan["clips"]],
            [25.0, 55.0],
        )

    def test_plan_subtitle_guided_short_clips_preserves_balanced_plan_without_srt(
        self,
    ):
        plan = repurpose.plan_subtitle_guided_short_clips(
            100,
            clip_duration_seconds=20,
            clip_count=2,
            subtitle_path="C:/tmp/missing-subtitle.srt",
        )

        self.assertEqual(plan["selection_mode"], "balanced")
        self.assertEqual(
            [clip["start_seconds"] for clip in plan["clips"]],
            [0.0, 80.0],
        )

    def test_subtitle_guided_short_clip_quality_cases_choose_strong_bilingual_hooks(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle_path = Path(temp_dir) / "quality-cases.srt"
            subtitle_path.write_text(
                """1
00:00:05,000 --> 00:00:08,000
Here is a routine introduction to the topic.

2
00:00:25,000 --> 00:00:29,000
Why does inflation quietly destroy your savings?

3
00:00:45,000 --> 00:00:48,000
We will now move to the next point.

4
00:01:05,000 --> 00:01:09,000
Bu hata paranizi enflasyon karsisinda eritir!

5
00:01:25,000 --> 00:01:29,000
3 money mistakes you should never repeat.
""",
                encoding="utf-8",
            )

            plan = repurpose.plan_subtitle_guided_short_clips(
                120,
                clip_duration_seconds=20,
                clip_count=3,
                subtitle_path=str(subtitle_path),
            )

        self.assertEqual(plan["selection_mode"], "subtitle")
        self.assertEqual(
            [clip["start_seconds"] for clip in plan["clips"]],
            [25.0, 65.0, 85.0],
        )


class TestShortClipRendering(unittest.TestCase):
    @patch("app.services.repurpose.utils.get_ffmpeg_binary", return_value="ffmpeg")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_uses_non_overwriting_fast_copy(
        self, run, get_ffmpeg_binary
    ):
        run.return_value = Mock(returncode=0)
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 32.5, "duration_seconds": 30.0}],
            )

            output_file = str(Path(output_dir) / "short_clip_01.mp4")

        self.assertEqual(result, {"rendered_clip_count": 1, "error_count": 0})
        get_ffmpeg_binary.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-ss",
                "32.5",
                "-i",
                "C:/tmp/source.mp4",
                "-t",
                "30.0",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_file,
            ],
        )

    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_does_not_overwrite_existing_output(self, run):
        with tempfile.TemporaryDirectory() as output_dir:
            (Path(output_dir) / "short_clip_01.mp4").touch()
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 0, "duration_seconds": 30}],
            )

        self.assertEqual(result, {"rendered_clip_count": 0, "error_count": 1})
        run.assert_not_called()

    @patch("app.services.repurpose.utils.get_ffmpeg_binary", return_value="ffmpeg")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_uses_precise_reencode_when_requested(
        self, run, get_ffmpeg_binary
    ):
        run.return_value = Mock(returncode=0)
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 32.5, "duration_seconds": 30.0}],
                render_mode="precise",
            )

            output_file = str(Path(output_dir) / "short_clip_01.mp4")

        self.assertEqual(result, {"rendered_clip_count": 1, "error_count": 0})
        get_ffmpeg_binary.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-i",
                "C:/tmp/source.mp4",
                "-ss",
                "32.5",
                "-t",
                "30.0",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                output_file,
            ],
        )

    @patch("app.services.repurpose.utils.get_ffmpeg_binary", return_value="ffmpeg")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_uses_requested_hardware_codec_in_precise_mode(
        self, run, get_ffmpeg_binary
    ):
        run.return_value = Mock(returncode=0)
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 0, "duration_seconds": 30}],
                render_mode="precise",
                video_codec="h264_amf",
            )

            output_file = str(Path(output_dir) / "short_clip_01.mp4")

        self.assertEqual(result, {"rendered_clip_count": 1, "error_count": 0})
        get_ffmpeg_binary.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-i",
                "C:/tmp/source.mp4",
                "-ss",
                "0.0",
                "-t",
                "30.0",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "h264_amf",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                output_file,
            ],
        )

    @patch("app.services.repurpose.utils.get_ffmpeg_binary", return_value="ffmpeg")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_reframes_precise_output_for_portrait(
        self, run, get_ffmpeg_binary
    ):
        run.return_value = Mock(returncode=0)
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 0, "duration_seconds": 30}],
                render_mode="precise",
                target_aspect="9:16",
            )

            output_file = str(Path(output_dir) / "short_clip_01.mp4")

        self.assertEqual(result, {"rendered_clip_count": 1, "error_count": 0})
        get_ffmpeg_binary.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-vf") + 1], "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920")
        self.assertEqual(command[-1], output_file)

    @patch("app.services.repurpose.utils.get_ffmpeg_binary")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_rejects_portrait_reframe_in_fast_mode(
        self, run, get_ffmpeg_binary
    ):
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 0, "duration_seconds": 30}],
                target_aspect="9:16",
            )

        self.assertEqual(result, {"rendered_clip_count": 0, "error_count": 1})
        get_ffmpeg_binary.assert_not_called()
        run.assert_not_called()

    @patch("app.services.repurpose.utils.get_ffmpeg_binary")
    @patch("app.services.repurpose.subprocess.run")
    def test_render_short_clips_rejects_unsupported_codec_in_precise_mode(
        self, run, get_ffmpeg_binary
    ):
        with tempfile.TemporaryDirectory() as output_dir:
            result = repurpose.render_short_clips(
                "C:/tmp/source.mp4",
                output_dir,
                [{"index": 1, "start_seconds": 0, "duration_seconds": 30}],
                render_mode="precise",
                video_codec="not-a-codec",
            )

        self.assertEqual(result, {"rendered_clip_count": 0, "error_count": 1})
        get_ffmpeg_binary.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
