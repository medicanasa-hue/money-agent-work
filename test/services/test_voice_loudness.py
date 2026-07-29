import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.voice import loudness, subtitles


class TestNarrationLoudness(unittest.TestCase):
    def test_normalize_narration_loudness_writes_a_derived_wav(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "narration.m4a"
            source.write_bytes(b"original narration")

            def write_normalized_audio(command, **_kwargs):
                Path(command[-1]).write_bytes(b"normalized narration")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(loudness.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
                patch.object(
                    loudness.subprocess, "run", side_effect=write_normalized_audio
                ) as run,
            ):
                result = loudness.normalize_narration_loudness(str(source))

            self.assertEqual(result, str(source.with_suffix(".normalized.wav")))
            self.assertEqual(source.read_bytes(), b"original narration")
            command = run.call_args.args[0]
            self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", command)
            self.assertIn("0:a:0", command)
            self.assertIn("pcm_s16le", command)

    def test_normalize_narration_loudness_keeps_original_after_ffmpeg_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "narration.mp3"
            source.write_bytes(b"original narration")

            with (
                patch.object(loudness.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
                patch.object(
                    loudness.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, "", "filter failed"),
                ),
                patch.object(loudness.logger, "warning") as warning,
            ):
                result = loudness.normalize_narration_loudness(str(source))

            self.assertEqual(result, str(source))
            warning.assert_called_once()

    def test_get_audio_duration_accepts_a_normalized_wav_file(self):
        class FakeAudioClip:
            duration = 2.5

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with (
            patch.object(subtitles.os.path, "exists", return_value=True),
            patch.object(subtitles, "AudioFileClip", return_value=FakeAudioClip()),
        ):
            self.assertEqual(subtitles.get_audio_duration("narration.normalized.wav"), 2.5)

    def test_normalize_narration_loudness_keeps_original_when_ffmpeg_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "narration.mp3"
            source.write_bytes(b"original narration")

            with (
                patch.object(loudness.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
                patch.object(loudness.subprocess, "run", side_effect=OSError("missing")),
                patch.object(loudness.logger, "warning") as warning,
            ):
                result = loudness.normalize_narration_loudness(str(source))

            self.assertEqual(result, str(source))
            warning.assert_called_once()
