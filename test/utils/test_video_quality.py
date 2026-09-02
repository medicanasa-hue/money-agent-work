import subprocess

import pytest

from app.utils import video_quality


@pytest.fixture(autouse=True)
def prevent_unexpected_processes(monkeypatch):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Invalid inputs or unavailable FFmpeg must not start a process")

    monkeypatch.setattr(video_quality.subprocess, "run", unexpected_run)


@pytest.fixture
def fake_video(tmp_path):
    path = tmp_path / "my video 日本語.mov"
    path.write_bytes(b"fake video")
    return path


def _set_ffmpeg(monkeypatch, binary="/usr/bin/ffmpeg"):
    monkeypatch.setattr(video_quality.utils, "get_ffmpeg_binary", lambda: binary)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["ffmpeg"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.parametrize(
    "func, kwargs",
    [
        (video_quality.detect_scene_change_timestamps, {}),
        (video_quality.detect_sustained_near_black_segments, {}),
        (video_quality.detect_sustained_frozen_segments, {}),
    ],
)
def test_unavailable_measurements_return_none_for_missing_file(
    monkeypatch, tmp_path, func, kwargs
):
    _set_ffmpeg(monkeypatch)
    missing = tmp_path / "missing.mp4"
    assert func(str(missing), **kwargs) is None


def test_ffmpeg_binary_unavailable_returns_none(monkeypatch, fake_video):
    monkeypatch.setattr(video_quality.utils, "get_ffmpeg_binary", lambda: "")
    assert video_quality.detect_scene_change_timestamps(str(fake_video)) is None
    assert video_quality.detect_sustained_near_black_segments(str(fake_video)) is None
    assert video_quality.detect_sustained_frozen_segments(str(fake_video)) is None


def test_ffmpeg_discovery_error_returns_none(monkeypatch, fake_video):
    def unavailable_binary():
        raise RuntimeError("FFmpeg discovery failed")

    monkeypatch.setattr(video_quality.utils, "get_ffmpeg_binary", unavailable_binary)
    assert video_quality.detect_scene_change_timestamps(str(fake_video)) is None
    assert video_quality.detect_sustained_near_black_segments(str(fake_video)) is None
    assert video_quality.detect_sustained_frozen_segments(str(fake_video)) is None


@pytest.mark.parametrize(
    "func, kwargs",
    [
        (video_quality.detect_scene_change_timestamps, {}),
        (video_quality.detect_sustained_near_black_segments, {}),
        (video_quality.detect_sustained_frozen_segments, {}),
    ],
)
@pytest.mark.parametrize(
    "side_effect",
    [
        OSError("ffmpeg not found"),
        subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1),
    ],
)
def test_subprocess_errors_return_none(
    monkeypatch, fake_video, func, kwargs, side_effect
):
    _set_ffmpeg(monkeypatch)

    def fake_run(*args, **kwargs_):
        raise side_effect

    monkeypatch.setattr(video_quality.subprocess, "run", fake_run)
    assert func(str(fake_video), **kwargs) is None


@pytest.mark.parametrize(
    "func, kwargs",
    [
        (video_quality.detect_scene_change_timestamps, {}),
        (video_quality.detect_sustained_near_black_segments, {}),
        (video_quality.detect_sustained_frozen_segments, {}),
    ],
)
def test_nonzero_returncode_returns_none(monkeypatch, fake_video, func, kwargs):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(returncode=1),
    )
    assert func(str(fake_video), **kwargs) is None


@pytest.mark.parametrize(
    "func, kwargs",
    [
        (video_quality.detect_scene_change_timestamps, {}),
        (video_quality.detect_sustained_near_black_segments, {}),
        (video_quality.detect_sustained_frozen_segments, {}),
    ],
)
def test_valid_run_without_artifacts_returns_empty_list(
    monkeypatch, fake_video, func, kwargs
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(stdout="", stderr=""),
    )
    assert func(str(fake_video), **kwargs) == []


@pytest.mark.parametrize(
    "threshold, timeout",
    [
        (101, 30),
        (-1, 30),
        (float("nan"), 30),
        (float("inf"), 30),
        (None, 30),
        ("invalid", 30),
        (10, 0),
        (10, -1),
        (10, "bad"),
        (10, None),
    ],
)
def test_scene_change_rejects_invalid_threshold_and_timeout(
    monkeypatch, fake_video, threshold, timeout
):
    _set_ffmpeg(monkeypatch)
    assert (
        video_quality.detect_scene_change_timestamps(
            str(fake_video), threshold=threshold, timeout_seconds=timeout
        )
        is None
    )


def test_scene_change_parses_stdout_and_stderr_and_removes_invalids(
    monkeypatch, fake_video
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(
            stdout=(
                "lavfi.scd.time: 1.234567\nlavfi.scd.time: 2.3456\n"
                "lavfi.scd.time: 1e999\nlavfi.scd.time: 1.2.3\n"
            ),
            stderr=(
                "lavfi.scd.time: 2.3456\nlavfi.scd.time: -0.5\n"
                "lavfi.scd.time: 10.0\nlavfi.scd.time: 0\n"
            ),
        ),
    )
    assert video_quality.detect_scene_change_timestamps(
        str(fake_video), threshold=12.5, timeout_seconds=7
    ) == [
        0.0,
        1.235,
        2.346,
        10.0,
    ]


