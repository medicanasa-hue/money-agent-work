import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
