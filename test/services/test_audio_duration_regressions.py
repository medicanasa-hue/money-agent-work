import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from app.config import config
from app.models.schema import VideoParams
from app.services import task, voice


class TestGeneratedAudioDuration(unittest.TestCase):
    def test_final_audio_duration_includes_trailing_audio_beyond_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = os.path.join(directory, "audio.mp3")
            cues = object()

            def duration(source):
                return 9.2 if source == audio_path else 6.0

            with (
                patch.dict(config.app, {"audio_loudness_normalization_enabled": False}),
                patch.object(task.utils, "task_dir", return_value=directory),
                patch.object(task.voice, "tts", return_value=cues),
                patch.object(
                    task.voice, "get_audio_duration", side_effect=duration
                ) as probe,
            ):
                result = task.generate_audio(
                    "duration", VideoParams(video_subject="test"), "Merhaba."
                )

            self.assertEqual(result, (audio_path, 10, cues))
            probe.assert_called_once_with(audio_path)

    def test_unreadable_audio_duration_falls_back_to_provider_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = os.path.join(directory, "audio.mp3")
            cues = object()

            def duration(source):
                return 0 if source == audio_path else 6.2

            with (
                patch.dict(config.app, {"audio_loudness_normalization_enabled": False}),
                patch.object(task.utils, "task_dir", return_value=directory),
                patch.object(task.voice, "tts", return_value=cues),
                patch.object(
                    task.voice, "get_audio_duration", side_effect=duration
                ) as probe,
            ):
                result = task.generate_audio(
                    "duration", VideoParams(video_subject="test"), "Merhaba."
                )

            self.assertEqual(result, (audio_path, 7, cues))
            self.assertEqual(
                [call.args[0] for call in probe.call_args_list], [audio_path, cues]
            )

    def test_normalized_audio_is_the_duration_source(self):
        with tempfile.TemporaryDirectory() as directory:
            normalized_path = os.path.join(directory, "audio.normalized.wav")
            cues = object()
            with (
                patch.dict(config.app, {"audio_loudness_normalization_enabled": True}),
                patch.object(task.utils, "task_dir", return_value=directory),
                patch.object(task.voice, "tts", return_value=cues),
                patch.object(task.voice, "is_no_voice", return_value=False),
                patch.object(
                    task.voice,
                    "normalize_narration_loudness",
                    return_value=normalized_path,
                ),
                patch.object(
                    task.voice, "get_audio_duration", return_value=8.1
                ) as probe,
            ):
                result = task.generate_audio(
                    "duration", VideoParams(video_subject="test"), "Merhaba."
                )

            self.assertEqual(result, (normalized_path, 9, cues))
            probe.assert_called_once_with(normalized_path)


class TestTtsClipCleanup(unittest.TestCase):
    def test_siliconflow_timeline_reaches_the_exact_audio_end(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = MagicMock(duration=4.1)
            with (
                patch.dict(config.siliconflow, {"api_key": "test-key"}),
                patch.object(
                    voice.requests,
                    "post",
                    return_value=SimpleNamespace(status_code=200, content=b"audio"),
                ),
                patch.object(voice, "AudioFileClip", return_value=clip),
            ):
                result = voice.siliconflow_tts(
                    "One. Two. Three.",
                    "model",
                    "voice",
                    1.0,
                    os.path.join(directory, "audio.mp3"),
                )
            self.assertEqual(result.offset[-1][1], int(4.1 * 10_000_000))
            clip.close.assert_called_once_with()

    def test_siliconflow_closes_clip_when_duration_reading_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = MagicMock()
            type(clip).duration = PropertyMock(
                side_effect=OSError("unreadable duration")
            )
            with (
                patch.dict(config.siliconflow, {"api_key": "test-key"}),
                patch.object(
                    voice.requests,
                    "post",
                    return_value=SimpleNamespace(status_code=200, content=b"audio"),
                ),
                patch.object(voice, "AudioFileClip", return_value=clip),
            ):
                voice.siliconflow_tts(
                    "Hello.",
                    "model",
                    "voice",
                    1.0,
                    os.path.join(directory, "audio.mp3"),
                )
            clip.close.assert_called_once_with()

    def test_clip_closes_even_when_duration_reading_fails(self):
        providers = (
            (voice.elevenlabs_tts, "voice-id"),
            (voice.chatterbox_tts, "voice-name"),
        )
        for provider, selected_voice in providers:
            with (
                self.subTest(provider=provider.__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                clips = [MagicMock() for _ in range(3)]
                for clip in clips:
                    type(clip).duration = PropertyMock(
                        side_effect=OSError("unreadable duration")
                    )
                response = SimpleNamespace(status_code=200, content=b"fake audio")
                with (
                    patch.dict(config.elevenlabs, {"api_key": "test-key"}),
                    patch.dict(
                        config.chatterbox, {"base_url": "http://localhost:9999"}
                    ),
                    patch.object(voice.requests, "post", return_value=response),
                    patch.object(voice, "AudioFileClip", side_effect=clips),
                ):
                    result = provider(
                        "Merhaba.", selected_voice, os.path.join(directory, "audio.mp3")
                    )

                self.assertIsNone(result)
                for clip in clips:
                    clip.close.assert_called_once_with()
