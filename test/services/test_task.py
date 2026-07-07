import unittest
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import task as tm
from app.services import video as vd
from app.models.schema import MaterialInfo, VideoConcatMode, VideoParams
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")
RUN_INTEGRATION_TESTS = os.environ.get("MPT_RUN_INTEGRATION_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}

class TestTaskService(unittest.TestCase):
    def setUp(self):
        pass
    
    def tearDown(self):
        pass

    def _run_start_with_upload_config(self, require_review, material_attribution_records=None):
        params = VideoParams(
            video_subject="upload review",
            video_script="",
            video_source="pexels",
        )
        mock_state = SimpleNamespace(update_task=MagicMock())
        mock_upload_service = SimpleNamespace(
            is_configured=MagicMock(return_value=True),
            auto_upload=True,
            platforms=["youtube"],
            youtube_privacy_status="unlisted",
        )

        def fake_get_video_materials(*_args, material_attributions=None, **_kwargs):
            if material_attributions is not None and material_attribution_records:
                material_attributions.extend(material_attribution_records)
            return ["material.mp4"]

        with (
            patch.object(
                tm.config,
                "app",
                dict(tm.config.app, upload_post_require_review=require_review),
            ),
            patch.object(tm.sm, "state", mock_state),
            patch.object(tm.upload_post, "upload_post_service", mock_upload_service),
            patch.object(tm, "generate_script", return_value="script"),
            patch.object(tm, "generate_terms", return_value=["term"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 10, None),
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(tm, "get_video_materials", side_effect=fake_get_video_materials),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final-a.mp4", "final-b.mp4"], ["combined.mp4"]),
            ),
            patch.object(
                tm.llm,
                "generate_social_metadata",
                return_value={
                    "title": "Upload title",
                    "caption": "Upload caption",
                    "hashtags": ["shorts"],
                },
            ) as generate_metadata,
            patch.object(
                tm.upload_post,
                "cross_post_video",
                return_value={"success": True},
            ) as cross_post,
        ):
            result = tm.start("upload-review-task", params)

        return result, cross_post, generate_metadata

    def test_start_queues_pending_uploads_when_review_required(self):
        result, cross_post, generate_metadata = self._run_start_with_upload_config(
            require_review=True
        )

        cross_post.assert_not_called()
        generate_metadata.assert_not_called()
        self.assertIsNone(result["cross_post_results"])
        self.assertEqual(
            result["pending_uploads"],
            [
                {
                    "video_path": "final-a.mp4",
                    "title": "upload review",
                    "platforms": ["youtube"],
                    "status": "pending",
                },
                {
                    "video_path": "final-b.mp4",
                    "title": "upload review",
                    "platforms": ["youtube"],
                    "status": "pending",
                },
            ],
        )

    def test_start_cross_posts_when_review_is_disabled(self):
        result, cross_post, generate_metadata = self._run_start_with_upload_config(
            require_review=False
        )

        self.assertEqual(cross_post.call_count, 2)
        generate_metadata.assert_called_once()
        self.assertEqual(result["cross_post_results"], [{"success": True}, {"success": True}])
        self.assertIsNone(result["pending_uploads"])

    def test_start_adds_material_attributions_to_youtube_description(self):
        attribution_records = [
            {
                "provider": "wikimedia",
                "title": "City clip",
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "attribution": "City clip - Jane Doe - CC BY-SA 4.0",
                "source_url": "https://commons.wikimedia.org/wiki/File:City.webm",
            }
        ]
        result, cross_post, _generate_metadata = self._run_start_with_upload_config(
            require_review=False,
            material_attribution_records=attribution_records,
        )

        self.assertEqual(result["material_attributions"], attribution_records)
        youtube_extra = cross_post.call_args.kwargs["youtube_extra"]
        self.assertIn("Upload caption", youtube_extra["youtube_description"])
        self.assertIn("Credits:", youtube_extra["youtube_description"])
        self.assertIn(
            "City clip - Jane Doe - CC BY-SA 4.0",
            youtube_extra["youtube_description"],
        )

    def test_generate_terms_falls_back_when_smart_scene_queries_are_empty(self):
        params = VideoParams(
            video_subject="budget planning",
            video_script="",
            video_source="pexels",
            smart_scene_queries=True,
        )

        with (
            patch.object(tm.llm, "generate_scene_queries", return_value=[]) as scene_queries,
            patch.object(tm.llm, "generate_terms", return_value=["fallback term"]) as fallback_terms,
        ):
            result = tm.generate_terms(
                task_id="smart-scene-fallback",
                params=params,
                video_script="First bills, then savings.",
            )

        self.assertEqual(result, ["fallback term"])
        scene_queries.assert_called_once_with(
            video_subject="budget planning",
            video_script="First bills, then savings.",
            amount=8,
            language="",
        )
        fallback_terms.assert_called_once_with(
            video_subject="budget planning",
            video_script="First bills, then savings.",
            amount=8,
            match_script_order=True,
        )

    def test_generate_subtitle_returns_ass_when_karaoke_ass_created(self):
        def fake_create_karaoke_subtitle(text, sub_maker, subtitle_file):
            Path(subtitle_file).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello world\n",
                encoding="utf-8",
            )
            return True

        def fake_create_karaoke_ass_subtitle(sub_maker, subtitle_file):
            Path(subtitle_file).write_text(
                "[Script Info]\n[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Karaoke,,0,0,0,,{\\kf100}Hello world\n",
                encoding="utf-8",
            )
            return True

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            tm.config,
            "app",
            dict(tm.config.app, subtitle_provider="edge"),
        ), patch(
            "app.utils.utils.task_dir",
            lambda tid="": str(Path(tmp_dir) / tid) if tid else str(Path(tmp_dir)),
        ), patch.object(
            tm.voice,
            "create_karaoke_subtitle",
            side_effect=fake_create_karaoke_subtitle,
        ) as create_srt, patch.object(
            tm.voice,
            "create_karaoke_ass_subtitle",
            side_effect=fake_create_karaoke_ass_subtitle,
        ) as create_ass, patch.object(
            tm.subtitle, "create"
        ) as whisper_create:
            task_id = "karaoke-ass-task"
            Path(tmp_dir, task_id).mkdir(parents=True, exist_ok=True)

            subtitle_path = tm.generate_subtitle(
                task_id=task_id,
                params=SimpleNamespace(subtitle_enabled=True, subtitle_style="karaoke"),
                video_script="Hello world.",
                sub_maker=SimpleNamespace(cues=[object()]),
                audio_file="",
            )
            subtitle_exists = Path(subtitle_path).exists()
            srt_exists = Path(subtitle_path).with_suffix(".srt").exists()

        self.assertTrue(subtitle_path.endswith("subtitle.ass"))
        self.assertTrue(subtitle_exists)
        self.assertTrue(srt_exists)
        create_srt.assert_called_once()
        create_ass.assert_called_once()
        whisper_create.assert_not_called()

    def test_generate_subtitle_returns_srt_when_karaoke_ass_fails(self):
        def fake_create_karaoke_subtitle(text, sub_maker, subtitle_file):
            Path(subtitle_file).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello world\n",
                encoding="utf-8",
            )
            return True

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            tm.config,
            "app",
            dict(tm.config.app, subtitle_provider="edge"),
        ), patch(
            "app.utils.utils.task_dir",
            lambda tid="": str(Path(tmp_dir) / tid) if tid else str(Path(tmp_dir)),
        ), patch.object(
            tm.voice,
            "create_karaoke_subtitle",
            side_effect=fake_create_karaoke_subtitle,
        ), patch.object(
            tm.voice,
            "create_karaoke_ass_subtitle",
            return_value=False,
        ) as create_ass, patch.object(
            tm.subtitle, "create"
        ) as whisper_create:
            task_id = "karaoke-ass-fallback-task"
            Path(tmp_dir, task_id).mkdir(parents=True, exist_ok=True)

            subtitle_path = tm.generate_subtitle(
                task_id=task_id,
                params=SimpleNamespace(subtitle_enabled=True, subtitle_style="karaoke"),
                video_script="Hello world.",
                sub_maker=SimpleNamespace(cues=[object()]),
                audio_file="",
            )
            subtitle_exists = Path(subtitle_path).exists()

        self.assertTrue(subtitle_path.endswith("subtitle.srt"))
        self.assertTrue(subtitle_exists)
        create_ass.assert_called_once()
        whisper_create.assert_not_called()

    def test_generate_script_forwards_advanced_prompt_options(self):
        """
        任务生成入口和 WebUI/API 共用 VideoParams。这里验证自动生成文案时，
        高级提示词参数会继续传到 LLM 服务层，避免只在 /scripts 接口生效。
        """
        params = VideoParams(
            video_subject="咖啡",
            video_script="",
            video_language="zh-CN",
            paragraph_number=2,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
        )

        with patch.object(tm.llm, "generate_script", return_value="生成的文案") as generate:
            result = tm.generate_script("task-id", params)

        self.assertEqual(result, "生成的文案")
        generate.assert_called_once_with(
            video_subject="咖啡",
            language="zh-CN",
            paragraph_number=2,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
        )

    def test_start_applies_video_quality_params_without_persisting_config(self):
        params = VideoParams(
            video_subject="quality",
            video_script="",
            video_source="pexels",
            video_codec="h264_nvenc",
            video_crf=18,
            video_encoder_preset="slow",
            video_fps=60,
            audio_bitrate="256k",
        )
        mock_state = SimpleNamespace(update_task=MagicMock())
        mock_upload_service = SimpleNamespace(
            is_configured=MagicMock(return_value=False),
            auto_upload=False,
        )

        def generate_final_videos_with_quality(*_args, **_kwargs):
            self.assertEqual(vd._get_configured_video_codec(), "h264_nvenc")
            self.assertEqual(vd._get_configured_libx264_crf(), "18")
            self.assertEqual(vd._get_configured_libx264_preset(), "slow")
            self.assertEqual(vd._get_configured_video_fps(), 60)
            self.assertEqual(vd._get_configured_audio_bitrate(), "256k")
            return ["final.mp4"], ["combined.mp4"]

        with (
            patch.dict(
                config.app,
                {
                    "video_codec": "libx264",
                    "video_crf": 24,
                    "video_encoder_preset": "fast",
                    "video_fps": 24,
                    "audio_bitrate": "128k",
                },
                clear=False,
            ),
            patch.object(tm.sm, "state", mock_state),
            patch.object(tm.upload_post, "upload_post_service", mock_upload_service),
            patch.object(tm, "generate_script", return_value="script"),
            patch.object(tm, "generate_terms", return_value=["term"]),
            patch.object(tm, "save_script_data"),
            patch.object(tm, "generate_audio", return_value=("audio.mp3", 10, None)),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(tm, "get_video_materials", return_value=["material.mp4"]),
            patch.object(
                tm,
                "generate_final_videos",
                side_effect=generate_final_videos_with_quality,
            ),
        ):
            result = tm.start("quality-task", params)

            self.assertEqual(result["videos"], ["final.mp4"])
            self.assertEqual(config.app["video_codec"], "libx264")
            self.assertEqual(config.app["video_crf"], 24)
            self.assertEqual(config.app["video_encoder_preset"], "fast")
            self.assertEqual(config.app["video_fps"], 24)
            self.assertEqual(config.app["audio_bitrate"], "128k")

        self.assertEqual(
            vd._get_configured_video_codec(),
            config.app.get("video_codec", "libx264"),
        )

    def test_generate_terms_uses_script_order_mode_when_enabled(self):
        """
        默认模式不受影响；只有用户显式开启素材按文案顺序匹配时，任务层才
        要求 LLM 生成有序关键词，并适当增加关键词数量以覆盖更多脚本片段。
        """
        params = VideoParams(
            video_subject="城市通勤",
            video_script="",
            match_materials_to_script=True,
        )

        with patch.object(tm.llm, "generate_terms", return_value=["city", "train"]) as generate:
            result = tm.generate_terms("task-id", params, "先城市，再地铁")

        self.assertEqual(result, ["city", "train"])
        generate.assert_called_once_with(
            video_subject="城市通勤",
            video_script="先城市，再地铁",
            amount=8,
            match_script_order=True,
        )
    
    def test_generate_terms_uses_smart_scene_queries_when_enabled(self):
        params = VideoParams(
            video_subject="inflation",
            video_script="",
            smart_scene_queries=True,
        )

        with (
            patch.object(
                tm.llm,
                "generate_scene_queries",
                return_value=["family bills kitchen", "market prices rising"],
            ) as generate_scene_queries,
            patch.object(tm.llm, "generate_terms") as generate_terms,
        ):
            result = tm.generate_terms("task-id", params, "Aile butcesi zorlanir.")

        self.assertEqual(result, ["family bills kitchen", "market prices rising"])
        generate_scene_queries.assert_called_once_with(
            video_subject="inflation",
            video_script="Aile butcesi zorlanir.",
            amount=8,
            language="",
        )
        generate_terms.assert_not_called()

    def test_get_video_materials_treats_smart_scene_queries_as_ordered(self):
        params = VideoParams(
            video_subject="inflation",
            video_script="",
            video_source="pexels",
            smart_scene_queries=True,
            video_clip_duration=5,
        )

        with patch.object(
            tm.material,
            "download_videos",
            return_value=["clip-1.mp4"],
        ) as download_videos:
            cooldown_stats = {"moved_recent_count": 0}
            result = tm.get_video_materials(
                "task-id",
                params,
                ["family bills kitchen"],
                audio_duration=10,
                cooldown_stats=cooldown_stats,
            )

        self.assertEqual(result, ["clip-1.mp4"])
        kwargs = download_videos.call_args.kwargs
        self.assertTrue(kwargs["match_script_order"])
        self.assertEqual(kwargs["video_concat_mode"], VideoConcatMode.sequential)
        self.assertIs(kwargs["cooldown_stats"], cooldown_stats)

    def test_generate_audio_uses_custom_file_inside_task_directory(self):
        task_id = "test-custom-audio-safe"
        task_dir = utils.task_dir(task_id)
        custom_audio_file = os.path.join(task_dir, "custom-audio.mp3")
        with open(custom_audio_file, "wb") as audio:
            audio.write(b"fake audio")

        params = VideoParams(
            video_subject="custom audio",
            video_script="",
            custom_audio_file=custom_audio_file,
            voice_name="test-voice",
        )

        try:
            with (
                patch.object(tm.voice, "tts") as tts,
                patch.object(tm.voice, "get_audio_duration", return_value=7),
            ):
                audio_file, audio_duration, sub_maker = tm.generate_audio(
                    task_id, params, "script"
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(audio_file, os.path.realpath(custom_audio_file))
        self.assertEqual(audio_duration, 7)
        self.assertIsNone(sub_maker)
        tts.assert_not_called()

    def test_generate_audio_accepts_server_side_custom_file(self):
        task_id = "test-custom-audio-server-side"
        task_dir = utils.task_dir(task_id)

        with tempfile.NamedTemporaryFile(suffix=".mp3") as server_audio:
            server_audio.write(b"fake audio")
            server_audio.flush()
            params = VideoParams(
                video_subject="custom audio",
                video_script="",
                custom_audio_file=server_audio.name,
                voice_name="test-voice",
            )

            try:
                with (
                    patch.object(tm.voice, "tts") as tts,
                    patch.object(tm.voice, "get_audio_duration", return_value=6),
                ):
                    audio_file, audio_duration, result_sub_maker = tm.generate_audio(
                        task_id, params, "script"
                    )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(audio_file, os.path.realpath(server_audio.name))
        self.assertEqual(audio_duration, 6)
        self.assertIsNone(result_sub_maker)
        tts.assert_not_called()

    def test_generate_audio_rejects_missing_custom_file_without_tts(self):
        task_id = "test-custom-audio-missing"
        task_dir = utils.task_dir(task_id)
        missing_audio_file = os.path.join(task_dir, "missing.mp3")
        params = VideoParams(
            video_subject="custom audio",
            video_script="",
            custom_audio_file=missing_audio_file,
            voice_name="test-voice",
        )

        try:
            with (
                patch.object(tm.voice, "tts") as tts,
                patch.object(tm.sm.state, "update_task") as update_task,
            ):
                audio_file, audio_duration, result_sub_maker = tm.generate_audio(
                    task_id, params, "script"
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertIsNone(audio_file)
        self.assertIsNone(audio_duration)
        self.assertIsNone(result_sub_maker)
        tts.assert_not_called()
        update_task.assert_called_with(task_id, state=tm.const.TASK_STATE_FAILED)

    def test_generate_subtitle_uses_whisper_for_custom_audio_without_sub_maker(self):
        """
        自定义音频不会经过 TTS，所以没有 sub_maker。
        Whisper 可以直接从音频文件转写，此时不能被 sub_maker 为空的保护逻辑提前跳过。
        """
        task_id = "test-custom-audio-whisper-subtitle"
        task_dir = utils.task_dir(task_id)
        audio_file = os.path.join(task_dir, "custom-audio.mp3")
        Path(audio_file).write_bytes(b"fake audio")
        params = VideoParams(
            video_subject="custom audio",
            video_script="Hello world.",
            subtitle_enabled=True,
        )

        def fake_whisper_create(audio_file, subtitle_file):
            Path(subtitle_file).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n\n",
                encoding="utf-8",
            )

        try:
            with (
                patch.object(
                    tm.config,
                    "app",
                    dict(tm.config.app, subtitle_provider="whisper"),
                ),
                patch.object(
                    tm.subtitle, "create", side_effect=fake_whisper_create
                ) as create,
                patch.object(tm.subtitle, "correct") as correct,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=None,
                    audio_file=audio_file,
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertTrue(subtitle_path.endswith("subtitle.srt"))
        create.assert_called_once_with(audio_file=audio_file, subtitle_file=subtitle_path)
        correct.assert_called_once_with(
            subtitle_file=subtitle_path, video_script="Hello world."
        )

    def test_generate_subtitle_skips_edge_provider_without_sub_maker(self):
        """
        Edge 字幕依赖 TTS 返回的 sub_maker 时间轴。
        自定义音频缺少该对象时应继续跳过，避免产生不可信的字幕时间轴。
        """
        task_id = "test-custom-audio-edge-no-submaker"
        task_dir = utils.task_dir(task_id)
        audio_file = os.path.join(task_dir, "custom-audio.mp3")
        Path(audio_file).write_bytes(b"fake audio")
        params = VideoParams(
            video_subject="custom audio",
            video_script="Hello world.",
            subtitle_enabled=True,
        )

        try:
            with (
                patch.object(
                    tm.config,
                    "app",
                    dict(
                        tm.config.app,
                        subtitle_provider="edge",
                        custom_audio_subtitle_provider="none",
                    ),
                ),
                patch.object(tm.voice, "create_subtitle") as create_subtitle,
                patch.object(tm.subtitle, "create") as whisper_create,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=None,
                    audio_file=audio_file,
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(subtitle_path, "")
        create_subtitle.assert_not_called()
        whisper_create.assert_not_called()

    def test_generate_subtitle_uses_whisper_for_custom_audio_by_default(self):
        task_id = "test-custom-audio-default-whisper"
        task_dir = utils.task_dir(task_id)
        audio_file = os.path.join(task_dir, "custom-audio.mp3")
        Path(audio_file).write_bytes(b"fake audio")
        params = VideoParams(
            video_subject="custom audio",
            video_script="Hello world.",
            subtitle_enabled=True,
        )

        def fake_whisper_create(audio_file, subtitle_file):
            Path(subtitle_file).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n\n",
                encoding="utf-8",
            )

        app_config = dict(tm.config.app, subtitle_provider="edge")
        app_config.pop("custom_audio_subtitle_provider", None)
        try:
            with (
                patch.object(tm.config, "app", app_config),
                patch.object(
                    tm.subtitle, "create", side_effect=fake_whisper_create
                ) as create,
                patch.object(tm.subtitle, "correct") as correct,
                patch.object(tm.voice, "create_subtitle") as create_subtitle,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=None,
                    audio_file=audio_file,
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertTrue(subtitle_path.endswith("subtitle.srt"))
        create.assert_called_once_with(audio_file=audio_file, subtitle_file=subtitle_path)
        correct.assert_called_once_with(
            subtitle_file=subtitle_path, video_script="Hello world."
        )
        create_subtitle.assert_not_called()

    def test_get_video_materials_uses_selected_online_materials(self):
        selected_materials = [
            MaterialInfo(
                provider="pexels",
                url="https://v.example/manual.mp4",
                duration=5,
                title="Manual clip",
                license="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0/",
                attribution="Manual clip - Creator - CC BY 4.0",
            )
        ]
        params = VideoParams(
            video_subject="manual materials",
            video_script="script",
            video_source="pexels",
            video_materials=selected_materials,
            video_clip_duration=5,
            video_count=1,
        )

        material_attributions = []

        def fake_selected_download(*, attribution_records=None, **kwargs):
            attribution_records.append(
                {
                    "video_path": "/tmp/manual.mp4",
                    "provider": "pexels",
                    "title": "Manual clip",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Manual clip - Creator - CC BY 4.0",
                    "source_url": "https://v.example/manual.mp4",
                }
            )
            return ["/tmp/manual.mp4"]

        with (
            patch.object(
                tm.material,
                "download_selected_videos",
                side_effect=fake_selected_download,
            ) as selected_download,
            patch.object(tm.material, "download_videos") as auto_download,
        ):
            result = tm.get_video_materials(
                "manual-material-task",
                params,
                ["city"],
                audio_duration=5,
                material_attributions=material_attributions,
            )

        self.assertEqual(result, ["/tmp/manual.mp4"])
        self.assertEqual(
            material_attributions,
            [
                {
                    "video_path": "/tmp/manual.mp4",
                    "provider": "pexels",
                    "title": "Manual clip",
                    "license": "CC BY 4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Manual clip - Creator - CC BY 4.0",
                    "source_url": "https://v.example/manual.mp4",
                }
            ],
        )
        selected_download.assert_called_once_with(
            task_id="manual-material-task",
            selected_items=selected_materials,
            audio_duration=5,
            max_clip_duration=5,
            attribution_records=material_attributions,
        )
        auto_download.assert_not_called()

    @unittest.skipUnless(
        RUN_INTEGRATION_TESTS,
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_task_local_materials(self):
        task_id = "00000000-0000-0000-0000-000000000000"
        video_materials=[]
        for i in range(1, 4):
            video_materials.append(MaterialInfo(
                provider="local",
                url=os.path.join(resources_dir, f"{i}.png"),
                duration=0
            ))

        params = VideoParams(
            video_subject="金钱的作用",
            video_script="金钱不仅是交换媒介，更是社会资源的分配工具。它能满足基本生存需求，如食物和住房，也能提供教育、医疗等提升生活品质的机会。拥有足够的金钱意味着更多选择权，比如职业自由或创业可能。但金钱的作用也有边界，它无法直接购买幸福、健康或真诚的人际关系。过度追逐财富可能导致价值观扭曲，忽视精神层面的需求。理想的状态是理性看待金钱，将其作为实现目标的工具而非终极目的。",
            video_terms="money importance, wealth and society, financial freedom, money and happiness, role of money",
            video_aspect="9:16",
            video_concat_mode="random",
            video_transition_mode="None",
            video_clip_duration=3,
            video_count=1,
            video_source="local",
            video_materials=video_materials,
            video_language="",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            voice_volume=1.0,
            voice_rate=1.0,
            bgm_type="random",
            bgm_file="",
            bgm_volume=0.2,
            subtitle_enabled=True,
            subtitle_position="bottom",
            custom_position=70.0,
            font_name="MicrosoftYaHeiBold.ttc",
            text_fore_color="#FFFFFF",
            text_background_color=True,
            font_size=60,
            stroke_color="#000000",
            stroke_width=1.5,
            n_threads=2,
            paragraph_number=1
        )
        result = tm.start(task_id=task_id, params=params)
        print(result)
    

if __name__ == "__main__":
    unittest.main()
