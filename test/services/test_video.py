
import unittest
import os
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from moviepy import (
    VideoFileClip,
)
# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.schema import MaterialInfo, TaskVideoRequest, VideoParams
from app.services import state as sm
from app.services import video as vd
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")


class _FakeRequest:
    def __init__(self):
        self.headers = {"x-task-id": "test-request"}


class TestSecurityControls(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_task_query_returns_relative_task_url_without_mutating_state(self):
        """
        endpoint 未显式配置时，任务查询接口不能使用 Host 派生绝对 URL，
        也不能把展示 URL 回写到任务状态里，否则不同 Host 查询会污染结果。
        """
        task_id = "security-task-url"
        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")
        config.app["endpoint"] = ""

        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_COMPLETE,
                videos=[video_path],
                combined_videos=[video_path],
            )

            response = video_controller.get_task(_FakeRequest(), task_id=task_id)

            self.assertEqual(response["data"]["videos"], [f"/tasks/{task_id}/final-1.mp4"])
            self.assertEqual(sm.state.get_task(task_id)["videos"], [video_path])
        finally:
            sm.state.delete_task(task_id)
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_in_memory_task_manager_rejects_when_queue_is_full(self):
        """
        并发数用尽后，等待队列必须有硬上限。这里用 max_concurrent_tasks=0
        强制任务进入队列，验证超过 max_queued_tasks 时会拒绝继续入队。
        """
        manager = InMemoryTaskManager(max_concurrent_tasks=0, max_queued_tasks=1)

        manager.add_task(lambda: None)

        with self.assertRaises(TaskQueueFullError):
            manager.add_task(lambda: None)

    def test_create_task_preserves_video_quality_params_for_worker(self):
        body = TaskVideoRequest(
            video_subject="quality api",
            video_codec=" H264_NVENC ",
            video_crf=18,
            video_encoder_preset=" Slow ",
            video_fps=60,
            audio_bitrate=256,
        )

        try:
            with patch.object(
                utils, "get_uuid", return_value="quality-api-task"
            ), patch.object(video_controller.task_manager, "add_task") as add_task:
                response = video_controller.create_task(
                    _FakeRequest(),
                    body,
                    stop_at="video",
                )

            params = response["data"]["params"]
            self.assertEqual(params["video_codec"], "h264_nvenc")
            self.assertEqual(params["video_crf"], 18)
            self.assertEqual(params["video_encoder_preset"], "slow")
            self.assertEqual(params["video_fps"], 60)
            self.assertEqual(params["audio_bitrate"], "256k")

            worker_params = add_task.call_args.kwargs["params"]
            self.assertEqual(worker_params.video_codec, "h264_nvenc")
            self.assertEqual(worker_params.video_crf, 18)
            self.assertEqual(worker_params.video_encoder_preset, "slow")
            self.assertEqual(worker_params.video_fps, 60)
            self.assertEqual(worker_params.audio_bitrate, "256k")
            self.assertEqual(add_task.call_args.kwargs["stop_at"], "video")
        finally:
            sm.state.delete_task("quality-api-task")

