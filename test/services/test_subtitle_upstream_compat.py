from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import ImageFont

from app.config import config
from app.services import subtitle, video, voice


FONT = str(
    Path(__file__).resolve().parents[2] / "resource/fonts/BeVietnamPro-Medium.ttf"
)


@pytest.mark.parametrize(
    "text,width",
    [
        ("AAAA", 1000),
        ("AAAA\nMMMM\nNNNN", 1000),
        ("A long narration without descenders and with extra words", 160),
    ],
)
def test_subtitle_height_uses_complete_font_lines(text, width):
    wrapped, height = video.wrap_text(text, max_width=width, font=FONT, fontsize=32)
    ascent, descent = ImageFont.truetype(FONT, 32).getmetrics()
    assert height >= len(wrapped.split("\n")) * (ascent + descent)


@pytest.mark.parametrize("prompt", ["MoneyPrinterTurbo, İstanbul, TÜBİTAK", "", None])
def test_whisper_receives_optional_vocabulary_without_losing_language_hint(
    tmp_path, prompt
):
    transcribe = Mock(
        return_value=([], SimpleNamespace(language="tr", language_probability=0.99))
    )
    with (
        patch.object(subtitle, "model", SimpleNamespace(transcribe=transcribe)),
        patch.dict(config.whisper, {"initial_prompt": prompt}),
    ):
        subtitle.create(
            "synthetic.wav", str(tmp_path / "subtitle.srt"), language="tr-TR"
        )
    assert transcribe.call_args.kwargs["language"] == "tr"
    if prompt:
        assert transcribe.call_args.kwargs["initial_prompt"] == prompt
    else:
        assert "initial_prompt" not in transcribe.call_args.kwargs


def test_gemini_catalog_includes_new_official_voices_and_removes_invalid_ids():
    catalogue = voice.get_gemini_voices()
    assert "gemini:Sulafat-Warm" in catalogue
    assert "gemini:Callirrhoe-Easy-going" in catalogue
    assert not any(item.startswith("gemini:Thalia-") for item in catalogue)
    assert len(catalogue) == len(set(catalogue)) == 30


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("gemini:Zephyr-Female", "Zephyr"),
        ("gemini:Zephyr-Bright", "Zephyr"),
        ("gemini:Callirrhoe-Easy-going", "Callirrhoe"),
    ],
)
def test_gemini_dispatch_accepts_old_and_new_saved_voice_labels(
    selection, expected, tmp_path
):
    with patch.object(voice, "gemini_tts", return_value="timings") as tts:
        result = voice.tts("Hello.", selection, 1.0, str(tmp_path / "audio.mp3"))
    assert result == "timings"
    assert tts.call_args.args[1] == expected
