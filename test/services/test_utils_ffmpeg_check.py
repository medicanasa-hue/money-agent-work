import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import task
from app.services.state import MemoryState
from app.utils import utils


@pytest.mark.parametrize(
    "result", [0, 1, FileNotFoundError(), subprocess.TimeoutExpired("ffmpeg", 3)]
)
def test_ffmpeg_probe_is_bounded_and_handles_unavailable_tools(result):
    with (
        patch.object(
            utils, "get_ffmpeg_binary", return_value="C:/portable tools/ffmpeg.exe"
        ),
        patch("subprocess.run") as run,
    ):
        if isinstance(result, Exception):
            run.side_effect = result
        else:
            run.return_value = SimpleNamespace(returncode=result)
        assert utils.check_ffmpeg_ready(timeout=3) is (result == 0)
    assert run.call_args.args[0] == ["C:/portable tools/ffmpeg.exe", "-version"]
    assert run.call_args.kwargs["timeout"] == 3
    assert not run.call_args.kwargs.get("shell", False)


@pytest.mark.parametrize("stop_at", ["audio", "subtitle", "materials", "video"])
def test_missing_ffmpeg_fails_before_paid_generation(stop_at):
    state = MemoryState(persist=False)
    with (
        patch.object(task.sm, "state", state),
        patch.object(utils, "check_ffmpeg_ready", return_value=False, create=True),
        patch.object(task, "generate_script") as generate,
        patch.object(task, "generate_audio") as audio,
    ):
        result = task._start(
            "missing-ffmpeg", VideoParams(video_subject="test"), stop_at
        )
    assert result["failed_stage"] == "preflight"
    assert "FFmpeg" in result["error"]
    generate.assert_not_called()
    audio.assert_not_called()


@pytest.mark.parametrize("stop_at", ["script", "terms"])
def test_text_only_generation_does_not_require_ffmpeg(stop_at):
    with (
        patch.object(task.sm, "state", MemoryState(persist=False)),
        patch.object(
            utils, "check_ffmpeg_ready", return_value=False, create=True
        ) as probe,
        patch.object(task, "generate_script", return_value="Hello world."),
        patch.object(task, "generate_terms", return_value=["nature"]),
    ):
        result = task._start("text-only", VideoParams(video_subject="test"), stop_at)
    probe.assert_not_called()
    assert result["script"] == "Hello world."
