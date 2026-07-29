import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import thumbnail


class TestThumbnailService(unittest.TestCase):
    def test_extract_thumbnail_candidates_builds_ffmpeg_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")
            output_dir = Path(temp_dir) / "thumbnails"

            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"fake-thumbnail")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(
                thumbnail.utils,
                "get_ffmpeg_binary",
                return_value="ffmpeg-test",
            ), patch.object(
                thumbnail.subprocess,
                "run",
                side_effect=fake_run,
            ) as run:
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(output_dir),
                    thumbnail_concepts=["Close-up with bold keyword", "Before/after"],
                    timestamps=[1, 3],
                    count=2,
                )

        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(result["candidates"][0]["path"].endswith("thumbnail-1.jpg"))
        self.assertEqual(result["candidates"][0]["concept"], "Close-up with bold keyword")
        self.assertEqual(run.call_args_list[0].args[0][0], "ffmpeg-test")
        self.assertIn("-ss", run.call_args_list[0].args[0])
        self.assertIn(str(video_path), run.call_args_list[0].args[0])

    def test_extract_thumbnail_candidates_requires_created_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")

            with patch.object(
                thumbnail.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ):
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(Path(temp_dir) / "thumbnails"),
                    timestamps=[1],
                    count=1,
                )

        self.assertEqual(result["candidates"], [])
        self.assertIn("Thumbnail file was not created", result["error"])

    def test_extract_thumbnail_candidates_does_not_return_stale_file_when_ffmpeg_creates_no_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")
            output_dir = Path(temp_dir) / "thumbnails"
            output_dir.mkdir()
            stale_output = output_dir / "thumbnail-1.jpg"
            stale_output.write_bytes(b"old-thumbnail")

            with patch.object(
                thumbnail.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(output_dir),
                    timestamps=[1],
                    count=1,
                )
            self.assertFalse(stale_output.exists())

        self.assertEqual(result["candidates"], [])
        self.assertIn("Thumbnail file was not created", result["error"])

    def test_extract_thumbnail_candidates_retries_defaults_when_hook_timestamps_make_no_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")
            output_dir = Path(temp_dir) / "thumbnails"

            def fake_run(command, **kwargs):
                timestamp = float(command[command.index("-ss") + 1])
                if timestamp in {1.0, 3.0}:
                    Path(command[-1]).write_bytes(b"fallback-thumbnail")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(
                thumbnail.subprocess,
                "run",
                side_effect=fake_run,
            ) as run:
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(output_dir),
                    thumbnail_concepts=["First concept", "Second concept"],
                    timestamps=[30, 60],
                    count=2,
                )

        self.assertEqual(result["error"], "")
        self.assertEqual(
            [candidate["timestamp_sec"] for candidate in result["candidates"]],
            [1.0, 3.0],
        )
        self.assertEqual(
            [float(call.args[0][call.args[0].index("-ss") + 1]) for call in run.call_args_list],
            [30.0, 60.0, 1.0, 3.0],
        )

    def test_extract_thumbnail_candidates_does_not_retry_defaults_after_ffmpeg_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")

            with patch.object(
                thumbnail.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="decoder failed",
                ),
            ) as run:
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(Path(temp_dir) / "thumbnails"),
                    timestamps=[30, 60],
                    count=2,
                )

        self.assertEqual(result["candidates"], [])
        self.assertIn("decoder failed", result["error"])
        self.assertEqual(run.call_count, 2)

    def test_extract_thumbnail_candidates_returns_controlled_error_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "final.mp4"
            video_path.write_bytes(b"fake-video")

            with patch.object(
                thumbnail.subprocess,
                "run",
                side_effect=FileNotFoundError("missing ffmpeg"),
            ):
                result = thumbnail.extract_thumbnail_candidates(
                    str(video_path),
                    str(Path(temp_dir) / "thumbnails"),
                    timestamps=[1],
                    count=1,
                )

        self.assertEqual(result["candidates"], [])
        self.assertIn("ffmpeg unavailable", result["error"])

    def test_generate_thumbnail_candidates_forwards_hook_timestamps(self):
        expected = {"candidates": [], "error": ""}
        with (
            patch.object(
                thumbnail,
                "thumbnail_output_dir",
                return_value="C:/task/thumbnails",
            ),
            patch.object(
                thumbnail.video_service,
                "get_video_duration",
                return_value=None,
            ) as get_video_duration,
            patch.object(
                thumbnail,
                "extract_thumbnail_candidates",
                return_value=expected,
            ) as extract,
        ):
            result = thumbnail.generate_thumbnail_candidates(
                task_id="task-1",
                video_paths=["C:/task/final.mp4"],
                thumbnail_concepts=["Bold first-frame text"],
                hook_timestamps=[0.75, 3.5],
            )

        self.assertIs(result, expected)
        self.assertEqual(
            extract.call_args.kwargs["timestamps"],
            [0.75, 3.5],
        )
        get_video_duration.assert_called_once_with("C:/task/final.mp4")

    def test_generate_thumbnail_candidates_discards_hook_timestamps_past_video_duration(self):
        expected = {"candidates": [], "error": ""}
        video_service = SimpleNamespace(
            get_video_duration=Mock(return_value=12.5),
        )
        with (
            patch.object(
                thumbnail,
                "video_service",
                video_service,
                create=True,
            ),
            patch.object(
                thumbnail,
                "thumbnail_output_dir",
                return_value="C:/task/thumbnails",
            ),
            patch.object(
                thumbnail,
                "extract_thumbnail_candidates",
                return_value=expected,
            ) as extract,
        ):
            result = thumbnail.generate_thumbnail_candidates(
                task_id="task-1",
                video_paths=["C:/task/final.mp4"],
                hook_timestamps=[0.75, 8, 13],
            )

        self.assertIs(result, expected)
        video_service.get_video_duration.assert_called_once_with("C:/task/final.mp4")
        self.assertEqual(extract.call_args.kwargs["timestamps"], [0.75, 8.0])

    def test_generate_thumbnail_candidates_uses_defaults_without_hook_timestamps(self):
        with (
            patch.object(
                thumbnail,
                "thumbnail_output_dir",
                return_value="C:/task/thumbnails",
            ),
            patch.object(
                thumbnail,
                "extract_thumbnail_candidates",
                return_value={"candidates": [], "error": ""},
            ) as extract,
        ):
            thumbnail.generate_thumbnail_candidates(
                task_id="task-1",
                video_paths=["C:/task/final.mp4"],
            )

        self.assertIsNone(extract.call_args.kwargs["timestamps"])
        self.assertEqual(
            thumbnail._thumbnail_timestamps(None),
            list(thumbnail.DEFAULT_THUMBNAIL_TIMESTAMPS),
        )

    def test_thumbnail_timestamps_reject_non_finite_values(self):
        self.assertEqual(
            thumbnail._thumbnail_timestamps([float("nan"), float("inf")], count=2),
            list(thumbnail.DEFAULT_THUMBNAIL_TIMESTAMPS[:2]),
        )

    def test_thumbnail_timestamps_deduplicate_normalized_values(self):
        self.assertEqual(
            thumbnail._thumbnail_timestamps([-1, 0, 0.0, "2", 2], count=3),
            [0.0, 2.0],
        )


if __name__ == "__main__":
    unittest.main()
