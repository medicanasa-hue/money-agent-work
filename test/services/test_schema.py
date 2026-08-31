import dataclasses
import subprocess
import sys
import unittest
import warnings
from pathlib import Path

from pydantic import ValidationError

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.models import schema as schema_models  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import (  # noqa: E402
    MaterialInfo,
    VideoAspect,
    VideoParams,
    VideoTransitionMode,
)


class TestMaterialInfo(unittest.TestCase):
    def test_schema_import_has_no_pydantic_v2_config_deprecations(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import warnings; from pydantic import PydanticDeprecatedSince20; "
                "warnings.simplefilter('error', PydanticDeprecatedSince20); "
                "import app.models.schema",
            ],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_metadata_defaults_preserve_legacy_construction(self):
        first = MaterialInfo("coverr", "https://example.com/video.mp4", 12, 1920, 1080)
        second = MaterialInfo()

        self.assertEqual(first.provider, "coverr")
        self.assertEqual(first.width, 1920)
        self.assertEqual(first.search_query, "")
        self.assertEqual(first.title, "")
        self.assertEqual(first.description, "")
        self.assertEqual(first.tags, [])

        first.tags.append("city")
        self.assertEqual(second.tags, [])

    def test_metadata_values_are_excluded_from_repr(self):
        material = MaterialInfo(
            search_query="private acquisition strategy",
            title="Confidential working title",
            description="Internal launch details",
            tags=["private-tag"],
        )

        representation = repr(material)

        self.assertNotIn("private acquisition strategy", representation)
        self.assertNotIn("Confidential working title", representation)
        self.assertNotIn("Internal launch details", representation)
        self.assertNotIn("private-tag", representation)

    def test_metadata_round_trips_through_dataclass_and_video_params(self):
        material = MaterialInfo(
            provider="pixabay",
            url="https://example.com/video.mp4",
            duration=8,
            width=1920,
            height=1080,
            search_query="modern city skyline",
            title="City skyline at sunrise",
            description="A wide view of a modern city at sunrise.",
            tags=["city", "skyline", "sunrise"],
            license="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution="City skyline at sunrise - Creator - CC BY 4.0",
        )
        expected = {
            "provider": "pixabay",
            "url": "https://example.com/video.mp4",
            "duration": 8,
            "width": 1920,
            "height": 1080,
            "search_query": "modern city skyline",
            "title": "City skyline at sunrise",
            "description": "A wide view of a modern city at sunrise.",
            "tags": ["city", "skyline", "sunrise"],
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "City skyline at sunrise - Creator - CC BY 4.0",
            "preview_url": "",
            "preview_quality_score": None,
        }

        self.assertEqual(dataclasses.asdict(material), expected)

        params = VideoParams(video_subject="test", video_materials=[material])
        self.assertEqual(params.model_dump()["video_materials"], [expected])


class TestResponseSchemas(unittest.TestCase):
    def test_response_examples_remain_in_json_schema(self):
        response_model_names = (
            "TaskResponse",
            "TaskQueryResponse",
            "TaskDeletionResponse",
            "VideoScriptResponse",
            "VideoTermsResponse",
            "VideoSocialMetadataResponse",
            "ContentIntelligenceResponse",
            "BgmRetrieveResponse",
            "BgmUploadResponse",
            "VideoMaterialRetrieveResponse",
            "VideoMaterialUploadResponse",
        )

        for name in response_model_names:
            with self.subTest(response_model=name):
                model_schema = getattr(
                    schema_models, name
                ).model_json_schema()
                self.assertTrue(
                    "example" in model_schema or "examples" in model_schema
                )


class TestVideoAspect(unittest.TestCase):
    def test_to_resolution_known_aspects(self):
        self.assertEqual(VideoAspect.landscape.to_resolution(), (1920, 1080))
        self.assertEqual(VideoAspect.portrait.to_resolution(), (1080, 1920))
        self.assertEqual(VideoAspect.portrait_4_5.value, "4:5")
        self.assertEqual(VideoAspect.portrait_4_5.to_resolution(), (1080, 1350))
        self.assertEqual(VideoAspect.square.to_resolution(), (1080, 1080))

    def test_to_resolution_rejects_unsupported_value(self):
        with self.assertRaises(ValueError):
            VideoAspect.to_resolution("3:2")


class TestVideoParams(unittest.TestCase):
    def test_video_aspects_accepts_multiple_selected_formats(self):
        params = VideoParams(
            video_subject="test",
            video_aspects=["9:16", "4:5"],
        )

        self.assertEqual(
            params.video_aspects,
            [VideoAspect.portrait, VideoAspect.portrait_4_5],
        )

    def test_subtitle_style_uses_config_default(self):
        params = VideoParams(video_subject="test")

        self.assertEqual(
            params.subtitle_style,
            config.ui.get("subtitle_style", "classic"),
        )

    def test_subtitle_style_accepts_karaoke(self):
        params = VideoParams(video_subject="test", subtitle_style="karaoke")

        self.assertEqual(params.subtitle_style, "karaoke")

    def test_video_transition_defaults_to_crossfade_but_can_be_disabled(self):
        self.assertEqual(
            VideoParams(video_subject="test").video_transition_mode,
            VideoTransitionMode.crossfade,
        )
        self.assertIsNone(
            VideoParams(
                video_subject="test",
                video_transition_mode=None,
            ).video_transition_mode
        )

    def test_default_video_params_dump_without_serializer_warnings(self):
        params = VideoParams(video_subject="test")

        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            params.model_dump()

        serializer_warnings = [
            item
            for item in caught_warnings
            if "Pydantic serializer warnings" in str(item.message)
        ]
        self.assertEqual(serializer_warnings, [])

    def test_audio_bitrate_normalizes_safe_values(self):
        self.assertEqual(
            VideoParams(video_subject="test", audio_bitrate=256).audio_bitrate,
            "256k",
        )
        self.assertEqual(
            VideoParams(video_subject="test", audio_bitrate="320K").audio_bitrate,
            "320k",
        )
        self.assertEqual(
            VideoParams(video_subject="test", audio_bitrate="256kbps").audio_bitrate,
            "256k",
        )

    def test_audio_bitrate_rejects_invalid_values(self):
        for invalid_bitrate in (31, 513, True, "lossless", "0k"):
            with self.subTest(audio_bitrate=invalid_bitrate):
                with self.assertRaises(ValidationError):
                    VideoParams(
                        video_subject="test",
                        audio_bitrate=invalid_bitrate,
                    )

    def test_video_quality_choice_fields_normalize_safe_values(self):
        params = VideoParams(
            video_subject="test",
            video_codec=" H264_NVENC ",
            video_encoder_preset=" Slow ",
        )

        self.assertEqual(params.video_codec, "h264_nvenc")
        self.assertEqual(params.video_encoder_preset, "slow")

    def test_video_fps_normalizes_safe_fps_suffix(self):
        self.assertEqual(
            VideoParams(video_subject="test", video_fps="60fps").video_fps,
            60,
        )

    def test_video_quality_numeric_fields_reject_bool_values(self):
        for field_name in ("video_crf", "video_fps"):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    VideoParams(video_subject="test", **{field_name: True})


if __name__ == "__main__":
    unittest.main()
