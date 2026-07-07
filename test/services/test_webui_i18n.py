import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _load_translation(locale):
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data.get("Translation", {})


def _load_webui_helpers(*names):
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"tr": lambda key: key}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


class TestWebuiI18n(unittest.TestCase):
    def test_english_locale_covers_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        en_keys = set(_load_translation("en"))

        self.assertEqual(sorted(visitor.keys - en_keys), [])

    def test_russian_locale_covers_english_locale(self):
        en_keys = set(_load_translation("en"))
        ru_keys = set(_load_translation("ru"))

        self.assertEqual(sorted(en_keys - ru_keys), [])

    def test_russian_locale_covers_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        ru_keys = set(_load_translation("ru"))

        self.assertEqual(sorted(visitor.keys - ru_keys), [])

    def test_video_quality_labels_are_localized_in_non_english_locales(self):
        en = _load_translation("en")
        quality_keys = (
            "Video Quality CRF",
            "Video Quality CRF Help",
            "Video Quality Preset",
            "Video Quality Preset Help",
            "Balanced Quality Preset",
            "Fast Draft Preset",
            "High Quality Preset",
            "Archive Quality Preset",
            "Custom Quality Preset",
            "Encoder Preset",
            "Encoder Preset Help",
            "Output FPS",
            "Output FPS Help",
            "Audio Bitrate",
            "Audio Bitrate Help",
        )

        for locale in ("tr", "ru"):
            localized = _load_translation(locale)
            for key in quality_keys:
                self.assertIn(key, localized)
                self.assertNotEqual(localized[key], en[key])

    def test_script_language_options_include_russian(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        support_locales = None

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "support_locales"
                for target in node.targets
            ):
                support_locales = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(support_locales)
        self.assertIn("ru-RU", support_locales)

    def test_manual_candidate_recommendation_uses_first_qualified_candidate(self):
        helpers = _load_webui_helpers(
            "_is_vertical_high_resolution",
            "_manual_recommended_candidate_url",
            "_manual_candidate_badges",
        )
        candidates = [
            {
                "url": "https://v.example/wide.mp4",
                "width": 1920,
                "height": 1080,
            },
            {
                "url": "https://v.example/portrait.mp4",
                "width": 1080,
                "height": 1920,
            },
            {
                "url": "https://v.example/portrait-2.mp4",
                "width": 1080,
                "height": 1920,
            },
        ]

        recommended_url = helpers["_manual_recommended_candidate_url"](candidates)
        recommended_badges = helpers["_manual_candidate_badges"](
            candidates[1],
            is_system_recommendation=candidates[1]["url"] == recommended_url,
        )
        first_badges = helpers["_manual_candidate_badges"](
            candidates[0],
            is_system_recommendation=candidates[0]["url"] == recommended_url,
        )

        self.assertEqual(recommended_url, "https://v.example/portrait.mp4")
        self.assertIn("System Recommendation Badge", recommended_badges)
        self.assertIn("Vertical Video Badge", recommended_badges)
        self.assertNotIn("System Recommendation Badge", first_badges)

    def test_video_quality_ui_helpers_normalize_safe_values(self):
        helpers = _load_webui_helpers(
            "_normalize_int_range",
            "_normalize_video_crf_value",
            "_normalize_video_fps_value",
            "_normalize_audio_bitrate_kbps",
            "_libx264_preset_options",
            "_normalize_libx264_preset",
            "_video_codec_options",
            "_normalize_video_codec",
        )

        self.assertEqual(helpers["_normalize_video_crf_value"]("18"), 18)
        self.assertEqual(helpers["_normalize_video_crf_value"](True), 20)
        self.assertEqual(helpers["_normalize_video_crf_value"](99), 20)
        self.assertEqual(helpers["_normalize_video_fps_value"]("60"), 60)
        self.assertEqual(helpers["_normalize_video_fps_value"]("60fps"), 60)
        self.assertEqual(helpers["_normalize_video_fps_value"](0), 30)
        self.assertEqual(helpers["_normalize_video_fps_value"](121), 30)
        self.assertEqual(helpers["_normalize_audio_bitrate_kbps"]("256k"), 256)
        self.assertEqual(helpers["_normalize_audio_bitrate_kbps"]("256K"), 256)
        self.assertEqual(helpers["_normalize_audio_bitrate_kbps"]("256kbps"), 256)
        self.assertEqual(helpers["_normalize_audio_bitrate_kbps"](16), 192)
        self.assertEqual(helpers["_normalize_audio_bitrate_kbps"](True), 192)
        self.assertEqual(helpers["_normalize_libx264_preset"](" Slow "), "slow")
        self.assertEqual(helpers["_normalize_libx264_preset"]("turbo"), "medium")
        self.assertIn("veryslow", helpers["_libx264_preset_options"]())
        self.assertEqual(helpers["_normalize_video_codec"]("h264_nvenc"), "h264_nvenc")
        self.assertEqual(helpers["_normalize_video_codec"]("not-a-codec"), "libx264")
        self.assertIn(
            ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
            helpers["_video_codec_options"](),
        )

    def test_subject_repeat_warning_text_mentions_recent_match(self):
        helpers = _load_webui_helpers("_subject_repeat_warning_text")
        helpers["tr"] = lambda key: (
            "Similar Subject Warning: {subject} in the last {days} days ({created_at})"
        )

        warning = helpers["_subject_repeat_warning_text"](
            [
                {
                    "subject": "Budget mistakes beginners make",
                    "created_at": "2026-07-04T10:00:00+00:00",
                }
            ],
            days=5,
        )

        self.assertIn("Similar Subject Warning", warning)
        self.assertIn("Budget mistakes beginners make", warning)
        self.assertIn("5", warning)
        self.assertEqual(helpers["_subject_repeat_warning_text"]([], days=5), "")

    def test_subject_repeat_suggestion_text_mentions_subject(self):
        helpers = _load_webui_helpers("_subject_repeat_suggestion_text")
        helpers["tr"] = lambda key: "Fresh angles for {subject}"

        suggestion = helpers["_subject_repeat_suggestion_text"](
            "Budget mistakes beginners make"
        )

        self.assertIn("Budget mistakes beginners make", suggestion)
        self.assertEqual(helpers["_subject_repeat_suggestion_text"](""), "")

    def test_content_preflight_warning_helpers(self):
        helpers = _load_webui_helpers(
            "_content_preflight_warning_text",
            "_quality_gate_warning_text",
        )

        class FakeContentQuality:
            DEFAULT_QUALITY_GATE_THRESHOLD = 60

            @staticmethod
            def is_preflight_report_stale(
                report,
                video_subject="",
                video_script="",
                platform="tiktok",
                language="auto",
            ):
                return report.get("stale", False)

        translations = {
            "Content Preflight Missing Warning": "missing preflight",
            "Content Preflight Stale Warning": "stale preflight",
            "Viral Quality Gate Warning": (
                "score {score} below threshold {threshold}"
            ),
        }
        helpers["content_quality"] = FakeContentQuality
        helpers["tr"] = lambda key: translations.get(key, key)

        self.assertEqual(
            helpers["_content_preflight_warning_text"](
                None,
                "Budget mistakes",
                "Save this.",
                "tiktok",
                "en",
            ),
            "missing preflight",
        )
        self.assertEqual(
            helpers["_content_preflight_warning_text"](
                {"stale": True},
                "Budget mistakes",
                "Save this.",
                "tiktok",
                "en",
            ),
            "stale preflight",
        )
        self.assertEqual(
            helpers["_quality_gate_warning_text"](
                {"warn": True, "score": 45, "threshold": 60}
            ),
            "score 45 below threshold 60",
        )
        self.assertEqual(helpers["_quality_gate_warning_text"]({"warn": False}), "")

    def test_preflight_inputs_fall_back_to_current_session_values(self):
        helpers = _load_webui_helpers(
            "_session_text_value",
            "_preflight_input_values",
        )
        helpers["st"] = SimpleNamespace(
            session_state={
                "video_subject": " Current subject ",
                "video_script": " Current script ",
            }
        )

        values = helpers["_preflight_input_values"](
            SimpleNamespace(video_subject="", video_script="", video_language="")
        )

        self.assertEqual(values["video_subject"], "Current subject")
        self.assertEqual(values["video_script"], "Current script")
        self.assertEqual(values["language"], "auto")

        values = helpers["_preflight_input_values"](
            SimpleNamespace(
                video_subject="Param subject",
                video_script="Param script",
                video_language="tr-TR",
            )
        )

        self.assertEqual(values["video_subject"], "Param subject")
        self.assertEqual(values["video_script"], "Param script")
        self.assertEqual(values["language"], "tr-TR")

    def test_video_quality_helper_applies_values_to_task_params(self):
        helpers = _load_webui_helpers(
            "_normalize_int_range",
            "_normalize_video_crf_value",
            "_normalize_video_fps_value",
            "_normalize_audio_bitrate_kbps",
            "_libx264_preset_options",
            "_normalize_libx264_preset",
            "_video_codec_options",
            "_normalize_video_codec",
            "_apply_video_quality_params",
        )
        helpers["st"] = SimpleNamespace(
            session_state={
                "video_crf": "18",
                "video_encoder_preset": "slow",
                "video_fps": "60",
                "audio_bitrate_kbps": "256",
            }
        )
        helpers["config"] = SimpleNamespace(
            app={
                "video_codec": "h264_nvenc",
                "video_crf": 24,
                "video_encoder_preset": "fast",
                "video_fps": 24,
                "audio_bitrate": "128k",
            }
        )
        params = SimpleNamespace()

        helpers["_apply_video_quality_params"](params)

        self.assertEqual(params.video_codec, "h264_nvenc")
        self.assertEqual(params.video_crf, 18)
        self.assertEqual(params.video_encoder_preset, "slow")
        self.assertEqual(params.video_fps, 60)
        self.assertEqual(params.audio_bitrate, "256k")

    def test_current_preset_app_config_preserves_video_quality_fields(self):
        helpers = _load_webui_helpers(
            "_normalize_int_range",
            "_normalize_video_crf_value",
            "_normalize_video_fps_value",
            "_normalize_audio_bitrate_kbps",
            "_libx264_preset_options",
            "_normalize_libx264_preset",
            "_video_codec_options",
            "_normalize_video_codec",
            "_current_preset_app_config",
        )
        helpers["st"] = SimpleNamespace(
            session_state={
                "video_cooldown_enabled": True,
                "video_cooldown_days": "14",
                "video_crf": "18",
                "video_encoder_preset": " Slow ",
                "video_fps": "60",
                "audio_bitrate_kbps": "256k",
            }
        )
        helpers["config"] = SimpleNamespace(
            app={
                "video_codec": " H264_NVENC ",
                "video_crf": 24,
                "video_encoder_preset": "fast",
                "video_fps": 24,
                "audio_bitrate": "128k",
            }
        )

        app_config = helpers["_current_preset_app_config"]()

        self.assertEqual(app_config["video_codec"], "h264_nvenc")
        self.assertEqual(app_config["video_cooldown_enabled"], True)
        self.assertEqual(app_config["video_cooldown_days"], 14)
        self.assertEqual(app_config["video_crf"], 18)
        self.assertEqual(app_config["video_encoder_preset"], "slow")
        self.assertEqual(app_config["video_fps"], 60)
        self.assertEqual(app_config["audio_bitrate"], "256k")

    def test_clone_video_params_preserves_video_quality_fields(self):
        from app.models.schema import VideoParams

        helpers = _load_webui_helpers("_clone_video_params")
        helpers["VideoParams"] = VideoParams

        source_params = VideoParams(
            video_subject="quality clone",
            video_codec=" H264_NVENC ",
            video_crf=18,
            video_encoder_preset=" Slow ",
            video_fps=60,
            audio_bitrate=256,
        )

        cloned_params = helpers["_clone_video_params"](source_params)

        self.assertIsNot(cloned_params, source_params)
        self.assertEqual(cloned_params.video_codec, "h264_nvenc")
        self.assertEqual(cloned_params.video_crf, 18)
        self.assertEqual(cloned_params.video_encoder_preset, "slow")
        self.assertEqual(cloned_params.video_fps, 60)
        self.assertEqual(cloned_params.audio_bitrate, "256k")
