"""Exercise render fallbacks at the FFmpeg boundary without external services."""

import subprocess
from pathlib import Path

import pytest

from app.config import config
from app.services import video


@pytest.fixture(autouse=True)
def isolated_video_runtime(monkeypatch):
    monkeypatch.setattr(config, "app", {})
    monkeypatch.setattr(video, "_runtime_disabled_video_codecs", set())
    monkeypatch.setattr(video.utils, "get_ffmpeg_binary", lambda: "ffmpeg-test")
    monkeypatch.setattr(
        video,
        "_get_effective_video_codec",
        lambda preferred=None: preferred or "libx264",
    )


@pytest.fixture
def media_files(tmp_path):
    paths = {}
    for name in (
        "source.mp4",
        "voice.wav",
        "music.mp3",
        "captions.ass",
        "captions.srt",
    ):
        path = tmp_path / name
        path.write_bytes(b"source fixture")
        paths[name] = str(path)
    output = tmp_path / "final.mp4"
    output.write_bytes(b"previous completed output")
    paths["final.mp4"] = str(output)
    return paths


class FFmpegProcess:
    """Model exit status and partial output at the subprocess boundary."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.commands = []

    def __call__(self, command, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        self.commands.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome != "missing-output":
            Path(command[-1]).write_bytes(b"rendered" if outcome == 0 else b"partial")
        return subprocess.CompletedProcess(
            command,
            returncode=0 if outcome == "missing-output" else outcome,
            stdout="",
            stderr="" if outcome == 0 else "encoder unavailable",
        )


def install_ffmpeg(monkeypatch, outcomes):
    process = FFmpegProcess(outcomes)
    monkeypatch.setattr(video.subprocess, "run", process)
    return process


def option(command, name):
    return command[command.index(name) + 1]


def burn_subtitles(kind, media_files):
    common = {
        "input_file": media_files["source.mp4"],
        "subtitle_file": media_files[f"captions.{kind}"],
        "output_file": media_files["final.mp4"],
        "threads": None,
    }
    if kind == "ass":
        return video._burn_ass_subtitles_with_ffmpeg(**common)
    return video._burn_srt_subtitles_with_ffmpeg(
        **common,
        params=video.VideoParams(
            video_subject="test",
            font_name="Missing Font.ttf",
            subtitle_position="top",
            text_fore_color="#123456",
            stroke_color="#AABBCC",
        ),
        video_width=1080,
        video_height=1920,
        video_aspect=video.VideoAspect.portrait,
        font_path="missing-test-font.ttf",
    )


@pytest.mark.parametrize("kind", ["ass", "srt"])
def test_subtitle_burn_replaces_final_only_after_cpu_fallback_succeeds(
    kind, media_files, monkeypatch
):
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "h264_nvenc")
    process = install_ffmpeg(monkeypatch, [1, 0])

    used_codec = burn_subtitles(kind, media_files)

    assert used_codec == "libx264"
    assert [option(command, "-c:v") for command in process.commands] == [
        "h264_nvenc",
        "libx264",
    ]
    assert Path(media_files["final.mp4"]).read_bytes() == b"rendered"
    assert Path(media_files["source.mp4"]).read_bytes() == b"source fixture"
    assert not Path(process.commands[-1][-1]).exists()
    assert video._runtime_disabled_video_codecs == {"h264_nvenc"}
    for command in process.commands:
        assert option(command, "-c:a") == "copy"
        assert option(command, "-threads") == "2"
        assert option(command, "-pix_fmt") == "yuv420p"
    if kind == "srt":
        subtitles_filter = option(process.commands[-1], "-vf")
        assert "charenc=UTF-8" in subtitles_filter
        assert "PrimaryColour=&H00563412" in subtitles_filter
        assert "OutlineColour=&H00CCBBAA" in subtitles_filter
        assert "Alignment=8" in subtitles_filter
        assert "MarginV=96" in subtitles_filter
        assert "Fontname=Missing Font" in subtitles_filter


@pytest.mark.parametrize("kind", ["ass", "srt"])
@pytest.mark.parametrize("outcomes", [[1, 1], ["missing-output", "missing-output"]])
def test_failed_subtitle_burn_preserves_final_and_removes_partial_files(
    kind, outcomes, media_files, monkeypatch
):
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "h264_nvenc")
    process = install_ffmpeg(monkeypatch, outcomes)

    assert burn_subtitles(kind, media_files) is None

    assert Path(media_files["final.mp4"]).read_bytes() == b"previous completed output"
    assert all(not Path(command[-1]).exists() for command in process.commands)
    assert video._runtime_disabled_video_codecs == set()


@pytest.mark.parametrize("kind", ["ass", "srt"])
def test_subtitle_burn_handles_missing_ffmpeg_without_losing_final(
    kind, media_files, monkeypatch
):
    process = install_ffmpeg(monkeypatch, [FileNotFoundError("ffmpeg missing")])

    assert burn_subtitles(kind, media_files) is None

    assert len(process.commands) == 1
    assert not Path(process.commands[0][-1]).exists()
    assert Path(media_files["final.mp4"]).read_bytes() == b"previous completed output"


def test_crossfade_cpu_fallback_keeps_overlap_timeline_and_duration_cap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda: "h264_nvenc")
    process = install_ffmpeg(monkeypatch, [1, 0])
    output = tmp_path / "crossfade.mp4"

    used_codec = video.crossfade_video_clips_with_ffmpeg(
        ["one.mp4", "two.mp4", "three.mp4"],
        [1.5, 2.0, 1.25],
        str(output),
        threads=3,
        max_duration=4.0,
    )

    assert used_codec == "libx264"
    assert output.read_bytes() == b"rendered"
    assert video._runtime_disabled_video_codecs == {"h264_nvenc"}
    first, second = process.commands
    graph = option(second, "-filter_complex")
    assert "[v0][v1]xfade=transition=fade:duration=0.350:offset=1.150[xf1]" in graph
    assert "[xf1][v2]xfade=transition=fade:duration=0.350:offset=2.800[xf2]" in graph
    assert "[xf2]setsar=1[square]" in graph
    assert option(first, "-filter_complex") == graph
    assert option(second, "-t") == "4.000"
    assert option(second, "-map") == "[square]"
    assert option(second, "-c:v") == "libx264"


def test_crossfade_does_not_disable_gpu_when_cpu_also_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda: "h264_nvenc")
    process = install_ffmpeg(monkeypatch, [1, 1])

    with pytest.raises(RuntimeError, match="encoder unavailable"):
        video.crossfade_video_clips_with_ffmpeg(
            ["one.mp4", "two.mp4"],
            [2, 3],
            str(tmp_path / "out.mp4"),
            2,
            max_duration=float("nan"),
        )

    assert len(process.commands) == 2
    assert all("-t" not in command for command in process.commands)
    assert video._runtime_disabled_video_codecs == set()


@pytest.mark.parametrize("durations", [[2], [2, "bad"], [2, float("inf")], [2, 0]])
def test_crossfade_rejects_invalid_timeline_before_running_ffmpeg(
    durations, tmp_path, monkeypatch
):
    process = install_ffmpeg(monkeypatch, [])

    with pytest.raises(ValueError, match="crossfade"):
        video.crossfade_video_clips_with_ffmpeg(
            ["one.mp4", "two.mp4"],
            durations,
            str(tmp_path / "out.mp4"),
            2,
        )

    assert process.commands == []


def test_clip_render_retries_unsupported_filters_without_losing_crop_or_trim(
    media_files, monkeypatch
):
    process = install_ffmpeg(monkeypatch, [1, 1, 0])
    with video.video_quality_config({"video_deband_enabled": True, "video_fps": 24}):
        rendered = video._fast_render_clip_with_ffmpeg(
            media_files["source.mp4"],
            media_files["final.mp4"],
            start_time=1.25,
            duration=2.5,
            target_width=1080,
            target_height=1920,
            threads=4,
            brightness_adjustment=0.2,
            saturation_multiplier=3.0,
            warmth_adjustment=-0.2,
            crop_x_ratio=0.8,
        )

    assert rendered is True
    assert Path(media_files["final.mp4"]).read_bytes() == b"rendered"
    filters = [option(command, "-vf") for command in process.commands]
    assert "deband=" in filters[0]
    assert "deband=" not in filters[1]
    assert "out_color_matrix=bt709" in filters[1]
    assert "out_color_matrix=bt709" not in filters[2]
    for command, filterspec in zip(process.commands, filters):
        assert "crop=1080:1920:(iw-ow)*0.800000:(ih-oh)/2" in filterspec
        assert "eq=brightness=0.0400:saturation=1.1500" in filterspec
        assert "colorbalance=rm=-0.0600:bm=0.0600:pl=1" in filterspec
        assert option(command, "-ss") == "1.25"
        assert option(command, "-t") == "2.5"
        assert option(command, "-r") == "24"
        assert option(command, "-threads") == "4"


def test_clip_render_ignores_invalid_color_hints_and_returns_false_when_unavailable(
    media_files, monkeypatch
):
    process = install_ffmpeg(monkeypatch, [OSError("missing executable"), 1])

    assert (
        video._fast_render_clip_with_ffmpeg(
            media_files["source.mp4"],
            media_files["final.mp4"],
            start_time=0,
            duration=2,
            target_width=1280,
            target_height=720,
            threads=2,
            brightness_adjustment="bad",
            saturation_multiplier=float("nan"),
            warmth_adjustment=None,
            crop_y_ratio=0.25,
        )
        is False
    )

    assert len(process.commands) == 2
    for command in process.commands:
        filterspec = option(command, "-vf")
        assert "eq=" not in filterspec
        assert "colorbalance=" not in filterspec
        assert "nan" not in filterspec
        assert "crop=1280:720:(iw-ow)/2:(ih-oh)*0.250000" in filterspec


def test_image_render_retries_gpu_on_cpu_preserving_focal_crop(
    media_files, monkeypatch
):
    process = install_ffmpeg(monkeypatch, [1, 1, 0])

    assert (
        video._fast_render_image_with_ffmpeg(
            media_files["source.mp4"],
            media_files["final.mp4"],
            width=1920,
            height=1080,
            duration=2,
            codec="h264_amf",
            threads=0,
            focal_x_ratio=0.75,
            focal_y_ratio=0.5,
            target_width=1079,
            target_height=1919,
        )
        is True
    )

    assert [option(command, "-c:v") for command in process.commands] == [
        "h264_amf",
        "h264_amf",
        "libx264",
    ]
    assert option(process.commands[0], "-preset") == "quality"
    for command in process.commands:
        filterspec = option(command, "-vf")
        assert "crop=1078:1918:(iw-ow)*0.865566:(ih-oh)/2" in filterspec
        assert "d=1:s=1078x1918:fps=30" in filterspec
        assert option(command, "-t") == "2.0"
        assert option(command, "-threads") == "2"
    assert Path(media_files["final.mp4"]).read_bytes() == b"rendered"


@pytest.mark.parametrize("with_bgm", [False, True])
def test_audio_mux_skips_unverified_streams_before_spawning_ffmpeg(
    with_bgm, media_files, monkeypatch
):
    monkeypatch.setattr(
        video, "_video_stream_matches_encoding_contract", lambda _path: False
    )
    process = install_ffmpeg(monkeypatch, [])
    arguments = {
        "video_path": media_files["source.mp4"],
        "audio_path": media_files["voice.wav"],
        "output_file": media_files["final.mp4"],
        "video_duration": 2.0,
        "audio_bitrate": "192k",
    }
    if with_bgm:
        result = video._fast_mux_video_with_audio_and_bgm(
            **arguments,
            bgm_file=media_files["music.mp3"],
            bgm_volume=0.2,
        )
    else:
        result = video._fast_mux_video_with_audio(**arguments)

    assert result is False
    assert process.commands == []
    assert Path(media_files["final.mp4"]).read_bytes() == b"previous completed output"


def test_bgm_mux_ducks_against_adjusted_narration_and_fades_short_video(
    media_files, monkeypatch
):
    monkeypatch.setattr(
        video, "_video_stream_matches_encoding_contract", lambda _path: True
    )
    process = install_ffmpeg(monkeypatch, [0])

    result = video._fast_mux_video_with_audio_and_bgm(
        video_path=media_files["source.mp4"],
        audio_path=media_files["voice.wav"],
        bgm_file=media_files["music.mp3"],
        output_file=media_files["final.mp4"],
        video_duration=2,
        audio_bitrate="256k",
        bgm_volume=0.25,
        voice_volume=2,
    )

    assert result is True
    command = process.commands[0]
    graph = option(command, "-filter_complex")
    assert "[1:a]volume=2.0[voice_input]" in graph
    assert "[voice_input]asplit=2[voice][sidechain]" in graph
    assert "volume=0.25,afade=t=out:st=0.0:d=2.0[bgm]" in graph
    assert "sidechaincompress=threshold=0.06:ratio=8" in graph
    assert "amix=inputs=2:duration=longest:normalize=0" in graph
    assert "alimiter=limit=0.95:level=0:latency=1[mixed]" in graph
    assert option(command, "-stream_loop") == "-1"
    assert option(command, "-c:v") == "copy"
    assert option(command, "-b:a") == "256k"
    assert option(command, "-t") == "2.0"
    assert Path(media_files["final.mp4"]).read_bytes() == b"rendered"


@pytest.mark.parametrize("outcome", [1, OSError("cannot start ffmpeg")])
def test_narration_mux_returns_false_for_process_failure(
    media_files, monkeypatch, outcome
):
    monkeypatch.setattr(
        video, "_video_stream_matches_encoding_contract", lambda _path: True
    )
    process = install_ffmpeg(monkeypatch, [outcome])

    assert (
        video._fast_mux_video_with_audio(
            video_path=media_files["source.mp4"],
            audio_path=media_files["voice.wav"],
            output_file=media_files["final.mp4"],
            video_duration=3,
            audio_bitrate="192k",
            voice_volume=0.5,
        )
        is False
    )

    assert len(process.commands) == 1
    assert option(process.commands[0], "-filter:a") == (
        "volume=0.5,alimiter=limit=0.95:level=0:latency=1"
    )