def test_scene_change_invokes_ffmpeg_with_unicode_path_and_timeout(
    monkeypatch, tmp_path
):
    path = tmp_path / "clip with spaces 日本語.mp4"
    path.write_bytes(b"fake")
    _set_ffmpeg(monkeypatch)
    seen = {}

    def fake_run(cmd, capture_output, text, check, timeout):
        assert isinstance(cmd, list)
        assert capture_output is True and text is True and check is False
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        return _completed(stdout="lavfi.scd.time: 1.234")

    monkeypatch.setattr(video_quality.subprocess, "run", fake_run)

    result = video_quality.detect_scene_change_timestamps(str(path), timeout_seconds=17)
    assert result == [1.234]
    assert seen["timeout"] == 17
    assert seen["cmd"][0] == "/usr/bin/ffmpeg"
    assert seen["cmd"][3] == "-i"
    assert seen["cmd"][4] == str(path)
    assert seen["cmd"][6].startswith("scale=320:-2,scdet=threshold=")


def test_black_detection_parses_valid_intervals_and_rejects_invalid(
    monkeypatch, fake_video
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(
            stdout="black_start:8.12345 black_end:9.87654\n",
            stderr=(
                "black_start:1.234 black_end:2.345\n"
                "black_start:NaN black_end:2.0\n"
                "black_start:4.0 black_end:3.0\n"
                "black_start:5.678 black_end:5.678\n"
                "black_start:malformed black_end:6.0\n"
                "black_start:1.2.3 black_end:6.0\n"
                "black_start:1.0 black_end:2.3.4\n"
            ),
        ),
    )

    assert video_quality.detect_sustained_near_black_segments(str(fake_video)) == [
        (1.234, 2.345),
        (5.678, 5.678),
        (8.123, 9.877),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_duration_seconds": 0},
        {"min_duration_seconds": -1},
        {"min_duration_seconds": float("nan")},
        {"min_duration_seconds": None},
        {"noise_tolerance": -0.1},
        {"noise_tolerance": 2},
        {"noise_tolerance": float("inf")},
        {"timeout_seconds": 0},
        {"timeout_seconds": "bad"},
    ],
)
def test_freeze_detection_rejects_invalid_numeric_inputs(
    monkeypatch, fake_video, kwargs
):
    _set_ffmpeg(monkeypatch)
    assert (
        video_quality.detect_sustained_frozen_segments(str(fake_video), **kwargs)
        is None
    )


def test_freeze_detection_parses_complete_events_and_reconstructs_start_from_end(
    monkeypatch, fake_video
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(
            stderr=(
                "lavfi.freezedetect.freeze_start: 1.0\n"
                "lavfi.freezedetect.freeze_duration: 1.25\n"
                "lavfi.freezedetect.freeze_end: 2.5\n"
                "lavfi.freezedetect.freeze_duration: 2.0\n"
                "lavfi.freezedetect.freeze_end: 5.0\n"
            )
        ),
    )
    assert video_quality.detect_sustained_frozen_segments(str(fake_video)) == [
        (1.0, 2.5),
        (3.0, 5.0),
    ]


def test_freeze_detection_recovers_end_from_start_plus_duration(
    monkeypatch, fake_video
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs_: _completed(
            stderr=(
                "lavfi.freezedetect.freeze_start: 7.0\n"
                "lavfi.freezedetect.freeze_duration: 1.5\n"
                "lavfi.freezedetect.freeze_start: -1.0\n"
                "lavfi.freezedetect.freeze_end: 1e999\n"
                "lavfi.freezedetect.freeze_end: 1.2.3\n"
            )
        ),
    )
    assert video_quality.detect_sustained_frozen_segments(str(fake_video)) == [
        (7.0, 8.5),
    ]


def test_freeze_detection_sorts_deduplicates_and_rejects_incomplete_intervals(
    monkeypatch, fake_video
):
    _set_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        video_quality.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            stderr=(
                "lavfi.freezedetect.freeze_end: 20\n"
                "lavfi.freezedetect.freeze_start: 5.12345\n"
                "lavfi.freezedetect.freeze_end: 6.98765\n"
                "lavfi.freezedetect.freeze_duration: 3\n"
                "lavfi.freezedetect.freeze_end: 2\n"
                "lavfi.freezedetect.freeze_start: 7\n"
                "lavfi.freezedetect.freeze_end: 6\n"
            ),
            stdout=(
                "lavfi.freezedetect.freeze_start: 5.12345\n"
                "lavfi.freezedetect.freeze_end: 6.98765\n"
                "lavfi.freezedetect.freeze_start: 8\n"
            ),
        ),
    )
    assert video_quality.detect_sustained_frozen_segments(str(fake_video)) == [
        (0.0, 2.0),
        (5.123, 6.988),
    ]


@pytest.mark.parametrize("threshold", [0, 100])
def test_scene_threshold_boundaries_are_forwarded(monkeypatch, fake_video, threshold):
    _set_ffmpeg(monkeypatch)

    def fake_run(command, **kwargs):
        assert command[command.index("-vf") + 1] == (
            f"scale=320:-2,scdet=threshold={threshold}"
        )
        return _completed()

    monkeypatch.setattr(video_quality.subprocess, "run", fake_run)
    assert (
        video_quality.detect_scene_change_timestamps(
            str(fake_video), threshold=threshold
        )
        == []
    )
