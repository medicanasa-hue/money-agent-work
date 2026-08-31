"""Offline contracts for recovery, material selection and partial video renders."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models import const
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode, VideoParams
from app.services import task as tm
from app.services.state import MemoryState


@pytest.fixture
def environment(tmp_path, monkeypatch):
    task_root = tmp_path / "tasks"

    def task_dir(task_id="", **kwargs):
        directory = task_root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    state = MemoryState()
    monkeypatch.setattr(tm.sm, "state", state)
    monkeypatch.setattr(tm.utils, "task_dir", task_dir)
    monkeypatch.setattr(tm.utils, "check_ffmpeg_ready", lambda: True)
    monkeypatch.setattr(tm.config, "app", {})
    monkeypatch.setattr(tm.config, "ui", {})
    monkeypatch.setattr(
        tm.quality_baseline,
        "refresh_automatic_render_quality_baseline",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        tm.scheduled_job_notifications, "notify_render_quality_attention", Mock()
    )
    monkeypatch.setattr(
        tm.upload_post,
        "upload_post_service",
        SimpleNamespace(
            is_configured=lambda: True,
            auto_upload=True,
            platforms=["youtube"],
            youtube_privacy_status="private",
        ),
    )
    for owner, name in (
        (tm.llm, "generate_script"),
        (tm.llm, "generate_terms"),
        (tm.llm, "generate_scene_queries"),
        (tm.material, "download_videos"),
        (tm.material, "download_selected_videos"),
        (tm.video, "combine_videos"),
        (tm.video, "generate_video"),
        (tm, "generate_audio"),
        (tm, "generate_subtitle"),
        (tm.upload_post, "cross_post_video"),
        (tm, "_schedule_cross_post"),
    ):
        monkeypatch.setattr(
            owner, name, Mock(side_effect=AssertionError("unexpected work"))
        )
    audio = Path(task_dir("edge")) / "audio.mp3"
    audio.write_bytes(b"fixture narration handled by mocked media boundary")
    subtitles = audio.with_name("subtitle.srt")
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nİlk kahve.\n\n"
        "2\n00:00:01,500 --> 00:00:03,250\nGüzel bir gün.\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        state=state, root=task_root, audio=audio, subtitles=subtitles
    )


def resume_inputs(environment):
    return {
        "resume_video_script": "İlk kahve. Güzel bir gün.",
        "resume_video_terms": ["coffee", "morning"],
        "resume_audio_file": str(environment.audio),
        "resume_audio_duration": 3.25,
        "resume_subtitle_path": str(environment.subtitles),
    }


def test_partial_aspect_render_keeps_outputs_and_requires_upload_review(
    environment, monkeypatch
):
    params = VideoParams(
        video_subject="Kahve",
        video_aspects=[VideoAspect.portrait, VideoAspect.landscape],
        smart_scene_queries=True,
        voice_name="no-voice",
        bgm_type="",
        outro_image_file="outro.png",
        outro_duration=2,
    )
    monkeypatch.setattr(
        tm.material,
        "download_videos",
        Mock(side_effect=[["portrait.mp4"], []]),
    )
    combined_inputs = []

    def combine(**kwargs):
        combined_inputs.append(kwargs)
        Path(kwargs["combined_video_path"]).write_bytes(b"combined fixture")

    def render(**kwargs):
        if kwargs["video_aspect"] == VideoAspect.landscape:
            raise RuntimeError("landscape encoder failed")
        Path(kwargs["output_file"]).write_bytes(b"final fixture")
        return {
            "configured_codec": "h264_nvenc",
            "used_codec": "libx264",
            "fallback_used": True,
        }

    monkeypatch.setattr(tm.video, "combine_videos", combine)
    monkeypatch.setattr(tm.video, "generate_video", render)
    inspect = Mock(return_value={"ok": True, "warnings": []})
    monkeypatch.setattr(tm.render_quality, "inspect_rendered_video", inspect)

    result = tm.start(
        "edge", params, require_upload_review=True, **resume_inputs(environment)
    )

    saved = environment.state.get_task("edge")
    assert saved["state"] == const.TASK_STATE_COMPLETE
    assert saved["partial_success"] is True
    assert saved["failed_aspects"] == ["16:9"]
    assert len(result["videos"]) == 1
    assert Path(result["videos"][0]).name == "final-1-9x16.mp4"
    assert Path(result["videos"][0]).read_bytes() == b"final fixture"
    assert saved["pending_uploads"][0]["video_path"] == result["videos"][0]
    assert saved["pending_uploads"][0]["status"] == "pending"
    assert saved["video_encoder_results"][0]["fallback_used"] is True
    assert saved["materials_by_aspect"] == {"9:16": ["portrait.mp4"]}
    assert all(item["video_paths"] == ["portrait.mp4"] for item in combined_inputs)
    assert all(
        item["video_concat_mode"] == VideoConcatMode.sequential
        for item in combined_inputs
    )
    assert combined_inputs[0]["cue_end_times"] == [1.5, 3.25]
    assert inspect.call_args.kwargs["expected_duration"] == 5.25
    assert inspect.call_args.kwargs["allow_silent_audio"] is True
    tm._schedule_cross_post.assert_not_called()
    checkpoint = json.loads(
        (environment.root / "edge" / "script.json").read_text(encoding="utf-8")
    )
    assert checkpoint["script"] == "İlk kahve. Güzel bir gün."
    assert checkpoint["params"]["video_aspects"] == ["9:16", "16:9"]


def test_all_render_preparations_fail_with_identifiable_aspects(
    environment, monkeypatch
):
    params = VideoParams(
        video_subject="Kahve",
        bgm_type="",
        video_aspects=[VideoAspect.portrait, VideoAspect.landscape],
    )
    monkeypatch.setattr(tm.material, "download_videos", Mock(return_value=["clip.mp4"]))
    monkeypatch.setattr(
        tm.video, "combine_videos", Mock(side_effect=OSError("unavailable encoder"))
    )

    result = tm.start("edge", params, **resume_inputs(environment))

    assert result["state"] == const.TASK_STATE_FAILED
    assert result["failed_stage"] == "video"
    assert result["failed_aspects"] == ["9:16", "16:9"]
    assert (
        environment.state.get_task("edge")["failed_aspects"] == result["failed_aspects"]
    )
    assert not result.get("videos")
    tm.video.generate_video.assert_not_called()
    tm._schedule_cross_post.assert_not_called()


def test_missing_primary_materials_do_not_publish_secondary_only_video(
    environment, monkeypatch
):
    params = VideoParams(
        video_subject="Kahve",
        bgm_type="",
        video_aspects=[VideoAspect.portrait, VideoAspect.landscape],
    )
    monkeypatch.setattr(
        tm.material, "download_videos", Mock(side_effect=[[], ["wide.mp4"]])
    )

    result = tm.start("edge", params, **resume_inputs(environment))

    assert result["state"] == const.TASK_STATE_FAILED
    assert result["failed_stage"] == "materials"
    tm.video.combine_videos.assert_not_called()
    tm._schedule_cross_post.assert_not_called()


def test_material_stop_preserves_attribution_cooldown_and_aspect_map(
    environment, monkeypatch
):
    params = VideoParams(
        video_subject="Kahve",
        video_aspects=[VideoAspect.portrait, VideoAspect.landscape],
    )

    def download(**kwargs):
        kwargs["cooldown_stats"].update(moved_recent_count=2, days="legacy-invalid")
        kwargs["attribution_records"].append({"attribution": "Fixture author"})
        return [f"{kwargs['video_aspect'].value}.mp4"]

    monkeypatch.setattr(tm.material, "download_videos", download)

    result = tm.start("edge", params, stop_at="materials", **resume_inputs(environment))

    assert result["materials"] == ["9:16.mp4"]
    assert result["materials_by_aspect"]["16:9"] == ["16:9.mp4"]
    assert result["cooldown"] == {"moved_recent_count": 2, "days": 7}
    assert result["material_attributions"] == [{"attribution": "Fixture author"}] * 2
    assert environment.state.get_task("edge")["state"] == const.TASK_STATE_COMPLETE
    tm.video.combine_videos.assert_not_called()


@pytest.mark.parametrize("available", [True, False])
def test_selected_online_materials_do_not_fall_back_to_unselected_search(
    environment, monkeypatch, available
):
    selected = MaterialInfo(
        provider="pexels", url="https://example.invalid/selected.mp4"
    )
    params = VideoParams(
        video_subject="Kahve",
        video_count=2,
        video_materials=[MaterialInfo(provider="local", url="ignored.mp4"), selected],
    )
    download = Mock(return_value=["selected-local.mp4"] if available else [])
    monkeypatch.setattr(tm.material, "download_selected_videos", download)

    result = tm.get_video_materials("edge", params, ["unused"], 3.25)

    assert result == (["selected-local.mp4"] if available else None)
    assert download.call_args.kwargs["selected_items"] == [selected]
    assert download.call_args.kwargs["audio_duration"] == 6.5
    tm.material.download_videos.assert_not_called()
    if not available:
        assert environment.state.get_task("edge")["state"] == const.TASK_STATE_FAILED


@pytest.mark.parametrize("available", [True, False])
def test_local_materials_keep_credit_or_fail_without_network(
    environment, monkeypatch, available
):
    item = MaterialInfo(
        provider="local", url="owned.mp4", attribution="Owner", license="CC BY"
    )
    params = VideoParams(
        video_subject="Kahve", video_source="local", video_materials=[item]
    )
    monkeypatch.setattr(
        tm.video, "preprocess_video", Mock(return_value=[item] if available else [])
    )
    credits = []

    result = tm.get_video_materials(
        "edge", params, [], 3, material_attributions=credits
    )

    assert result == (["owned.mp4"] if available else None)
    assert credits == (
        [
            {
                "video_path": "owned.mp4",
                "provider": "local",
                "title": "",
                "license": "CC BY",
                "license_url": "",
                "attribution": "Owner",
                "source_url": "owned.mp4",
            }
        ]
        if available
        else []
    )
    tm.material.download_videos.assert_not_called()
    if not available:
        assert environment.state.get_task("edge")["state"] == const.TASK_STATE_FAILED


@pytest.mark.parametrize(
    "saved_data",
    [
        [],
        {"script": "", "params": {}},
        {"script": "Saved", "params": []},
        {"script": "Saved", "params": {"video_subject": "Coffee", "video_fps": 0}},
        {"script": "Saved", "params": {"video_subject": "Coffee"}},
        {
            "script": "Saved",
            "params": {"video_subject": "Coffee"},
            "search_terms": [None],
        },
        {
            "script": "Saved",
            "params": {"video_subject": "Coffee"},
            "search_terms": [" "],
        },
    ],
)
def test_invalid_checkpoints_leave_interrupted_task_untouched(environment, saved_data):
    checkpoint = environment.root / "edge" / "script.json"
    checkpoint.write_text(json.dumps(saved_data), encoding="utf-8")
    environment.state.update_task(
        "edge", state=const.TASK_STATE_FAILED, interrupted=True
    )
    before = environment.state.get_task("edge")

    assert tm.resume_interrupted_task("edge") is None
    assert environment.state.get_task("edge") == before
    tm.llm.generate_script.assert_not_called()
    tm.video.combine_videos.assert_not_called()


@pytest.mark.parametrize("contents", [b"{", b"\xff", None])
def test_unreadable_or_missing_checkpoint_never_restarts_generation(
    environment, contents
):
    if contents is not None:
        (environment.root / "edge" / "script.json").write_bytes(contents)
    environment.state.update_task(
        "edge", state=const.TASK_STATE_FAILED, interrupted=True
    )

    assert tm.resume_interrupted_task("edge") is None
    tm.llm.generate_script.assert_not_called()


def test_local_checkpoint_does_not_require_online_search_terms(environment):
    checkpoint = {
        "script": "Kayıtlı metin.",
        "params": {"video_subject": "Kahve", "video_source": "local"},
    }
    (environment.root / "edge" / "script.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )

    params, script, terms = tm._load_resume_checkpoint("edge")

    assert params.video_source == "local"
    assert script == "Kayıtlı metin."
    assert terms == []


@pytest.mark.parametrize(
    "subtitle_enabled,expected_subtitle", [(True, "subtitle.ass"), (False, "")]
)
def test_resume_skips_unreadable_normalized_audio_and_keeps_safe_fallback(
    environment, monkeypatch, subtitle_enabled, expected_subtitle
):
    directory = environment.root / "edge"
    (directory / "audio.normalized.wav").write_bytes(b"broken audio")
    (directory / "subtitle.ass").write_text("fixture ASS", encoding="utf-8")
    params = VideoParams(
        video_subject="Kahve",
        subtitle_style="karaoke",
        subtitle_enabled=subtitle_enabled,
    )
    monkeypatch.setattr(
        tm.voice,
        "get_audio_duration",
        Mock(side_effect=[ValueError("bad header"), 3.25]),
    )

    audio, duration, subtitles = tm._load_resume_media(str(directory), params)

    assert audio == str(environment.audio)
    assert duration == 4
    assert subtitles == (
        str(directory / expected_subtitle) if expected_subtitle else ""
    )


def test_karaoke_aspect_variant_preserves_dialogue_and_srt_fallback(environment):
    source = environment.root / "edge" / "subtitle.ass"
    source.write_text(
        "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:03.25,Default,,0,0,0,,Kahve güzel.\n",
        encoding="utf-8",
    )

    variant = tm._karaoke_subtitle_path_for_aspect(
        "edge", str(source), VideoAspect.landscape
    )

    assert Path(variant).name == "subtitle-16x9.ass"
    assert "PlayResX: 1920" in Path(variant).read_text(encoding="utf-8")
    assert "Kahve güzel." in Path(variant).read_text(encoding="utf-8")
    assert (
        Path(variant).with_suffix(".srt").read_bytes()
        == environment.subtitles.read_bytes()
    )
    assert tm._subtitle_cue_end_times(variant) == [1.5, 3.25]


@pytest.mark.parametrize("failure", ["missing_events", "copy_failure"])
def test_karaoke_variant_failure_keeps_original_subtitles(
    environment, monkeypatch, failure
):
    source = environment.root / "edge" / "subtitle.ass"
    source.write_text("no events", encoding="utf-8")
    if failure == "copy_failure":
        monkeypatch.setattr(
            tm.voice, "create_karaoke_ass_variant", lambda **kwargs: True
        )
        monkeypatch.setattr(
            tm.shutil, "copyfile", Mock(side_effect=OSError("read only target"))
        )

    assert tm._karaoke_subtitle_path_for_aspect(
        "edge", str(source), VideoAspect.square
    ) == str(source)
    assert source.read_text(encoding="utf-8") == "no events"


def test_failed_quality_inspection_does_not_remove_completed_video(
    environment, monkeypatch
):
    def render(**kwargs):
        Path(kwargs["output_file"]).write_bytes(b"completed video fixture")
        return {"used_codec": "libx264"}

    monkeypatch.setattr(tm.video, "combine_videos", Mock())
    monkeypatch.setattr(tm.video, "generate_video", render)
    monkeypatch.setattr(
        tm.render_quality,
        "inspect_rendered_video",
        Mock(side_effect=OSError("probe failed")),
    )
    quality_reports = []

    videos, combined, warnings = tm.generate_final_videos(
        "edge",
        VideoParams(video_subject="Kahve", bgm_type=""),
        ["clip.mp4"],
        str(environment.audio),
        "",
        expected_audio_duration=3.25,
        render_quality_reports=quality_reports,
    )

    assert len(videos) == len(combined) == 1
    assert Path(videos[0]).read_bytes() == b"completed video fixture"
    assert warnings == []
    assert quality_reports == [
        {
            "video_path": videos[0],
            "ok": False,
            "warnings": ["rendered video could not be inspected"],
        }
    ]


@pytest.mark.parametrize(
    "update",
    [None, {"notification_summary": {"warnings": 1}}, RuntimeError("disk full")],
)
def test_quality_baseline_or_notification_failure_does_not_raise(
    environment, monkeypatch, update
):
    refresh = (
        Mock(side_effect=update)
        if isinstance(update, Exception)
        else Mock(return_value=update)
    )
    monkeypatch.setattr(
        tm.quality_baseline, "refresh_automatic_render_quality_baseline", refresh
    )
    notification = Mock(side_effect=OSError("offline"))
    monkeypatch.setattr(
        tm.scheduled_job_notifications, "notify_render_quality_attention", notification
    )

    tm._refresh_automatic_render_quality_baseline([{"ok": False}])

    assert refresh.call_count == 1
    assert notification.call_count == (1 if isinstance(update, dict) else 0)
