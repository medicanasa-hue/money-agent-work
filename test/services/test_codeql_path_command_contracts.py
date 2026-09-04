import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.controllers.v1 import video as video_controller
from app.models.exception import HttpException
from app.services import video
from app.utils.file_security import resolve_path_within_directory


def _request() -> SimpleNamespace:
    return SimpleNamespace(headers={"x-task-id": "request-123"})


def test_resolve_path_accepts_existing_file_inside_allowed_directory(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    internal_file = allowed_dir / "clip.mp4"
    internal_file.write_bytes(b"video")

    resolved = resolve_path_within_directory(str(allowed_dir), internal_file.name)

    assert resolved == os.path.realpath(internal_file)


def test_resolve_path_rejects_relative_and_absolute_outside_files(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"private")

    for unsafe_path in (os.path.join("..", outside_file.name), str(outside_file)):
        with pytest.raises(ValueError, match="outside the allowed directory"):
            resolve_path_within_directory(str(allowed_dir), unsafe_path)


def test_resolve_path_rejects_symlink_escape_when_supported(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"private")
    escape_link = allowed_dir / "escape.mp4"

    try:
        escape_link.symlink_to(outside_file)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable on this platform: {exc}")

    with pytest.raises(ValueError, match="outside the allowed directory"):
        resolve_path_within_directory(str(allowed_dir), escape_link.name)


def test_stream_rejects_traversal_before_file_size_or_open(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "outside.mp4").write_bytes(b"private")

    with (
        patch.object(video_controller.utils, "task_dir", return_value=str(tasks_dir)),
        patch.object(video_controller.os.path, "getsize") as getsize,
        patch("builtins.open") as open_file,
    ):
        with pytest.raises(HttpException) as raised:
            asyncio.run(
                video_controller.stream_video(_request(), "../outside.mp4")
            )

    assert raised.value.status_code == 403
    getsize.assert_not_called()
    open_file.assert_not_called()


def test_download_rejects_traversal_before_response_file_sink(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "outside.mp4").write_bytes(b"private")

    with (
        patch.object(video_controller.utils, "task_dir", return_value=str(tasks_dir)),
        patch.object(video_controller, "FileResponse") as file_response,
    ):
        with pytest.raises(HttpException) as raised:
            asyncio.run(
                video_controller.download_video(_request(), "../outside.mp4")
            )

    assert raised.value.status_code == 403
    file_response.assert_not_called()


def test_bgm_metacharacters_stay_in_one_argv_element_without_shell(tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    bgm_name = "music;echo-owned&whoami$(id).mp3"
    bgm_path = songs_dir / bgm_name
    bgm_path.write_bytes(b"music")
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"voice")
    output_path = tmp_path / "output.mp4"

    with patch.object(video.utils, "song_dir", return_value=str(songs_dir)):
        resolved_bgm = video.get_bgm_file(bgm_file=bgm_name)

    assert resolved_bgm == os.path.realpath(bgm_path)
    assert Path(resolved_bgm).is_relative_to(songs_dir.resolve())

    completed_process = SimpleNamespace(returncode=0, stderr="", stdout="")
    with (
        patch.object(
            video, "_video_stream_matches_encoding_contract", return_value=True
        ),
        patch.object(video.utils, "get_ffmpeg_binary", return_value="ffmpeg"),
        patch.object(
            video.subprocess, "run", return_value=completed_process
        ) as run_process,
    ):
        result = video._fast_mux_video_with_audio_and_bgm(
            video_path=str(video_path),
            audio_path=str(audio_path),
            bgm_file=resolved_bgm,
            output_file=str(output_path),
            video_duration=2,
            audio_bitrate="192k",
            bgm_volume=0.2,
        )

    assert result is True
    run_process.assert_called_once()
    command = run_process.call_args.args[0]
    kwargs = run_process.call_args.kwargs
    bgm_argument_index = command.index("-stream_loop") + 3
    assert isinstance(command, list)
    assert command[bgm_argument_index] == resolved_bgm
    assert command.count(resolved_bgm) == 1
    assert kwargs.get("shell", False) is False
