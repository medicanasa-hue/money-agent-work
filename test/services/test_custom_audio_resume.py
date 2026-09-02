"""Recovery must preserve the selected narration and corrected subtitles."""

import json
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import const
from app.models.schema import VideoParams
from app.services import task as tm
from app.services.state import MemoryState


def write_wav(destination, duration=3.25):
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * int(duration * 8000))


def wav_duration(filename):
    try:
        with wave.open(str(filename), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, wave.Error):
        return 0.0


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    project = tmp_path / "project"
    task_root = project / "tasks"
    directory = task_root / "interrupted"
    directory.mkdir(parents=True)
    original = directory / "custom-audio.wav"
    write_wav(original)
    # Unrelated products from an older attempt must never override the selection.
    write_wav(directory / "audio.normalized.wav", duration=8)
    write_wav(directory / "audio.mp3", duration=9)
    srt = directory / "subtitle.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:03,250\nDüzeltilmiş Türkçe metin.\n",
        encoding="utf-8",
    )
    ass = directory / "subtitle.ass"
    ass.write_text("[Events]\n; Düzeltilmiş karaoke metni.\n", encoding="utf-8")
    state = MemoryState()
    state.update_task("interrupted", state=const.TASK_STATE_FAILED, interrupted=True)
    monkeypatch.setattr(tm.sm, "state", state)
    monkeypatch.setattr(tm.config, "app", {})
    monkeypatch.setattr(tm.utils, "task_dir", lambda part="": str(task_root / part))
    monkeypatch.setattr(tm.utils, "root_dir", lambda: str(project))
    monkeypatch.setattr(tm.voice, "get_audio_duration", wav_duration)
    start = Mock(return_value={"videos": ["final.mp4"]})
    monkeypatch.setattr(tm, "start", start)
    normalize = Mock(side_effect=AssertionError("normalization was not selected"))
    monkeypatch.setattr(tm.voice, "normalize_narration_loudness", normalize)
    return SimpleNamespace(
        root=project,
        directory=directory,
        original=original,
        srt=srt,
        ass=ass,
        state=state,
        start=start,
        normalize=normalize,
    )


def save_checkpoint(recovery, selected_audio, subtitle_style="classic"):
    params = VideoParams(
        video_subject="Kahve",
        video_source="local",
        custom_audio_file=str(selected_audio),
        subtitle_style=subtitle_style,
    )
    checkpoint = {
        "script": "Düzeltilmiş Türkçe metin.",
        "search_terms": [],
        "params": params.model_dump(mode="json"),
    }
    (recovery.directory / "script.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )


@pytest.mark.parametrize("style,extension", [("classic", ".srt"), ("karaoke", ".ass")])
@pytest.mark.parametrize("normalize", [False, True])
def test_resume_preserves_selected_audio_fractional_duration_and_corrected_subtitle(
    recovery, monkeypatch, style, extension, normalize
):
    save_checkpoint(recovery, recovery.original, style)
    saved_subtitles = {item: item.read_bytes() for item in (recovery.srt, recovery.ass)}
    original_bytes = recovery.original.read_bytes()
    monkeypatch.setattr(
        tm.config, "app", {"audio_loudness_normalization_enabled": normalize}
    )

    def normalized_copy(input_path, output_path):
        assert Path(input_path) == recovery.original
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        return output_path

    normalizer = Mock(side_effect=normalized_copy)
    monkeypatch.setattr(tm.voice, "normalize_narration_loudness", normalizer)

    result = tm.resume_interrupted_task("interrupted")

    assert result == {"videos": ["final.mp4"]}
    submitted = recovery.start.call_args.kwargs
    expected_audio = (
        recovery.directory / "audio.normalized.wav" if normalize else recovery.original
    )
    assert submitted["resume_audio_file"] == str(expected_audio)
    assert submitted["resume_audio_duration"] == 3.25
    assert submitted["resume_subtitle_path"] == str(
        recovery.directory / f"subtitle{extension}"
    )
    assert submitted["require_upload_review"] is True
    assert normalizer.call_count == int(normalize)
    assert recovery.original.read_bytes() == original_bytes
    assert all(item.read_bytes() == data for item, data in saved_subtitles.items())


@pytest.mark.parametrize(
    "invalid", ["missing", "unreadable", "zero", "nan", "inf", "negative", "escape"]
)
def test_unusable_selected_audio_does_not_resume_with_old_audio_or_tts(
    recovery, monkeypatch, invalid
):
    selected_audio = recovery.original
    if invalid == "missing":
        selected_audio = recovery.directory / "missing.wav"
    elif invalid == "unreadable":
        selected_audio.write_bytes(b"invalid audio header")
    elif invalid == "escape":
        selected_audio = "../outside.wav"
        write_wav(recovery.root.parent / "outside.wav")
    else:
        durations = {
            "zero": 0,
            "nan": float("nan"),
            "inf": float("inf"),
            "negative": -1,
        }
        real_probe = tm.voice.get_audio_duration
        monkeypatch.setattr(
            tm.voice,
            "get_audio_duration",
            lambda filename: (
                durations[invalid]
                if Path(filename) == recovery.original
                else real_probe(filename)
            ),
        )
    save_checkpoint(recovery, selected_audio)
    before = recovery.state.get_task("interrupted")
    tts = Mock(side_effect=AssertionError("must not synthesize replacement narration"))
    monkeypatch.setattr(tm.voice, "tts", tts)

    assert tm.resume_interrupted_task("interrupted") is None

    recovery.start.assert_not_called()
    recovery.normalize.assert_not_called()
    tts.assert_not_called()
    assert recovery.state.get_task("interrupted") == before


def test_failed_normalization_keeps_selected_original_instead_of_old_output(
    recovery, monkeypatch
):
    save_checkpoint(recovery, recovery.original)
    monkeypatch.setattr(
        tm.config, "app", {"audio_loudness_normalization_enabled": True}
    )
    monkeypatch.setattr(
        tm.voice,
        "normalize_narration_loudness",
        Mock(return_value=str(recovery.original)),
    )

    tm.resume_interrupted_task("interrupted")

    assert recovery.start.call_args.kwargs["resume_audio_file"] == str(
        recovery.original
    )
    assert recovery.start.call_args.kwargs["resume_audio_duration"] == 3.25


@pytest.mark.parametrize("duration", [0, float("nan"), float("inf"), -1])
def test_invalid_normalized_audio_stops_recovery(recovery, monkeypatch, duration):
    save_checkpoint(recovery, recovery.original)
    normalized = recovery.directory / "audio.normalized.wav"
    monkeypatch.setattr(
        tm.config, "app", {"audio_loudness_normalization_enabled": True}
    )
    monkeypatch.setattr(
        tm.voice, "normalize_narration_loudness", Mock(return_value=str(normalized))
    )
    monkeypatch.setattr(
        tm.voice,
        "get_audio_duration",
        lambda filename: 3.25 if Path(filename) == recovery.original else duration,
    )

    assert tm.resume_interrupted_task("interrupted") is None
    recovery.start.assert_not_called()
