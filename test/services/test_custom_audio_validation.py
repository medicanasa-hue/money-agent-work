"""Custom narration must fail before providers run and disclose estimated captions."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.models import const
from app.models.schema import VideoParams
from app.services import task
from app.services.state import MemoryState


@pytest.fixture
def environment(tmp_path, monkeypatch):
    task_root = tmp_path / "tasks"
    task_directory = task_root / "custom-audio"
    task_directory.mkdir(parents=True)
    narration = task_directory / "custom-audio.wav"
    narration.write_bytes(b"duration probing is stubbed")
    monkeypatch.setattr(task.utils, "task_dir", lambda part="": str(task_root / part))
    monkeypatch.setattr(task.utils, "check_ffmpeg_ready", lambda: True)
    monkeypatch.setattr(task.config, "app", {})
    monkeypatch.setattr(task.sm, "state", MemoryState(persist=False))
    monkeypatch.setattr(
        task.voice, "tts", Mock(side_effect=AssertionError("TTS must not run"))
    )
    monkeypatch.setattr(
        task.llm,
        "generate_script",
        Mock(side_effect=AssertionError("LLM must not run")),
    )
    return task_directory, narration


@pytest.mark.parametrize(
    "duration", [float("nan"), float("inf"), -float("inf"), 0, -1, None, "bad"]
)
def test_invalid_duration_is_rejected_before_script_generation(
    environment, monkeypatch, duration
):
    _, narration = environment
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: duration)
    params = VideoParams(
        video_subject="İstanbul", custom_audio_file=str(narration), bgm_type=""
    )

    result = task._start("custom-audio", params, stop_at="audio")

    assert result["state"] == const.TASK_STATE_FAILED
    assert result["failed_stage"] == "audio"
    assert "duration" in result["error"].lower()
    assert "upload" in result["error"].lower()
    assert str(narration) not in result["error"]
    assert task.sm.state.get_task("custom-audio")["error"] == result["error"]
    task.llm.generate_script.assert_not_called()
    task.voice.tts.assert_not_called()


def test_missing_audio_explains_how_to_recover(environment):
    task_directory, _ = environment
    params = VideoParams(
        video_subject="İstanbul",
        custom_audio_file=str(task_directory / "missing.mp3"),
        bgm_type="",
    )

    result = task._start("custom-audio", params, stop_at="audio")

    assert result["failed_stage"] == "audio"
    assert "missing" in result["error"].lower()
    assert "upload" in result["error"].lower()
    task.llm.generate_script.assert_not_called()
    task.voice.tts.assert_not_called()


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, None])
def test_custom_audio_rechecks_duration_after_normalization(
    environment, monkeypatch, duration
):
    task_directory, narration = environment
    normalized = task_directory / "audio.normalized.wav"
    normalized.write_bytes(b"normalized audio")
    monkeypatch.setattr(
        task.config, "app", {"audio_loudness_normalization_enabled": True}
    )
    monkeypatch.setattr(
        task.voice,
        "normalize_narration_loudness",
        lambda *args, **kwargs: str(normalized),
    )
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: duration)
    params = VideoParams(video_subject="İstanbul", custom_audio_file=str(narration))

    assert task.generate_audio("custom-audio", params, "Özgün ses.") == (
        None,
        None,
        None,
    )

    assert task.sm.state.get_task("custom-audio")["failed_stage"] == "audio"
    task.voice.tts.assert_not_called()


@pytest.mark.parametrize("language", ["tr", "de"])
def test_script_timed_captions_write_persistent_estimate_warning(
    environment, monkeypatch, language
):
    task_directory, narration = environment
    script = "İstanbul'da güneş doğuyor. Son kelime tamam."
    params = VideoParams(
        video_subject="İstanbul",
        video_script=script,
        video_language=language,
        custom_audio_file=str(narration),
        subtitle_enabled=True,
    )
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: 1.37)
    monkeypatch.setattr(task.subtitle, "create", Mock(return_value=None))
    monkeypatch.setattr(
        task.subtitle,
        "correct",
        Mock(side_effect=AssertionError("No transcript to correct")),
    )

    result = task.generate_subtitle(
        "custom-audio", params, script, None, str(narration)
    )

    assert Path(result).is_file()
    assert task.subtitle.file_to_subtitles(result)
    report = json.loads(
        (task_directory / "subtitle.review.json").read_text(encoding="utf-8")
    )
    assert report["timing_source"] == "script_estimate"
    assert report["subtitle_count"] > 0
    assert isinstance(report["items"], list)
    task.voice.tts.assert_not_called()


@pytest.mark.parametrize("language", ["tr", "de"])
def test_real_transcript_replaces_previous_estimate_warning(
    environment, monkeypatch, language
):
    task_directory, narration = environment
    script = "İstanbul'da güneş doğuyor."
    params = VideoParams(
        video_subject="İstanbul",
        video_script=script,
        video_language=language,
        custom_audio_file=str(narration),
    )
    report_path = task_directory / "subtitle.review.json"
    report_path.write_text(
        json.dumps({"timing_source": "script_estimate", "items": []}), encoding="utf-8"
    )
    (task_directory / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,370\nÖnceki tahmini metin.\n\n", encoding="utf-8"
    )
    (task_directory / "subtitle.generated.srt").write_bytes(
        (task_directory / "subtitle.srt").read_bytes()
    )

    def transcribe(**kwargs):
        Path(kwargs["subtitle_file"]).write_text(
            f"1\n00:00:00,000 --> 00:00:01,370\n{script}\n\n", encoding="utf-8"
        )

    monkeypatch.setattr(task.subtitle, "create", transcribe)
    monkeypatch.setattr(task.subtitle, "correct", Mock())
    result = task.generate_subtitle(
        "custom-audio", params, script, None, str(narration)
    )

    assert Path(result).is_file()
    assert script in Path(result).read_text(encoding="utf-8")
    assert (
        json.loads(report_path.read_text(encoding="utf-8")).get("timing_source")
        != "script_estimate"
    )


@pytest.mark.parametrize("duration", [float("nan"), float("inf")])
def test_estimated_captions_reject_nonfinite_duration(
    environment, monkeypatch, duration
):
    task_directory, narration = environment
    output = task_directory / "subtitle.srt"
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: duration)
    create = Mock(
        side_effect=AssertionError("Cannot place captions on a non-finite timeline")
    )
    monkeypatch.setattr(task.voice, "create_subtitle", create)

    assert (
        task._create_script_timed_subtitle_for_custom_audio(
            str(narration), "İstanbul.", str(output)
        )
        is False
    )

    assert not output.exists()
    create.assert_not_called()


def test_repeated_whisper_failure_keeps_edited_captions_and_estimate_warning(
    environment, monkeypatch
):
    task_directory, narration = environment
    script = "İstanbul'da güneş doğuyor."
    params = VideoParams(
        video_subject="İstanbul", video_script=script, video_language="tr"
    )
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: 1.37)
    monkeypatch.setattr(task.subtitle, "create", Mock(return_value=None))
    correct = Mock(side_effect=AssertionError("No new transcript to correct"))
    monkeypatch.setattr(task.subtitle, "correct", correct)
    result = task.generate_subtitle(
        "custom-audio", params, script, None, str(narration)
    )
    subtitle_path = Path(result)
    edited = subtitle_path.read_text(encoding="utf-8").replace("güneş", "GÜNEŞ")
    subtitle_path.write_text(edited, encoding="utf-8")
    report_path = task_directory / "subtitle.review.json"
    saved_report = report_path.read_bytes()

    for _ in range(2):
        retried = task.generate_subtitle(
            "custom-audio", params, script, None, str(narration)
        )
        assert Path(retried).read_text(encoding="utf-8") == edited
        assert report_path.read_bytes() == saved_report
    correct.assert_not_called()


def test_failed_whisper_does_not_use_old_karaoke_word_timings(environment, monkeypatch):
    task_directory, narration = environment
    script = "İstanbul'da güneş doğuyor."
    params = VideoParams(
        video_subject="İstanbul", video_language="tr", subtitle_style="karaoke"
    )
    (task_directory / "subtitle.words.json").write_text(
        json.dumps([{"text": "ESKI", "start_time": 0.1, "end_time": 1.2}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _: 1.37)
    monkeypatch.setattr(task.subtitle, "create", Mock(return_value=None))
    monkeypatch.setattr(task.subtitle, "correct", Mock())
    karaoke = Mock(wraps=task.voice.create_karaoke_ass_from_word_timings)
    monkeypatch.setattr(task.voice, "create_karaoke_ass_from_word_timings", karaoke)

    result = task.generate_subtitle(
        "custom-audio", params, script, None, str(narration)
    )

    assert Path(result).suffix == ".ass"
    assert "ESKI" not in Path(result).read_text(encoding="utf-8")
    assert karaoke.call_args.kwargs["word_timings"] == []