class TestVideoService(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.test_img_path = os.path.join(resources_dir, "1.png")
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()
    
    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()
    
    def test_preprocess_video(self):
        if not os.path.exists(self.test_img_path):
            self.fail(f"test image not found: {self.test_img_path}")

        local_videos_dir = utils.storage_dir("local_videos", create=True)
        safe_img_path = os.path.join(local_videos_dir, "test-preprocess-1.png")
        shutil.copy2(self.test_img_path, safe_img_path)

        # test preprocess_video function
        m = MaterialInfo()
        m.url = os.path.basename(safe_img_path)
        m.provider = "local"
        print(m)

        try:
            materials = vd.preprocess_video([m], clip_duration=4)
            print(materials)

            # verify result
            self.assertIsNotNone(materials)
            self.assertEqual(len(materials), 1)
            self.assertTrue(materials[0].url.endswith(".mp4"))

            # moviepy get video info
            clip = VideoFileClip(materials[0].url)
            try:
                print(clip)
                self.assertEqual(clip.size[0] % 2, 0)
                self.assertEqual(clip.size[1] % 2, 0)
            finally:
                clip.close()

            # clean generated test video file
            if os.path.exists(materials[0].url):
                os.remove(materials[0].url)
        finally:
            if os.path.exists(safe_img_path):
                os.remove(safe_img_path)

    def test_preprocess_video_rejects_material_outside_local_videos(self):
        """
        local 素材路径来自 API 参数，不能允许任意绝对路径进入 MoviePy。
        这里验证非 local_videos 白名单目录内的路径会被跳过，避免任意文件读取。
        """
        m = MaterialInfo(provider="local", url=self.test_img_path)

        materials = vd.preprocess_video([m], clip_duration=4)

        self.assertEqual(materials, [])

    def test_preprocess_video_uses_codec_fallback_for_image_exports(self):
        config.app["video_fps"] = 60

        class _FakeProbeClip:
            size = (1080, 1920)

            def close(self):
                pass

        class _FakeImageClip:
            size = (1080, 1920)
            duration = 4

            def with_duration(self, duration):
                self.duration = duration
                return self

            def with_position(self, position):
                self.position = position
                return self

            def resized(self, resize_func):
                self.resize_func = resize_func
                return self

            def close(self):
                pass

        class _FakeCompositeClip:
            def __init__(self, clips, size=None, bg_color=None):
                self.clips = clips
                self.size = size
                self.bg_color = bg_color

            def write_videofile(self, *args, **kwargs):
                raise AssertionError("image exports should use codec fallback")

            def close(self):
                pass

        composite_clips = []

        def fake_composite_clip(clips, size=None, bg_color=None):
            clip = _FakeCompositeClip(clips, size=size, bg_color=bg_color)
            composite_clips.append(clip)
            return clip

        material = MaterialInfo(provider="local", url="image.png")

        with (
            patch.object(vd.utils, "storage_dir", return_value="C:/local"),
            patch.object(
                vd.file_security,
                "resolve_path_within_directory",
                return_value="C:/local/image.png",
            ),
            patch.object(
                vd,
                "_open_image_clip_with_fallback",
                return_value=(_FakeProbeClip(), "C:/local/image.png"),
            ),
            patch.object(vd, "ImageClip", return_value=_FakeImageClip()),
            patch.object(vd, "CompositeVideoClip", side_effect=fake_composite_clip),
            patch.object(vd, "_get_configured_video_codec", return_value="h264_nvenc"),
            patch.object(vd, "_write_videofile_with_codec_fallback") as write_video,
        ):
            materials = vd.preprocess_video([material], clip_duration=4)

        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].url, "C:/local/image.png.mp4")
        self.assertEqual(composite_clips[0].size, (1080, 1920))
        self.assertEqual(composite_clips[0].bg_color, (0, 0, 0))
        write_video.assert_called_once()
        self.assertEqual(write_video.call_args.args[0], composite_clips[0])
        self.assertEqual(write_video.call_args.kwargs["output_file"], "C:/local/image.png.mp4")
        self.assertEqual(write_video.call_args.kwargs["codec"], "h264_nvenc")
        self.assertEqual(write_video.call_args.kwargs["fps"], 60)
        self.assertIsNone(write_video.call_args.kwargs["logger"])

    def test_preprocess_video_closes_image_composite_when_export_fails(self):
        class _FakeReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _FakeProbeClip:
            size = (1080, 1920)

            def __init__(self):
                self.reader = _FakeReader()

        class _FakeImageClip:
            size = (1080, 1920)
            duration = 4

            def __init__(self):
                self.reader = _FakeReader()

            def with_duration(self, duration):
                self.duration = duration
                return self

            def with_position(self, position):
                self.position = position
                return self

            def resized(self, resize_func):
                self.resize_func = resize_func
                return self

        class _FakeMask:
            def __init__(self):
                self.reader = _FakeReader()

        class _FakeCompositeClip:
            def __init__(self, clips, size=None, bg_color=None):
                self.clips = clips
                self.size = size
                self.bg_color = bg_color
                self.mask = _FakeMask()

        composite_clips = []
        composite_masks = []

        def fake_composite_clip(clips, size=None, bg_color=None):
            clip = _FakeCompositeClip(clips, size=size, bg_color=bg_color)
            composite_clips.append(clip)
            composite_masks.append(clip.mask)
            return clip

        material = MaterialInfo(provider="local", url="image.png")

        with (
            patch.object(vd.utils, "storage_dir", return_value="C:/local"),
            patch.object(
                vd.file_security,
                "resolve_path_within_directory",
                return_value="C:/local/image.png",
            ),
            patch.object(
                vd,
                "_open_image_clip_with_fallback",
                return_value=(_FakeProbeClip(), "C:/local/image.png"),
            ),
            patch.object(vd, "ImageClip", return_value=_FakeImageClip()),
            patch.object(vd, "CompositeVideoClip", side_effect=fake_composite_clip),
            patch.object(
                vd,
                "_write_videofile_with_codec_fallback",
                side_effect=RuntimeError("image export failed"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                vd.preprocess_video([material], clip_duration=4)

        self.assertEqual(len(composite_clips), 1)
        self.assertTrue(composite_masks[0].reader.closed)

    def test_image_zoom_scale_preserves_default_motion_but_caps_long_clips(self):
        self.assertEqual(vd._image_zoom_scale(0, 4), 1.0)
        self.assertEqual(vd._image_zoom_scale(4, 4), 1.12)
        self.assertEqual(vd._image_zoom_scale(30, 30), 1.2)

    def test_preprocess_video_skips_duplicate_resolved_local_materials(self):
        class _FakeVideoClip:
            size = (1080, 1920)

        materials = [
            MaterialInfo(provider="local", url="clip.mp4"),
            MaterialInfo(provider="local", url="C:\\local\\clip.mp4"),
        ]

        with (
            patch.object(vd.utils, "storage_dir", return_value="C:/local"),
            patch.object(
                vd.file_security,
                "resolve_path_within_directory",
                return_value="C:/local/clip.mp4",
            ),
            patch.object(
                vd,
                "_open_video_clip_quietly",
                return_value=_FakeVideoClip(),
            ) as open_video,
        ):
            result = vd.preprocess_video(materials)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "C:/local/clip.mp4")
        open_video.assert_called_once_with("C:/local/clip.mp4")

    def test_get_bgm_file_accepts_song_directory_filename(self):
        """
        BGM 列表接口现在只暴露文件名；生成视频时应能把文件名安全解析回
        resource/songs 白名单目录，保持正常使用路径可用。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-safe-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(vd.get_bgm_file(bgm_file="test-safe-bgm.mp3"), bgm_path)
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_accepts_project_relative_song_path(self):
        """
        用户在 WebUI 中可能直接填写 ./resource/songs/xxx.mp3。该路径虽然是
        项目根目录相对路径，但实际文件仍在 resource/songs 白名单目录内，
        应该被接受，避免自定义背景音乐被误判为不存在。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-relative-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(
                vd.get_bgm_file(bgm_file="./resource/songs/test-relative-bgm.mp3"),
                bgm_path,
            )
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_rejects_path_outside_song_directory(self):
        """
        用户传入的 bgm_file 不能直接作为本地路径打开，否则可能读取系统文件。
        即使外部文件存在，也必须因为不在 songs 目录内被拒绝。
        """
        with tempfile.NamedTemporaryFile(suffix=".mp3") as temp_bgm:
            self.assertEqual(vd.get_bgm_file(bgm_file=temp_bgm.name), "")

    def test_get_ffmpeg_binary_uses_configured_env_path(self):
        """配置中显式指定 ffmpeg 时，应优先使用该路径。"""
        with patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "/tmp/custom-ffmpeg"}, clear=True):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/custom-ffmpeg")

    def test_get_ffmpeg_binary_falls_back_to_imageio_ffmpeg(self):
        """
        Windows 便携包里系统 PATH 可能没有 ffmpeg，但 moviepy 依赖的
        imageio-ffmpeg 通常会提供可执行文件。这里验证该兜底路径可用。
        """
        fake_imageio_ffmpeg = types.SimpleNamespace(
            get_ffmpeg_exe=lambda: "/tmp/bundled-ffmpeg"
        )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            utils.shutil, "which", return_value=None
        ), patch.dict(sys.modules, {"imageio_ffmpeg": fake_imageio_ffmpeg}):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/bundled-ffmpeg")

    def test_get_effective_video_codec_falls_back_when_encoder_missing(self):
        """
        用户选择的硬件编码器必须先经过 FFmpeg encoder 列表检测。检测不到
        时直接回退 libx264，避免生成任务在写文件阶段才失败。
        """
        config.app["video_codec"] = "h264_nvenc"

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=False):
            self.assertEqual(vd._get_effective_video_codec(), "libx264")

    def test_get_effective_video_codec_normalizes_preferred_codec(self):
        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            self.assertEqual(
                vd._get_effective_video_codec(" H264_NVENC "),
                "h264_nvenc",
            )

    def test_disable_runtime_video_codec_normalizes_safe_values(self):
        vd._disable_runtime_video_codec(" H264_NVENC ", "encoder failed")

        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)
        self.assertNotIn(" H264_NVENC ", vd._runtime_disabled_video_codecs)

    def test_ffmpeg_encoder_exists_falls_back_when_probe_fails(self):
        """
        Windows 上用户配置的 ffmpeg 可能因为路径损坏、权限或杀软拦截而无法
        正常执行。encoder 探测失败时必须返回 False，让上层稳定回退 libx264。
        """
        with patch.object(
            vd.subprocess,
            "run",
            side_effect=OSError("permission denied"),
        ):
            self.assertFalse(vd._ffmpeg_encoder_exists("C:/ffmpeg/bin/ffmpeg.exe", "h264_nvenc"))

    def test_write_videofile_falls_back_after_runtime_encoder_failure(self):
        """
        FFmpeg 声明支持某个硬件编码器，不代表当前显卡或驱动一定可用。
        首次实际编码失败后，应立即用 libx264 重试，并在本进程禁用该编码器。
        """

        class _FakeClip:
            def __init__(self):
                self.codecs = []
                self.kwargs = []

            def write_videofile(self, output_file, codec, **kwargs):
                self.codecs.append(codec)
                self.kwargs.append(kwargs)
                if codec == "h264_nvenc":
                    raise RuntimeError("nvenc device not available")

        fake_clip = _FakeClip()

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            used_codec = vd._write_videofile_with_codec_fallback(
                fake_clip,
                "/tmp/fake.mp4",
                codec="h264_nvenc",
                logger=None,
                fps=30,
            )

        self.assertEqual(used_codec, "libx264")
        self.assertEqual(fake_clip.codecs, ["h264_nvenc", "libx264"])
        self.assertNotIn("-crf", fake_clip.kwargs[0]["ffmpeg_params"])
        self.assertNotIn("-preset", fake_clip.kwargs[0]["ffmpeg_params"])
        self.assertEqual(
            fake_clip.kwargs[1]["ffmpeg_params"],
            [
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
            ],
        )
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_write_videofile_does_not_disable_codec_when_fallback_also_fails(self):
        """
        如果 libx264 兜底也失败，失败原因更可能是输出路径、权限、文件占用等
        通用问题，不能误判为硬件编码器不可用。
        """

        class _FakeClip:
            def write_videofile(self, output_file, codec, **kwargs):
                raise RuntimeError(f"{codec} cannot write output")

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            with self.assertRaises(RuntimeError):
                vd._write_videofile_with_codec_fallback(
                    _FakeClip(),
                    "/tmp/fake.mp4",
                    codec="h264_nvenc",
                    logger=None,
                    fps=30,
                )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_write_videofile_adds_mp4_params_for_software_encoder_outputs(self):
        class _FakeClip:
            def __init__(self):
                self.kwargs = []

            def write_videofile(self, output_file, codec, **kwargs):
                self.kwargs.append(kwargs)

        fake_clip = _FakeClip()

        used_codec = vd._write_videofile_with_codec_fallback(
            fake_clip,
            "/tmp/fake.mp4",
            codec="libx264",
            logger=None,
            fps=30,
        )

        self.assertEqual(used_codec, "libx264")
        self.assertEqual(
            fake_clip.kwargs[0]["ffmpeg_params"],
            [
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
            ],
        )

    def test_libx264_quality_args_use_configured_crf_and_preset(self):
        config.app["video_crf"] = 18
        config.app["video_encoder_preset"] = "slow"

        self.assertEqual(
            vd._ffmpeg_libx264_quality_args("libx264"),
            ["-preset", "slow", "-crf", "18"],
        )
        self.assertEqual(
            vd._ffmpeg_libx264_quality_args(" LIBX264 "),
            ["-preset", "slow", "-crf", "18"],
        )
        self.assertEqual(vd._ffmpeg_libx264_quality_args("h264_nvenc"), [])
        self.assertEqual(
            vd._ffmpeg_libx264_quality_args("libx264", bitrate="6000k"),
            ["-preset", "slow"],
        )
        self.assertEqual(
            vd._ffmpeg_libx264_quality_args(
                "libx264",
                existing_params=["-crf", "23"],
            ),
            ["-preset", "slow"],
        )
        self.assertEqual(
            vd._ffmpeg_libx264_quality_args(
                "libx264",
                existing_params=["-preset", "fast"],
            ),
            ["-crf", "18"],
        )

        config.app["video_crf"] = 52
        config.app["video_encoder_preset"] = "glacial"
        self.assertEqual(
            vd._ffmpeg_libx264_quality_args("libx264"),
            ["-preset", "medium", "-crf", "20"],
        )

    def test_configured_video_codec_normalizes_safe_string_values(self):
        config.app["video_codec"] = " H264_NVENC "

        self.assertEqual(vd._get_configured_video_codec(), "h264_nvenc")

    def test_video_quality_config_overrides_config_without_persisting(self):
        config.app["video_codec"] = "libx264"
        config.app["video_crf"] = 24
        config.app["video_encoder_preset"] = "fast"
        config.app["video_fps"] = 24
        config.app["audio_bitrate"] = "128k"

        with vd.video_quality_config(
            {
                "video_codec": "h264_nvenc",
                "video_crf": 18,
                "video_encoder_preset": "slow",
                "video_fps": 60,
                "audio_bitrate": "256k",
            }
        ):
            self.assertEqual(vd._get_configured_video_codec(), "h264_nvenc")
            self.assertEqual(vd._get_configured_libx264_crf(), "18")
            self.assertEqual(vd._get_configured_libx264_preset(), "slow")
            self.assertEqual(vd._get_configured_video_fps(), 60)
            self.assertEqual(vd._get_configured_audio_bitrate(), "256k")

        self.assertEqual(vd._get_configured_video_codec(), "libx264")
        self.assertEqual(vd._get_configured_libx264_crf(), "24")
        self.assertEqual(vd._get_configured_libx264_preset(), "fast")
        self.assertEqual(vd._get_configured_video_fps(), 24)
        self.assertEqual(vd._get_configured_audio_bitrate(), "128k")

    def test_video_quality_config_ignores_none_nested_overrides(self):
        config.app["video_codec"] = "libx264"
        config.app["video_crf"] = 24

        with vd.video_quality_config(
            {
                "video_codec": "h264_nvenc",
                "video_crf": 18,
            }
        ):
            with vd.video_quality_config(
                {
                    "video_codec": None,
                    "video_crf": None,
                    "unknown_quality_key": "ignored",
                }
            ):
                self.assertEqual(vd._get_configured_video_codec(), "h264_nvenc")
                self.assertEqual(vd._get_configured_libx264_crf(), "18")

            self.assertEqual(vd._get_configured_video_codec(), "h264_nvenc")
            self.assertEqual(vd._get_configured_libx264_crf(), "18")

        self.assertEqual(vd._get_configured_video_codec(), "libx264")
        self.assertEqual(vd._get_configured_libx264_crf(), "24")

    def test_configured_video_fps_accepts_safe_integer_values(self):
        config.app["video_fps"] = 60
        self.assertEqual(vd._get_configured_video_fps(), 60)

        config.app["video_fps"] = "48"
        self.assertEqual(vd._get_configured_video_fps(), 48)

        config.app["video_fps"] = "60fps"
        self.assertEqual(vd._get_configured_video_fps(), 60)

        for invalid_fps in (0, 121, True, "fast"):
            config.app["video_fps"] = invalid_fps
            self.assertEqual(vd._get_configured_video_fps(), 30)

    def test_configured_audio_bitrate_accepts_safe_kbps_values(self):
        config.app["audio_bitrate"] = "256k"
        self.assertEqual(vd._get_configured_audio_bitrate(), "256k")

        config.app["audio_bitrate"] = 320
        self.assertEqual(vd._get_configured_audio_bitrate(), "320k")

        config.app["audio_bitrate"] = "256kbps"
        self.assertEqual(vd._get_configured_audio_bitrate(), "256k")

        for invalid_bitrate in (15, 513, True, "lossless", "0k"):
            config.app["audio_bitrate"] = invalid_bitrate
            self.assertEqual(vd._get_configured_audio_bitrate(), "192k")

    def test_format_ffmpeg_concat_path_normalizes_windows_path(self):
        """
        concat demuxer 的文件列表对 Windows 反斜杠较敏感，写入 list 前统一
        转成正斜杠，并继续保留单引号转义。
        """
        with patch.object(vd.os.path, "abspath", return_value=r"C:\Users\Harry's Videos\clip.mp4"):
            self.assertEqual(
                vd._format_ffmpeg_concat_path(r"C:\Users\Harry's Videos\clip.mp4"),
                "C:/Users/Harry'\\''s Videos/clip.mp4",
            )

    def test_concat_video_clips_prefers_stream_copy_to_avoid_reencoding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with (
                patch.object(
                    vd,
                    "_get_effective_video_codec",
                    side_effect=AssertionError("stream copy should avoid encoder lookup"),
                ),
                patch.object(
                    vd.subprocess,
                    "run",
                    return_value=types.SimpleNamespace(
                        returncode=0,
                        stdout="",
                        stderr="",
                    ),
                ) as run,
            ):
                result = vd.concat_video_clips_with_ffmpeg(
                    clip_files=[clip_file],
                    output_file=output_file,
                    threads=1,
                    output_dir=temp_dir,
                )

        command = run.call_args.args[0]
        self.assertEqual(result, "copy")
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
        self.assertNotIn("-pix_fmt", command)

    def test_build_ass_subtitles_filter_escapes_windows_path(self):
        with patch.object(
            vd.os.path,
            "abspath",
            return_value=r"C:\Users\Harry's Videos\clip,1.ass",
        ):
            self.assertEqual(
                vd._build_ass_subtitles_filter("ignored.ass"),
                "subtitles='C\\:/Users/Harry\\'s Videos/clip\\,1.ass'",
            )

    def test_generate_video_uses_ass_burn_branch_without_subtitlesclip(self):
        class _FakeVideoClip:
            duration = 1

            def __init__(self):
                self.closed = False

            def with_audio(self, audio_clip):
                self.audio_clip = audio_clip
                return self

            def close(self):
                self.closed = True

        class _FakeAudioClip:
            fps = 48000

            def with_effects(self, effects):
                return self

        fake_video_clip = _FakeVideoClip()

        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle_file = Path(temp_dir) / "subtitle.ass"
            output_file = Path(temp_dir) / "final.mp4"
            subtitle_file.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            params = VideoParams(video_subject="karaoke", subtitle_enabled=True)

            with patch.object(
                vd, "_open_video_clip_quietly", return_value=fake_video_clip
            ), patch.object(
                vd, "AudioFileClip", return_value=_FakeAudioClip()
            ), patch.object(
                vd, "SubtitlesClip"
            ) as subtitles_clip, patch.object(
                vd, "get_bgm_file", return_value=""
            ), patch.object(
                vd, "_write_videofile_with_codec_fallback"
            ) as write_video, patch.object(
                vd, "_burn_ass_subtitles_with_ffmpeg", return_value=True
            ) as burn_ass:
                vd.generate_video(
                    video_path="input.mp4",
                    audio_path="audio.mp3",
                    subtitle_path=str(subtitle_file),
                    output_file=str(output_file),
                    params=params,
                )

        subtitles_clip.assert_not_called()
        self.assertTrue(
            write_video.call_args.kwargs["output_file"].endswith(".nosub.mp4")
        )
        burn_ass.assert_called_once()
        self.assertEqual(burn_ass.call_args.kwargs["subtitle_file"], str(subtitle_file))
        self.assertEqual(burn_ass.call_args.kwargs["output_file"], str(output_file))
        self.assertTrue(fake_video_clip.closed)

    def test_generate_video_closes_clip_when_export_fails(self):
        class _FakeAudioReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _FakeAudioClip:
            fps = 48000

            def __init__(self):
                self.reader = _FakeAudioReader()

            def with_effects(self, effects):
                return self

            def close(self):
                self.reader.close()

        class _FakeVideoClip:
            duration = 1

            def __init__(self):
                self.closed = False
                self.audio_clip = None

            def with_audio(self, audio_clip):
                self.audio_clip = audio_clip
                return self

            def close(self):
                self.closed = True
                if self.audio_clip is not None:
                    self.audio_clip.close()

        fake_video_clip = _FakeVideoClip()
        fake_audio_clip = _FakeAudioClip()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = Path(temp_dir) / "final.mp4"
            params = VideoParams(video_subject="export failure", subtitle_enabled=False)

            with (
                patch.object(vd, "_open_video_clip_quietly", return_value=fake_video_clip),
                patch.object(vd, "AudioFileClip", return_value=fake_audio_clip),
                patch.object(vd, "get_bgm_file", return_value=""),
                patch.object(
                    vd,
                    "_write_videofile_with_codec_fallback",
                    side_effect=RuntimeError("export failed"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    vd.generate_video(
                        video_path="input.mp4",
                        audio_path="audio.mp3",
                        subtitle_path="",
                        output_file=str(output_file),
                        params=params,
                    )

        self.assertTrue(fake_video_clip.closed)
        self.assertTrue(fake_audio_clip.reader.closed)

    def test_generate_video_uses_current_dir_for_relative_output_temp_audio(self):
        config.app["audio_bitrate"] = "256k"

        class _FakeVideoClip:
            duration = 1

            def __init__(self):
                self.closed = False

            def with_audio(self, audio_clip):
                self.audio_clip = audio_clip
                return self

            def close(self):
                self.closed = True

        class _FakeAudioClip:
            fps = 48000

            def with_effects(self, effects):
                return self

        temp_audio_dirs = []

        def fake_temp_audio_dir(output_dir):
            temp_audio_dirs.append(output_dir)
            return "TEMP_AUDIO_DIR"

        with (
            patch.object(vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()),
            patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
            patch.object(vd, "get_bgm_file", return_value=""),
            patch.object(vd, "_get_temp_audio_dir", side_effect=fake_temp_audio_dir),
            patch.object(vd, "_write_videofile_with_codec_fallback") as write_video,
        ):
            vd.generate_video(
                video_path="input.mp4",
                audio_path="audio.mp3",
                subtitle_path="",
                output_file="final.mp4",
                params=VideoParams(video_subject="relative output", subtitle_enabled=False),
            )

        self.assertEqual(temp_audio_dirs, ["."])
        self.assertEqual(
            write_video.call_args.kwargs["temp_audiofile_path"],
            "TEMP_AUDIO_DIR",
        )
        self.assertEqual(write_video.call_args.kwargs["audio_bitrate"], "256k")

    def test_burn_ass_subtitles_writes_temp_then_replaces_final(self):
        def fake_run(command, capture_output, text, check):
            temp_output_file = command[-1]
            self.assertTrue(temp_output_file.endswith(".assburn.tmp.mp4"))
            self.assertEqual(command[command.index("-preset") + 1], "medium")
            self.assertEqual(command[command.index("-crf") + 1], "20")
            self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
            Path(temp_output_file).write_bytes(b"new-video")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = os.path.join(temp_dir, "nosub.mp4")
            subtitle_file = os.path.join(temp_dir, "subtitle.ass")
            output_file = os.path.join(temp_dir, "final.mp4")
            Path(input_file).write_bytes(b"input-video")
            Path(subtitle_file).write_text("[Script Info]\n", encoding="utf-8")
            Path(output_file).write_bytes(b"old-video")

            with patch.object(vd.subprocess, "run", side_effect=fake_run):
                burned = vd._burn_ass_subtitles_with_ffmpeg(
                    input_file=input_file,
                    subtitle_file=subtitle_file,
                    output_file=output_file,
                    threads=1,
                )

            temp_output_file = vd._ass_burn_temp_output_file(output_file)
            self.assertTrue(burned)
            self.assertEqual(Path(output_file).read_bytes(), b"new-video")
            self.assertFalse(os.path.exists(temp_output_file))

    def test_burn_ass_subtitles_failure_keeps_existing_final(self):
        def fake_run(command, capture_output, text, check):
            Path(command[-1]).write_bytes(b"partial-video")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="libass failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            input_file = os.path.join(temp_dir, "nosub.mp4")
            subtitle_file = os.path.join(temp_dir, "subtitle.ass")
            output_file = os.path.join(temp_dir, "final.mp4")
            Path(input_file).write_bytes(b"input-video")
            Path(subtitle_file).write_text("[Script Info]\n", encoding="utf-8")
            Path(output_file).write_bytes(b"old-video")

            with patch.object(vd.subprocess, "run", side_effect=fake_run):
                burned = vd._burn_ass_subtitles_with_ffmpeg(
                    input_file=input_file,
                    subtitle_file=subtitle_file,
                    output_file=output_file,
                    threads=1,
                )

            temp_output_file = vd._ass_burn_temp_output_file(output_file)
            self.assertFalse(burned)
            self.assertEqual(Path(output_file).read_bytes(), b"old-video")
            self.assertFalse(os.path.exists(temp_output_file))

    def test_generate_video_falls_back_to_srt_when_ass_burn_fails(self):
        class _FakeVideoClip:
            duration = 1

            def with_audio(self, audio_clip):
                return self

            def close(self):
                pass

        class _FakeAudioClip:
            fps = 48000

            def with_effects(self, effects):
                return self

        class _FakeSubtitlesClip:
            subtitles = []

        written_outputs = []

        def fake_write_video(clip, output_file, **kwargs):
            written_outputs.append(output_file)
            Path(output_file).write_bytes(b"video")

        with tempfile.TemporaryDirectory() as temp_dir:
            ass_file = Path(temp_dir) / "subtitle.ass"
            srt_file = Path(temp_dir) / "subtitle.srt"
            output_file = Path(temp_dir) / "final.mp4"
            ass_file.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            srt_file.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            params = VideoParams(video_subject="karaoke", subtitle_enabled=True)

            with patch.object(
                vd,
                "_open_video_clip_quietly",
                side_effect=[_FakeVideoClip(), _FakeVideoClip()],
            ), patch.object(
                vd, "AudioFileClip", return_value=_FakeAudioClip()
            ), patch.object(
                vd, "CompositeVideoClip", side_effect=lambda clips: clips[0]
            ), patch.object(
                vd, "SubtitlesClip", return_value=_FakeSubtitlesClip()
            ) as subtitles_clip, patch.object(
                vd, "get_bgm_file", return_value=""
            ), patch.object(
                vd, "_write_videofile_with_codec_fallback", side_effect=fake_write_video
            ), patch.object(
                vd, "_burn_ass_subtitles_with_ffmpeg", return_value=False
            ) as burn_ass:
                vd.generate_video(
                    video_path="input.mp4",
                    audio_path="audio.mp3",
                    subtitle_path=str(ass_file),
                    output_file=str(output_file),
                    params=params,
                )

        burn_ass.assert_called_once()
        self.assertTrue(written_outputs[0].endswith(".nosub.mp4"))
        self.assertEqual(written_outputs[1], str(output_file))
        self.assertEqual(subtitles_clip.call_args.kwargs["subtitles"], str(srt_file))

    def test_generate_video_keeps_nosub_output_when_ass_burn_fails_without_srt(self):
        class _FakeVideoClip:
            duration = 1

            def with_audio(self, audio_clip):
                return self

            def close(self):
                pass

        class _FakeAudioClip:
            fps = 48000

            def with_effects(self, effects):
                return self

        def fake_write_video(clip, output_file, **kwargs):
            Path(output_file).write_bytes(b"nosub-video")

        with tempfile.TemporaryDirectory() as temp_dir:
            ass_file = Path(temp_dir) / "subtitle.ass"
            output_file = Path(temp_dir) / "final.mp4"
            ass_file.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
            output_file.write_bytes(b"old-video")
            params = VideoParams(video_subject="karaoke", subtitle_enabled=True)

            with patch.object(
                vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()
            ), patch.object(
                vd, "AudioFileClip", return_value=_FakeAudioClip()
            ), patch.object(
                vd, "SubtitlesClip"
            ) as subtitles_clip, patch.object(
                vd, "get_bgm_file", return_value=""
            ), patch.object(
                vd, "_write_videofile_with_codec_fallback", side_effect=fake_write_video
            ) as write_video, patch.object(
                vd, "_burn_ass_subtitles_with_ffmpeg", return_value=False
            ):
                vd.generate_video(
                    video_path="input.mp4",
                    audio_path="audio.mp3",
                    subtitle_path=str(ass_file),
                    output_file=str(output_file),
                    params=params,
                )

            self.assertEqual(output_file.read_bytes(), b"nosub-video")
            self.assertFalse(
                os.path.exists(vd._ass_burn_temp_output_file(str(output_file)))
            )

        subtitles_clip.assert_not_called()
        write_video.assert_called_once()

    def test_concat_video_clips_falls_back_after_runtime_encoder_failure(self):
        """
        最终 ffmpeg concat 阶段也要具备同样的回退能力。这里用 mock 模拟
        h264_nvenc 编码失败，确认会自动再用 libx264 执行一次。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check):
            if "-c" in command and command[command.index("-c") + 1] == "copy":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="stream copy incompatible",
                )
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            if codec == "h264_nvenc":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="nvenc device not available",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run) as run:
                    vd.concat_video_clips_with_ffmpeg(
                        clip_files=[clip_file],
                        output_file=output_file,
                        threads=1,
                        output_dir=temp_dir,
                    )

        used_codecs = [
            call.args[0][call.args[0].index("-c:v") + 1]
            for call in run.call_args_list
            if "-c:v" in call.args[0]
        ]
        first_command = run.call_args_list[0].args[0]
        self.assertEqual(first_command[first_command.index("-c") + 1], "copy")
        self.assertEqual(used_codecs, ["h264_nvenc", "libx264"])
        reencode_commands = [
            call.args[0]
            for call in run.call_args_list
            if "-c:v" in call.args[0]
        ]
        self.assertNotIn("-crf", reencode_commands[0])
        self.assertNotIn("-preset", reencode_commands[0])
        self.assertEqual(
            reencode_commands[1][reencode_commands[1].index("-preset") + 1],
            "medium",
        )
        self.assertEqual(
            reencode_commands[1][reencode_commands[1].index("-crf") + 1],
            "20",
        )
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_concat_video_clips_does_not_disable_codec_when_fallback_also_fails(self):
        """
        concat 阶段如果 libx264 也失败，说明可能是输入 list、路径或输出权限
        问题，不能把硬件编码器加入运行时禁用列表。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check):
            if "-c" in command and command[command.index("-c") + 1] == "copy":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="stream copy incompatible",
                )
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"{codec} cannot write output",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run):
                    with self.assertRaises(RuntimeError):
                        vd.concat_video_clips_with_ffmpeg(
                            clip_files=[clip_file],
                            output_file=output_file,
                            threads=1,
                            output_dir=temp_dir,
                        )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_open_video_clip_quietly_suppresses_moviepy_stdout(self):
        """
        MoviePy 2.1.x 的 FFMPEG_VideoReader 会直接向 stdout 打印 metadata
        和 ffmpeg 命令。项目服务层应屏蔽这类依赖库噪声，避免用户把
        `audio_found: False` 误判为最终视频没有音频。
        """
        video_path = os.path.join(resources_dir, "1.png.mp4")
        if not os.path.exists(video_path):
            self.fail(f"test video not found: {video_path}")

        stdout = StringIO()
        with redirect_stdout(stdout):
            clip = vd._open_video_clip_quietly(video_path)

        try:
            self.assertEqual(stdout.getvalue(), "")
            self.assertIsNone(clip.audio)
            self.assertGreater(clip.duration, 0)
        finally:
            vd.close_clip(clip)

    def test_combine_videos_closes_audio_clip_when_duration_read_fails(self):
        """
        `combine_videos()` 只需要读取旁白音频时长。即使读取 duration
        时发生异常，也必须关闭 AudioFileClip，避免文件句柄泄漏。
        """

        class _FakeAudioReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _BrokenAudioClip:
            def __init__(self):
                self.reader = _FakeAudioReader()

            @property
            def duration(self):
                raise RuntimeError("failed to read duration")

        fake_audio_clip = _BrokenAudioClip()

        with patch.object(vd, "AudioFileClip", return_value=fake_audio_clip):
            with self.assertRaises(RuntimeError):
                vd.combine_videos(
                    combined_video_path="/tmp/unused-combined.mp4",
                    video_paths=[],
                    audio_file="/tmp/unused-audio.mp3",
                )

        self.assertTrue(fake_audio_clip.reader.closed)

    def test_combine_videos_handles_none_transition_mode(self):
        """
        Ensure `combine_videos` safely handles
        `video_transition_mode=None`.
        """
        class _FakeAudioClip:
            @property
            def duration(self):
                return 10.0

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")
            audio_file = os.path.join(temp_dir, "audio.mp3")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                # Use empty video_paths to avoid heavy video processing while
                # still exercising transition mode normalization logic.
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=[],
                    audio_file=audio_file,
                    video_transition_mode=None,
                )
                self.assertEqual(result, combined_video_path)

    def test_combine_videos_accepts_plain_string_concat_mode(self):
        config.app["video_fps"] = 48

        class _FakeAudioClip:
            duration = 1.0

            def close(self):
                pass

        class _FakeVideoClip:
            duration = 3.0
            size = (1080, 1920)
            w = 1080
            h = 1920

            def subclipped(self, start_time, end_time):
                return self

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()),
                patch.object(vd, "_write_videofile_with_codec_fallback") as write_mock,
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
                patch.object(vd.shutil, "copy"),
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["clip.mp4"],
                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode="sequential",
                    video_transition_mode=None,
                    max_clip_duration=5,
                )

        self.assertEqual(result, combined_video_path)
        write_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.kwargs["fps"], 48)

    def test_combine_videos_uses_current_dir_for_relative_output_temp_clips(self):
        class _FakeAudioClip:
            duration = 1.0

            def close(self):
                pass

        class _FakeVideoClip:
            duration = 3.0
            size = (1080, 1920)
            w = 1080
            h = 1920

            def subclipped(self, start_time, end_time):
                return self

        with (
            patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
            patch.object(vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()),
            patch.object(vd, "_write_videofile_with_codec_fallback") as write_mock,
            patch.object(vd.shutil, "copy"),
            patch.object(vd, "delete_files"),
        ):
            result = vd.combine_videos(
                combined_video_path="combined.mp4",
                video_paths=["clip.mp4"],
                audio_file="audio.mp3",
                video_aspect=vd.VideoAspect.portrait,
                video_concat_mode=vd.VideoConcatMode.sequential,
                video_transition_mode=None,
                max_clip_duration=5,
            )

        self.assertEqual(result, "combined.mp4")
        self.assertEqual(write_mock.call_args.args[1], os.path.join(".", "temp-clip-1.mp4"))

    def test_combine_videos_closes_processing_clip_when_temp_export_fails(self):
        class _FakeAudioClip:
            duration = 1.0

            def close(self):
                pass

        class _FakeReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _FakeVideoClip:
            duration = 3.0
            size = (1080, 1920)
            w = 1080
            h = 1920

            def __init__(self):
                self.reader = _FakeReader()

            def subclipped(self, start_time, end_time):
                return self

        opened_clips = []

        def fake_open_video_clip(video_path):
            clip = _FakeVideoClip()
            opened_clips.append(clip)
            return clip

        with (
            patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
            patch.object(vd, "_open_video_clip_quietly", side_effect=fake_open_video_clip),
            patch.object(
                vd,
                "_write_videofile_with_codec_fallback",
                side_effect=RuntimeError("export failed"),
            ),
        ):
            result = vd.combine_videos(
                combined_video_path="combined.mp4",
                video_paths=["clip.mp4"],
                audio_file="audio.mp3",
                video_aspect=vd.VideoAspect.portrait,
                video_concat_mode=vd.VideoConcatMode.sequential,
                video_transition_mode=None,
                max_clip_duration=5,
            )

        self.assertEqual(result, "combined.mp4")
        self.assertEqual(len(opened_clips), 2)
        self.assertTrue(opened_clips[0].reader.closed)
        self.assertTrue(opened_clips[1].reader.closed)

    def test_combine_videos_crops_mismatched_aspect_without_black_bars(self):
        class _FakeAudioClip:
            duration = 1.0

            def close(self):
                pass

        resize_sizes = []
        crop_calls = []

        class _FakeVideoClip:
            def __init__(self, size=(1920, 1080), duration=3.0, was_cropped=False):
                self.duration = duration
                self.size = size
                self.w = size[0]
                self.h = size[1]
                self.was_cropped = was_cropped

            def subclipped(self, start_time, end_time):
                return _FakeVideoClip(
                    size=self.size,
                    duration=end_time - start_time,
                    was_cropped=self.was_cropped,
                )

            def resized(self, new_size):
                resize_sizes.append(new_size)
                return _FakeVideoClip(
                    size=new_size,
                    duration=self.duration,
                    was_cropped=self.was_cropped,
                )

            def cropped(self, **kwargs):
                crop_calls.append(kwargs)
                return _FakeVideoClip(
                    size=(kwargs["width"], kwargs["height"]),
                    duration=self.duration,
                    was_cropped=True,
                )

        written_clips = []

        def fake_write_video(clip, output_file, **kwargs):
            written_clips.append(clip)

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()),
                patch.object(vd, "_write_videofile_with_codec_fallback", side_effect=fake_write_video),
                patch.object(vd, "CompositeVideoClip", side_effect=AssertionError("black bars should not be used")),
                patch.object(vd.shutil, "copy"),
                patch.object(vd, "delete_files"),
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["landscape.mp4"],
                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    video_transition_mode=None,
                    max_clip_duration=5,
                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(resize_sizes, [(3414, 1920)])
        self.assertEqual(
            crop_calls,
            [{"x_center": 1707.0, "y_center": 960.0, "width": 1080, "height": 1920}],
        )
        self.assertTrue(written_clips[0].was_cropped)
        self.assertEqual(written_clips[0].size, (1080, 1920))

    def test_fit_clip_to_target_frame_resizes_matching_aspect_without_crop(self):
        class _FakeVideoClip:
            duration = 3.0
            size = (720, 1280)
            w = 720
            h = 1280

            def __init__(self):
                self.resized_to = None

            def resized(self, new_size):
                self.resized_to = new_size
                return self

            def cropped(self, **kwargs):
                raise AssertionError("matching aspect clips should not be cropped")

        clip = _FakeVideoClip()

        result = vd._fit_clip_to_target_frame(clip, 1080, 1920)

        self.assertEqual(result, clip)
        self.assertEqual(clip.resized_to, (1080, 1920))

    def test_get_effective_transition_duration_caps_short_clips(self):
        self.assertEqual(vd._get_effective_transition_duration(5.0), 1.0)
        self.assertEqual(vd._get_effective_transition_duration(0.5), 0.25)
        self.assertEqual(vd._get_effective_transition_duration(0), 0.0)

    def test_combine_videos_limits_transition_duration_for_short_clips(self):
        class _FakeAudioClip:
            duration = 0.4

            def close(self):
                pass

        class _FakeVideoClip:
            duration = 0.5
            size = (1080, 1920)
            w = 1080
            h = 1920

            def subclipped(self, start_time, end_time):
                clip = _FakeVideoClip()
                clip.duration = end_time - start_time
                return clip

        transition_durations = []

        def fake_fadein_transition(clip, transition_duration):
            transition_durations.append(transition_duration)
            return clip

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()),
                patch.object(
                    vd.video_effects,
                    "fadein_transition",
                    side_effect=fake_fadein_transition,
                ),
                patch.object(vd, "_write_videofile_with_codec_fallback"),
                patch.object(vd.shutil, "copy"),
                patch.object(vd, "delete_files"),
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["short.mp4"],
                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    video_transition_mode=vd.VideoTransitionMode.fade_in,
                    max_clip_duration=5,
                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(transition_durations, [0.25])

    def test_combine_videos_keeps_small_duration_safety_margin(self):
        """
        音频和素材累计时长刚好相等时，仍应继续追加一个短片段作为安全余量。

        FFmpeg 按帧率拼接后可能让最终视频比理论时长短几十毫秒。如果这里
        在 10.0s == 10.0s 时立即停止，成片末尾就可能出现音频还在播放但
        视频素材已经结束的边界问题。
        """

        class _FakeAudioClip:
            duration = 10.0

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920

            def subclipped(self, start_time, end_time):
                return _FakeVideoClip(end_time - start_time)

        video_durations = {
            "clip-1.mp4": 3.0,
            "clip-2.mp4": 4.0,
            "clip-3.mp4": 3.0,
            "clip-4.mp4": 2.0,
        }

        def _open_fake_video_clip(video_path):
            return _FakeVideoClip(video_durations[video_path])

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                with patch.object(
                    vd, "_open_video_clip_quietly", side_effect=_open_fake_video_clip
                ):
                    with patch.object(
                        vd, "_write_videofile_with_codec_fallback"
                    ) as write_mock:
                        with patch.object(vd, "concat_video_clips_with_ffmpeg"):
                            with patch.object(vd, "delete_files"):
                                result = vd.combine_videos(
                                    combined_video_path=combined_video_path,
                                    video_paths=list(video_durations.keys()),
                                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                                    video_aspect=vd.VideoAspect.portrait,
                                    video_concat_mode=vd.VideoConcatMode.sequential,
                                    video_transition_mode=None,
                                    max_clip_duration=10,
                                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(write_mock.call_count, 4)

    def test_prioritize_unique_source_clips_uses_each_source_before_reuse(self):
        """
        随机模式下，一个长素材会被拆成多个片段。调度层应先让每个源素材
        至少出现一次，再使用同一源素材的其他切片，降低用户感知到的重复。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 4, 8, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 4, 8, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.random,
        )

        self.assertCountEqual(ordered_clips, clips)
        first_round_sources = [clip.source_file_path for clip in ordered_clips[:3]]
        self.assertCountEqual(first_round_sources, ["a.mp4", "b.mp4", "c.mp4"])

    def test_prioritize_unique_source_clips_keeps_sequential_order(self):
        """
        顺序模式本身只取每个素材的首段，不应被随机调度逻辑改变顺序。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.sequential,
        )

        self.assertEqual(ordered_clips, clips)

    def test_prioritize_unique_source_clips_prefers_long_primary_clip(self):
        """
        同一个源素材的最后一个切片可能短于目标片段时长。首轮去重时应优先
        选择较长片段，否则会因为累计时长不足而提前复用素材。
        """
        short_tail = vd.SubClippedVideoClip(
            "a.mp4", 6, 6.5, source_file_path="a.mp4"
        )
        full_clip = vd.SubClippedVideoClip(
            "a.mp4", 0, 3, source_file_path="a.mp4"
        )
        other_source = vd.SubClippedVideoClip(
            "b.mp4", 0, 3, source_file_path="b.mp4"
        )

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=[short_tail, full_clip, other_source],
            concat_mode=vd.VideoConcatMode.random,
        )

        first_a_clip = next(
            clip for clip in ordered_clips if clip.source_file_path == "a.mp4"
        )
        self.assertEqual(first_a_clip, full_clip)
    
    def test_prioritize_unique_source_clips_normalizes_path_separators(self):
        short_slice = vd.SubClippedVideoClip(
            "C:\\cache\\clip.mp4",
            0,
            2,
            source_file_path="C:\\cache\\clip.mp4",
        )
        full_slice = vd.SubClippedVideoClip(
            "C:/cache/clip.mp4",
            2,
            6,
            source_file_path="C:/cache/clip.mp4",
        )
        other_source = vd.SubClippedVideoClip(
            "C:/cache/other.mp4",
            0,
            3,
            source_file_path="C:/cache/other.mp4",
        )

        with patch.object(vd.random, "shuffle", side_effect=lambda items: None):
            ordered_clips = vd._prioritize_unique_source_clips(
                subclipped_items=[short_slice, full_slice, other_source],
                concat_mode=vd.VideoConcatMode.random,
            )

        self.assertEqual(ordered_clips[:2], [full_slice, other_source])
        self.assertEqual(ordered_clips[2], short_slice)

    def test_wrap_text(self):
        """test text wrapping function"""
        try:
            font_path = os.path.join(utils.font_dir(), "STHeitiMedium.ttc")
            if not os.path.exists(font_path):
                self.fail(f"font file not found: {font_path}")
                
            # test english text wrapping
            test_text_en = "This is a test text for wrapping long sentences in english language"
            
            wrapped_text_en, text_height_en = vd.wrap_text(
                text=test_text_en,
                max_width=300,
                font=font_path,
                fontsize=30
            )
            print(wrapped_text_en, text_height_en)
            # verify text is wrapped
            self.assertIn("\n", wrapped_text_en)
            
            # test chinese text wrapping
            test_text_zh = "这是一段用来测试中文长句换行的文本内容，应该会根据宽度限制进行换行处理"
            wrapped_text_zh, text_height_zh = vd.wrap_text(
                text=test_text_zh,
                max_width=300,
                font=font_path,
                fontsize=30
            )   
            print(wrapped_text_zh, text_height_zh)
            # verify chinese text is wrapped
            self.assertIn("\n", wrapped_text_zh)
        except Exception as e:
            self.fail(f"test wrap_text failed: {str(e)}")

    def test_rounded_subtitle_background_clip_has_transparent_corners(self):
        """
        圆角字幕背景只在用户显式开启时使用。这里直接验证生成的 RGBA
        背景具备透明圆角和半透明中心，避免后续改动把圆角效果退化成实心矩形。
        """
        clip = vd._rounded_subtitle_background_clip(
            width=120,
            height=48,
            color="#123456",
            alpha=140,
            radius=16,
        )
        try:
            frame = clip.get_frame(0)
            mask = clip.mask.get_frame(0)

            self.assertEqual(frame.shape[0:2], (48, 120))
            self.assertEqual(tuple(frame[24, 60]), (18, 52, 86))
            self.assertEqual(mask[0, 0], 0)
            self.assertGreater(mask[24, 60], 0.5)
            self.assertLess(mask[24, 60], 0.6)
        finally:
            clip.close()

    def test_get_temp_audio_dir_returns_system_temp_on_windows(self):
        with patch("sys.platform", "win32"):
            result = vd._get_temp_audio_dir("/some/output/dir")
            self.assertEqual(result, tempfile.gettempdir())

    def test_get_temp_audio_dir_returns_output_dir_on_non_windows(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                with patch("sys.platform", platform):
                    result = vd._get_temp_audio_dir("/some/output/dir")
                    self.assertEqual(result, "/some/output/dir")


if __name__ == "__main__":
    unittest.main()
