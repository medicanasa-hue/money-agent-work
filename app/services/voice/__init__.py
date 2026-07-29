import edge_tts
import os
import requests
import subprocess
from edge_tts import SubMaker
from moviepy.audio.io.AudioFileClip import AudioFileClip
from openai import OpenAI

from app.config import config
from app.utils import utils
from .discovery import (
    get_all_azure_voices,
    get_chatterbox_voices,
    get_elevenlabs_voices,
    get_gemini_voices,
    get_mimo_voices,
    get_siliconflow_voices,
)
from .dispatch import (
    convert_rate_to_percent,
    create_edge_tts_communicate,
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    generate_silent_audio,
    get_edge_tts_timeout_seconds,
    populate_legacy_submaker_with_full_text,
    stream_edge_tts_chunks,
    tts,
)
from .naming import (
    NO_VOICE_NAME,
    estimate_no_voice_duration,
    is_azure_v2_voice,
    is_chatterbox_voice,
    is_elevenlabs_voice,
    is_gemini_voice,
    is_mimo_voice,
    is_no_voice,
    is_siliconflow_voice,
    parse_voice_name,
)
from .loudness import normalize_narration_loudness
from .providers import (
    _build_azure_v2_ssml,
    azure_tts_v1,
    azure_tts_v2,
    chatterbox_tts,
    elevenlabs_tts,
    gemini_tts,
    mimo_tts,
    siliconflow_tts,
)
from .subtitles import (
    _build_subtitle_items_from_edge_cues,
    _match_script_line,
    create_karaoke_ass_variant,
    create_karaoke_ass_from_word_timings,
    create_karaoke_ass_subtitle,
    create_karaoke_subtitle,
    create_subtitle,
    get_audio_duration,
    inspect_subtitle_readability,
    mktimestamp,
    reflow_subtitle_items,
)

__all__ = [
    "NO_VOICE_NAME",
    "OpenAI",
    "AudioFileClip",
    "SubMaker",
    "_build_subtitle_items_from_edge_cues",
    "_build_azure_v2_ssml",
    "_match_script_line",
    "azure_tts_v1",
    "azure_tts_v2",
    "chatterbox_tts",
    "config",
    "convert_rate_to_percent",
    "create_edge_tts_communicate",
    "create_karaoke_ass_variant",
    "create_karaoke_ass_from_word_timings",
    "create_karaoke_ass_subtitle",
    "create_karaoke_subtitle",
    "create_subtitle",
    "edge_tts",
    "elevenlabs_tts",
    "ensure_file_path_exists",
    "ensure_legacy_submaker_fields",
    "estimate_no_voice_duration",
    "gemini_tts",
    "generate_silent_audio",
    "get_all_azure_voices",
    "get_audio_duration",
    "get_chatterbox_voices",
    "get_edge_tts_timeout_seconds",
    "get_elevenlabs_voices",
    "get_gemini_voices",
    "get_mimo_voices",
    "get_siliconflow_voices",
    "is_azure_v2_voice",
    "is_chatterbox_voice",
    "is_elevenlabs_voice",
    "is_gemini_voice",
    "is_mimo_voice",
    "is_no_voice",
    "is_siliconflow_voice",
    "inspect_subtitle_readability",
    "mimo_tts",
    "mktimestamp",
    "normalize_narration_loudness",
    "os",
    "parse_voice_name",
    "populate_legacy_submaker_with_full_text",
    "reflow_subtitle_items",
    "requests",
    "siliconflow_tts",
    "subprocess",
    "stream_edge_tts_chunks",
    "tts",
    "utils",
]
