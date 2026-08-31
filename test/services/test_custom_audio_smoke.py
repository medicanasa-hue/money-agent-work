"""Render local WAV/MP3 narration with a synthetic tone, never a live TTS service."""

import math
import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import VideoFileClip

from app.config import config
from app.models.schema import VideoParams
from app.services import task, video
from app.services.state import MemoryState
from app.utils import utils


SAMPLE_RATE = 48000
TONE_DURATION = 1.37


def _run_ffmpeg(*arguments):
    return subprocess.run(
        [utils.get_ffmpeg_binary(), "-nostdin", "-v", "error", "-y", *arguments],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _decode_mono_audio(path):
    result = _run_ffmpeg(
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    )
    return np.frombuffer(result.stdout, dtype="<f4")


def _write_test_tone(path):
    """The final tone marker detects truncated audio; it is not Turkish speech."""
    times = np.arange(round(TONE_DURATION * SAMPLE_RATE)) / SAMPLE_RATE
    frequency = np.where(times < 1.15, 440.0, 997.0)
    samples = (0.12 * np.sin(2 * np.pi * frequency * times) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(samples.tobytes())


def _render_custom_audio(input_audio, output_dir, subtitles, *, normalize=False):
    """Reusable local-only path for CI tones or an explicitly supplied synthetic recording."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = output_dir / "source.mp4"
    output = output_dir / "custom-audio.mp4"
    params = VideoParams(
        video_subject="offline custom audio smoke",
        custom_audio_file=str(Path(input_audio).resolve()),
        video_aspect="1:1",
        video_language="tr",
        bgm_type="",
        bgm_volume=0,
        voice_volume=1,
        font_name="BeVietnamPro-Medium.ttf",
        font_size=44,
        stroke_width=2,
        text_background_color=False,
        n_threads=2,
    )
    with (
        patch.dict(
            config.app,
            {
                "video_codec": "libx264",
                "video_fps": 24,
                "video_encoder_preset": "ultrafast",
                "audio_loudness_normalization_enabled": normalize,
            },
            clear=True,
        ),
        patch.object(utils, "task_dir", return_value=str(output_dir)),
        patch.object(task.sm, "state", MemoryState(persist=False)),
        patch.object(
            task.voice,
            "tts",
            side_effect=AssertionError("custom audio must not invoke TTS"),
        ) as tts,
    ):
        audio_file, duration, sub_maker = task.generate_audio(
            "custom-audio-smoke", params, "Önceden hazırlanmış özel ses."
        )
        assert audio_file is not None
        assert duration is not None and duration > 0
        assert sub_maker is None
        tts.assert_not_called()

        # Match the normal pipeline's safety margin while preserving fractional audio.
        video_duration = math.ceil((duration + 0.1) * 24) / 24
        _run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=c=0x13263b:s=1080x1080:r=24",
            "-t",
            str(video_duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
        result = video.generate_video(
            str(source),
            audio_file,
            str(subtitles),
            str(output),
            params,
            return_encoder_result=True,
        )
        tts.assert_not_called()

    assert result["used_codec"] == "libx264"
    assert result["bgm_mix_succeeded"] is True
    return {
        "output": output,
        "selected_audio": Path(audio_file),
        "reported_duration": duration,
        "video_duration": video_duration,
    }


@pytest.mark.parametrize("extension", ["wav", "mp3"])
@pytest.mark.parametrize("normalize", [False, True], ids=["original", "normalized"])
def test_custom_audio_retains_fractional_duration_tail_and_turkish_captions(
    tmp_path, extension, normalize
):
    wav = tmp_path / "tone.wav"
    _write_test_tone(wav)
    source_audio = wav
    if extension == "mp3":
        source_audio = tmp_path / "tone.mp3"
        _run_ffmpeg(
            "-i",
            str(wav),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(source_audio),
        )
    original_bytes = source_audio.read_bytes()
    subtitles = tmp_path / "turkce.srt"
    subtitles.write_text(
        "1\n00:00:00,150 --> 00:00:00,700\nİstanbul: ışık ve gölge.\n\n"
        "2\n00:00:01,050 --> 00:00:01,370\nSon bölüm: Türkçe ses.\n\n",
        encoding="utf-8",
    )

    result = _render_custom_audio(
        source_audio,
        tmp_path / "render",
        subtitles,
        normalize=normalize,
    )

    assert source_audio.read_bytes() == original_bytes
    # MP3 containers can include encoder delay; neither format should round to 2 seconds.
    assert TONE_DURATION - 0.02 <= result["reported_duration"] <= TONE_DURATION + 0.06
    if normalize:
        assert result["selected_audio"].name == "audio.normalized.wav"
        assert result["selected_audio"] != source_audio
    else:
        assert result["selected_audio"] == source_audio.resolve()

    selected_pcm = _decode_mono_audio(result["selected_audio"])
    output_pcm = _decode_mono_audio(result["output"])
    assert abs(len(selected_pcm) / SAMPLE_RATE - TONE_DURATION) < 0.005
    assert len(output_pcm) / SAMPLE_RATE >= TONE_DURATION - 0.025
    tail_start, tail_end = int(1.20 * SAMPLE_RATE), int(1.34 * SAMPLE_RATE)
    source_tail = selected_pcm[tail_start:tail_end]
    output_tail = output_pcm[tail_start:tail_end]
    source_rms = np.sqrt(np.mean(source_tail**2))
    output_rms = np.sqrt(np.mean(output_tail**2))
    assert source_rms > 0.02
    assert output_rms >= source_rms * 0.75
    spectrum = np.abs(np.fft.rfft(output_tail))
    dominant_hz = np.fft.rfftfreq(len(output_tail), 1 / SAMPLE_RATE)[spectrum.argmax()]
    assert abs(dominant_hz - 997) < 10

    with VideoFileClip(str(result["output"])) as rendered:
        assert rendered.size == [1080, 1080]
        assert rendered.audio is not None
        assert rendered.duration >= TONE_DURATION
        assert rendered.duration <= result["video_duration"] + 0.05
        initial_frame = rendered.get_frame(0.05)
        assert (initial_frame.min(axis=2) > 220).sum() < 100
        for timestamp in (0.4, 1.32):
            caption_frame = rendered.get_frame(timestamp)
            assert (caption_frame.min(axis=2) > 220).sum() > 500
    _run_ffmpeg("-xerror", "-i", str(result["output"]), "-f", "null", "-")
