import json
from unittest.mock import Mock, patch

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# 测试文件直接运行时，也能从仓库根目录导入 app 包。
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import subtitle


class TestSubtitleService(unittest.TestCase):
    def test_file_to_subtitles_returns_empty_for_missing_input(self):
        """空路径和不存在的文件都应安全返回空列表。"""
        self.assertEqual(subtitle.file_to_subtitles(""), [])
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_file = Path(tmp_dir) / "missing.srt"
            self.assertEqual(subtitle.file_to_subtitles(str(missing_file)), [])

    def test_levenshtein_distance_and_similarity_cover_common_boundaries(self):
        """
        字幕校正依赖编辑距离选择是否继续合并相邻字幕，因此覆盖空字符串、
        参数交换、大小写忽略和明显不相似四种边界，防止算法调整后误合并。
        """
        self.assertEqual(subtitle.levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(subtitle.levenshtein_distance("a", "longer"), 6)
        self.assertEqual(subtitle.levenshtein_distance("hello", ""), 5)
        self.assertEqual(subtitle.similarity("Hello", "hello"), 1.0)
        self.assertLess(subtitle.similarity("hello", "world"), 0.5)

    def test_create_returns_empty_when_whisper_is_unavailable(self):
        """可选 Whisper 依赖未安装时应跳过，而不是在任务线程中抛异常。"""
        with patch.object(subtitle, "WhisperModel", None):
            self.assertEqual(subtitle.create("audio.mp3"), "")

    def test_create_returns_none_when_whisper_model_cannot_load(self):
        """模型下载或初始化失败时必须返回失败结果，并允许任务层更新状态。"""
        with patch.object(subtitle, "model", None), patch.object(
            subtitle,
            "WhisperModel",
            side_effect=RuntimeError("model unavailable"),
        ):
            self.assertIsNone(subtitle.create("audio.mp3"))

    def test_create_writes_punctuated_and_trailing_segments(self):
        """
        使用假的 Whisper 模型覆盖逐词时间戳处理，不访问网络也不加载真实模型。
        一个 segment 同时包含标点断句和末尾无标点文本，可验证两条关键写入路径。
        """

        class _FakeWhisperModel:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs

            def transcribe(self, audio_file, **kwargs):
                words = [
                    SimpleNamespace(start=0.0, end=0.4, word="Hello"),
                    SimpleNamespace(start=0.4, end=0.9, word=" world."),
                    SimpleNamespace(start=1.0, end=1.5, word="Again"),
                ]
                segment = SimpleNamespace(
                    start=0.0,
                    end=1.8,
                    words=words,
                )
                info = SimpleNamespace(language="en", language_probability=0.99)
                return [segment], info

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "generated.srt"
            with patch.object(subtitle, "model", None), patch.object(
                subtitle,
                "WhisperModel",
                _FakeWhisperModel,
            ):
                subtitle.create("audio.mp3", str(subtitle_file))

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello world", "Again"])

    def test_correct_ignores_markdown_separator_lines(self):
        """
        Whisper fallback 校正阶段也必须忽略 `---` 这类不可发声脚本行。

        如果这里继续保留 Markdown 分隔符，`correct()` 会认为脚本行数多于
        字幕行数，并补出 `00:00:00,000 --> 00:00:00,000`，剪辑软件会把
        生成的 SRT 判定为不可导入。
        """
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "第一段\n\n"
            "2\n"
            "00:00:01,100 --> 00:00:02,000\n"
            "第二段\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="第一段\n---\n第二段",
            )

            corrected_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertIn("第一段", corrected_srt)
        self.assertIn("第二段", corrected_srt)
        self.assertNotIn("---", corrected_srt)
        self.assertNotIn("00:00:00,000 --> 00:00:00,000", corrected_srt)

    def test_correct_merges_adjacent_subtitles_for_one_script_sentence(self):
        """
        Whisper 可能把一句文案拆成多个时间块。校正逻辑应合并时间范围并恢复
        原始脚本文本，避免最终字幕出现不必要的碎片。
        """
        original_srt = (
            "1\n00:00:00,100 --> 00:00:01,000\nHello\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nworld\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Hello world")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1], "00:00:00,100 --> 00:00:02,000")
        self.assertEqual(items[0][2], "Hello world")

    def test_correct_replaces_mismatch_and_appends_missing_script_line(self):
        """
        转写结果与脚本完全不一致时仍应以脚本为准；脚本多出的句子没有可复用
        时间轴时使用明确的零时间占位，避免丢失文本且保持现有兼容行为。
        """
        original_srt = "1\n00:00:00,100 --> 00:00:01,000\nWrong text\n\n"

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(str(subtitle_file), "Expected sentence. Extra sentence.")
            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(
            [item[2] for item in items],
            ["Expected sentence", "Extra sentence"],
        )
        self.assertEqual(items[1][1], "00:00:00,000 --> 00:00:00,000")

    def test_file_to_subtitles_keeps_last_block_without_trailing_newline(self):
        """
        The final subtitle must be parsed even when the SRT file does not end
        with a trailing blank line. Many tools omit it, and previously the last
        block was silently dropped because only a blank line flushed a block.
        """
        srt_without_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_without_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][2], "Hello")
        self.assertEqual(items[1][2], "World")

    def test_file_to_subtitles_parses_blocks_with_trailing_newline(self):
        """A normal SRT ending in a blank line still parses all blocks."""
        srt_with_trailing_blank = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello\n\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "World\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt_with_trailing_blank, encoding="utf-8")

            items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[2] for item in items], ["Hello", "World"])

    def test_create_uses_segment_text_when_word_timestamps_are_missing(self):
        segment = SimpleNamespace(
            start=1.25,
            end=2.75,
            text="Hello subtitle.",
            words=None,
        )
        transcribe = Mock(
            return_value=(
                [segment],
                SimpleNamespace(language="en", language_probability=0.99),
            )
        )
        fake_model = SimpleNamespace(transcribe=transcribe)

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            subtitle, "model", fake_model
        ):
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            word_timing_file = Path(tmp_dir) / "subtitle.words.json"
            subtitle.create(
                audio_file="custom-audio.mp3",
                subtitle_file=str(subtitle_file),
                word_timing_file=str(word_timing_file),
            )
            rendered_subtitle = subtitle_file.read_text(encoding="utf-8")
            word_timings = json.loads(word_timing_file.read_text(encoding="utf-8"))

        self.assertIn("00:00:01,250 --> 00:00:02,750", rendered_subtitle)
        self.assertIn("Hello subtitle.", rendered_subtitle)
        self.assertEqual(word_timings, [])
    def test_create_writes_word_timing_sidecar_when_requested(self):
        segment = SimpleNamespace(
            start=0.0,
            end=1.0,
            words=[
                SimpleNamespace(word="Merhaba", start=0.0, end=0.4),
                SimpleNamespace(word=" dunya.", start=0.4, end=1.0),
            ],
        )
        transcribe = Mock(
            return_value=(
                [segment],
                SimpleNamespace(language="tr", language_probability=0.99),
            )
        )
        fake_model = SimpleNamespace(transcribe=transcribe)

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            subtitle, "model", fake_model
        ):
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            word_timing_file = Path(tmp_dir) / "subtitle.words.json"
            subtitle.create(
                audio_file="custom-audio.mp3",
                subtitle_file=str(subtitle_file),
                word_timing_file=str(word_timing_file),
            )
            word_timings = json.loads(word_timing_file.read_text(encoding="utf-8"))

        self.assertEqual(
            word_timings,
            [
                {"text": "Merhaba", "start_time": 0.0, "end_time": 0.4},
                {"text": "dunya.", "start_time": 0.4, "end_time": 1.0},
            ],
        )
    def test_create_passes_turkish_language_hint_to_whisper(self):
        transcribe = Mock(
            return_value=(
                [],
                SimpleNamespace(language="tr", language_probability=0.99),
            )
        )
        fake_model = SimpleNamespace(transcribe=transcribe)

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            subtitle, "model", fake_model
        ):
            subtitle.create(
                audio_file="custom-audio.mp3",
                subtitle_file=str(Path(tmp_dir) / "subtitle.srt"),
                language="tr-TR",
            )

        self.assertEqual(transcribe.call_args.kwargs["language"], "tr")
    def test_create_keeps_auto_detection_for_unsupported_language(self):
        transcribe = Mock(
            return_value=(
                [],
                SimpleNamespace(language="fr", language_probability=0.99),
            )
        )
        fake_model = SimpleNamespace(transcribe=transcribe)

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            subtitle, "model", fake_model
        ):
            subtitle.create(
                audio_file="custom-audio.mp3",
                subtitle_file=str(Path(tmp_dir) / "subtitle.srt"),
                language="fr",
            )

        self.assertNotIn("language", transcribe.call_args.kwargs)
    def test_create_uses_turbo_fallback_after_memory_load_error(self):
        transcribe = Mock(
            return_value=(
                [],
                SimpleNamespace(language="tr", language_probability=0.99),
            )
        )
        fallback_model = SimpleNamespace(transcribe=transcribe)

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            subtitle, "model", None
        ), patch.object(
            subtitle, "model_size", "large-v3"
        ), patch.object(
            subtitle,
            "fallback_model_size",
            "large-v3-turbo",
            create=True,
        ), patch.object(
            subtitle.os.path, "isdir", return_value=False
        ), patch.object(
            subtitle.os.path, "isfile", return_value=False
        ), patch.object(
            subtitle,
            "WhisperModel",
            side_effect=[
                RuntimeError("mkl_malloc: failed to allocate memory"),
                fallback_model,
            ],
        ) as whisper_model:
            subtitle.create(
                audio_file="custom-audio.mp3",
                subtitle_file=str(Path(tmp_dir) / "subtitle.srt"),
                language="tr",
            )

        self.assertEqual(
            [call.kwargs["model_size_or_path"] for call in whisper_model.call_args_list],
            ["large-v3", "large-v3-turbo"],
        )
        self.assertEqual(transcribe.call_args.kwargs["language"], "tr")
    def test_correct_fixes_script_typo_without_changing_subtitle_timestamps(self):
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "Meraba dünya\n\n"
            "2\n"
            "00:00:01,100 --> 00:00:02,000\n"
            "Bu bir test\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")
            original_times = [item[1] for item in subtitle.file_to_subtitles(str(subtitle_file))]

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="Merhaba dünya. Bu bir test.",
            )

            corrected_items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[1] for item in corrected_items], original_times)
        self.assertEqual(
            [item[2] for item in corrected_items],
            ["Merhaba dünya.", "Bu bir test."],
        )
    def test_correct_fixes_turkish_word_split_without_changing_timestamps(self):
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "Bugün ki durum iyi\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")
            original_times = [item[1] for item in subtitle.file_to_subtitles(str(subtitle_file))]

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="Bugünkü durum iyi.",
            )

            corrected_items = subtitle.file_to_subtitles(str(subtitle_file))

        self.assertEqual([item[1] for item in corrected_items], original_times)
        self.assertEqual([item[2] for item in corrected_items], ["Bugünkü durum iyi."])
    def test_correct_leaves_low_confidence_mismatch_untouched(self):
        original_srt = (
            "1\n"
            "00:00:00,100 --> 00:00:01,000\n"
            "Tamamen alakasız\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(original_srt, encoding="utf-8")

            subtitle.correct(
                subtitle_file=str(subtitle_file),
                video_script="Bu metin başka bir konuda ilerliyor.",
            )

            corrected_srt = subtitle_file.read_text(encoding="utf-8")

        self.assertEqual(corrected_srt, original_srt)
    def test_build_subtitle_suspicion_report_flags_script_mismatch(self):
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Meraba dunya\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt, encoding="utf-8")

            report = subtitle.build_subtitle_suspicion_report(
                subtitle_file=str(subtitle_file),
                video_script="Merhaba dünya.",
                language="tr",
            )

        self.assertEqual(report["language"], "tr")
        self.assertEqual(report["suspicious_count"], 1)
        self.assertEqual(report["items"][0]["reason"], "script_mismatch")
        self.assertEqual(report["items"][0]["suggested_text"], "Merhaba dünya.")
        self.assertEqual(
            report["items"][0]["time_range"],
            "00:00:00,000 --> 00:00:01,000",
        )
    def test_build_subtitle_suspicion_report_flags_unmatched_subtitle(self):
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Tamamen alakasız\n\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(srt, encoding="utf-8")

            report = subtitle.build_subtitle_suspicion_report(
                subtitle_file=str(subtitle_file),
                video_script="Bu metin başka bir konuda ilerliyor.",
                language="tr",
            )

        self.assertEqual(report["suspicious_count"], 1)
        self.assertEqual(report["items"][0]["reason"], "no_script_match")
        self.assertIsNone(report["items"][0]["suggested_text"])
    def test_build_subtitle_suspicion_report_skips_unsupported_language(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            subtitle_file.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nBonjour\n\n",
                encoding="utf-8",
            )

            report = subtitle.build_subtitle_suspicion_report(
                subtitle_file=str(subtitle_file),
                video_script="Bonjour.",
                language="fr",
            )

        self.assertIsNone(report)
    def test_write_subtitle_suspicion_report_preserves_unicode(self):
        report = {
            "language": "tr",
            "subtitle_count": 1,
            "suspicious_count": 1,
            "items": [
                {
                    "index": 1,
                    "subtitle_text": "Meraba dünya",
                    "suggested_text": "Merhaba dünya.",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "subtitle.review.json"
            subtitle.write_subtitle_suspicion_report(report, str(report_path))
            saved_report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(saved_report, report)
    def test_manual_subtitle_corrections_restore_text_after_regeneration(self):
        generated_srt = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Hello wurld.\n\n"
        )
        manual_srt = generated_srt.replace("Hello wurld.", "Hello world!")

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            baseline_file = Path(tmp_dir) / "subtitle.generated.srt"
            corrections_file = Path(tmp_dir) / "subtitle.corrections.json"
            subtitle_file.write_text(generated_srt, encoding="utf-8")
            self.assertTrue(
                subtitle.save_subtitle_generated_baseline(
                    str(subtitle_file), str(baseline_file)
                )
            )

            subtitle_file.write_text(manual_srt, encoding="utf-8")
            captured = subtitle.capture_manual_subtitle_corrections(
                str(subtitle_file),
                str(baseline_file),
                str(corrections_file),
            )
            subtitle_file.write_text(generated_srt, encoding="utf-8")
            restored = subtitle.apply_manual_subtitle_corrections(
                str(subtitle_file), str(corrections_file)
            )
            restored_text = subtitle.file_to_subtitles(str(subtitle_file))[0][2]
            corrections = json.loads(corrections_file.read_text(encoding="utf-8"))

        self.assertEqual(captured, 1)
        self.assertEqual(restored, 1)
        self.assertEqual(restored_text, "Hello world!")
        self.assertEqual(corrections["corrections"][0]["original_text"], "Hello wurld.")
        self.assertEqual(corrections["corrections"][0]["replacement_text"], "Hello world!")
    def test_manual_subtitle_corrections_do_not_override_new_source_text(self):
        baseline_srt = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "Original wording.\n\n"
        )
        changed_srt = baseline_srt.replace("Original wording.", "Fresh wording.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            subtitle_file = Path(tmp_dir) / "subtitle.srt"
            baseline_file = Path(tmp_dir) / "subtitle.generated.srt"
            corrections_file = Path(tmp_dir) / "subtitle.corrections.json"
            subtitle_file.write_text(baseline_srt, encoding="utf-8")
            subtitle.save_subtitle_generated_baseline(
                str(subtitle_file), str(baseline_file)
            )
            subtitle_file.write_text(
                baseline_srt.replace("Original wording.", "Manual wording."),
                encoding="utf-8",
            )
            subtitle.capture_manual_subtitle_corrections(
                str(subtitle_file),
                str(baseline_file),
                str(corrections_file),
            )
            subtitle_file.write_text(changed_srt, encoding="utf-8")
            restored = subtitle.apply_manual_subtitle_corrections(
                str(subtitle_file), str(corrections_file)
            )
            restored_text = subtitle.file_to_subtitles(str(subtitle_file))[0][2]

        self.assertEqual(restored, 0)
        self.assertEqual(restored_text, "Fresh wording.")
    def test_model_load_error_guidance_identifies_memory_exhaustion(self):
        guidance = subtitle._model_load_error_guidance(
            RuntimeError("mkl_malloc: failed to allocate memory")
        )

        self.assertIn("memory", guidance.casefold())
        self.assertNotIn("network", guidance.casefold())


if __name__ == "__main__":
    unittest.main()
