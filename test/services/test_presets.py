import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import presets


class TestVideoPresets(unittest.TestCase):
    def test_save_and_load_preset_roundtrip(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            path = presets.save_preset(
                "Shorts Default",
                {
                    "video_subject": "city cafe ideas",
                    "video_aspect": "9:16",
                    "voice_name": "en-US-AriaNeural-Female",
                    "font_size": 48,
                    "subtitle_enabled": False,
                },
                preset_dir=preset_dir,
            )

            self.assertTrue(Path(path).is_file())

            loaded = presets.load_preset("Shorts Default", preset_dir=preset_dir)

        self.assertEqual(loaded["version"], presets.PRESET_VERSION)
        self.assertEqual(loaded["name"], "Shorts Default")
        self.assertEqual(loaded["params"]["video_subject"], "city cafe ideas")
        self.assertEqual(loaded["params"]["video_aspect"], "9:16")
        self.assertEqual(loaded["params"]["voice_name"], "en-US-AriaNeural-Female")
        self.assertEqual(loaded["params"]["font_size"], 48)
        self.assertFalse(loaded["params"]["subtitle_enabled"])

    def test_save_and_load_preset_preserves_cooldown_app_config(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            presets.save_preset(
                "Cooldown Preset",
                {
                    "video_subject": "daily news",
                },
                preset_dir=preset_dir,
                app_config={
                    "video_cooldown_enabled": True,
                    "video_cooldown_days": 14,
                },
            )

            loaded = presets.load_preset("Cooldown Preset", preset_dir=preset_dir)

        self.assertEqual(
            loaded["app_config"],
            {
                "video_cooldown_enabled": True,
                "video_cooldown_days": 14,
            },
        )

    def test_save_and_load_preset_preserves_video_quality_app_config(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            presets.save_preset(
                "Quality Preset",
                {
                    "video_subject": "market update",
                },
                preset_dir=preset_dir,
                app_config={
                    "video_codec": "h264_nvenc",
                    "video_crf": "18",
                    "video_encoder_preset": "slow",
                    "video_fps": "60",
                    "audio_bitrate": 256,
                },
            )

            loaded = presets.load_preset("Quality Preset", preset_dir=preset_dir)

        self.assertEqual(
            loaded["app_config"],
            {
                "video_codec": "h264_nvenc",
                "video_crf": 18,
                "video_encoder_preset": "slow",
                "video_fps": 60,
                "audio_bitrate": "256k",
            },
        )

    def test_partial_import_uses_video_params_defaults(self):
        payload = presets.import_preset_payload(
            {
                "name": "Voice Style",
                "params": {
                    "voice_name": "en-US-JennyNeural-Female",
                },
            }
        )

        self.assertEqual(payload["name"], "Voice Style")
        self.assertNotIn("app_config", payload)
        self.assertEqual(payload["params"]["video_subject"], "")
        self.assertEqual(payload["params"]["voice_name"], "en-US-JennyNeural-Female")
        self.assertEqual(payload["params"]["video_aspect"], "9:16")

    def test_import_preset_rejects_unknown_app_config_field(self):
        with self.assertRaises(presets.PresetError):
            presets.import_preset_payload(
                {
                    "name": "Bad App Config",
                    "params": {
                        "video_subject": "topic",
                    },
                    "app_config": {
                        "video_cooldown_enabled": True,
                        "unknown_config": True,
                    },
                }
            )

    def test_import_preset_rejects_invalid_cooldown_days(self):
        with self.assertRaises(presets.PresetError):
            presets.import_preset_payload(
                {
                    "name": "Bad Cooldown",
                    "params": {
                        "video_subject": "topic",
                    },
                    "app_config": {
                        "video_cooldown_enabled": True,
                        "video_cooldown_days": 5,
                    },
                }
            )

    def test_import_preset_rejects_invalid_video_codec(self):
        with self.assertRaises(presets.PresetError):
            presets.import_preset_payload(
                {
                    "name": "Bad Codec",
                    "params": {
                        "video_subject": "topic",
                    },
                    "app_config": {
                        "video_codec": "not_a_codec",
                    },
                }
            )

    def test_unknown_param_is_rejected(self):
        with self.assertRaises(presets.PresetError):
            presets.import_preset_payload(
                {
                    "name": "Bad Preset",
                    "params": {
                        "video_subject": "topic",
                        "not_a_video_param": True,
                    },
                }
            )

    def test_unsupported_version_is_rejected(self):
        with self.assertRaises(presets.PresetError):
            presets.import_preset_payload(
                {
                    "version": 999,
                    "name": "Future Preset",
                    "params": {},
                }
            )

    def test_preset_name_cannot_escape_directory(self):
        with self.assertRaises(presets.PresetError):
            presets.save_preset("../outside", {"video_subject": "topic"})

    def test_invalid_json_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            path = Path(preset_dir) / "broken.json"
            path.write_text("{bad-json", encoding="utf-8")

            with self.assertRaises(presets.PresetError):
                presets.load_preset("broken", preset_dir=preset_dir)

    def test_list_presets_returns_safe_names(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            presets.save_preset("B preset", {"video_subject": "b"}, preset_dir=preset_dir)
            presets.save_preset("a preset", {"video_subject": "a"}, preset_dir=preset_dir)
            (Path(preset_dir) / "ignored.txt").write_text("x", encoding="utf-8")

            self.assertEqual(
                presets.list_presets(preset_dir=preset_dir),
                ["a preset", "B preset"],
            )

    def test_delete_preset_removes_only_valid_preset_file(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            path = Path(
                presets.save_preset(
                    "Delete Me",
                    {"video_subject": "topic"},
                    preset_dir=preset_dir,
                )
            )

            presets.delete_preset("Delete Me", preset_dir=preset_dir)

            self.assertFalse(path.exists())
            self.assertEqual(presets.list_presets(preset_dir=preset_dir), [])

    def test_delete_missing_preset_is_rejected(self):
        with tempfile.TemporaryDirectory() as preset_dir:
            with self.assertRaises(presets.PresetError):
                presets.delete_preset("Missing", preset_dir=preset_dir)

    def test_builtin_presets_are_loadable(self):
        names = presets.list_builtin_presets()

        self.assertIn("Finance Shorts TR", names)

        payload = presets.load_builtin_preset("Finance Shorts TR")

        self.assertEqual(payload["version"], presets.PRESET_VERSION)
        self.assertEqual(payload["name"], "Finance Shorts TR")
        self.assertEqual(payload["params"]["video_language"], "tr-TR")
        self.assertTrue(payload["params"]["match_materials_to_script"])

    def test_missing_builtin_preset_is_rejected(self):
        with self.assertRaises(presets.PresetError):
            presets.load_builtin_preset("Not Real")


if __name__ == "__main__":
    unittest.main()
