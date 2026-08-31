import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from app.models.schema import VideoAspect
from app.services import render_quality


_EXPECTED_ENCODING_CONTRACT = {
    "codec": "h264",
    "pixel_format": "yuv420p",
    "fps": 30.0,
    "max_keyframe_gap_seconds": 2.0,
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "color_range": "tv",
    "sample_aspect_ratio": "1:1",
}


class _FakeFrame:
    def __init__(self, luma):
        self._luma = luma

    def mean(self):
        return self._luma


class _FakeAudio:
    def __init__(self, peak=0.1):
        self.peak = peak
        self.duration = 10.0

    def to_soundarray(self, tt=None, **_kwargs):
        sample_count = len(tt) if tt is not None else 0
        return [[self.peak, -self.peak] for _ in range(sample_count)]

    def get_frame(self, _time):
        return [self.peak, -self.peak]


class _VectorSamplingBugAudio(_FakeAudio):
    def to_soundarray(self, tt=None, **_kwargs):
        sample_count = len(tt) if tt is not None else 0
        return [[0.0001, -0.0001] for _ in range(sample_count)]


class _FakeClip:
    def __init__(
        self,
        *,
        size=(1080, 1920),
        duration=10.0,
        fps=30,
        audio=True,
        audio_peak=0.1,
        luma=80,
    ):
        self.size = size
        self.duration = duration
        self.fps = fps
        self.audio = _FakeAudio(audio_peak) if audio else None
        self._luma = luma
        self.closed = False

    def get_frame(self, _time):
        return _FakeFrame(self._luma)

    def close(self):
        self.closed = True


class TestRenderQuality(unittest.TestCase):
    def test_build_visual_sharpness_report_summarizes_edge_detail(self):
        flat_frame = np.full((16, 16, 3), 120, dtype=np.uint8)
        detailed_frame = np.indices((16, 16)).sum(axis=0) % 2
        detailed_frame = (detailed_frame * 255).astype(np.uint8)
        detailed_frame = np.stack([detailed_frame] * 3, axis=-1)

        flat_report = render_quality.build_visual_sharpness_report([flat_frame])
        detailed_report = render_quality.build_visual_sharpness_report([detailed_frame])

        self.assertEqual(flat_report["sample_count"], 1)
        self.assertEqual(detailed_report["sample_count"], 1)
        self.assertEqual(flat_report["mean_laplacian_variance"], 0.0)
        self.assertGreater(
            detailed_report["mean_laplacian_variance"],
            flat_report["mean_laplacian_variance"],
        )

    def test_build_color_consistency_report_flags_mixed_color_character(self):
        cool_frame = np.full((8, 8, 3), [60, 110, 190], dtype=np.uint8)
        warm_frame = np.full((8, 8, 3), [210, 100, 50], dtype=np.uint8)

        report = render_quality.build_color_consistency_report(
            [cool_frame, warm_frame]
        )

        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["status"], "mixed")
        self.assertGreater(report["warmth_spread"], 0.5)

    def test_detect_sustained_near_black_segments_uses_shared_helper(self):
        expected_segments = [(5.1, 5.7)]

        with patch.object(
            render_quality.video_quality,
            "detect_sustained_near_black_segments",
            return_value=expected_segments,
        ) as detect:
            result = render_quality._detect_sustained_near_black_segments("final.mp4")

        self.assertEqual(result, expected_segments)
        detect.assert_called_once_with(
            "final.mp4",
            min_duration_seconds=render_quality._BLACKDETECT_MIN_DURATION_SECONDS,
            pixel_threshold=render_quality._BLACKDETECT_PIXEL_THRESHOLD,
            timeout_seconds=render_quality._BLACKDETECT_TIMEOUT_SECONDS,
        )

    def test_detect_sustained_frozen_segments_uses_shared_helper(self):
        expected_segments = [(5.1, 7.0)]

        with patch.object(
            render_quality.video_quality,
            "detect_sustained_frozen_segments",
            return_value=expected_segments,
        ) as detect:
            result = render_quality._detect_sustained_frozen_segments("final.mp4")

        self.assertEqual(result, expected_segments)
        detect.assert_called_once_with(
            "final.mp4",
            min_duration_seconds=render_quality._FREEZEDETECT_MIN_DURATION_SECONDS,
            noise_tolerance=render_quality._FREEZEDETECT_NOISE_TOLERANCE,
            timeout_seconds=render_quality._FREEZEDETECT_TIMEOUT_SECONDS,
        )

    def test_is_near_black_frame_allows_dark_frame_with_visible_detail(self):
        dark_but_visible_frame = np.zeros((20, 20, 3), dtype=np.uint8)
        dark_but_visible_frame[:1, :, :] = 255

        self.assertFalse(render_quality._is_near_black_frame(dark_but_visible_frame))

    def test_inspect_rendered_video_reports_healthy_expected_output(self):
        clip = _FakeClip()

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_aspect=VideoAspect.portrait,
                expected_duration=10,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["resolution"], [1080, 1920])
        self.assertEqual(report["expected_duration"], 10.0)
        self.assertEqual(report["duration_delta"], 0.0)
        self.assertTrue(report["has_audio"])
        self.assertEqual(report["sampled_audio_peak"], 0.1)
        self.assertEqual(report["warnings"], [])
        self.assertTrue(clip.closed)

    def test_inspect_rendered_video_allows_intentionally_silent_audio(self):
        clip = _FakeClip(audio_peak=0.0)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                allow_silent_audio=True,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["sampled_audio_peak"], 0.0)
        self.assertNotIn("sampled audio is near-silent", report["warnings"])

    def test_inspect_rendered_video_warns_when_no_visual_frame_can_be_sampled(self):
        class _UnreadableVideoClip(_FakeClip):
            def get_frame(self, _time):
                raise OSError("frame decode failed")

        clip = _UnreadableVideoClip()
        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "_detect_sustained_near_black_segments",
                return_value=None,
            ),
            patch.object(
                render_quality,
                "_detect_sustained_frozen_segments",
                return_value=None,
            ),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["visual_sharpness"]["sample_count"], 0)
        self.assertIn("rendered video frames could not be sampled", report["warnings"])

    def test_inspect_rendered_video_includes_advisory_color_consistency(self):
        clip = _FakeClip()
        color_report = {"sample_count": 3, "status": "mixed"}

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "build_color_consistency_report",
                return_value=color_report,
            ) as build_report,
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertEqual(report["color_consistency"], color_report)
        build_report.assert_called_once()

    def test_inspect_rendered_video_includes_advisory_visual_sharpness(self):
        clip = _FakeClip()
        sharpness_report = {
            "sample_count": 3,
            "mean_laplacian_variance": 42.5,
            "minimum_laplacian_variance": 10.0,
            "maximum_laplacian_variance": 80.0,
        }

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "build_visual_sharpness_report",
                return_value=sharpness_report,
            ) as build_report,
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertEqual(report["visual_sharpness"], sharpness_report)
        build_report.assert_called_once()

    def test_inspect_rendered_video_validates_expected_encoding_contract(self):
        clip = _FakeClip()

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "_probe_video_encoding",
                return_value=_EXPECTED_ENCODING_CONTRACT,
                create=True,
            ),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_encoding=_EXPECTED_ENCODING_CONTRACT,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["encoding"], _EXPECTED_ENCODING_CONTRACT)

    def test_video_stream_matches_encoding_contract_fails_closed_on_unknown_or_mismatched_stream(self):
        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value=_EXPECTED_ENCODING_CONTRACT,
        ):
            self.assertTrue(
                render_quality.video_stream_matches_encoding_contract(
                    "final.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value=None,
        ):
            self.assertFalse(
                render_quality.video_stream_matches_encoding_contract(
                    "final.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value={**_EXPECTED_ENCODING_CONTRACT, "color_space": "unknown"},
        ):
            self.assertFalse(
                render_quality.video_stream_matches_encoding_contract(
                    "final.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value={**_EXPECTED_ENCODING_CONTRACT, "color_range": "unknown"},
        ):
            self.assertFalse(
                render_quality.video_stream_matches_encoding_contract(
                    "final.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

    def test_video_stream_matches_encoding_contract_rejects_unverified_long_keyframe_gap(self):
        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value={
                **_EXPECTED_ENCODING_CONTRACT,
                "max_keyframe_gap_seconds": None,
                "duration": 10.0,
            },
        ):
            self.assertFalse(
                render_quality.video_stream_matches_encoding_contract(
                    "final.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

        with patch.object(
            render_quality,
            "_probe_video_encoding",
            return_value={
                **_EXPECTED_ENCODING_CONTRACT,
                "max_keyframe_gap_seconds": None,
                "duration": 1.0,
            },
        ):
            self.assertTrue(
                render_quality.video_stream_matches_encoding_contract(
                    "short.mp4",
                    _EXPECTED_ENCODING_CONTRACT,
                )
            )

    def test_inspect_rendered_video_warns_when_encoding_contract_does_not_match(self):
        clip = _FakeClip()
        actual_encoding = {
            **_EXPECTED_ENCODING_CONTRACT,
            "codec": "hevc",
            "fps": 24.0,
            "max_keyframe_gap_seconds": 3.0,
            "color_space": "unknown",
            "color_range": "unknown",
            "sample_aspect_ratio": "4:3",
        }

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "_probe_video_encoding",
                return_value=actual_encoding,
                create=True,
            ),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_encoding=_EXPECTED_ENCODING_CONTRACT,
            )

        self.assertFalse(report["ok"])
        self.assertIn("video codec does not match the encoding contract", report["warnings"])
        self.assertIn("video frame rate does not match the encoding contract", report["warnings"])
        self.assertIn("video keyframe interval exceeds the encoding contract", report["warnings"])
        self.assertIn("video color space does not match the encoding contract", report["warnings"])
        self.assertIn("video color range does not match the encoding contract", report["warnings"])
        self.assertIn(
            "video sample aspect ratio does not match the encoding contract",
            report["warnings"],
        )

    def test_probe_video_encoding_reads_stream_metadata_and_keyframe_gap(self):
        ffprobe_output = {
            "streams": [
                {
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                    "color_primaries": "bt709",
                    "color_range": "tv",
                    "sample_aspect_ratio": "1:1",
                    "duration": "4.004000",
                }
            ],
            "frames": [
                {"best_effort_timestamp_time": "0.000000"},
                {"best_effort_timestamp_time": "2.002000"},
                {"best_effort_timestamp_time": "4.004000"},
            ],
        }

        with (
            patch.object(
                render_quality,
                "_get_ffprobe_binary",
                return_value="ffprobe",
                create=True,
            ),
            patch.object(
                render_quality.subprocess,
                "run",
                return_value=types.SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(ffprobe_output),
                    stderr="",
                ),
                create=True,
            ) as run,
        ):
            encoding = render_quality._probe_video_encoding("final.mp4")

        self.assertEqual(encoding["codec"], "h264")
        self.assertEqual(encoding["pixel_format"], "yuv420p")
        self.assertAlmostEqual(encoding["fps"], 30000 / 1001)
        self.assertAlmostEqual(encoding["max_keyframe_gap_seconds"], 2.002)
        self.assertEqual(encoding["color_range"], "tv")
        self.assertEqual(encoding["sample_aspect_ratio"], "1:1")
        self.assertAlmostEqual(encoding["duration"], 4.004)
        command = run.call_args.args[0]
        self.assertIn("-skip_frame", command)
        self.assertIn("nokey", command)
        self.assertIn("color_range", command[command.index("-show_entries") + 1])

    def test_vmaf_capability_detects_available_ffmpeg_filter(self):
        with (
            patch.object(render_quality.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
            patch.object(
                render_quality.subprocess,
                "run",
                return_value=types.SimpleNamespace(
                    returncode=0,
                    stdout=" .. libvmaf           VV->V      Calculate VMAF\n",
                    stderr="",
                ),
            ),
        ):
            self.assertTrue(render_quality.is_vmaf_available())

    def test_calculate_vmaf_runs_candidate_against_reference_and_reads_log(self):
        vmaf_result = {
            "mean": 96.5,
            "minimum": 92.0,
            "maximum": 99.0,
            "harmonic_mean": 96.2,
            "sampled_frame_count": 24,
        }
        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "is_vmaf_available", return_value=True, create=True),
            patch.object(
                render_quality.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run,
            patch.object(render_quality, "_read_vmaf_log", return_value=vmaf_result, create=True),
        ):
            result = render_quality.calculate_vmaf(
                "reference.mp4",
                "candidate.mp4",
                frame_subsample=2,
            )

        self.assertEqual(result, vmaf_result)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-i") + 1], "candidate.mp4")
        self.assertEqual(command[command.index("-i", command.index("-i") + 1) + 1], "reference.mp4")
        self.assertIn("libvmaf=", command[command.index("-filter_complex") + 1])
        self.assertIn("n_subsample=2", command[command.index("-filter_complex") + 1])

    def test_read_vmaf_log_returns_pooled_metrics(self):
        payload = {
            "pooled_metrics": {
                "vmaf": {
                    "mean": 96.5,
                    "min": 92.0,
                    "max": 99.0,
                    "harmonic_mean": 96.2,
                }
            },
            "frames": [{"frameNum": 0}, {"frameNum": 1}],
        }

        with patch.object(render_quality, "_load_json_file", return_value=payload, create=True):
            result = render_quality._read_vmaf_log("vmaf.json")

        self.assertEqual(
            result,
            {
                "mean": 96.5,
                "minimum": 92.0,
                "maximum": 99.0,
                "harmonic_mean": 96.2,
                "sampled_frame_count": 2,
            },
        )

    def test_build_visual_regression_gallery_saves_comparable_frames_and_html(self):
        class _GalleryClip(_FakeClip):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.saved_frames = []

            def save_frame(self, frame_path, t):
                self.saved_frames.append((frame_path, t))
                Path(frame_path).write_bytes(b"frame")

        clips = [_GalleryClip(duration=10), _GalleryClip(duration=8)]
        with tempfile.TemporaryDirectory() as temp_dir:
            first_video = Path(temp_dir) / "first video.mp4"
            second_video = Path(temp_dir) / "second.mp4"
            first_video.touch()
            second_video.touch()
            gallery_dir = Path(temp_dir) / "gallery"

            with patch.object(render_quality, "VideoFileClip", side_effect=clips):
                gallery = render_quality.build_visual_regression_gallery(
                    [str(first_video), str(second_video)],
                    str(gallery_dir),
                )

            html_path = Path(gallery["html_path"])
            self.assertTrue(html_path.is_file())
            self.assertEqual(gallery["video_count"], 2)
            self.assertEqual(len(gallery["frame_paths"]), 6)
            self.assertTrue(all(Path(frame_path).is_file() for frame_path in gallery["frame_paths"]))
            self.assertEqual([saved[1] for saved in clips[0].saved_frames], [2.0, 5.0, 8.0])
            self.assertEqual([saved[1] for saved in clips[1].saved_frames], [1.6, 4.0, 6.4])
            self.assertIn("first video.mp4", html_path.read_text(encoding="utf-8"))
            self.assertTrue(all(clip.closed for clip in clips))

    def test_create_subtitle_safe_zone_snapshot_marks_rendered_portrait_frame(self):
        class _SnapshotClip:
            size = (100, 200)
            duration = 10.0

            def __init__(self):
                self.closed = False

            def get_frame(self, _time):
                return np.zeros((200, 100, 3), dtype=np.uint8)

            def close(self):
                self.closed = True

        clip = _SnapshotClip()
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            output_path = Path(temp_dir) / "safe-zone.png"
            video_path.touch()

            with patch.object(render_quality, "VideoFileClip", return_value=clip):
                snapshot = render_quality.create_subtitle_safe_zone_snapshot(
                    str(video_path),
                    str(output_path),
                    video_aspect=VideoAspect.portrait,
                )

            self.assertEqual(snapshot["snapshot_path"], str(output_path))
            self.assertEqual(snapshot["sample_time"], 5.0)
            self.assertEqual(snapshot["safe_bottom_margin_ratio"], 0.16)
            self.assertEqual(snapshot["safe_zone_top"], 168)
            self.assertTrue(output_path.is_file())
            with Image.open(output_path) as image:
                self.assertEqual(image.getpixel((10, 100)), (0, 0, 0))
                self.assertNotEqual(image.getpixel((10, 190)), (0, 0, 0))
        self.assertTrue(clip.closed)

    def test_inspect_rendered_video_allows_small_duration_difference(self):
        clip = _FakeClip(duration=10.8)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_duration=10,
            )

        self.assertTrue(report["ok"])
        self.assertAlmostEqual(report["duration_delta"], 0.8)
        self.assertEqual(report["warnings"], [])

    def test_inspect_rendered_video_warns_when_duration_differs_from_audio(self):
        clip = _FakeClip(duration=12.1)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_duration=10,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["expected_duration"], 10.0)
        self.assertAlmostEqual(report["duration_delta"], 2.1)
        self.assertIn(
            "video duration differs from expected audio duration",
            report["warnings"],
        )

    def test_inspect_rendered_video_warns_about_missing_audio_and_dark_frames(self):
        clip = _FakeClip(audio=False, luma=0)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video(
                "final.mp4",
                expected_aspect=VideoAspect.portrait,
            )

        self.assertFalse(report["ok"])
        self.assertIn("audio track is missing", report["warnings"])
        self.assertIn("sampled frames are near-black", report["warnings"])
        expected_sample_times = list(
            render_quality._render_quality_frame_sample_times(clip.duration)
        )
        self.assertEqual(report["near_black_frame_count"], len(expected_sample_times))
        self.assertEqual(report["near_black_sample_times"], expected_sample_times)

    def test_inspect_rendered_video_warns_when_captions_cover_a_black_visual(self):
        clip = _FakeClip()
        caption_over_black_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        caption_over_black_frame[82:94, 5:95] = 255
        clip.get_frame = lambda _time: caption_over_black_frame

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["near_black_frame_count"], 0)
        self.assertEqual(
            report["caption_over_black_frame_count"],
            len(render_quality._render_quality_frame_sample_times(clip.duration)),
        )
        self.assertIn(
            "sampled frames appear to contain captions over a black visual",
            report["warnings"],
        )

    def test_inspect_rendered_video_flags_an_isolated_near_black_sample(self):
        clip = _FakeClip()
        dark_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        normal_frame = np.full((100, 100, 3), 80, dtype=np.uint8)
        first_sample_time = render_quality._render_quality_frame_sample_times(
            clip.duration
        )[0]
        clip.get_frame = lambda time: (
            dark_frame if time == first_sample_time else normal_frame
        )

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["near_black_frame_count"], 1)
        self.assertEqual(report["near_black_sample_times"], [first_sample_time])
        self.assertIn("some sampled frames are near-black", report["warnings"])

    def test_inspect_rendered_video_detects_black_gap_between_legacy_samples(self):
        clip = _FakeClip()
        dark_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        normal_frame = np.full((100, 100, 3), 80, dtype=np.uint8)
        clip.get_frame = lambda time: (
            dark_frame if 5.1 <= time < 5.7 else normal_frame
        )

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertGreater(report["near_black_frame_count"], 0)
        self.assertTrue(
            any(5.1 <= time < 5.7 for time in report["near_black_sample_times"])
        )

    def test_inspect_rendered_video_uses_sustained_black_segments_when_available(self):
        clip = _FakeClip()

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "_detect_sustained_near_black_segments",
                return_value=[(5.1, 5.7)],
                create=True,
            ),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["near_black_frame_count"], 1)
        self.assertEqual(report["near_black_sample_times"], [5.1])
        self.assertIn("some sampled frames are near-black", report["warnings"])

    def test_inspect_rendered_video_warns_about_sustained_frozen_visual(self):
        clip = _FakeClip()

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
            patch.object(
                render_quality,
                "_detect_sustained_frozen_segments",
                return_value=[(5.1, 7.0)],
                create=True,
            ),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["frozen_segment_count"], 1)
        self.assertEqual(report["frozen_segment_start_times"], [5.1])
        self.assertIn("video contains a sustained frozen visual", report["warnings"])

    def test_inspect_rendered_video_flags_an_isolated_caption_over_black_sample(self):
        clip = _FakeClip()
        caption_over_black_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        caption_over_black_frame[82:94, 5:95] = 255
        normal_frame = np.full((100, 100, 3), 80, dtype=np.uint8)
        first_sample_time = render_quality._render_quality_frame_sample_times(
            clip.duration
        )[0]
        clip.get_frame = lambda time: (
            caption_over_black_frame if time == first_sample_time else normal_frame
        )

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["caption_over_black_frame_count"], 1)
        self.assertEqual(
            report["caption_over_black_sample_times"],
            [first_sample_time],
        )
        self.assertIn(
            "some sampled frames appear to contain captions over a black visual",
            report["warnings"],
        )

    def test_inspect_rendered_video_allows_a_black_subtitle_panel_on_a_visible_scene(self):
        clip = _FakeClip()
        visible_scene = np.full((100, 100, 3), 80, dtype=np.uint8)
        visible_scene[82:97, :] = 0
        visible_scene[84:92, 5:95] = 255
        clip.get_frame = lambda _time: visible_scene

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertTrue(report["ok"])
        self.assertEqual(report["caption_over_black_frame_count"], 0)
        self.assertEqual(report["warnings"], [])

    def test_inspect_rendered_video_warns_when_audio_track_is_near_silent(self):
        clip = _FakeClip(audio_peak=0.0001)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertAlmostEqual(report["sampled_audio_peak"], 0.0001)
        self.assertIn("sampled audio is near-silent", report["warnings"])

    def test_inspect_rendered_video_avoids_moviepy_vector_audio_sampling_bug(self):
        clip = _FakeClip()
        clip.audio = _VectorSamplingBugAudio(peak=0.2)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertTrue(report["ok"])
        self.assertAlmostEqual(report["sampled_audio_peak"], 0.2)
        self.assertNotIn("sampled audio is near-silent", report["warnings"])

    def test_inspect_rendered_video_warns_when_audio_is_effectively_silent(self):
        clip = _FakeClip(audio_peak=0.0025)

        with (
            patch.object(render_quality.os.path, "isfile", return_value=True),
            patch.object(render_quality, "VideoFileClip", return_value=clip),
        ):
            report = render_quality.inspect_rendered_video("final.mp4")

        self.assertFalse(report["ok"])
        self.assertAlmostEqual(report["sampled_audio_peak"], 0.0025)
        self.assertIn("sampled audio is near-silent", report["warnings"])

    def test_inspect_rendered_video_returns_report_for_missing_file(self):
        with patch.object(render_quality.os.path, "isfile", return_value=False):
            report = render_quality.inspect_rendered_video("missing.mp4")

        self.assertFalse(report["ok"])
        self.assertEqual(report["warnings"], ["rendered video file is missing"])

    def test_inspect_rendered_video_returns_report_for_missing_path(self):
        report = render_quality.inspect_rendered_video(None)

        self.assertFalse(report["ok"])
        self.assertEqual(report["warnings"], ["rendered video file is missing"])

    def test_build_task_visual_review_package_collects_gallery_reports_and_safe_zones(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "tasks" / "review-task"
            task_dir.mkdir(parents=True)
            first_video = task_dir / "final-1.mp4"
            second_video = task_dir / "final-1-4x5.mp4"
            first_video.touch()
            second_video.touch()
            gallery = {
                "html_path": str(task_dir / "visual-review" / "frames" / "index.html"),
                "frame_paths": ["frame.jpg"],
                "video_count": 2,
            }

            with (
                patch.object(render_quality.utils, "storage_dir", return_value=temp_dir),
                patch.object(
                    render_quality,
                    "build_visual_regression_gallery",
                    return_value=gallery,
                ) as build_gallery,
                patch.object(
                    render_quality,
                    "inspect_rendered_video",
                    side_effect=[
                        {"ok": True, "resolution": [1080, 1920]},
                        {"ok": True, "resolution": [1080, 1350]},
                    ],
                ),
                patch.object(
                    render_quality,
                    "create_subtitle_safe_zone_snapshot",
                    side_effect=[
                        {"snapshot_path": "portrait.png"},
                        {"snapshot_path": "four-by-five.png"},
                    ],
                ) as create_snapshot,
            ):
                package = render_quality.build_task_visual_review_package("review-task")

            manifest = json.loads(
                Path(package["manifest_path"]).read_text(encoding="utf-8")
            )

        self.assertTrue(package["ok"])
        self.assertEqual(package["video_paths"], [str(first_video), str(second_video)])
        self.assertEqual(package["gallery"], gallery)
        self.assertEqual(len(package["quality_reports"]), 2)
        self.assertEqual(len(package["safe_zone_snapshots"]), 2)
        self.assertEqual(manifest["task_id"], "review-task")
        build_gallery.assert_called_once()
        self.assertEqual(create_snapshot.call_count, 2)

    def test_build_task_visual_review_uses_saved_render_expectations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "tasks" / "review-expectations-task"
            task_dir.mkdir(parents=True)
            video_path = task_dir / "final-1-9x16.mp4"
            video_path.touch()
            (task_dir / "script.json").write_text(
                json.dumps({"params": {"video_aspect": "9:16"}}),
                encoding="utf-8",
            )

            with (
                patch.object(render_quality.utils, "storage_dir", return_value=temp_dir),
                patch.object(render_quality, "sm", create=True) as state_module,
                patch.object(
                    render_quality,
                    "inspect_rendered_video",
                    return_value={"ok": True, "resolution": [1080, 1920]},
                ) as inspect,
                patch.object(
                    render_quality,
                    "build_visual_regression_gallery",
                    return_value={"html_path": "gallery.html", "frame_paths": [], "video_count": 1},
                ),
                patch.object(
                    render_quality,
                    "create_subtitle_safe_zone_snapshot",
                    return_value={"snapshot_path": "safe-zone.png"},
                ),
            ):
                state_module.state.get_task.return_value = {"audio_duration": 12.5}
                render_quality.build_task_visual_review_package(
                    "review-expectations-task"
                )

        inspect.assert_called_once_with(
            str(video_path),
            expected_aspect=VideoAspect.portrait,
            expected_duration=12.5,
        )

    def test_build_task_visual_review_allows_intentionally_silent_no_voice_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "tasks" / "review-no-voice-task"
            task_dir.mkdir(parents=True)
            video_path = task_dir / "final-1.mp4"
            video_path.touch()
            (task_dir / "script.json").write_text(
                json.dumps(
                    {
                        "params": {
                            "video_aspect": "9:16",
                            "voice_name": "no-voice",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(render_quality.utils, "storage_dir", return_value=temp_dir),
                patch.object(render_quality.sm.state, "get_task", return_value=None),
                patch.object(
                    render_quality,
                    "inspect_rendered_video",
                    return_value={"ok": True, "resolution": [1080, 1920]},
                ) as inspect,
                patch.object(
                    render_quality,
                    "build_visual_regression_gallery",
                    return_value={"html_path": "gallery.html", "frame_paths": [], "video_count": 1},
                ),
                patch.object(
                    render_quality,
                    "create_subtitle_safe_zone_snapshot",
                    return_value={"snapshot_path": "safe-zone.png"},
                ),
            ):
                render_quality.build_task_visual_review_package("review-no-voice-task")

        inspect.assert_called_once_with(
            str(video_path),
            expected_aspect=VideoAspect.portrait,
            expected_duration=None,
            allow_silent_audio=True,
        )

    def test_build_task_visual_review_uses_saved_primary_aspect_without_filename_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_dir = Path(temp_dir) / "tasks" / "review-primary-aspect-task"
            task_dir.mkdir(parents=True)
            video_path = task_dir / "final-1.mp4"
            video_path.touch()
            (task_dir / "script.json").write_text(
                json.dumps({"params": {"video_aspect": "4:5"}}),
                encoding="utf-8",
            )

            with (
                patch.object(render_quality.utils, "storage_dir", return_value=temp_dir),
                patch.object(render_quality.sm.state, "get_task", return_value=None),
                patch.object(
                    render_quality,
                    "inspect_rendered_video",
                    return_value={"ok": True, "resolution": [1080, 1350]},
                ) as inspect,
                patch.object(
                    render_quality,
                    "build_visual_regression_gallery",
                    return_value={"html_path": "gallery.html", "frame_paths": [], "video_count": 1},
                ),
                patch.object(
                    render_quality,
                    "create_subtitle_safe_zone_snapshot",
                    return_value={"snapshot_path": "safe-zone.png"},
                ),
            ):
                render_quality.build_task_visual_review_package(
                    "review-primary-aspect-task"
                )

        inspect.assert_called_once_with(
            str(video_path),
            expected_aspect=VideoAspect.portrait_4_5,
            expected_duration=None,
        )

    def test_build_task_visual_review_package_rejects_unknown_task(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            render_quality.utils, "storage_dir", return_value=temp_dir
        ):
            package = render_quality.build_task_visual_review_package("missing-task")

        self.assertFalse(package["ok"])
        self.assertEqual(package["error"], "no_final_videos")
