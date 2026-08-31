import hashlib
import json
import math
import os
import re
import sys
import threading
import webbrowser
from collections.abc import Mapping
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# The project package must take precedence over third-party packages also named
# ``app`` when Streamlit is launched outside the repository root.
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import (
    batch_postprocessing,
    cost_estimate,
    content_quality,
    content_intelligence,
    history,
    llm,
    local_material_catalog,
    material,
    material_benchmark,
    metrics_sync,
    publish_insights,
    presets,
    provider_health,
    quality_calibration,
    render_quality,
    review_feedback,
    subtitle,
    thumbnail,
    upload_post,
    visual_duplicates,
    viral_analyzer,
    voice,
    webui_task,
)
from app.services import bgm as bgm_service
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import task as tm
from app.services import video as video_service
from app.services.state import state
from app.utils.openmontage_materials import (
    find_openmontage_output,
    validate_openmontage_output,
)
from app.utils.logging_utils import configure_terminal_logger
from app.utils import file_security, utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}

div[data-testid="stVerticalBlockBorderWrapper"] {
    border-color: #D0D5DD !important;
    border-radius: 8px !important;
}

div[data-testid="stMetric"] {
    padding: 0.25rem 0;
}

div[data-testid="stMetric"] label {
    color: #667085 !important;
}

div[data-testid="stMetricValue"] {
    font-size: 1.05rem !important;
}

div[data-testid="stCaptionContainer"] {
    color: #667085 !important;
    line-height: 1.35 !important;
}

div[data-testid="stDataFrame"] {
    border: 1px solid #EAECF0 !important;
    border-radius: 8px !important;
}

details[data-testid="stExpander"] {
    border-color: #D0D5DD !important;
    border-radius: 8px !important;
}

@media (max-width: 640px) {
    h1 {
        font-size: clamp(1.4rem, 7vw, 1.75rem) !important;
        line-height: 1.2 !important;
        white-space: nowrap !important;
    }

    div[data-testid="stMetric"] {
        padding: 0.15rem 0;
    }

    div[data-testid="stMetricValue"] {
        font-size: 0.95rem !important;
    }

    div[data-testid="stCaptionContainer"],
    div[data-testid="stMarkdownContainer"] p,
    pre,
    code {
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
    }

    div[data-testid="stTabs"] [role="tablist"] {
        overflow-x: auto !important;
        scrollbar-width: thin;
    }
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# å®šä¹‰èµ„æºç›®å½•
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
_SUPPORTED_UI_LANGUAGE_CODES = ("en", "tr")
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)


def _visible_ui_locale_codes(locales):
    """Keep the product language picker focused on the active supported UI."""
    return [code for code in _SUPPORTED_UI_LANGUAGE_CODES if code in locales]


def _find_final_task_video(task_path: str) -> str:
    """Return the first numbered final output and ignore temporary videos."""
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """Record browser uploads that cannot be restored automatically."""
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """Return uploaded inputs still required after restoring an old task."""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            unmet.add("custom_audio")

    return unmet


def _parse_chatterbox_voices(voices):
    # Chatterbox æ˜¯è‡ªæ‰˜ç®¡æœåŠ¡ï¼ŒéŸ³è‰²åˆ—è¡¨ç”±ç”¨æˆ·åœ¨ WebUI ä¸­æ‰‹åŠ¨è¾“å…¥ã€‚
    # è¿™é‡Œç»Ÿä¸€å…¼å®¹ TOML æ•°ç»„å’Œè¾“å…¥æ¡†é‡Œçš„é€—å·åˆ†éš”å­—ç¬¦ä¸²ï¼Œé¿å…ä¸‹æ‹‰æ¡†ã€
    # è¯•å¬æŒ‰é’®å’Œåç»­ç”Ÿæˆæµç¨‹ä½¿ç”¨ä¸åŒæ ¼å¼å¯¼è‡´çŠ¶æ€ä¸ä¸€è‡´ã€‚
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit çš„æŒ‰é’®ä¼šè§¦å‘æ•´é¡µ rerunï¼Œè€Œ Chatterbox é…ç½®è¾“å…¥æ¡†ä½äº
    # â€œè¯•å¬è¯­éŸ³åˆæˆâ€æŒ‰é’®ä¹‹åã€‚å¦‚æœè¯•å¬æ—¶åªè¯»å– config.chatterboxï¼Œå¯èƒ½æ‹¿ä¸åˆ°
    # ç”¨æˆ·åˆšåœ¨è¾“å…¥æ¡†é‡Œå¡«å…¥çš„ base_url/model/voicesã€‚å…ˆä» session_state åŒæ­¥ä¸€æ¬¡ï¼Œ
    # å¯ä»¥ä¿è¯æŒ‰é’®é€»è¾‘å’Œè¾“å…¥æ¡†æ˜¾ç¤ºé€»è¾‘ä½¿ç”¨åŒä¸€ä»½æœ€æ–°é…ç½®ã€‚
    config.chatterbox["base_url"] = (
        st.session_state.get(
            "chatterbox_base_url_input",
            config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
        )
        or ""
    ).strip()
    config.chatterbox["api_key"] = st.session_state.get(
        "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
    )
    config.chatterbox["model_id"] = (
        st.session_state.get(
            "chatterbox_model_input",
            config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
        )
        or DEFAULT_CHATTERBOX_MODEL
    ).strip()
    config.chatterbox["voices"] = _parse_chatterbox_voices(
        st.session_state.get(
            "chatterbox_voices_input",
            config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
        )
    )


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # æœ‰äº› OpenAI-compatible TTS æœåŠ¡ï¼Œä¾‹å¦‚ travisvn/chatterbox-tts-apiï¼Œ
    # å³ä½¿è¯·æ±‚ response_format=mp3ï¼Œä¹Ÿä¼šè¿”å› WAV å†…å®¹ã€‚WebUI è¯•å¬å¦‚æœå›ºå®š
    # ä½¿ç”¨ audio/mp3ï¼Œæµè§ˆå™¨å¯èƒ½æ— æ³•æ’­æ”¾ï¼Œå› æ­¤è¿™é‡ŒæŒ‰æ–‡ä»¶å¤´è¯†åˆ«çœŸå®æ ¼å¼ã€‚
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _uploaded_file_fingerprint(uploaded_file) -> str:
    """Build a session-local cache key without persisting uploaded audio."""
    content = bytes(uploaded_file.getbuffer())
    digest = hashlib.sha256(content).hexdigest()
    return f"{uploaded_file.name}:{len(content)}:{digest}"


def _render_custom_bgm_preview(uploaded_file, enabled: bool) -> None:
    if not uploaded_file or not enabled:
        return

    fingerprint = _uploaded_file_fingerprint(uploaded_file)
    cache = st.session_state.get("_custom_bgm_validation_cache")
    if not isinstance(cache, dict) or cache.get("fingerprint") != fingerprint:
        try:
            bgm_service.validate_bgm_upload(uploaded_file.name, uploaded_file)
        except bgm_service.BgmUploadError:
            logger.warning(
                "WebUI background music validation rejected the uploaded file"
            )
            cache = {"fingerprint": fingerprint, "status": "invalid"}
        except bgm_service.BgmServiceError:
            logger.error("WebUI background music validation service failed")
            cache = {"fingerprint": fingerprint, "status": "service_error"}
        else:
            cache = {"fingerprint": fingerprint, "status": "ready"}
        st.session_state["_custom_bgm_validation_cache"] = cache

    status = cache.get("status")
    if status == "invalid":
        st.error(tr("Invalid Background Music"))
        return
    if status == "service_error":
        st.error(tr("Background Music Validation Failed"))
        return

    st.info(f"{tr('Background Music Ready')}: {uploaded_file.name}")
    bgm_header = bytes(uploaded_file.getbuffer()[:12])
    st.audio(
        uploaded_file,
        format=_detect_audio_mime(uploaded_file.name, bgm_header),
    )


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _resolve_output_aspects(primary_aspect, additional_aspect_values):
    try:
        primary = VideoAspect(primary_aspect)
    except (TypeError, ValueError):
        primary = VideoAspect.portrait

    aspects = [primary]
    for value in additional_aspect_values or []:
        try:
            aspect = VideoAspect(value)
        except (TypeError, ValueError):
            continue
        if aspect not in aspects:
            aspects.append(aspect)
    return aspects


def _normalize_int_range(value, default, min_value, max_value):
    if isinstance(value, bool):
        return default
    try:
        parsed_value = int(value)
    except (TypeError, ValueError):
        return default
    if min_value <= parsed_value <= max_value:
        return parsed_value
    return default


def _normalize_video_crf_value(value):
    return _normalize_int_range(value, 20, 0, 51)


def _normalize_video_fps_value(value):
    if isinstance(value, str):
        value = value.strip().lower()
        if value.endswith("fps"):
            value = value[:-3].strip()
    return _normalize_int_range(value, 30, 1, 120)


def _normalize_audio_bitrate_kbps(value):
    if isinstance(value, bool):
        return 192
    if isinstance(value, str):
        value = value.strip().lower()
        if value.endswith("kbps"):
            value = value[:-4].strip()
        elif value.endswith("k"):
            value = value[:-1]
    return _normalize_int_range(value, 192, 32, 512)


def _apply_video_quality_preset_once(session_state, preset_name, preset_values):
    """Apply a preset only when the user selects a different one."""
    selected_preset = str(preset_name or "")
    applied_state_key = "_applied_video_quality_preset"
    if session_state.get(applied_state_key) == selected_preset:
        return False

    if isinstance(preset_values, dict):
        for field in (
            "video_crf",
            "video_encoder_preset",
            "video_fps",
            "audio_bitrate_kbps",
        ):
            if field in preset_values:
                session_state[field] = preset_values[field]
    session_state[applied_state_key] = selected_preset
    return bool(preset_values)


def _libx264_preset_options():
    return (
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    )


def _normalize_libx264_preset(value):
    if not isinstance(value, str):
        return "medium"
    preset = value.strip().lower()
    if preset in _libx264_preset_options():
        return preset
    return "medium"


def _video_codec_options():
    return [
        ("libx264 (CPU)", "libx264"),
        ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
        ("AMD AMF (h264_amf)", "h264_amf"),
        ("Intel QSV (h264_qsv)", "h264_qsv"),
        ("Windows MediaFoundation (h264_mf)", "h264_mf"),
        ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
    ]


def _normalize_video_codec(value):
    codec = str(value or "").strip().lower()
    if codec in {item[1] for item in _video_codec_options()}:
        return codec
    return "libx264"


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "video_script_prompt" not in st.session_state:
    st.session_state["video_script_prompt"] = ""
if "custom_system_prompt" not in st.session_state:
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
if "use_custom_system_prompt" not in st.session_state:
    st.session_state["use_custom_system_prompt"] = False
if "match_materials_to_script" not in st.session_state:
    st.session_state["match_materials_to_script"] = bool(
        config.app.get("match_materials_to_script", False)
    )
if "smart_scene_queries" not in st.session_state:
    st.session_state["smart_scene_queries"] = bool(
        config.app.get("smart_scene_queries", False)
    )
if "material_search_max_page" not in st.session_state:
    try:
        st.session_state["material_search_max_page"] = max(
            1,
            min(50, int(config.app.get("material_search_max_page", 1) or 1)),
        )
    except (TypeError, ValueError):
        st.session_state["material_search_max_page"] = 1
if "video_cooldown_enabled" not in st.session_state:
    video_cooldown_enabled = config.app.get("video_cooldown_enabled", False)
    if isinstance(video_cooldown_enabled, str):
        video_cooldown_enabled = video_cooldown_enabled.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    st.session_state["video_cooldown_enabled"] = bool(video_cooldown_enabled)
if "video_cooldown_days" not in st.session_state:
    try:
        st.session_state["video_cooldown_days"] = max(
            1,
            int(config.app.get("video_cooldown_days", 7) or 7),
        )
    except (TypeError, ValueError):
        st.session_state["video_cooldown_days"] = 7
if "video_deband_enabled" not in st.session_state:
    st.session_state["video_deband_enabled"] = _config_bool(
        config.app.get("video_deband_enabled", False)
    )
if "preview_quality_rerank_enabled" not in st.session_state:
    st.session_state["preview_quality_rerank_enabled"] = _config_bool(
        config.app.get("preview_quality_rerank_enabled", False)
    )
if "video_crf" not in st.session_state:
    st.session_state["video_crf"] = _normalize_video_crf_value(
        config.app.get("video_crf", 20)
    )
if "video_encoder_preset" not in st.session_state:
    st.session_state["video_encoder_preset"] = _normalize_libx264_preset(
        config.app.get("video_encoder_preset", "medium")
    )
if "video_fps" not in st.session_state:
    st.session_state["video_fps"] = _normalize_video_fps_value(
        config.app.get("video_fps", 30)
    )
if "audio_bitrate_kbps" not in st.session_state:
    st.session_state["audio_bitrate_kbps"] = _normalize_audio_bitrate_kbps(
        config.app.get("audio_bitrate", "192k")
    )
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "local_video_materials" not in st.session_state:
    st.session_state["local_video_materials"] = []
if "current_generation_task_id" not in st.session_state:
    st.session_state["current_generation_task_id"] = ""
if "background_generation_contexts" not in st.session_state:
    st.session_state["background_generation_contexts"] = {}
if "batch_subjects" not in st.session_state:
    st.session_state["batch_subjects"] = ""
if "use_manual_batch_scripts" not in st.session_state:
    st.session_state["use_manual_batch_scripts"] = False
if "batch_script_blocks" not in st.session_state:
    st.session_state["batch_script_blocks"] = ""
if "content_plan" not in st.session_state:
    st.session_state["content_plan"] = None
if "content_preflight_report" not in st.session_state:
    st.session_state["content_preflight_report"] = None
if "content_niche" not in st.session_state:
    st.session_state["content_niche"] = ""
if "content_target_audience" not in st.session_state:
    st.session_state["content_target_audience"] = ""
if "content_tone" not in st.session_state:
    st.session_state["content_tone"] = ""
if "content_plan_days" not in st.session_state:
    st.session_state["content_plan_days"] = 7
if "content_daily_count" not in st.session_state:
    st.session_state["content_daily_count"] = 1
if "content_idea_count" not in st.session_state:
    st.session_state["content_idea_count"] = 7
if "content_use_trend_context" not in st.session_state:
    st.session_state["content_use_trend_context"] = False
if "content_trend_source" not in st.session_state:
    st.session_state["content_trend_source"] = content_intelligence.TREND_SOURCE_STATIC
if "social_metadata" not in st.session_state:
    st.session_state["social_metadata"] = None
if "auto_social_metadata_after_video" not in st.session_state:
    st.session_state["auto_social_metadata_after_video"] = True
if "upload_post_enabled" not in st.session_state:
    st.session_state["upload_post_enabled"] = _config_bool(
        config.app.get("upload_post_enabled", False)
    )
if "upload_post_auto_upload" not in st.session_state:
    st.session_state["upload_post_auto_upload"] = _config_bool(
        config.app.get("upload_post_auto_upload", False)
    )
if "upload_post_allow_public_youtube" not in st.session_state:
    st.session_state["upload_post_allow_public_youtube"] = _config_bool(
        config.app.get("upload_post_allow_public_youtube", False)
    )
if "upload_post_youtube_privacy_status" not in st.session_state:
    st.session_state["upload_post_youtube_privacy_status"] = (
        upload_post.normalize_youtube_privacy_status(
            config.app.get(
                "upload_post_youtube_privacy_status",
                upload_post.YOUTUBE_PRIVACY_UNLISTED,
            ),
            allow_public=st.session_state["upload_post_allow_public_youtube"],
        )
    )
if "viral_analysis" not in st.session_state:
    st.session_state["viral_analysis"] = None
if "script_rewrite_suggestion" not in st.session_state:
    st.session_state["script_rewrite_suggestion"] = None
if "auto_viral_analysis_after_video" not in st.session_state:
    st.session_state["auto_viral_analysis_after_video"] = False
if "viral_quality_gate_enabled" not in st.session_state:
    st.session_state["viral_quality_gate_enabled"] = _config_bool(
        config.app.get("viral_quality_gate_enabled", False)
    )
if "viral_quality_gate_threshold" not in st.session_state:
    st.session_state["viral_quality_gate_threshold"] = _normalize_int_range(
        config.app.get(
            "viral_quality_gate_threshold",
            content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
        ),
        content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
        0,
        100,
    )
if "manual_video_selection_enabled" not in st.session_state:
    st.session_state["manual_video_selection_enabled"] = False
if "manual_video_candidates" not in st.session_state:
    st.session_state["manual_video_candidates"] = []
if "manual_video_selected_urls" not in st.session_state:
    st.session_state["manual_video_selected_urls"] = []

pending_content_topic = st.session_state.pop("_pending_content_topic", None)
if pending_content_topic:
    st.session_state["video_subject"] = pending_content_topic.get("subject", "")
    st.session_state["video_script_prompt"] = pending_content_topic.get(
        "script_prompt", ""
    )

# åŠ è½½è¯­è¨€æ–‡ä»¶
locales = utils.load_locales(i18n_dir)

# åˆ›å»ºä¸€ä¸ªé¡¶éƒ¨æ ï¼ŒåŒ…å«æ ‡é¢˜å’Œè¯­è¨€é€‰æ‹©
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"MoneyPrinterTurbo v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(_visible_ui_locale_codes(locales)):
        display_languages.append(f"{code} - {locales[code].get('Language', code)}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / Dil",
        options=display_languages,
        index=selected_index,
        key="top_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    configure_terminal_logger(
        sys.stdout,
        level="DEBUG",
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state.get("ui_language", "en"), {})
    return loc.get("Translation", {}).get(key, key)


def _render_production_step_header(active_step=1):
    steps = [
        (
            tr("Production Step Setup"),
            tr("Production Step Setup Description"),
        ),
        (
            tr("Production Step Quality Check"),
            tr("Production Step Quality Check Description"),
        ),
        (
            tr("Production Step Preview"),
            tr("Production Step Preview Description"),
        ),
    ]
    with st.container(border=True):
        st.write(tr("Production Hub"))
        st.caption(tr("Production Hub Description"))
        st.caption(tr("Production Step Flow Help"))
        step_cols = st.columns(3)
        for index, (label, description) in enumerate(steps, start=1):
            with step_cols[index - 1]:
                step_label = f"{index}. {label}"
                if index < active_step:
                    st.success(f"{step_label} - {tr('Ready')}")
                elif index == active_step:
                    st.success(step_label)
                else:
                    st.info(step_label)
                st.caption(description)


def _current_production_step():
    if (
        st.session_state.get("social_metadata")
        or st.session_state.get("upload_post_enabled")
        or st.session_state.get("batch_subjects")
        or st.session_state.get("batch_script_blocks")
    ):
        return 3
    if (
        st.session_state.get("content_preflight_report")
        or st.session_state.get("viral_analysis")
        or st.session_state.get("script_rewrite_suggestion")
    ):
        return 2
    return 1


def _social_platform_options():
    return [
        ("TikTok", "tiktok"),
        ("YouTube Shorts", "youtube_shorts"),
        ("Instagram Reels", "instagram_reels"),
    ]


def _current_social_platform():
    social_platforms = _social_platform_options()
    try:
        selected_index = int(st.session_state.get("social_platform_select", 0))
    except (TypeError, ValueError):
        selected_index = 0
    if selected_index < 0 or selected_index >= len(social_platforms):
        selected_index = 0
    return social_platforms[selected_index][1]


def _workspace_tab_specs():
    return [
        ("Workspace Tab Setup", "Workspace Tab Setup Help"),
        ("Workspace Tab Planning Quality", "Workspace Tab Planning Quality Help"),
        ("Workspace Tab Distribution", "Workspace Tab Distribution Help"),
        ("Workspace Tab Batch History", "Workspace Tab Batch History Help"),
    ]


def _render_workspace_tab_intro(title_key, help_key):
    with st.container(border=True):
        st.write(tr(title_key))
        st.caption(tr(help_key))


def _render_panel_intro(title_key, help_key):
    st.write(tr(title_key))
    st.caption(tr(help_key))


def _render_workspace_summary_metrics(items):
    if not items:
        return
    with st.container(border=True):
        cols = st.columns(len(items))
        for col, item in zip(cols, items):
            col.metric(tr(item["label"]), item["value"])
            caption = item.get("caption")
            if caption:
                col.caption(caption)


def _render_readiness_review(title_key, help_key, readiness):
    if not isinstance(readiness, dict):
        return

    metrics = readiness.get("metrics") or []
    with st.container(border=True):
        st.write(tr(title_key))
        st.caption(tr(help_key))
        for offset in range(0, len(metrics), 4):
            metric_cols = st.columns(len(metrics[offset : offset + 4]))
            for col, item in zip(metric_cols, metrics[offset : offset + 4]):
                col.metric(item["label"], item["value"])

        message = (
            f"{readiness.get('message', '')} "
            f"{tr('Next Action')}: {readiness.get('action') or tr('Optional')}"
        ).strip()
        level = readiness.get("level")
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        else:
            st.info(message)


def _render_video_script_settings_panel(params):
    st.write(tr("Video Script Settings"))
    params.video_subject = st.text_input(
        tr("Video Subject"),
        key="video_subject",
    ).strip()

    video_languages = [
        (tr("Auto Detect"), ""),
    ]
    for code in support_locales:
        video_languages.append((code, code))

    selected_index = st.selectbox(
        tr("Script Language"),
        index=0,
        options=range(len(video_languages)),
        format_func=lambda x: video_languages[x][0],
    )
    params.video_language = video_languages[selected_index][1]

    with st.expander(tr("Advanced Script Settings"), expanded=False):
        params.paragraph_number = st.slider(
            tr("Script Paragraph Number"),
            min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            value=st.session_state.get("paragraph_number_input", 1),
            key="paragraph_number_input",
        )
        params.video_script_prompt = st.text_area(
            tr("Custom Script Requirements"),
            height=100,
            max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
            placeholder=tr("Custom Script Requirements Placeholder"),
            key="video_script_prompt",
        ).strip()

        use_custom_system_prompt = st.checkbox(
            tr("Use Custom System Prompt"),
            help=tr("Use Custom System Prompt Help"),
            key="use_custom_system_prompt",
        )

        if use_custom_system_prompt:
            custom_system_prompt = st.text_area(
                tr("Custom System Prompt"),
                height=240,
                max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                key="custom_system_prompt",
            ).strip()
            params.custom_system_prompt = custom_system_prompt
        else:
            params.custom_system_prompt = ""

    if st.button(
        tr("Generate Video Script and Keywords"), key="auto_generate_script"
    ):
        with st.spinner(tr("Generating Video Script and Keywords")):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
            )
            terms = _generate_video_terms_for_ui(params, script)
            if "Error: " in script:
                st.error(tr(script))
            elif "Error: " in terms:
                st.error(tr(terms))
            else:
                st.session_state["video_script"] = script
                st.session_state["video_terms"] = ", ".join(terms)

    params.video_script = st.text_area(
        tr("Video Script"), height=320, key="video_script"
    ).strip()
    _render_script_stats(params.video_script)
    if st.button(tr("Generate Video Keywords"), key="auto_generate_terms"):
        if not params.video_script:
            st.error(tr("Please Enter the Video Subject"))
            st.stop()

        with st.spinner(tr("Generating Video Keywords")):
            terms = _generate_video_terms_for_ui(params, params.video_script)
            if "Error: " in terms:
                st.error(tr(terms))
            else:
                st.session_state["video_terms"] = ", ".join(terms)

    params.video_terms = st.text_area(
        tr("Video Keywords"), key="video_terms"
    ).strip()


def _subtitle_color_contrast_ratio(foreground, background):
    def relative_luminance(color):
        if not isinstance(color, str):
            return None
        color = color.strip().lstrip("#")
        if len(color) != 6:
            return None
        try:
            channels = [
                int(color[index : index + 2], 16) / 255
                for index in range(0, 6, 2)
            ]
        except ValueError:
            return None
        linear_channels = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return (
            0.2126 * linear_channels[0]
            + 0.7152 * linear_channels[1]
            + 0.0722 * linear_channels[2]
        )

    foreground_luminance = relative_luminance(foreground)
    background_luminance = relative_luminance(background)
    if foreground_luminance is None or background_luminance is None:
        return None
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _subtitle_layout_preflight(
    output_aspects,
    *,
    subtitle_enabled,
    subtitle_position,
    custom_position=None,
):
    """Summarize subtitle placement before rendering without changing it."""
    report = {"placements": [], "needs_review": False}
    if not subtitle_enabled:
        return report

    if isinstance(output_aspects, (str, bytes)):
        output_aspects = [output_aspects]
    try:
        raw_aspects = list(output_aspects or [])
    except TypeError:
        return report

    position = str(subtitle_position or "").strip().lower()
    normalized_custom_position = None
    if position == "custom":
        try:
            normalized_custom_position = float(custom_position)
        except (TypeError, ValueError):
            report["needs_review"] = True
        else:
            report["needs_review"] = (
                normalized_custom_position < 10 or normalized_custom_position > 90
            )

    seen_aspects = set()
    for raw_aspect in raw_aspects:
        try:
            aspect = VideoAspect(getattr(raw_aspect, "value", raw_aspect))
        except (TypeError, ValueError):
            continue
        if aspect.value in seen_aspects:
            continue
        seen_aspects.add(aspect.value)

        if position == "bottom":
            try:
                margin = float(video_service.get_subtitle_bottom_safe_margin_ratio(aspect))
            except (TypeError, ValueError):
                margin = 0.05
            placement = f"{aspect.value}: {tr('Bottom')} ({margin * 100:g}%)"
        elif position == "top":
            placement = f"{aspect.value}: {tr('Top')} (5%)"
        elif position == "custom":
            if normalized_custom_position is None:
                placement = f"{aspect.value}: {tr('Custom')}"
            else:
                placement = (
                    f"{aspect.value}: {tr('Custom')} "
                    f"({normalized_custom_position:g}%)"
                )
        else:
            placement = f"{aspect.value}: {tr('Center')}"
        report["placements"].append(placement)

    return report


def _render_subtitle_settings_panel(params):
    st.write(tr("Subtitle Settings"))
    params.subtitle_enabled = st.checkbox(
        tr("Enable Subtitles"),
        value=config.ui.get("subtitle_enabled", True),
    )
    config.ui["subtitle_enabled"] = params.subtitle_enabled
    font_names = get_all_fonts()
    saved_font_name = config.ui.get("font_name", "MicrosoftYaHeiBold.ttc")
    saved_font_name_index = 0
    if saved_font_name in font_names:
        saved_font_name_index = font_names.index(saved_font_name)
    params.font_name = st.selectbox(
        tr("Font"), font_names, index=saved_font_name_index
    )
    config.ui["font_name"] = params.font_name

    subtitle_styles = [
        (tr("Classic"), "classic"),
        (tr("Karaoke Word Highlight"), "karaoke"),
    ]
    saved_subtitle_style = config.ui.get("subtitle_style", "classic")
    saved_style_index = 0
    for i, (_, style_value) in enumerate(subtitle_styles):
        if style_value == saved_subtitle_style:
            saved_style_index = i
            break
    selected_style_index = st.selectbox(
        tr("Subtitle Style"),
        index=saved_style_index,
        options=range(len(subtitle_styles)),
        format_func=lambda x: subtitle_styles[x][0],
        help=tr("Karaoke Word Highlight Help"),
    )
    params.subtitle_style = subtitle_styles[selected_style_index][1]
    config.ui["subtitle_style"] = params.subtitle_style

    subtitle_positions = [
        (tr("Top"), "top"),
        (tr("Center"), "center"),
        (tr("Bottom"), "bottom"),
        (tr("Custom"), "custom"),
    ]
    saved_subtitle_position = config.ui.get("subtitle_position", "bottom")
    saved_position_index = 2
    for i, (_, pos_value) in enumerate(subtitle_positions):
        if pos_value == saved_subtitle_position:
            saved_position_index = i
            break
    selected_index = st.selectbox(
        tr("Position"),
        index=saved_position_index,
        options=range(len(subtitle_positions)),
        format_func=lambda x: subtitle_positions[x][0],
    )
    params.subtitle_position = subtitle_positions[selected_index][1]
    config.ui["subtitle_position"] = params.subtitle_position

    if params.subtitle_position == "custom":
        saved_custom_position = config.ui.get("custom_position", 70.0)
        custom_position = st.text_input(
            tr("Custom Position (% from top)"),
            value=str(saved_custom_position),
            key="custom_position_input",
        )
        try:
            params.custom_position = float(custom_position)
            if params.custom_position < 0 or params.custom_position > 100:
                st.error(tr("Please enter a value between 0 and 100"))
            else:
                config.ui["custom_position"] = params.custom_position
        except ValueError:
            st.error(tr("Please enter a valid number"))

    font_cols = st.columns([0.3, 0.7])
    with font_cols[0]:
        saved_text_fore_color = config.ui.get("text_fore_color", "#FFFFFF")
        params.text_fore_color = st.color_picker(
            tr("Font Color"), saved_text_fore_color
        )
        config.ui["text_fore_color"] = params.text_fore_color

    with font_cols[1]:
        saved_font_size = config.ui.get("font_size", 60)
        params.font_size = st.slider(tr("Font Size"), 30, 100, saved_font_size)
        config.ui["font_size"] = params.font_size

    stroke_cols = st.columns([0.3, 0.7])
    with stroke_cols[0]:
        params.stroke_color = st.color_picker(
            tr("Stroke Color"),
            config.ui.get("stroke_color", "#000000"),
        )
        config.ui["stroke_color"] = params.stroke_color
    with stroke_cols[1]:
        params.stroke_width = st.slider(
            tr("Stroke Width"),
            0.0,
            10.0,
            float(config.ui.get("stroke_width", 1.5)),
        )
        config.ui["stroke_width"] = params.stroke_width

    subtitle_bg_cols = st.columns([0.4, 0.6])
    saved_subtitle_background_enabled = config.ui.get(
        "subtitle_background_enabled", False
    )
    with subtitle_bg_cols[0]:
        subtitle_background_enabled = st.checkbox(
            tr("Enable Subtitle Background"),
            value=saved_subtitle_background_enabled,
        )
    config.ui["subtitle_background_enabled"] = subtitle_background_enabled
    if subtitle_background_enabled:
        with subtitle_bg_cols[1]:
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color", "#000000"
            )
            params.text_background_color = st.color_picker(
                tr("Subtitle Background Color"),
                saved_subtitle_background_color,
            )
            config.ui["subtitle_background_color"] = params.text_background_color
    else:
        params.text_background_color = False

    contrast_ratio = (
        _subtitle_color_contrast_ratio(
            params.text_fore_color,
            params.text_background_color,
        )
        if subtitle_background_enabled
        else None
    )
    if contrast_ratio is not None and contrast_ratio < 4.5:
        st.warning(
            tr("Subtitle Contrast Warning").format(ratio=f"{contrast_ratio:.1f}")
        )

    layout_preflight = _subtitle_layout_preflight(
        params.video_aspects or [params.video_aspect],
        subtitle_enabled=params.subtitle_enabled,
        subtitle_position=params.subtitle_position,
        custom_position=getattr(params, "custom_position", None),
    )
    if layout_preflight["placements"]:
        st.caption(
            f"{tr('Subtitle Layout Preflight')}: "
            + " | ".join(layout_preflight["placements"])
        )
    if layout_preflight["needs_review"]:
        st.warning(tr("Subtitle Custom Position Warning"))

    saved_rounded_subtitle_background = config.ui.get(
        "rounded_subtitle_background", False
    )
    params.rounded_subtitle_background = st.checkbox(
        tr("Rounded Subtitle Background"),
        value=(
            saved_rounded_subtitle_background
            if subtitle_background_enabled
            else False
        ),
        help=tr("Rounded Subtitle Background Help"),
        disabled=not subtitle_background_enabled,
    )
    if subtitle_background_enabled:
        config.ui["rounded_subtitle_background"] = (
            params.rounded_subtitle_background
        )


def _api_key_list(value):
    """Return API keys in the mutable list form used by the management panel."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _api_key_slot_labels(api_keys):
    api_keys = _api_key_list(api_keys)
    return [
        tr("Saved API Key").format(index=index)
        for index in range(1, len(api_keys) + 1)
    ]


def _remove_api_key_at_index(api_keys, selected_index):
    if (
        not isinstance(api_keys, list)
        or isinstance(selected_index, bool)
        or not isinstance(selected_index, int)
        or selected_index < 0
        or selected_index >= len(api_keys)
    ):
        return False
    api_keys.pop(selected_index)
    return True


def _render_video_source_api_key_management_panel(key_prefix: str):
    st.subheader(tr("Manage Video Source API Keys"))

    for config_key in (
        "pexels_api_keys",
        "pixabay_api_keys",
        "coverr_api_keys",
        "vecteezy_api_keys",
    ):
        config.app[config_key] = _api_key_list(config.app.get(config_key))

    col1, col2, col3, col4 = st.tabs([
        tr("Pexels API Keys"),
        tr("Pixabay API Keys"),
        tr("Coverr API Keys"),
        tr("Vecteezy API Keys"),
    ])

    with col1:
        st.subheader(tr("Pexels API Keys"))
        pexels_key_labels = _api_key_slot_labels(config.app["pexels_api_keys"])
        if pexels_key_labels:
            st.caption(
                tr("Configured API Key Count").format(count=len(pexels_key_labels))
            )
        else:
            st.info(tr("No Pexels API Keys currently"))

        new_key = st.text_input(
            tr("Add Pexels API Key"),
            key=f"{key_prefix}_pexels_new_key",
            type="password",
        )
        if st.button(
            tr("Add Pexels API Key"),
            key=f"{key_prefix}_pexels_add_key",
        ):
            if new_key and new_key not in config.app["pexels_api_keys"]:
                config.app["pexels_api_keys"].append(new_key)
                config.save_config()
                st.success(tr("Pexels API Key added successfully"))
            elif new_key in config.app["pexels_api_keys"]:
                st.warning(tr("This API Key already exists"))
            else:
                st.error(tr("Please enter a valid API Key"))

        if pexels_key_labels:
            delete_key_index = st.selectbox(
                tr("Select Pexels API Key to delete"),
                options=list(range(len(pexels_key_labels))),
                format_func=lambda index: pexels_key_labels[index],
                key=f"{key_prefix}_pexels_delete_key",
            )
            if st.button(
                tr("Delete Selected Pexels API Key"),
                key=f"{key_prefix}_pexels_delete_button",
            ):
                if _remove_api_key_at_index(
                    config.app["pexels_api_keys"], delete_key_index
                ):
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

    with col2:
        st.subheader(tr("Pixabay API Keys"))

        pixabay_key_labels = _api_key_slot_labels(config.app["pixabay_api_keys"])
        if pixabay_key_labels:
            st.caption(
                tr("Configured API Key Count").format(count=len(pixabay_key_labels))
            )
        else:
            st.info(tr("No Pixabay API Keys currently"))

        new_key = st.text_input(
            tr("Add Pixabay API Key"),
            key=f"{key_prefix}_pixabay_new_key",
            type="password",
        )
        if st.button(
            tr("Add Pixabay API Key"),
            key=f"{key_prefix}_pixabay_add_key",
        ):
            if new_key and new_key not in config.app["pixabay_api_keys"]:
                config.app["pixabay_api_keys"].append(new_key)
                config.save_config()
                st.success(tr("Pixabay API Key added successfully"))
            elif new_key in config.app["pixabay_api_keys"]:
                st.warning(tr("This API Key already exists"))
            else:
                st.error(tr("Please enter a valid API Key"))

        if pixabay_key_labels:
            delete_key_index = st.selectbox(
                tr("Select Pixabay API Key to delete"),
                options=list(range(len(pixabay_key_labels))),
                format_func=lambda index: pixabay_key_labels[index],
                key=f"{key_prefix}_pixabay_delete_key",
            )
            if st.button(
                tr("Delete Selected Pixabay API Key"),
                key=f"{key_prefix}_pixabay_delete_button",
            ):
                if _remove_api_key_at_index(
                    config.app["pixabay_api_keys"], delete_key_index
                ):
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

    with col3:
        st.subheader(tr("Coverr API Keys"))

        coverr_key_labels = _api_key_slot_labels(config.app["coverr_api_keys"])
        if coverr_key_labels:
            st.caption(
                tr("Configured API Key Count").format(count=len(coverr_key_labels))
            )
        else:
            st.info(tr("No Coverr API Keys currently"))

        new_key = st.text_input(
            tr("Add Coverr API Key"),
            key=f"{key_prefix}_coverr_new_key",
            type="password",
        )
        if st.button(
            tr("Add Coverr API Key"),
            key=f"{key_prefix}_coverr_add_key",
        ):
            if new_key and new_key not in config.app["coverr_api_keys"]:
                config.app["coverr_api_keys"].append(new_key)
                config.save_config()
                st.success(tr("Coverr API Key added successfully"))
            elif new_key in config.app["coverr_api_keys"]:
                st.warning(tr("This API Key already exists"))
            else:
                st.error(tr("Please enter a valid API Key"))

        if coverr_key_labels:
            delete_key_index = st.selectbox(
                tr("Select Coverr API Key to delete"),
                options=list(range(len(coverr_key_labels))),
                format_func=lambda index: coverr_key_labels[index],
                key=f"{key_prefix}_coverr_delete_key",
            )
            if st.button(
                tr("Delete Selected Coverr API Key"),
                key=f"{key_prefix}_coverr_delete_button",
            ):
                if _remove_api_key_at_index(
                    config.app["coverr_api_keys"], delete_key_index
                ):
                    config.save_config()
                    st.success(tr("Coverr API Key deleted successfully"))

    with col4:
        st.subheader(tr("Vecteezy API Keys"))
        vecteezy_key_labels = _api_key_slot_labels(config.app["vecteezy_api_keys"])
        if vecteezy_key_labels:
            st.caption(
                tr("Configured API Key Count").format(count=len(vecteezy_key_labels))
            )
        else:
            st.info(tr("No Vecteezy API Keys currently"))

        new_key = st.text_input(
            tr("Add Vecteezy API Key"),
            key=f"{key_prefix}_vecteezy_new_key",
            type="password",
        )
        if st.button(
            tr("Add Vecteezy API Key"),
            key=f"{key_prefix}_vecteezy_add_key",
        ):
            if new_key and new_key not in config.app["vecteezy_api_keys"]:
                config.app["vecteezy_api_keys"].append(new_key)
                config.save_config()
                st.success(tr("Vecteezy API Key added successfully"))
            elif new_key in config.app["vecteezy_api_keys"]:
                st.warning(tr("This API Key already exists"))
            else:
                st.error(tr("Please enter a valid API Key"))

        if vecteezy_key_labels:
            delete_key_index = st.selectbox(
                tr("Select Vecteezy API Key to delete"),
                options=list(range(len(vecteezy_key_labels))),
                format_func=lambda index: vecteezy_key_labels[index],
                key=f"{key_prefix}_vecteezy_delete_key",
            )
            if st.button(
                tr("Delete Selected Vecteezy API Key"),
                key=f"{key_prefix}_vecteezy_delete_button",
            ):
                if _remove_api_key_at_index(
                    config.app["vecteezy_api_keys"], delete_key_index
                ):
                    config.save_config()
                    st.success(tr("Vecteezy API Key deleted successfully"))

        account_id = st.text_input(
            tr("Vecteezy Account ID"),
            value=str(config.app.get("vecteezy_account_id") or ""),
            key=f"{key_prefix}_vecteezy_account_id",
            help=tr("Vecteezy Account ID Help"),
        ).strip()
        if st.button(
            tr("Save Vecteezy Account ID"),
            key=f"{key_prefix}_vecteezy_save_account_id",
        ):
            if account_id.isdigit():
                config.app["vecteezy_account_id"] = account_id
                config.save_config()
                st.success(tr("Vecteezy Account ID saved"))
            else:
                st.error(tr("Please enter a valid Vecteezy Account ID"))

def _render_basic_video_source_settings_panel():
    def get_keys_from_config(cfg_key):
        api_keys = config.app.get(cfg_key, [])
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        api_key = ", ".join(api_keys)
        return api_key

    def save_keys_to_config(cfg_key, value):
        value = value.replace(" ", "")
        if value:
            config.app[cfg_key] = value.split(",")

    st.write(tr("Video Source Settings"))

    pexels_api_key = get_keys_from_config("pexels_api_keys")
    pexels_api_key = st.text_input(
        tr("Pexels API Key"), value=pexels_api_key, type="password"
    )
    save_keys_to_config("pexels_api_keys", pexels_api_key)

    pixabay_api_key = get_keys_from_config("pixabay_api_keys")
    pixabay_api_key = st.text_input(
        tr("Pixabay API Key"), value=pixabay_api_key, type="password"
    )
    save_keys_to_config("pixabay_api_keys", pixabay_api_key)

    coverr_api_key = get_keys_from_config("coverr_api_keys")
    coverr_api_key = st.text_input(
        tr("Coverr API Key"), value=coverr_api_key, type="password"
    )
    save_keys_to_config("coverr_api_keys", coverr_api_key)

    dvids_api_key = get_keys_from_config("dvids_api_keys")
    dvids_api_key = st.text_input(
        tr("DVIDS API Key"),
        value=dvids_api_key,
        type="password",
        help=tr("DVIDS API Key Help"),
    )
    save_keys_to_config("dvids_api_keys", dvids_api_key)

    vecteezy_api_key = get_keys_from_config("vecteezy_api_keys")
    vecteezy_api_key = st.text_input(
        tr("Vecteezy API Key"), value=vecteezy_api_key, type="password"
    )
    save_keys_to_config("vecteezy_api_keys", vecteezy_api_key)
    config.app["vecteezy_account_id"] = st.text_input(
        tr("Vecteezy Account ID"),
        value=str(config.app.get("vecteezy_account_id") or ""),
        help=tr("Vecteezy Account ID Help"),
    ).strip()

    smithsonian_api_key = get_keys_from_config("smithsonian_api_keys")
    smithsonian_api_key = st.text_input(
        tr("Smithsonian Open Access API Key"),
        value=smithsonian_api_key,
        type="password",
        help=tr("Smithsonian Open Access API Key Help"),
    )
    save_keys_to_config("smithsonian_api_keys", smithsonian_api_key)

    europeana_api_key = get_keys_from_config("europeana_api_keys")
    europeana_api_key = st.text_input(
        tr("Europeana API Key"),
        value=europeana_api_key,
        type="password",
        help=tr("Europeana API Key Help"),
    )
    save_keys_to_config("europeana_api_keys", europeana_api_key)

    st.checkbox(
        tr("Prioritize Cleaner Preview Frames"),
        help=tr("Prioritize Cleaner Preview Frames Help"),
        key="preview_quality_rerank_enabled",
    )
    config.app["preview_quality_rerank_enabled"] = _config_bool(
        st.session_state["preview_quality_rerank_enabled"]
    )


def _output_aspect_quality_items(output_aspects):
    """Describe the actual output coverage without adding a separate panel."""
    formatted_aspects = []
    seen_aspects = set()
    for raw_aspect in output_aspects or []:
        try:
            aspect = VideoAspect(getattr(raw_aspect, "value", raw_aspect))
        except (TypeError, ValueError):
            continue
        if aspect.value in seen_aspects:
            continue
        seen_aspects.add(aspect.value)
        width, height = aspect.to_resolution()
        formatted_aspects.append(f"{aspect.value} ({width}x{height})")

    if not formatted_aspects:
        return []
    items = [{"label": tr("Video Ratio"), "value": formatted_aspects[0]}]
    if len(formatted_aspects) > 1:
        items.append(
            {
                "label": tr("Additional Output Formats"),
                "value": ", ".join(formatted_aspects[1:]),
            }
        )
    return items


def _video_quality_impact_items(
    crf,
    fps,
    audio_bitrate_kbps,
    encoder_preset,
    codec="libx264",
    output_aspects=None,
):
    crf = _normalize_video_crf_value(crf)
    fps = _normalize_video_fps_value(fps)
    audio_bitrate_kbps = _normalize_audio_bitrate_kbps(audio_bitrate_kbps)
    encoder_preset = _normalize_libx264_preset(encoder_preset)
    codec_name = str(codec or "").strip().lower()
    is_hardware_codec = codec_name not in {"", "libx264"}

    if codec_name == "h264_amf":
        quality = tr("Quality Impact Hardware Controlled")
    elif is_hardware_codec:
        quality = tr("Quality Impact Hardware Default")
    else:
        quality = (
            tr("Quality Impact High")
            if crf <= 18
            else tr("Quality Impact Balanced")
            if crf <= 23
            else tr("Quality Impact Draft")
        )
    speed = (
        tr("Render Speed Hardware")
        if is_hardware_codec
        else tr("Render Speed Fast")
        if encoder_preset in {"ultrafast", "superfast", "veryfast", "faster"}
        else tr("Render Speed Slow")
        if encoder_preset in {"slow", "slower", "veryslow"}
        else tr("Render Speed Balanced")
    )
    motion = tr("Motion Smooth") if fps >= 50 else tr("Motion Standard")
    file_size = (
        tr("File Size Larger")
        if crf <= 18 or fps >= 50 or audio_bitrate_kbps >= 256
        else tr("File Size Smaller")
        if crf >= 26 and fps <= 24 and audio_bitrate_kbps <= 128
        else tr("File Size Balanced")
    )
    audio = (
        tr("Audio Quality High")
        if audio_bitrate_kbps >= 256
        else tr("Audio Quality Draft")
        if audio_bitrate_kbps <= 128
        else tr("Audio Quality Balanced")
    )
    items = [
        {"label": tr("Visual Quality"), "value": quality},
        {"label": tr("Render Speed"), "value": speed},
        {"label": tr("Motion"), "value": motion},
        {"label": tr("File Size"), "value": file_size},
        {"label": tr("Audio Quality"), "value": audio},
    ]
    return items + _output_aspect_quality_items(output_aspects)


def _render_video_quality_impact_summary(output_aspects=None):
    with st.container(border=True):
        st.write(tr("Video Quality Impact"))
        st.caption(tr("Video Quality Impact Help"))
        _render_workspace_summary_metrics(
            _video_quality_impact_items(
                st.session_state.get("video_crf", 20),
                st.session_state.get("video_fps", 30),
                st.session_state.get("audio_bitrate_kbps", 192),
                st.session_state.get("video_encoder_preset", "medium"),
                config.app.get("video_codec", "libx264"),
                output_aspects=output_aspects,
            )
        )


def _render_advanced_video_settings_panel(params):
    params.match_materials_to_script = st.checkbox(
        tr("Match Materials to Script Order"),
        help=tr("Match Materials to Script Order Help"),
        key="match_materials_to_script",
    )
    config.app["match_materials_to_script"] = params.match_materials_to_script

    params.smart_scene_queries = st.checkbox(
        tr("Smart B-Roll Queries"),
        help=tr("Smart B-Roll Queries Help"),
        key="smart_scene_queries",
    )
    config.app["smart_scene_queries"] = params.smart_scene_queries

    st.slider(
        tr("Video Diversity Level"),
        min_value=1,
        max_value=50,
        step=1,
        help=tr("Video Diversity Level Help"),
        key="material_search_max_page",
    )
    config.app["material_search_max_page"] = st.session_state[
        "material_search_max_page"
    ]

    st.checkbox(
        tr("Avoid Recently Used Videos"),
        help=tr("Avoid Recently Used Videos Help"),
        key="video_cooldown_enabled",
    )
    config.app["video_cooldown_enabled"] = st.session_state[
        "video_cooldown_enabled"
    ]

    cooldown_day_options = [3, 7, 14, 30]
    current_cooldown_days = st.session_state.get("video_cooldown_days", 7)
    if current_cooldown_days not in cooldown_day_options:
        current_cooldown_days = 7
        st.session_state["video_cooldown_days"] = current_cooldown_days
    cooldown_day_labels = {
        days: tr("Cooldown Days Label").format(days=days)
        for days in cooldown_day_options
    }
    st.selectbox(
        tr("Recently Used Video Window"),
        options=cooldown_day_options,
        format_func=lambda days: cooldown_day_labels[days],
        help=tr("Recently Used Video Window Help"),
        key="video_cooldown_days",
        disabled=not st.session_state["video_cooldown_enabled"],
    )
    config.app["video_cooldown_days"] = st.session_state[
        "video_cooldown_days"
    ]

    video_codec_options = _video_codec_options()
    saved_video_codec = _normalize_video_codec(
        config.app.get("video_codec", "libx264")
    )
    saved_video_codec_values = [item[1] for item in video_codec_options]
    selected_codec_index = saved_video_codec_values.index(saved_video_codec)
    selected_codec_index = st.selectbox(
        tr("Video Encoder"),
        options=range(len(video_codec_options)),
        index=selected_codec_index,
        format_func=lambda x: video_codec_options[x][0],
        help=tr("Video Encoder Help"),
    )
    config.app["video_codec"] = video_codec_options[selected_codec_index][1]

    if st.button(tr("Check Video Encoder"), key="check_video_encoder"):
        with st.spinner(tr("Checking Video Encoder...")):
            for video_aspect in params.video_aspects or [params.video_aspect]:
                aspect_label = getattr(video_aspect, "value", str(video_aspect))
                try:
                    encoder_check = video_service.check_video_encoder(video_aspect)
                except Exception:
                    st.error(f"[{aspect_label}] {tr('Video Encoder Check Failed')}")
                    continue

                if encoder_check["fallback_used"]:
                    st.warning(
                        f"[{aspect_label}] "
                        + tr("Video Encoder Check Fallback").format(
                            configured_codec=encoder_check["configured_codec"],
                            used_codec=encoder_check["used_codec"],
                        )
                    )
                else:
                    st.success(
                        f"[{aspect_label}] "
                        + tr("Video Encoder Check Passed").format(
                            codec=encoder_check["used_codec"]
                        )
                    )

    st.session_state["video_crf"] = _normalize_video_crf_value(
        st.session_state.get("video_crf", config.app.get("video_crf", 20))
    )
    st.session_state["video_encoder_preset"] = _normalize_libx264_preset(
        st.session_state.get(
            "video_encoder_preset",
            config.app.get("video_encoder_preset", "medium"),
        )
    )
    st.session_state["video_fps"] = _normalize_video_fps_value(
        st.session_state.get("video_fps", config.app.get("video_fps", 30))
    )
    st.session_state["audio_bitrate_kbps"] = _normalize_audio_bitrate_kbps(
        st.session_state.get(
            "audio_bitrate_kbps",
            config.app.get("audio_bitrate", "192k"),
        )
    )

    quality_preset_options = [
        (tr("Balanced Quality Preset"), "balanced"),
        (tr("Fast Draft Preset"), "draft"),
        (tr("High Quality Preset"), "high"),
        (tr("Archive Quality Preset"), "archive"),
        (tr("Custom Quality Preset"), "custom"),
    ]
    quality_preset_values = {
        "draft": {
            "video_crf": 28,
            "video_encoder_preset": "veryfast",
            "video_fps": 24,
            "audio_bitrate_kbps": 128,
        },
        "balanced": {
            "video_crf": 20,
            "video_encoder_preset": "medium",
            "video_fps": 30,
            "audio_bitrate_kbps": 192,
        },
        "high": {
            "video_crf": 18,
            "video_encoder_preset": "slow",
            "video_fps": 30,
            "audio_bitrate_kbps": 256,
        },
        "archive": {
            "video_crf": 16,
            "video_encoder_preset": "slower",
            "video_fps": 30,
            "audio_bitrate_kbps": 320,
        },
    }
    saved_quality_preset = config.ui.get("video_quality_preset", "balanced")
    quality_preset_keys = [item[1] for item in quality_preset_options]
    if saved_quality_preset not in quality_preset_keys:
        saved_quality_preset = "balanced"
    if "_applied_video_quality_preset" not in st.session_state:
        st.session_state["_applied_video_quality_preset"] = saved_quality_preset
    selected_quality_preset_index = st.selectbox(
        tr("Video Quality Preset"),
        options=range(len(quality_preset_options)),
        index=quality_preset_keys.index(saved_quality_preset),
        format_func=lambda x: quality_preset_options[x][0],
        help=tr("Video Quality Preset Help"),
    )
    selected_quality_preset = quality_preset_options[
        selected_quality_preset_index
    ][1]
    config.ui["video_quality_preset"] = selected_quality_preset
    preset_values = quality_preset_values.get(selected_quality_preset)
    _apply_video_quality_preset_once(
        st.session_state,
        selected_quality_preset,
        preset_values,
    )

    quality_cols = st.columns(2)
    with quality_cols[0]:
        st.slider(
            tr("Video Quality CRF"),
            min_value=0,
            max_value=51,
            step=1,
            help=tr("Video Quality CRF Help"),
            key="video_crf",
        )
        config.app["video_crf"] = _normalize_video_crf_value(
            st.session_state["video_crf"]
        )
    with quality_cols[1]:
        preset_options = _libx264_preset_options()
        st.selectbox(
            tr("Encoder Preset"),
            options=preset_options,
            help=tr("Encoder Preset Help"),
            key="video_encoder_preset",
        )
        config.app["video_encoder_preset"] = _normalize_libx264_preset(
            st.session_state["video_encoder_preset"]
        )

    output_cols = st.columns(2)
    with output_cols[0]:
        st.number_input(
            tr("Output FPS"),
            min_value=1,
            max_value=120,
            step=1,
            help=tr("Output FPS Help"),
            key="video_fps",
        )
        config.app["video_fps"] = _normalize_video_fps_value(
            st.session_state["video_fps"]
        )
    with output_cols[1]:
        st.number_input(
            tr("Audio Bitrate"),
            min_value=32,
            max_value=512,
            step=16,
            help=tr("Audio Bitrate Help"),
            key="audio_bitrate_kbps",
        )
        audio_bitrate_kbps = _normalize_audio_bitrate_kbps(
            st.session_state["audio_bitrate_kbps"]
        )
        config.app["audio_bitrate"] = f"{audio_bitrate_kbps}k"
    st.checkbox(
        tr("Reduce Color Banding"),
        help=tr("Reduce Color Banding Help"),
        key="video_deband_enabled",
    )
    config.app["video_deband_enabled"] = _config_bool(
        st.session_state["video_deband_enabled"]
    )
    _apply_video_quality_params(params)
    _render_video_quality_impact_summary(
        params.video_aspects or [params.video_aspect]
    )


def _render_video_settings_panel(params):
    st.write(tr("Video Settings"))
    uploaded_files = []
    video_concat_modes = [
        (tr("Sequential"), "sequential"),
        (tr("Random"), "random"),
    ]
    saved_video_concat_mode = config.ui.get(
        "video_concat_mode", VideoConcatMode.random.value
    )

    api_sources = {
        "pexels": "Pexels",
        "pixabay": "Pixabay",
        "coverr": "Coverr",
        "dvids": "DVIDS Public Media",
        "vecteezy": tr("Vecteezy (free plan, attribution required)"),
        "nasa": "NASA Image Library",
        "noaa_ocean": tr("NOAA Ocean Exploration (public domain)"),
        "loc": tr("Library of Congress (public domain)"),
        "wikimedia": "Wikimedia Commons",
        "archive_org": "Internet Archive",
    }
    all_sources = {**api_sources, "local": tr("Local file")}

    saved_sources = config.app.get(
        "enabled_video_sources", ["pexels", "pixabay", "coverr"]
    )
    if isinstance(saved_sources, str):
        saved_sources = [saved_sources]
    saved_sources = [source for source in saved_sources if source in all_sources]
    if not saved_sources:
        saved_sources = ["pexels", "pixabay", "coverr"]

    enabled_video_sources = st.multiselect(
        tr("Video Sources"),
        options=list(all_sources.keys()),
        default=saved_sources,
        format_func=lambda x: all_sources[x],
        help=tr("Video Sources Help"),
    )
    if not enabled_video_sources:
        enabled_video_sources = ["pexels"]
        st.caption(tr("Video Sources Minimum"))

    config.app["enabled_video_sources"] = enabled_video_sources

    api_selection = [source for source in enabled_video_sources if source in api_sources]
    if len(api_selection) > 1:
        params.video_source = "multi"
    elif len(api_selection) == 1:
        params.video_source = api_selection[0]
    elif "local" in enabled_video_sources:
        params.video_source = "local"
    else:
        params.video_source = "pexels"
    config.app["video_source"] = params.video_source

    if "local" in enabled_video_sources:
        local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
        uploaded_files = st.file_uploader(
            tr("Upload Local Files"),
            type=local_file_types + [file_type.upper() for file_type in local_file_types],
            accept_multiple_files=True,
        )
        with st.expander(tr("Local Material Library"), expanded=False):
            st.caption(tr("Local Material Library Help"))
            if params.video_source != "local":
                st.info(tr("Local Material Library Local Source Only"))
            else:
                check_health = st.button(
                    tr("Check Local Material Files"),
                    key="check_local_material_library",
                )
                local_materials = local_material_catalog.list_local_materials(
                    check_health=check_health
                )
                if not local_materials:
                    st.caption(tr("No Saved Local Materials"))
                else:
                    materials_by_name = {
                        item["name"]: item for item in local_materials
                    }
                    recommended_names = {
                        item["name"]
                        for item in local_material_catalog.recommend_local_materials(
                            params.video_subject
                        )
                    }

                    def format_local_material(name):
                        item = materials_by_name[name]
                        labels = [name]
                        if name in recommended_names:
                            labels.append(tr("Recommended"))
                        if item["health"] is True:
                            labels.append(tr("Local Material Healthy"))
                        elif item["health"] is False:
                            labels.append(tr("Local Material Unreadable"))
                        if item["source_label"]:
                            labels.append(tr(item["source_label"]))
                        return " · ".join(labels)

                    material_names = list(materials_by_name)
                    selected_material_names = st.multiselect(
                        tr("Select Local Materials"),
                        options=material_names,
                        default=[],
                        format_func=format_local_material,
                        key="local_material_library_selection",
                    )
                    if st.button(
                        tr("Use Selected Local Materials"),
                        key="use_selected_local_materials",
                    ):
                        if not selected_material_names:
                            st.warning(tr("Please Select At Least One Local Material"))
                        else:
                            st.session_state["local_video_materials"] = [
                                {
                                    "provider": "local",
                                    "url": materials_by_name[name]["path"],
                                    "duration": 0,
                                    "title": name,
                                    "license": materials_by_name[name]["license"],
                                    "attribution": materials_by_name[name][
                                        "attribution"
                                    ],
                                }
                                for name in selected_material_names
                            ]
                            st.success(tr("Selected Local Materials Ready"))

                    tag_target = st.selectbox(
                        tr("Local file"),
                        options=material_names,
                        format_func=format_local_material,
                        key="local_material_tag_target",
                    )
                    tag_input = st.text_input(
                        tr("Local Material Tags"),
                        value=", ".join(materials_by_name[tag_target]["tags"]),
                        key=f"local_material_tags_{tag_target}",
                    )
                    if st.button(
                        tr("Save Local Material Tags"),
                        key="save_local_material_tags",
                    ):
                        local_material_catalog.save_local_material_tags(
                            tag_target, tag_input
                        )
                        st.success(tr("Local Material Tags Saved"))

                    public_domain_sources = {
                        source["id"]: source
                        for source in local_material_catalog.list_public_domain_sources()
                    }
                    source_options = [None, *public_domain_sources]
                    saved_source_id = materials_by_name[tag_target]["source_id"]
                    source_index = (
                        source_options.index(saved_source_id)
                        if saved_source_id in source_options
                        else 0
                    )
                    selected_source_id = st.selectbox(
                        tr("Public-Domain Source"),
                        options=source_options,
                        index=source_index,
                        format_func=lambda source_id: (
                            tr("No Public-Domain Source")
                            if source_id is None
                            else tr(public_domain_sources[source_id]["label"])
                        ),
                        key=f"local_material_source_{tag_target}",
                    )
                    st.caption(tr("Public-Domain Source Help"))
                    if st.button(
                        tr("Save Local Material Source"),
                        key="save_local_material_source",
                    ):
                        local_material_catalog.save_local_material_source(
                            tag_target, selected_source_id
                        )
                        st.success(tr("Local Material Source Saved"))
        if params.video_source == "local" and params.video_subject:
            openmontage_output = find_openmontage_output(
                params.video_subject,
                prefer_silent=True,
                video_aspect=params.video_aspect,
                language=params.video_language,
            )
            if openmontage_output:
                project_name = os.path.basename(os.path.dirname(openmontage_output))
                output_report = validate_openmontage_output(
                    openmontage_output,
                    video_aspect=params.video_aspect,
                )
                st.info(
                    tr("OpenMontage Matching Video Found").format(
                        project_name=project_name
                    )
                )
                bitrate_kbps = output_report.get("bitrate_kbps")
                if output_report.get("valid") and bitrate_kbps:
                    st.caption(
                        tr("OpenMontage Quality Review").format(
                            resolution=output_report.get("resolution") or "?",
                            bitrate_kbps=bitrate_kbps,
                        )
                    )
                if output_report.get("valid") and "low_bitrate_review_recommended" in output_report.get(
                    "quality_warnings", []
                ):
                    st.warning(tr("OpenMontage Quality Review Recommended"))
                if st.button(
                    tr("Use OpenMontage Video"), key="use_openmontage_output"
                ):
                    if not output_report.get("valid"):
                        st.warning(tr("OpenMontage Output Needs Native Render"))
                    else:
                        st.session_state["local_video_materials"] = [
                            {
                                "provider": "local",
                                "url": openmontage_output,
                                "duration": 0,
                            }
                        ]
                        st.success(tr("OpenMontage Video Selected"))

    selected_index = st.selectbox(
        tr("Video Concat Mode"),
        index=_find_option_index(video_concat_modes, saved_video_concat_mode, 1),
        options=range(len(video_concat_modes)),
        format_func=lambda x: video_concat_modes[x][0],
    )
    params.video_concat_mode = VideoConcatMode(
        video_concat_modes[selected_index][1]
    )
    config.ui["video_concat_mode"] = params.video_concat_mode.value

    video_transition_modes = [
        (tr("None"), VideoTransitionMode.none.value),
        (tr("Shuffle"), VideoTransitionMode.shuffle.value),
        (tr("Crossfade"), VideoTransitionMode.crossfade.value),
        (tr("FadeIn"), VideoTransitionMode.fade_in.value),
        (tr("FadeOut"), VideoTransitionMode.fade_out.value),
        (tr("SlideIn"), VideoTransitionMode.slide_in.value),
        (tr("SlideOut"), VideoTransitionMode.slide_out.value),
    ]
    saved_video_transition_mode = config.ui.get("video_transition_mode", VideoTransitionMode.crossfade.value)
    selected_index = st.selectbox(
        tr("Video Transition Mode"),
        options=range(len(video_transition_modes)),
        format_func=lambda x: video_transition_modes[x][0],
        index=_find_option_index(
            video_transition_modes, saved_video_transition_mode, 0
        ),
        help=tr("Video Transition Mode Help"),
    )
    params.video_transition_mode = VideoTransitionMode(
        video_transition_modes[selected_index][1]
    )
    if params.video_transition_mode.value is None:
        config.ui.pop("video_transition_mode", None)
    else:
        config.ui["video_transition_mode"] = params.video_transition_mode.value

    video_aspect_ratios = [
        (tr("Portrait"), VideoAspect.portrait.value),
        (tr("Portrait 4:5"), VideoAspect.portrait_4_5.value),
        (tr("Square"), VideoAspect.square.value),
        (tr("Landscape"), VideoAspect.landscape.value),
    ]
    saved_video_aspect = config.ui.get("video_aspect")
    default_aspect_index = _find_option_index(
        video_aspect_ratios,
        saved_video_aspect,
        3 if enabled_video_sources == ["coverr"] else 0,
    )
    selected_index = st.selectbox(
        tr("Video Ratio"),
        options=range(len(video_aspect_ratios)),
        format_func=lambda x: video_aspect_ratios[x][0],
        index=default_aspect_index,
        key=f"video_aspect_for_{params.video_source}",
    )
    params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])
    config.ui["video_aspect"] = params.video_aspect.value

    aspect_values = [value for _label, value in video_aspect_ratios]
    aspect_labels = {value: label for label, value in video_aspect_ratios}
    additional_aspect_values = [
        value for value in aspect_values if value != params.video_aspect.value
    ]
    saved_video_aspects = config.ui.get("video_aspects", [])
    if not isinstance(saved_video_aspects, (list, tuple)):
        saved_video_aspects = []
    aspect_selection_key = f"video_additional_aspects_for_{params.video_source}"
    saved_additional_aspects = [
        value
        for value in saved_video_aspects
        if value in additional_aspect_values
    ]
    current_additional_aspects = st.session_state.get(
        aspect_selection_key, saved_additional_aspects
    )
    if not isinstance(current_additional_aspects, (list, tuple)):
        current_additional_aspects = []
    st.session_state[aspect_selection_key] = [
        value
        for value in current_additional_aspects
        if value in additional_aspect_values
    ]
    selected_additional_aspects = st.multiselect(
        tr("Additional Output Formats"),
        options=additional_aspect_values,
        format_func=lambda value: aspect_labels[value],
        help=tr("Additional Output Formats Help"),
        key=aspect_selection_key,
    )
    render_aspects = _resolve_output_aspects(
        params.video_aspect, selected_additional_aspects
    )
    params.video_aspects = render_aspects if len(render_aspects) > 1 else None
    if params.video_aspects:
        config.ui["video_aspects"] = [
            aspect.value for aspect in params.video_aspects
        ]
    else:
        config.ui.pop("video_aspects", None)

    clip_duration_options = [2, 3, 4, 5, 6, 7, 8, 9, 10]
    saved_video_clip_duration = _config_int(config.ui, "video_clip_duration", 3)
    params.video_clip_duration = st.selectbox(
        tr("Clip Duration"),
        options=clip_duration_options,
        index=_find_option_index(
            clip_duration_options, saved_video_clip_duration, 1
        ),
    )
    config.ui["video_clip_duration"] = params.video_clip_duration

    if api_selection:
        st.checkbox(
            tr("Manual Video Selection"),
            help=tr("Manual Video Selection Help"),
            key="manual_video_selection_enabled",
        )

        if st.session_state.get("manual_video_selection_enabled"):
            manual_candidate_limit = st.slider(
                tr("Candidate Limit"),
                min_value=6,
                max_value=30,
                value=12,
                step=3,
                key="manual_video_candidate_limit",
            )
            manual_search_terms = params.video_terms or params.video_subject
            if st.button(
                tr("Find Video Candidates"),
                key="find_manual_video_candidates",
                width="stretch",
            ):
                if not manual_search_terms and not (
                    params.smart_scene_queries and params.video_script
                ):
                    st.error(tr("Please Enter the Video Subject or Script"))
                else:
                    with st.spinner(tr("Finding Video Candidates")):
                        try:
                            candidate_search_terms = manual_search_terms
                            if (
                                params.smart_scene_queries
                                and params.video_script
                                and not params.video_terms
                            ):
                                candidate_search_terms = _generate_video_terms_for_ui(
                                    params,
                                    params.video_script,
                                )
                            if (
                                isinstance(candidate_search_terms, str)
                                and "Error: " in candidate_search_terms
                            ):
                                st.error(tr(candidate_search_terms))
                                st.stop()
                            candidates = material.search_video_candidates(
                                search_terms=candidate_search_terms,
                                source=params.video_source,
                                video_aspect=params.video_aspect,
                                max_clip_duration=params.video_clip_duration,
                                limit=manual_candidate_limit,
                                enabled_sources=enabled_video_sources,
                            )
                            st.session_state["manual_video_candidates"] = [
                                _material_to_dict(item) for item in candidates
                            ]
                            st.session_state["manual_video_selected_urls"] = []
                        except Exception as e:
                            st.error(str(e))

            candidate_urls = [
                item.get("url", "")
                for item in st.session_state.get("manual_video_candidates", [])
                if item.get("url")
            ]
            if candidate_urls:
                valid_candidate_urls = set(candidate_urls)
                st.session_state["manual_video_selected_urls"] = [
                    url
                    for url in st.session_state.get(
                        "manual_video_selected_urls", []
                    )
                    if url in valid_candidate_urls
                ]
                selected_urls = st.multiselect(
                    tr("Select Video Candidates"),
                    options=candidate_urls,
                    format_func=_manual_candidate_label,
                    key="manual_video_selected_urls",
                )
                if selected_urls:
                    preview_urls = [
                        url
                        for url in selected_urls[:3]
                        if str(url).startswith(("https://", "http://"))
                    ]
                    if preview_urls:
                        st.caption(tr("Selected Video Preview"))
                    for preview_url in preview_urls:
                        st.video(preview_url)
            elif st.session_state.get("manual_video_candidates") == []:
                st.caption(tr("No Video Candidates Found"))

    _render_material_benchmark(params)

    video_count_options = [1, 2, 3, 4, 5]
    saved_video_count = _config_int(config.ui, "video_count", 1)
    params.video_count = st.selectbox(
        tr("Number of Videos Generated Simultaneously"),
        options=video_count_options,
        index=_find_option_index(video_count_options, saved_video_count, 0),
    )
    config.ui["video_count"] = params.video_count

    with st.expander(tr("Advanced Video Settings"), expanded=False):
        _render_advanced_video_settings_panel(params)

    return uploaded_files


def _tts_server_options():
    return [
        (voice.NO_VOICE_NAME, tr("No Voice")),
        ("azure-tts-v1", "Azure TTS V1"),
        ("azure-tts-v2", "Azure TTS V2"),
        ("siliconflow", "SiliconFlow TTS"),
        ("gemini-tts", "Google Gemini TTS"),
        ("mimo-tts", "Xiaomi MiMo TTS"),
        ("elevenlabs", "ElevenLabs TTS"),
        ("chatterbox", "Chatterbox TTS"),
    ]


def _friendly_voice_label(voice_name):
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        return parts[2] if len(parts) >= 3 else voice_name
    if voice.is_chatterbox_voice(voice_name):
        name = voice_name.split(":", 1)[1] if ":" in voice_name else voice_name
        return name.replace("-Female", "").replace("-Male", "")
    return (
        voice_name.replace("Female", tr("Female"))
        .replace("Male", tr("Male"))
        .replace("Neural", "")
    )


def _friendly_voice_names(filtered_voices, selected_tts_server):
    if selected_tts_server == voice.NO_VOICE_NAME:
        return {voice.NO_VOICE_NAME: tr("No Voice")}
    return {voice_name: _friendly_voice_label(voice_name) for voice_name in filtered_voices}


def _render_tts_provider_settings(selected_tts_server, voice_name):
    if selected_tts_server == "azure-tts-v2" or (
        voice_name and voice.is_azure_v2_voice(voice_name)
    ):
        saved_azure_speech_region = config.azure.get("speech_region", "")
        saved_azure_speech_key = config.azure.get("speech_key", "")
        azure_speech_region = st.text_input(
            tr("Speech Region"),
            value=saved_azure_speech_region,
            key="azure_speech_region_input",
        )
        azure_speech_key = st.text_input(
            tr("Speech Key"),
            value=saved_azure_speech_key,
            type="password",
            key="azure_speech_key_input",
        )
        config.azure["speech_region"] = azure_speech_region
        config.azure["speech_key"] = azure_speech_key

    if selected_tts_server == "gemini-tts" or (
        voice_name and voice.is_gemini_voice(voice_name)
    ):
        gemini_tts_api_key = st.text_input(
            tr("Gemini API Key"),
            value=config.app.get("gemini_api_key", ""),
            type="password",
            key="gemini_tts_api_key_input",
        )
        config.app["gemini_api_key"] = gemini_tts_api_key

    if selected_tts_server == "siliconflow" or (
        voice_name and voice.is_siliconflow_voice(voice_name)
    ):
        saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

        siliconflow_api_key = st.text_input(
            tr("SiliconFlow API Key"),
            value=saved_siliconflow_api_key,
            type="password",
            key="siliconflow_api_key_input",
        )

        st.info(
            tr("SiliconFlow TTS Settings")
            + ":\n"
            + "- "
            + tr("Speed: Range [0.25, 4.0], default is 1.0")
            + "\n"
            + "- "
            + tr("Volume: Uses Speech Volume setting, default 1.0 maps to gain 0")
        )

        config.siliconflow["api_key"] = siliconflow_api_key

    if selected_tts_server == "mimo-tts" or (
        voice_name and voice.is_mimo_voice(voice_name)
    ):
        saved_mimo_api_key = config.app.get("mimo_api_key", "")

        mimo_api_key = st.text_input(
            tr("MiMo API Key"),
            value=saved_mimo_api_key,
            type="password",
            key="mimo_tts_api_key_input",
        )

        st.info(
            tr("MiMo TTS Settings")
            + ":\n"
            + "- "
            + tr("Uses Xiaomi MiMo V2.5 TTS preset voices")
            + "\n"
            + "- "
            + tr("Speed and volume are currently handled by the provider defaults")
        )

        config.app["mimo_api_key"] = mimo_api_key

    if selected_tts_server == "elevenlabs" or (
        voice_name and voice.is_elevenlabs_voice(voice_name)
    ):
        saved_elevenlabs_api_key = config.elevenlabs.get("api_key", "")

        elevenlabs_api_key = st.text_input(
            tr("ElevenLabs API Key"),
            value=saved_elevenlabs_api_key,
            type="password",
            key="elevenlabs_api_key_input",
        )

        elevenlabs_models = [
            "eleven_multilingual_v2",
            "eleven_flash_v2_5",
            "eleven_v3",
        ]
        saved_elevenlabs_model = config.elevenlabs.get(
            "model_id", "eleven_multilingual_v2"
        )
        if saved_elevenlabs_model not in elevenlabs_models:
            saved_elevenlabs_model = "eleven_multilingual_v2"
        elevenlabs_model = st.selectbox(
            tr("ElevenLabs Model"),
            options=elevenlabs_models,
            index=elevenlabs_models.index(saved_elevenlabs_model),
            key="elevenlabs_model_select",
        )
        config.elevenlabs["model_id"] = elevenlabs_model

        st.info(
            "ElevenLabs TTS Settings:\n"
            "- Get your API key at https://elevenlabs.io/app/settings/api-keys\n"
            "- Mark voices as â˜… Favorite in the ElevenLabs voice library to make them appear here"
        )

        if elevenlabs_api_key != saved_elevenlabs_api_key:
            for key in list(st.session_state.keys()):
                if key.startswith("elevenlabs_voices_"):
                    del st.session_state[key]

        config.elevenlabs["api_key"] = elevenlabs_api_key

    if selected_tts_server == "chatterbox" or (
        voice_name and voice.is_chatterbox_voice(voice_name)
    ):
        chatterbox_base_url = st.text_input(
            tr("Chatterbox Base URL"),
            value=config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            key="chatterbox_base_url_input",
            placeholder="http://localhost:4123/v1",
        )
        config.chatterbox["base_url"] = (chatterbox_base_url or "").strip()

        chatterbox_api_key = st.text_input(
            tr("Chatterbox API Key"),
            value=config.chatterbox.get("api_key", ""),
            type="password",
            key="chatterbox_api_key_input",
        )
        config.chatterbox["api_key"] = chatterbox_api_key

        chatterbox_model = st.text_input(
            tr("Chatterbox Model"),
            value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            key="chatterbox_model_input",
        )
        config.chatterbox["model_id"] = (
            chatterbox_model or DEFAULT_CHATTERBOX_MODEL
        ).strip()

        saved_chatterbox_voices = (
            _parse_chatterbox_voices(config.chatterbox.get("voices"))
            or DEFAULT_CHATTERBOX_VOICES
        )
        if isinstance(saved_chatterbox_voices, list):
            saved_chatterbox_voices = ", ".join(saved_chatterbox_voices)
        chatterbox_voices = st.text_input(
            tr("Chatterbox Voices"),
            value=str(saved_chatterbox_voices or ""),
            key="chatterbox_voices_input",
            placeholder="default-Female, narrator-Male",
        )
        config.chatterbox["voices"] = _parse_chatterbox_voices(chatterbox_voices)

        st.info(
            "Chatterbox TTS Settings (self-hosted):\n"
            "- Run an OpenAI-compatible Chatterbox server (e.g. "
            "devnen/Chatterbox-TTS-Server or travisvn/chatterbox-tts-api) and "
            "set Base URL to its /v1 endpoint\n"
            "- Voices is a comma-separated list of voice names your server "
            "exposes; add a -Female or -Male suffix only to label the gender "
            "in this dropdown\n"
            "- Speech Volume is not applied for Chatterbox (the OpenAI "
            "/audio/speech API has no volume field); use Speech Rate instead"
        )


def _render_voice_volume_and_rate_settings(params):
    voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
    saved_voice_volume = config.ui.get("voice_volume", 1.0)
    params.voice_volume = st.selectbox(
        tr("Speech Volume"),
        options=voice_volume_options,
        index=_find_option_index(voice_volume_options, saved_voice_volume, 2),
    )
    config.ui["voice_volume"] = params.voice_volume

    voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]
    saved_voice_rate = config.ui.get("voice_rate", 1.0)
    params.voice_rate = st.selectbox(
        tr("Speech Rate"),
        options=voice_rate_options,
        index=_find_option_index(voice_rate_options, saved_voice_rate, 2),
    )
    config.ui["voice_rate"] = params.voice_rate


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """Estimate a conservative narration-duration range without an API call."""
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(
        re.findall(r"[,，.。!?！？;；:：]", normalized_text)
    )
    base_seconds = (
        len(script_chars) / 4.2
        + len(words) / 2.6
        + punctuation_count * 0.12
    )
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set(
            "àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ"
        )
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
) -> str:
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """Hash credentials used by the preview cache without storing the secret."""
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(
                config.azure.get("speech_key", "")
            ),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(
                config.siliconflow.get("api_key", "")
            )
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(
                config.app.get("gemini_api_key", "")
            )
        }
    if tts_server == "mimo-tts":
        return {
            "credential": _credential_signature(
                config.app.get("mimo_api_key", "")
            )
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(
                config.elevenlabs.get("api_key", "")
            ),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(
                config.chatterbox.get("api_key", "")
            ),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
) -> dict | None:
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            sub_maker = voice.tts(
                text=content,
                voice_name=voice_name,
                voice_rate=voice_rate,
                voice_file=audio_file,
                voice_volume=voice_volume,
            )
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(
                f"{preview_type} voice preview did not produce an audio file"
            )
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None
        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
        }
    finally:
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(f"failed to delete voice preview file: {audio_file}")


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    if not friendly_names or selected_tts_server == voice.NO_VOICE_NAME:
        return

    script_content = str(params.video_script or "").strip()
    estimated_range = _estimate_voiceover_duration_range(
        script_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(
        selected_tts_server
    )
    preview_columns = st.columns(2)
    sample_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        use_container_width=True,
    )
    full_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        disabled=not bool(script_content),
        help=tr("Full Voiceover Preview Cost Hint"),
        use_container_width=True,
    )
    preview_type = "sample" if sample_requested else "full" if full_requested else ""
    preview_content = sample_content if sample_requested else script_content

    def fingerprint(kind, content):
        return _voice_preview_fingerprint(
            preview_type=kind,
            content=content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
        )

    sample_fingerprint = fingerprint("sample", sample_content)
    full_fingerprint = fingerprint("full", script_content) if script_content else ""
    if preview_type:
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached = st.session_state.get("voice_preview_audio")
        if not cached or cached.get("fingerprint") != requested_fingerprint:
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                    )
            except Exception as exc:
                logger.exception(
                    f"failed to generate {preview_type} voice preview"
                )
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    st.session_state["voice_preview_audio"] = {
                        **preview_result,
                        "fingerprint": requested_fingerprint,
                    }
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached = st.session_state.get("voice_preview_audio")
    if (
        isinstance(cached, dict)
        and cached.get("fingerprint") in {sample_fingerprint, full_fingerprint}
        and cached.get("audio_bytes")
    ):
        st.audio(
            cached["audio_bytes"],
            format=cached.get("mime_type", "audio/mp3"),
        )
        if cached.get("preview_type") == "full":
            duration = cached.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(
                        duration=f"{duration:.1f}"
                    )
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _render_tts_settings_panel(params):
    tts_servers = _tts_server_options()

    saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    saved_tts_server_index = 0
    for index, (server_value, _) in enumerate(tts_servers):
        if server_value == saved_tts_server:
            saved_tts_server_index = index
            break

    tts_server_labels = dict(tts_servers)
    selected_tts_server = st.selectbox(
        tr("TTS Servers"),
        options=[server_value for server_value, _ in tts_servers],
        format_func=lambda server_value: tts_server_labels[server_value],
        index=saved_tts_server_index,
        key="tts_server_select",
    )
    config.ui["tts_server"] = selected_tts_server

    filtered_voices = []

    if selected_tts_server == voice.NO_VOICE_NAME:
        filtered_voices = [voice.NO_VOICE_NAME]
    elif selected_tts_server == "siliconflow":
        filtered_voices = voice.get_siliconflow_voices()
    elif selected_tts_server == "gemini-tts":
        filtered_voices = voice.get_gemini_voices()
    elif selected_tts_server == "mimo-tts":
        filtered_voices = voice.get_mimo_voices()
    elif selected_tts_server == "elevenlabs":
        saved_elevenlabs_api_key = st.session_state.get(
            "elevenlabs_api_key_input",
            config.elevenlabs.get("api_key", ""),
        )
        if saved_elevenlabs_api_key:
            config.elevenlabs["api_key"] = saved_elevenlabs_api_key
        api_key_fingerprint = (
            hashlib.sha256(saved_elevenlabs_api_key.encode("utf-8")).hexdigest()[:16]
            if saved_elevenlabs_api_key
            else "empty"
        )
        cache_key = f"elevenlabs_voice_catalog_{api_key_fingerprint}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = voice.get_elevenlabs_voice_catalog(
                saved_elevenlabs_api_key
            )
        if st.button("ğŸ”„ Sesleri Yenile", key="refresh_elevenlabs"):
            for key in list(st.session_state.keys()):
                if key.startswith(
                    ("elevenlabs_voice_catalog_", "elevenlabs_voices_")
                ):
                    del st.session_state[key]
            st.rerun()
        elevenlabs_catalog = st.session_state[cache_key]
        filtered_voices = list(elevenlabs_catalog.get("voices") or [])
        filtered_voice_count = int(
            elevenlabs_catalog.get("filtered_count") or 0
        )
        if filtered_voice_count:
            st.info(
                tr("ElevenLabs Free Tier Voice Filter").format(
                    count=filtered_voice_count
                )
            )
    elif selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()
        filtered_voices = voice.get_chatterbox_voices()
    else:
        all_voices = voice.get_all_azure_voices(filter_locals=None)

        for voice_name in all_voices:
            if selected_tts_server == "azure-tts-v2":
                if "V2" in voice_name:
                    filtered_voices.append(voice_name)
            else:
                if "V2" not in voice_name:
                    filtered_voices.append(voice_name)

    friendly_names = _friendly_voice_names(filtered_voices, selected_tts_server)

    saved_voice_name = config.ui.get("voice_name", "")
    saved_voice_name_index = 0

    if saved_voice_name in friendly_names:
        saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
    else:
        for index, voice_name in enumerate(filtered_voices):
            if voice_name.lower().startswith(st.session_state["ui_language"].lower()):
                saved_voice_name_index = index
                break

    if saved_voice_name_index >= len(friendly_names) and friendly_names:
        saved_voice_name_index = 0

    if friendly_names:
        selected_friendly_name = st.selectbox(
            tr("Speech Synthesis"),
            options=list(friendly_names.values()),
            index=min(saved_voice_name_index, len(friendly_names) - 1)
            if friendly_names
            else 0,
        )

        voice_name = list(friendly_names.keys())[
            list(friendly_names.values()).index(selected_friendly_name)
        ]
        params.voice_name = voice_name
        config.ui["voice_name"] = voice_name
    else:
        st.warning(
            tr(
                "No voices available for the selected TTS server. Please select another server."
            )
        )
        voice_name = ""
        params.voice_name = ""
        config.ui["voice_name"] = ""

    _render_tts_provider_settings(selected_tts_server, voice_name)
    _render_voice_volume_and_rate_settings(params)
    _render_voice_preview(
        params,
        friendly_names,
        selected_tts_server,
        voice_name,
    )


def _render_custom_audio_and_bgm_panel(params):
    custom_audio_file_types = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
    uploaded_audio_file = st.file_uploader(
        tr("Custom Audio File"),
        type=custom_audio_file_types
        + [file_type.upper() for file_type in custom_audio_file_types],
        accept_multiple_files=False,
        key="custom_audio_file_uploader",
    )
    if uploaded_audio_file:
        uploaded_audio_header = bytes(uploaded_audio_file.getbuffer()[:12])
        st.audio(
            uploaded_audio_file,
            format=_detect_audio_mime(
                uploaded_audio_file.name,
                uploaded_audio_header,
            ),
        )
        st.info(
            tr(
                "Custom audio will be used directly. TTS synthesis will be skipped for this task."
            )
        )

    if "audio_loudness_normalization_enabled" not in st.session_state:
        st.session_state["audio_loudness_normalization_enabled"] = bool(
            config.app.get("audio_loudness_normalization_enabled", False)
        )
    st.checkbox(
        tr("Normalize Narration Loudness"),
        key="audio_loudness_normalization_enabled",
        help=tr("Normalize Narration Loudness Help"),
    )
    config.app["audio_loudness_normalization_enabled"] = bool(
        st.session_state["audio_loudness_normalization_enabled"]
    )

    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Royalty-Free Music Library"), "library"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    saved_bgm_type = config.ui.get("bgm_type", "random")
    bgm_labels = {value: label for label, value in bgm_options}
    selected_bgm_type = st.selectbox(
        tr("Background Music"),
        index=_find_option_index(bgm_options, saved_bgm_type, 1),
        options=[value for _, value in bgm_options],
        format_func=lambda value: bgm_labels[value],
        key="bgm_type_select",
    )
    config.ui["bgm_type"] = selected_bgm_type
    params.bgm_type = selected_bgm_type
    params.bgm_file = ""
    params.video_music_prompt = ""
    uploaded_bgm_file = None
    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    saved_bgm_volume = config.ui.get("bgm_volume", 0.2)
    params.bgm_volume = st.selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        index=_find_option_index(bgm_volume_options, saved_bgm_volume, 2),
        key="bgm_volume_select",
    )
    config.ui["bgm_volume"] = params.bgm_volume

    if selected_bgm_type == "library":
        bgm_files = _list_bgm_files()
        if bgm_files:
            saved_bgm_file = config.ui.get("bgm_file", "")
            selected_bgm_file = st.selectbox(
                tr("Royalty-Free Music Track"),
                options=bgm_files,
                index=_find_option_index(bgm_files, saved_bgm_file, 0),
                key="royalty_free_bgm_select",
            )
            params.bgm_type = "custom"
            params.bgm_file = selected_bgm_file
            config.ui["bgm_file"] = selected_bgm_file
            st.audio(os.path.join(song_dir, selected_bgm_file))
        else:
            params.bgm_type = ""
            st.info(tr("No Royalty-Free Music Found"))
    elif selected_bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Custom Background Music"),
            type=custom_audio_file_types
            + [file_type.upper() for file_type in custom_audio_file_types],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
        )
        _render_custom_bgm_preview(
            uploaded_bgm_file,
            enabled=params.bgm_volume > 0,
        )
        if "custom_bgm_file_input" not in st.session_state:
            st.session_state["custom_bgm_file_input"] = config.ui.get(
                "bgm_file", ""
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        if uploaded_bgm_file:
            params.bgm_file = uploaded_bgm_file.name
            config.ui["bgm_file"] = ""
        elif custom_bgm_file:
            params.bgm_file = custom_bgm_file.strip()
            config.ui["bgm_file"] = params.bgm_file
    elif selected_bgm_type == "sonilo":
        sonilo_api_key = st.text_input(
            tr("Sonilo API Key"),
            value=str(config.app.get("sonilo_api_key", "") or ""),
            type="password",
            key="sonilo_api_key_input",
        )
        config.app["sonilo_api_key"] = sonilo_api_key
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            value=str(config.ui.get("sonilo_bgm_prompt", "") or ""),
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            key="sonilo_bgm_prompt_input",
            help=tr("Sonilo Music Prompt Help"),
        )
        config.ui["sonilo_bgm_prompt"] = params.video_music_prompt
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
        if params.bgm_volume > 0 and not sonilo_service.is_enabled():
            st.warning(tr("Sonilo API Key Required"))
    elif selected_bgm_type == "elevenlabs":
        tts_uses_elevenlabs = (
            config.ui.get("voice_mode", "tts") == "tts"
            and config.ui.get("tts_server") == "elevenlabs"
        )
        if not tts_uses_elevenlabs:
            elevenlabs_api_key = st.text_input(
                tr("ElevenLabs Music API Key"),
                value=str(config.elevenlabs.get("api_key", "") or ""),
                type="password",
                key="elevenlabs_api_key_input",
            )
            config.elevenlabs["api_key"] = elevenlabs_api_key
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            value=str(config.ui.get("elevenlabs_music_prompt", "") or ""),
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            key="elevenlabs_music_prompt_input",
            help=tr("ElevenLabs Music Prompt Help"),
        )
        config.ui["elevenlabs_music_prompt"] = params.video_music_prompt
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                st.error(
                    tr("ElevenLabs Connection Test Failed").format(error=str(exc))
                )
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
        if params.bgm_volume > 0 and not elevenlabs_music_service.is_enabled():
            st.warning(tr("ElevenLabs API Key Required"))
    else:
        config.ui["bgm_file"] = ""
    return uploaded_audio_file, uploaded_bgm_file


def _preset_summary_items(preset_names, builtin_preset_names):
    preset_names = preset_names or []
    builtin_preset_names = builtin_preset_names or []
    if preset_names:
        next_action = tr("Apply Preset")
    elif builtin_preset_names:
        next_action = tr("Apply Suggested Preset")
    else:
        next_action = tr("Save Current Preset")
    return [
        {
            "label": tr("Saved Presets"),
            "value": str(len(preset_names)),
        },
        {
            "label": tr("Built-in Presets"),
            "value": str(len(builtin_preset_names)),
        },
        {
            "label": tr("Next Action"),
            "value": next_action,
        },
    ]


def _render_preset_compact_summary(preset_names, builtin_preset_names):
    items = _preset_summary_items(preset_names, builtin_preset_names)
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _render_preset_details(params, preset_names, builtin_preset_names):
    if builtin_preset_names:
        builtin_preset_col, builtin_apply_col = st.columns([0.7, 0.3])
        with builtin_preset_col:
            selected_builtin_preset = st.selectbox(
                tr("Suggested Preset"),
                options=builtin_preset_names,
                key="suggested_video_preset_select",
            )
        with builtin_apply_col:
            if st.button(tr("Apply Suggested Preset"), key="apply_suggested_preset"):
                try:
                    st.session_state["_pending_video_preset"] = (
                        presets.load_builtin_preset(selected_builtin_preset)
                    )
                    st.rerun()
                except presets.PresetError as e:
                    st.error(f"{tr('Preset Error')}: {str(e)}")

    load_preset_col, save_preset_col = st.columns(2)

    with load_preset_col:
        if preset_names:
            selected_preset_name = st.selectbox(
                tr("Preset"),
                options=preset_names,
                key="video_preset_select",
            )

            try:
                selected_preset_payload = presets.load_preset(selected_preset_name)
                st.download_button(
                    tr("Download Preset"),
                    data=json.dumps(
                        selected_preset_payload,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file_name=f"{selected_preset_name}.json",
                    mime="application/json",
                    key="download_video_preset",
                )
            except presets.PresetError as e:
                st.error(f"{tr('Preset Error')}: {str(e)}")

            preset_action_cols = st.columns(2)
            with preset_action_cols[0]:
                if st.button(tr("Load Preset"), key="load_video_preset"):
                    try:
                        st.session_state["_pending_video_preset"] = (
                            presets.load_preset(selected_preset_name)
                        )
                        st.rerun()
                    except presets.PresetError as e:
                        st.error(f"{tr('Preset Error')}: {str(e)}")
            with preset_action_cols[1]:
                if st.button(tr("Delete Preset"), key="delete_video_preset"):
                    try:
                        presets.delete_preset(selected_preset_name)
                        st.success(f"{tr('Preset Deleted')}: {selected_preset_name}")
                        st.rerun()
                    except presets.PresetError as e:
                        st.error(f"{tr('Preset Error')}: {str(e)}")
        else:
            st.info(tr("No Presets Saved"))

    with save_preset_col:
        preset_name = st.text_input(tr("Preset Name"), key="save_video_preset_name")
        if st.button(tr("Save Current Preset"), key="save_current_video_preset"):
            if not preset_name.strip():
                st.error(tr("Enter a preset name"))
            else:
                try:
                    presets.save_preset(
                        preset_name,
                        params,
                        app_config=_current_preset_app_config(),
                    )
                    st.success(
                        f"{tr('Preset Saved')}: "
                        f"{presets.normalize_preset_name(preset_name)}"
                    )
                except presets.PresetError as e:
                    st.error(f"{tr('Preset Error')}: {str(e)}")

        uploaded_preset = st.file_uploader(
            tr("Import Preset File"),
            type=["json"],
            accept_multiple_files=False,
            key="import_video_preset_file",
        )
        if st.button(tr("Import Preset"), key="import_video_preset"):
            if uploaded_preset is None:
                st.error(tr("Please choose a preset file"))
            else:
                try:
                    imported_payload = presets.import_preset_payload(
                        json.loads(uploaded_preset.getvalue().decode("utf-8"))
                    )
                    presets.save_preset(
                        imported_payload["name"],
                        imported_payload["params"],
                        app_config=imported_payload.get("app_config"),
                    )
                    st.session_state["_pending_video_preset"] = imported_payload
                    st.success(f"{tr('Preset Imported')}: {imported_payload['name']}")
                    st.rerun()
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    presets.PresetError,
                ) as e:
                    st.error(f"{tr('Preset Error')}: {str(e)}")


def _render_presets_panel(params):
    _render_panel_intro("Presets", "Presets Panel Help")
    preset_names = presets.list_presets()
    builtin_preset_names = presets.list_builtin_presets()
    _render_preset_compact_summary(preset_names, builtin_preset_names)

    with st.expander(tr("Preset Details"), expanded=False):
        _render_preset_details(params, preset_names, builtin_preset_names)


def _social_metadata_hashtags_text(hashtags):
    if isinstance(hashtags, str):
        return hashtags
    if isinstance(hashtags, (list, tuple)):
        return " ".join(str(tag) for tag in hashtags if str(tag).strip())
    return ""


def _social_metadata_signature(social_metadata):
    try:
        return json.dumps(
            social_metadata or {},
            ensure_ascii=False,
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return ""


def _prepare_social_metadata_edit_fields(social_metadata):
    signature = _social_metadata_signature(social_metadata)
    if st.session_state.get("_social_metadata_edit_signature") == signature:
        return
    st.session_state["social_metadata_title"] = social_metadata.get("title", "")
    st.session_state["social_metadata_caption"] = social_metadata.get("caption", "")
    st.session_state["social_metadata_hashtags"] = _social_metadata_hashtags_text(
        social_metadata.get("hashtags", [])
    )
    st.session_state["_social_metadata_edit_signature"] = signature


def _social_metadata_from_edit_fields():
    hashtags_text = st.session_state.get("social_metadata_hashtags", "")
    hashtags = [
        tag.strip()
        for tag in hashtags_text.replace(",", " ").split()
        if tag.strip()
    ]
    return {
        "title": st.session_state.get("social_metadata_title", ""),
        "caption": st.session_state.get("social_metadata_caption", ""),
        "hashtags": hashtags,
    }


def _render_social_metadata_fields(social_metadata):
    _prepare_social_metadata_edit_fields(social_metadata)
    st.text_input(
        tr("Social Title"),
        key="social_metadata_title",
    )
    st.text_area(
        tr("Social Description"),
        height=120,
        key="social_metadata_caption",
    )
    st.text_input(
        tr("Social Hashtags"),
        key="social_metadata_hashtags",
    )
    st.session_state["social_metadata"] = _social_metadata_from_edit_fields()


def _social_metadata_summary_items(social_metadata, selected_social_platform):
    social_metadata = social_metadata if isinstance(social_metadata, dict) else {}
    hashtags = social_metadata.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtag_count = len([tag for tag in hashtags.replace(",", " ").split() if tag])
    elif isinstance(hashtags, (list, tuple)):
        hashtag_count = len([tag for tag in hashtags if str(tag).strip()])
    else:
        hashtag_count = 0

    return [
        {
            "label": tr("Social Platform"),
            "value": _plain_value(selected_social_platform) or tr("Unknown"),
        },
        {
            "label": tr("Social Title"),
            "value": tr("Ready") if social_metadata.get("title") else tr("Not Run"),
        },
        {
            "label": tr("Social Description"),
            "value": tr("Ready") if social_metadata.get("caption") else tr("Not Run"),
        },
        {
            "label": tr("Social Hashtags"),
            "value": str(hashtag_count),
        },
    ]


def _render_social_metadata_compact_summary(social_metadata, selected_social_platform):
    items = _social_metadata_summary_items(social_metadata, selected_social_platform)
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _social_metadata_fingerprint(video_subject, video_script, platform, language):
    return {
        "video_subject": str(video_subject or "").strip(),
        "video_script": str(video_script or "").strip(),
        "platform": str(platform or "").strip(),
        "language": str(language or "auto").strip() or "auto",
    }


def _social_metadata_warning_text(fingerprint, video_subject, video_script, platform, language):
    if not isinstance(fingerprint, dict) or not fingerprint:
        return ""
    current_fingerprint = _social_metadata_fingerprint(
        video_subject,
        video_script,
        platform,
        language,
    )
    if fingerprint != current_fingerprint:
        return tr("Social Metadata Stale Warning")
    return ""


def _render_social_metadata_panel(params):
    _render_panel_intro("Social Metadata", "Social Metadata Panel Help")
    st.checkbox(
        tr("Auto Social Metadata After Video"),
        key="auto_social_metadata_after_video",
    )
    social_platforms = _social_platform_options()
    selected_social_platform_index = st.selectbox(
        tr("Social Platform"),
        options=range(len(social_platforms)),
        format_func=lambda x: social_platforms[x][0],
        key="social_platform_select",
    )
    selected_social_platform = social_platforms[selected_social_platform_index][1]
    social_batch_items = _get_batch_items()
    social_inputs = _social_metadata_input_values(params, social_batch_items)

    if st.button(tr("Generate Social Metadata"), key="generate_social_metadata"):
        if not social_inputs["video_subject"] and not social_inputs["video_script"]:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            with st.spinner(tr("Generating Social Metadata")):
                st.session_state["social_metadata"] = llm.generate_social_metadata(
                    video_subject=social_inputs["video_subject"],
                    video_script=social_inputs["video_script"],
                    language=social_inputs["language"],
                    platform=selected_social_platform,
                )
                st.session_state["social_metadata_fingerprint"] = (
                    _social_metadata_fingerprint(
                        social_inputs["video_subject"],
                        social_inputs["video_script"],
                        selected_social_platform,
                        social_inputs["language"],
                    )
                )

    social_metadata = st.session_state.get("social_metadata")
    social_metadata_warning = _social_metadata_warning_text(
        st.session_state.get("social_metadata_fingerprint"),
        social_inputs["video_subject"],
        social_inputs["video_script"],
        selected_social_platform,
        social_inputs["language"],
    )
    if social_metadata and social_metadata_warning:
        st.warning(social_metadata_warning)
    _render_social_metadata_compact_summary(
        social_metadata,
        selected_social_platform,
    )
    if social_metadata:
        with st.expander(tr("Social Metadata Details"), expanded=False):
            _render_social_metadata_fields(social_metadata)

    return selected_social_platform


def _render_publishing_safety_summary(youtube_privacy_labels):
    selected_youtube_privacy = st.session_state.get(
        "upload_post_youtube_privacy_status",
        upload_post.YOUTUBE_PRIVACY_UNLISTED,
    )
    summary_parts = [
        f"{tr('Upload-Post Enabled')}: "
        f"{tr('Enabled') if st.session_state.get('upload_post_enabled') else tr('Disabled')}",
        f"{tr('Auto Upload After Video')}: "
        f"{tr('Enabled') if st.session_state.get('upload_post_auto_upload') else tr('Disabled')}",
        f"{tr('YouTube Privacy Status')}: "
        f"{youtube_privacy_labels.get(selected_youtube_privacy, tr('Unlisted Upload'))}",
    ]
    st.info(f"{tr('Publishing Safety Summary')}: " + " | ".join(summary_parts))


def _publishing_youtube_privacy_options():
    youtube_privacy_options = [
        upload_post.YOUTUBE_PRIVACY_PRIVATE,
        upload_post.YOUTUBE_PRIVACY_UNLISTED,
        upload_post.YOUTUBE_PRIVACY_PUBLIC,
    ]
    youtube_privacy_labels = {
        upload_post.YOUTUBE_PRIVACY_PRIVATE: tr("Private Upload"),
        upload_post.YOUTUBE_PRIVACY_UNLISTED: tr("Unlisted Upload"),
        upload_post.YOUTUBE_PRIVACY_PUBLIC: tr("Public Upload"),
    }
    return youtube_privacy_options, youtube_privacy_labels


def _publishing_settings_summary_items(youtube_privacy_labels):
    selected_youtube_privacy = st.session_state.get(
        "upload_post_youtube_privacy_status",
        upload_post.YOUTUBE_PRIVACY_UNLISTED,
    )
    is_public = selected_youtube_privacy == upload_post.YOUTUBE_PRIVACY_PUBLIC
    public_allowed = bool(st.session_state.get("upload_post_allow_public_youtube"))
    return [
        {
            "label": tr("Publishing Settings"),
            "value": tr("Enabled")
            if st.session_state.get("upload_post_enabled")
            else tr("Disabled"),
        },
        {
            "label": tr("Auto Upload After Video"),
            "value": tr("Enabled")
            if st.session_state.get("upload_post_auto_upload")
            else tr("Disabled"),
        },
        {
            "label": tr("YouTube Privacy Status"),
            "value": youtube_privacy_labels.get(
                selected_youtube_privacy,
                tr("Unlisted Upload"),
            ),
        },
        {
            "label": tr("Public Upload Approval"),
            "value": tr("Ready")
            if (not is_public or public_allowed)
            else tr("Needs Approval"),
        },
    ]


def _render_publishing_settings_compact_summary(youtube_privacy_labels):
    items = _publishing_settings_summary_items(youtube_privacy_labels)
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _render_publishing_settings_panel():
    _render_panel_intro("Publishing Settings", "Publishing Settings Panel Help")
    youtube_privacy_options, youtube_privacy_labels = _publishing_youtube_privacy_options()
    _render_publishing_settings_compact_summary(youtube_privacy_labels)

    with st.expander(tr("Publishing Settings Details"), expanded=False):
        st.info(tr("Upload-Post Config Hint"))

        st.checkbox(
            tr("Upload-Post Enabled"),
            key="upload_post_enabled",
            help=tr("Upload-Post Enabled Help"),
        )
        config.app["upload_post_enabled"] = st.session_state["upload_post_enabled"]

        st.checkbox(
            tr("Auto Upload After Video"),
            key="upload_post_auto_upload",
            help=tr("Auto Upload After Video Help"),
            disabled=not st.session_state["upload_post_enabled"],
        )
        config.app["upload_post_auto_upload"] = st.session_state[
            "upload_post_auto_upload"
        ]

        current_youtube_privacy = st.session_state.get(
            "upload_post_youtube_privacy_status",
            upload_post.YOUTUBE_PRIVACY_UNLISTED,
        )
        if current_youtube_privacy not in youtube_privacy_options:
            current_youtube_privacy = upload_post.YOUTUBE_PRIVACY_UNLISTED
            st.session_state["upload_post_youtube_privacy_status"] = (
                current_youtube_privacy
            )

        st.selectbox(
            tr("YouTube Privacy Status"),
            options=youtube_privacy_options,
            format_func=lambda value: youtube_privacy_labels[value],
            key="upload_post_youtube_privacy_status",
            help=tr("YouTube Privacy Status Help"),
            disabled=not st.session_state["upload_post_enabled"],
        )

        selected_youtube_privacy = st.session_state["upload_post_youtube_privacy_status"]
        if selected_youtube_privacy == upload_post.YOUTUBE_PRIVACY_PUBLIC:
            st.warning(tr("Public YouTube Upload Warning"))
            st.checkbox(
                tr("Allow Public YouTube Upload"),
                key="upload_post_allow_public_youtube",
                help=tr("Allow Public YouTube Upload Help"),
                disabled=not st.session_state["upload_post_enabled"],
            )
            if not st.session_state["upload_post_allow_public_youtube"]:
                st.caption(tr("Public YouTube Upload Blocked"))
        else:
            st.session_state["upload_post_allow_public_youtube"] = False

        config.app["upload_post_youtube_privacy_status"] = selected_youtube_privacy
        config.app["upload_post_allow_public_youtube"] = st.session_state[
            "upload_post_allow_public_youtube"
        ]
        _render_publishing_safety_summary(youtube_privacy_labels)


def _batch_generation_summary_items(batch_items, use_manual_batch_scripts=False):
    items = batch_items or []
    scripted_count = len([item for item in items if item.get("script")])
    return [
        {
            "label": tr("Batch Subjects"),
            "value": str(len(items)),
        },
        {
            "label": tr("Batch Scripts"),
            "value": f"{scripted_count}/{len(items)}" if items else tr("Optional"),
        },
        {
            "label": tr("Use Manual Batch Scripts"),
            "value": tr("Enabled")
            if use_manual_batch_scripts
            else tr("Disabled"),
        },
    ]


def _render_batch_generation_compact_summary(batch_items, use_manual_batch_scripts):
    items = _batch_generation_summary_items(
        batch_items,
        use_manual_batch_scripts=use_manual_batch_scripts,
    )
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _batch_generation_readiness_state(batch_items, use_manual_batch_scripts):
    items = batch_items or []
    item_count = len(items)
    scripted_count = len([item for item in items if item.get("script")])
    if not item_count:
        level = "info"
        message = tr("Batch Needs Ideas")
        action = tr("Add Batch Subjects")
    elif use_manual_batch_scripts and scripted_count < item_count:
        level = "warning"
        message = tr("Batch Scripts Need Review")
        action = tr("Review Batch Scripts")
    else:
        level = "success"
        message = tr("Batch Ready for Generation")
        action = tr("Generate Batch")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": _batch_generation_summary_items(
            items,
            use_manual_batch_scripts=use_manual_batch_scripts,
        ),
    }


def _render_batch_generation_readiness(batch_items, use_manual_batch_scripts):
    _render_readiness_review(
        "Batch Readiness",
        "Batch Readiness Help",
        _batch_generation_readiness_state(batch_items, use_manual_batch_scripts),
    )


def _render_batch_generation_inputs(use_manual_batch_scripts):
    if use_manual_batch_scripts:
        st.text_area(
            tr("Batch Script Blocks"),
            height=220,
            help=tr("Batch Script Blocks Help"),
            key="batch_script_blocks",
        )
    else:
        st.text_area(
            tr("Batch Subjects"),
            height=140,
            help=tr("Batch Subjects Help"),
            key="batch_subjects",
        )


def _render_batch_generation_panel():
    _render_panel_intro("Batch Generation", "Batch Generation Panel Help")
    use_manual_batch_scripts = st.checkbox(
        tr("Use Manual Batch Scripts"),
        key="use_manual_batch_scripts",
    )
    batch_items = _get_batch_items()
    _render_batch_generation_readiness(batch_items, use_manual_batch_scripts)
    with st.expander(tr("Batch Generation Details"), expanded=False):
        _render_batch_generation_inputs(use_manual_batch_scripts)
    if batch_items:
        st.caption(f"{len(batch_items)} {tr('Batch Subjects Ready')}")
        _render_batch_preview_table(batch_items)


def _batch_preview_rows(batch_items, limit=5):
    rows = []
    for index, item in enumerate(batch_items[:limit], start=1):
        rows.append(
            {
                "#": index,
                tr("Subject"): item.get("subject", ""),
                tr("Video Script"): tr("Ready")
                if item.get("script")
                else tr("Optional"),
            }
        )
    return rows


def _render_batch_preview_table(batch_items):
    rows = _batch_preview_rows(batch_items)
    if not rows:
        return
    st.write(tr("Batch Preview"))
    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
    )


def _batch_workflow_steps(batch_items):
    items = batch_items or []
    item_count = len(items)
    scripted_count = len([item for item in items if item.get("script")])
    preflight_report = st.session_state.get("content_preflight_report") or {}
    viral_analysis = st.session_state.get("viral_analysis") or {}
    has_preflight = bool(preflight_report)
    has_viral = _score_value(viral_analysis.get("overall_score")) is not None

    return [
        {
            "label": tr("Batch Subjects"),
            "status": f"{item_count} {tr('Ready')}" if item_count else tr("Empty"),
            "action": tr("Optional") if item_count else tr("Add Batch Subjects"),
        },
        {
            "label": tr("Batch Scripts"),
            "status": f"{scripted_count}/{item_count}" if item_count else tr("Optional"),
            "action": tr("Optional")
            if scripted_count == item_count and item_count
            else tr("Use Manual Batch Scripts"),
        },
        {
            "label": tr("Content Preflight"),
            "status": tr("Ready") if has_preflight else tr("Not Run"),
            "action": tr("Optional") if has_preflight else tr("Analyze Topic"),
        },
        {
            "label": tr("Viral Analysis"),
            "status": tr("Ready") if has_viral else tr("Not Run"),
            "action": tr("Optional") if has_viral else tr("Generate Viral Analysis"),
        },
        {
            "label": tr("Batch Generation"),
            "status": tr("Ready") if item_count else tr("Empty"),
            "action": tr("Generate Batch") if item_count else tr("Add Batch Subjects"),
        },
    ]


def _render_batch_workflow_steps(batch_items):
    steps = _batch_workflow_steps(batch_items)
    with st.container(border=True):
        st.write(tr("Batch Workflow"))
        st.caption(tr("Batch Workflow Help"))
        for offset in range(0, len(steps), 4):
            cols = st.columns(len(steps[offset : offset + 4]))
            for index, step in enumerate(steps[offset : offset + 4], start=offset + 1):
                with cols[index - offset - 1]:
                    st.metric(f"{index}. {step['label']}", step["status"])
                    st.caption(f"{tr('Next Action')}: {step['action']}")


def _recent_jobs_summary_rows(recent_jobs, limit=5):
    rows = []
    for job in recent_jobs[:limit]:
        viral_analysis = job.get("viral_analysis") or {}
        viral_score = _score_value(viral_analysis.get("overall_score"))
        video_count = _job_output_count(job, "videos")
        pending_upload_count = _job_output_count(job, "pending_uploads")
        thumbnail_count = _job_output_count(job, "thumbnail_candidates")
        next_action = _recent_job_next_action(
            video_count,
            pending_upload_count,
            thumbnail_count,
            viral_score,
        )
        rows.append(
            {
                tr("Subject"): job.get("subject")
                or job.get("task_id")
                or tr("Untitled"),
                tr("Status"): (
                    tr("Partial Output")
                    if job.get("partial_success")
                    else job.get("status", "")
                ),
                tr("Videos"): str(video_count),
                tr("Pending Uploads"): str(pending_upload_count),
                tr("Thumbnail Candidates"): str(thumbnail_count),
                tr("Viral Score"): (
                    f"{viral_score}/100"
                    if viral_score is not None
                    else tr("Score Not Available")
                ),
                tr("Next Action"): next_action,
                tr("Created At"): job.get("created_at", ""),
            }
        )
    return rows


def _recent_job_next_action(
    video_count,
    pending_upload_count,
    thumbnail_count,
    viral_score,
):
    if pending_upload_count:
        return tr("Review Pending Uploads")
    if not video_count:
        return tr("Review Video Output")
    if not thumbnail_count:
        return tr("Review Thumbnail Candidates")
    if viral_score is None:
        return tr("Generate Viral Analysis")
    return tr("Review Recent Jobs")


def _job_output_count(job, key):
    value = job.get(key) if isinstance(job, dict) else None
    if isinstance(value, list):
        return len([item for item in value if item])
    if isinstance(value, dict):
        return 1 if value else 0
    return 1 if value else 0


def _recent_jobs_readiness_state(recent_jobs):
    jobs = recent_jobs or []
    recent_count = len(jobs)
    video_count = sum(_job_output_count(job, "videos") for job in jobs)
    pending_upload_count = sum(_job_output_count(job, "pending_uploads") for job in jobs)
    thumbnail_count = sum(_job_output_count(job, "thumbnail_candidates") for job in jobs)
    scored_count = sum(
        1
        for job in jobs
        if _score_value((job.get("viral_analysis") or {}).get("overall_score"))
        is not None
    )

    if not recent_count:
        level = "info"
        message = tr("Recent Jobs Need Output")
        action = tr("Generate Video")
    elif pending_upload_count:
        level = "warning"
        message = tr("Recent Jobs Pending Uploads")
        action = tr("Review Pending Uploads")
    elif not video_count:
        level = "warning"
        message = tr("Recent Jobs Need Output")
        action = tr("Review Video Output")
    elif not thumbnail_count:
        level = "info"
        message = tr("Recent Jobs Ready")
        action = tr("Review Thumbnail Candidates")
    else:
        level = "success"
        message = tr("Recent Jobs Ready")
        action = tr("Review Recent Jobs")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Recent Jobs"),
                "value": str(recent_count),
            },
            {
                "label": tr("Videos"),
                "value": str(video_count),
            },
            {
                "label": tr("Pending Uploads"),
                "value": str(pending_upload_count),
            },
            {
                "label": tr("Thumbnail Candidates"),
                "value": str(thumbnail_count),
            },
            {
                "label": tr("Viral Score"),
                "value": f"{scored_count} {tr('Ready')}",
            },
        ],
    }


def _history_performance_snapshot_state(recent_jobs):
    jobs = recent_jobs or []
    metric_jobs = [
        job
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("publish_metrics"), dict)
    ]
    total_views = 0
    total_engagements = 0
    scored_jobs = 0
    score_total = 0
    for job in metric_jobs:
        metrics = history.normalize_publish_metrics(job.get("publish_metrics"))
        total_views += int(metrics.get("views", 0) or 0)
        total_engagements += (
            int(metrics.get("likes", 0) or 0)
            + int(metrics.get("comments", 0) or 0)
            + int(metrics.get("shares", 0) or 0)
            + int(metrics.get("saves", 0) or 0)
        )
        score = _score_value((job.get("viral_analysis") or {}).get("overall_score"))
        if score is not None:
            scored_jobs += 1
            score_total += score

    engagement_rate = (
        f"{round((total_engagements / total_views) * 100, 1)}%"
        if total_views > 0
        else "0%"
    )
    average_score = (
        f"{int(round(score_total / scored_jobs))}/100" if scored_jobs else tr("Score Not Available")
    )
    if not metric_jobs:
        level = "info"
        message = tr("Analytics Needs Publish Metrics")
        action = tr("Save Publish Metrics")
    elif scored_jobs < len(metric_jobs):
        level = "info"
        message = tr("Analytics Needs Viral Scores")
        action = tr("Generate Viral Analysis")
    else:
        level = "success"
        message = tr("Analytics Ready")
        action = tr("Review Recent Jobs")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Metric Samples"),
                "value": str(len(metric_jobs)),
            },
            {
                "label": tr("Views"),
                "value": str(total_views),
            },
            {
                "label": tr("Engagement Rate"),
                "value": engagement_rate,
            },
            {
                "label": tr("Average Viral Score"),
                "value": average_score,
            },
        ],
    }


def _render_history_performance_snapshot(recent_jobs):
    _render_readiness_review(
        "Performance Snapshot",
        "Performance Snapshot Help",
        _history_performance_snapshot_state(recent_jobs),
    )


def _publish_insights_status_text(report):
    if not isinstance(report, dict):
        return ""
    if report.get("status") == "insufficient_data":
        return tr("Publish Insights Insufficient Data").format(
            samples=report.get("sample_size", 0),
            minimum=report.get("minimum_sample_size", 0),
        )
    return tr("Publish Insights Advisory")


def _publish_insight_suggestion_text(suggestion):
    suggestion = suggestion if isinstance(suggestion, dict) else {}
    key_by_type = {
        "collect_metrics": "Publish Insight Collect Metrics",
        "manual_review": "Publish Insight Manual Review",
        "quality_gate_alignment": "Publish Insight Quality Alignment",
        "quality_gate_recheck": "Publish Insight Quality Recheck",
        "quality_gate_inconclusive": "Publish Insight Quality Inconclusive",
        "collect_quality_scores": "Publish Insight Collect Quality Scores",
    }
    return tr(
        key_by_type.get(
            suggestion.get("type"),
            "Publish Insight Fallback",
        )
    )


def _render_publish_insights(entries):
    report = publish_insights.build_publish_performance_insights(entries)
    sample_size = report.get("sample_size", 0)
    minimum_sample_size = report.get("minimum_sample_size", 0)
    engagement_rate = report.get("median_engagement_rate_percent")
    engagement_text = (
        f"{float(engagement_rate):.2f}%"
        if isinstance(engagement_rate, (int, float))
        else tr("Score Not Available")
    )

    st.write(tr("Publish Insights"))
    insight_cols = st.columns(3)
    insight_cols[0].metric(
        tr("Publish Insights Samples"),
        f"{sample_size} / {minimum_sample_size}",
    )
    insight_cols[1].metric(
        tr("Publish Insights Median Views"),
        str(report.get("median_views", 0)),
    )
    insight_cols[2].metric(
        tr("Publish Insights Median Engagement"),
        engagement_text,
    )

    status_text = _publish_insights_status_text(report)
    if report.get("status") == "insufficient_data":
        st.info(status_text)
    else:
        st.caption(status_text)
    for suggestion in report.get("suggestions") or []:
        st.caption(_publish_insight_suggestion_text(suggestion))


def _material_benchmark_rows(report):
    if not isinstance(report, dict):
        return []

    def percent_label(value):
        try:
            return f"{round(min(1.0, max(0.0, float(value))) * 100):.0f}%"
        except (TypeError, ValueError):
            return tr("Score Not Available")

    rows = []
    for provider in report.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        try:
            candidate_count = max(0, int(provider.get("candidate_count", 0) or 0))
        except (TypeError, ValueError):
            candidate_count = 0
        rows.append(
            {
                tr("Material Benchmark Provider"): str(
                    provider.get("provider") or tr("Unknown")
                ),
                tr("Material Benchmark Candidates"): candidate_count,
                tr("Material Benchmark Aspect Fit"): percent_label(
                    provider.get("average_aspect_fit")
                ),
                tr("Material Benchmark Preview Quality"): percent_label(
                    provider.get("average_preview_quality")
                ),
            }
        )
    return rows


def _render_material_benchmark(params):
    with st.expander(tr("Material Benchmark"), expanded=False):
        st.caption(tr("Material Benchmark Help"))
        topic = st.text_input(
            tr("Benchmark Topic"),
            value=str(getattr(params, "video_subject", "") or ""),
            key="material_benchmark_topic",
        ).strip()
        if st.button(
            tr("Run Material Benchmark"),
            key="run_material_benchmark",
        ):
            if not topic:
                st.warning(tr("Please Enter the Video Subject or Script"))
            else:
                with st.spinner(tr("Running Material Benchmark")):
                    try:
                        st.session_state["material_benchmark_report"] = (
                            material_benchmark.benchmark_material_providers(
                                topic,
                                params.video_aspect,
                            )
                        )
                    except Exception:
                        st.session_state["material_benchmark_report"] = {
                            "ok": False,
                            "status": "provider_search_failed",
                        }

        report = st.session_state.get("material_benchmark_report")
        if not isinstance(report, dict):
            return
        if report.get("status") == "provider_search_failed":
            st.error(tr("Material Benchmark Failed"))
            return
        if not report.get("ok"):
            st.info(tr("Material Benchmark No Candidates"))
            return

        benchmark_cols = st.columns(2)
        benchmark_cols[0].metric(
            tr("Material Benchmark Candidates"),
            str(report.get("candidate_count", 0)),
        )
        benchmark_cols[1].metric(
            tr("Material Benchmark Providers"),
            str(report.get("provider_count", 0)),
        )
        rows = _material_benchmark_rows(report)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")


def _render_recent_jobs_readiness_review(recent_jobs):
    _render_readiness_review(
        "Recent Jobs Readiness",
        "Recent Jobs Readiness Help",
        _recent_jobs_readiness_state(recent_jobs),
    )


def _render_recent_jobs_compact_summary(recent_jobs):
    state = _recent_jobs_readiness_state(recent_jobs)
    metrics = state.get("metrics") or []
    if not metrics:
        return
    summary_cols = st.columns(len(metrics))
    for col, item in zip(summary_cols, metrics):
        col.metric(item["label"], item["value"])
    message = state.get("message")
    action = state.get("action")
    if message:
        st.caption(
            f"{message} {tr('Next Action')}: {action}"
            if action
            else message
        )


def _render_recent_jobs_summary_table(recent_jobs):
    rows = _recent_jobs_summary_rows(recent_jobs)
    if not rows:
        return
    st.write(tr("Recent Jobs Summary"))
    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
    )


def _render_recent_job_detail_summary(job):
    videos = job.get("videos") or []
    viral_analysis = job.get("viral_analysis") or {}
    viral_score = _score_value(viral_analysis.get("overall_score"))
    pending_upload_count = _job_output_count(job, "pending_uploads")
    thumbnail_count = _job_output_count(job, "thumbnail_candidates")

    with st.container(border=True):
        st.write(tr("Job Detail"))
        detail_cols = st.columns(5)
        detail_cols[0].metric(tr("Videos"), str(len(videos)))
        detail_cols[1].metric(tr("Thumbnail Candidates"), str(thumbnail_count))
        detail_cols[2].metric(tr("Pending Uploads"), str(pending_upload_count))
        detail_cols[3].metric(
            tr("Viral Score"),
            f"{viral_score}/100" if viral_score is not None else tr("Score Not Available"),
        )
        detail_cols[4].metric(
            tr("Next Action"),
            _recent_job_next_action(
                len(videos),
                pending_upload_count,
                thumbnail_count,
                viral_score,
            ),
        )
        scheduled_job = job.get("scheduled_job")
        if isinstance(scheduled_job, str) and scheduled_job.strip():
            st.caption(f"{tr('Scheduled Job')}: {scheduled_job.strip()}")


def _render_history_detail_section(title_key, help_key=""):
    section = st.container(border=True)
    section.write(tr(title_key))
    if help_key:
        section.caption(tr(help_key))
    return section


def _render_video_encoder_results(encoder_results):
    for encoder_result in encoder_results or []:
        if not isinstance(encoder_result, dict):
            continue
        used_codec = str(encoder_result.get("used_codec") or "").strip()
        if not used_codec:
            continue
        configured_codec = str(encoder_result.get("configured_codec") or "").strip()
        video_path = str(encoder_result.get("video_path") or "").strip()
        prefix = f"{video_path}: " if video_path else ""
        if encoder_result.get("fallback_used") and configured_codec:
            st.warning(f"{prefix}{configured_codec} → {used_codec}")
        else:
            st.caption(f"{prefix}{used_codec}")


def _render_quality_warning_text(warning):
    warning = str(warning or "").strip()
    translation_key = {
        "rendered video file is missing": "Render Quality File Missing",
        "video resolution is invalid": "Render Quality Invalid Resolution",
        "video resolution does not match the selected aspect": (
            "Render Quality Aspect Mismatch"
        ),
        "video duration is invalid": "Render Quality Invalid Duration",
        "video duration differs from expected audio duration": (
            "Render Quality Duration Mismatch"
        ),
        "video frame rate is invalid": "Render Quality Invalid Frame Rate",
        "audio track is missing": "Render Quality Missing Audio",
        "sampled audio is near-silent": "Render Quality Near Silent Audio",
        "sampled frames are near-black": "Render Quality Near Black Frames",
        "some sampled frames are near-black": (
            "Render Quality Some Near Black Frames"
        ),
        "sampled frames appear to contain captions over a black visual": (
            "Render Quality Caption Over Black"
        ),
        "some sampled frames appear to contain captions over a black visual": (
            "Render Quality Caption Over Black"
        ),
        "encoding contract could not be inspected": (
            "Render Quality Encoding Inspection Failed"
        ),
        "video codec does not match the encoding contract": (
            "Render Quality Codec Contract Mismatch"
        ),
        "video pixel format does not match the encoding contract": (
            "Render Quality Pixel Format Contract Mismatch"
        ),
        "video color space does not match the encoding contract": (
            "Render Quality Color Metadata Contract Mismatch"
        ),
        "video color transfer does not match the encoding contract": (
            "Render Quality Color Metadata Contract Mismatch"
        ),
        "video color primaries do not match the encoding contract": (
            "Render Quality Color Metadata Contract Mismatch"
        ),
        "video frame rate does not match the encoding contract": (
            "Render Quality Frame Rate Contract Mismatch"
        ),
        "video keyframe interval exceeds the encoding contract": (
            "Render Quality Keyframe Interval Mismatch"
        ),
        "rendered video could not be inspected": "Render Quality Inspection Failed",
    }.get(warning)
    return tr(translation_key) if translation_key else warning


def _render_render_quality_reports(render_quality_reports, show_paths=True):
    for report in render_quality_reports or []:
        if not isinstance(report, dict):
            continue
        video_path = str(report.get("video_path") or "").strip()
        prefix = f"{video_path}: " if show_paths and video_path else ""
        raw_warnings = report.get("warnings") or []
        if isinstance(raw_warnings, str):
            raw_warnings = [raw_warnings]
        raw_warning_texts = {
            str(item).strip() for item in raw_warnings if str(item).strip()
        }
        warnings = list(
            dict.fromkeys(
                _render_quality_warning_text(item)
                for item in raw_warnings
                if str(item).strip()
            )
        )
        color_consistency = report.get("color_consistency")
        color_inconsistent = (
            isinstance(color_consistency, dict)
            and color_consistency.get("status") == "mixed"
        )
        if color_inconsistent:
            warnings.append(tr("Render Quality Color Inconsistent"))

        details = []
        resolution = report.get("resolution")
        if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
            details.append(f"{resolution[0]}x{resolution[1]}")
        try:
            fps = float(report.get("fps") or 0)
        except (TypeError, ValueError):
            fps = 0.0
        if fps > 0:
            details.append(f"{fps:g} FPS")
        for key, label in (
            ("duration", "Video Duration"),
            ("expected_duration", "Audio Duration"),
        ):
            try:
                duration = float(report.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if duration > 0:
                details.append(f"{tr(label)}: {duration:g}s")
        if report.get("has_audio"):
            details.append(tr("Audio Track Present"))
        review_times = []
        for key in (
            "near_black_sample_times",
            "caption_over_black_sample_times",
        ):
            sample_times = report.get(key)
            if not isinstance(sample_times, (list, tuple)):
                continue
            for sample_time in sample_times:
                try:
                    seconds = float(sample_time)
                except (TypeError, ValueError):
                    continue
                if seconds < 0 or seconds == float("inf"):
                    continue
                if seconds not in review_times:
                    review_times.append(seconds)
        if review_times:
            formatted_times = ", ".join(f"{seconds:g}s" for seconds in review_times)
            details.append(f"{tr('Render Quality Review Times')}: {formatted_times}")
        if details:
            st.caption(f"{prefix}{' | '.join(details)}")
        if warnings:
            st.warning(f"{prefix}{' | '.join(warnings)}")
        elif report.get("ok") is True:
            st.success(f"{prefix}{tr('Render Quality Passed')}")
        if raw_warning_texts & {
            "sampled frames appear to contain captions over a black visual",
            "some sampled frames appear to contain captions over a black visual",
        }:
            st.caption(tr("Render Quality Caption Over Black Help"))
        elif raw_warning_texts & {
            "sampled frames are near-black",
            "some sampled frames are near-black",
        }:
            st.caption(tr("Render Quality Near Black Help"))
        if "audio track is missing" in raw_warning_texts:
            st.caption(tr("Render Quality Missing Audio Help"))
        elif "sampled audio is near-silent" in raw_warning_texts:
            st.caption(tr("Render Quality Near Silent Audio Help"))
        if raw_warning_texts & {
            "encoding contract could not be inspected",
            "video codec does not match the encoding contract",
            "video pixel format does not match the encoding contract",
            "video color space does not match the encoding contract",
            "video color transfer does not match the encoding contract",
            "video color primaries do not match the encoding contract",
            "video frame rate does not match the encoding contract",
            "video keyframe interval exceeds the encoding contract",
        }:
            st.caption(tr("Render Quality Encoding Contract Help"))
        if color_inconsistent:
            st.caption(tr("Render Quality Color Inconsistent Help"))


def _visual_review_preview_items(review_package, max_items=6):
    """Return existing local images without exposing their paths in the UI."""
    if not isinstance(review_package, dict):
        return []
    try:
        max_items = int(max_items)
    except (TypeError, ValueError):
        return []
    if max_items <= 0:
        return []

    preview_items = []
    seen_paths = set()

    def add_preview(label_key, candidate_path):
        if len(preview_items) >= max_items:
            return
        image_path = str(candidate_path or "").strip()
        if not image_path or image_path in seen_paths or not os.path.isfile(image_path):
            return
        seen_paths.add(image_path)
        preview_items.append((label_key, image_path))

    safe_zone_snapshots = review_package.get("safe_zone_snapshots") or []
    if isinstance(safe_zone_snapshots, (list, tuple)):
        for snapshot in safe_zone_snapshots:
            if isinstance(snapshot, dict):
                add_preview("Subtitle Safe Zone", snapshot.get("snapshot_path"))

    gallery = review_package.get("gallery")
    frame_paths = gallery.get("frame_paths") if isinstance(gallery, dict) else []
    if isinstance(frame_paths, (list, tuple)):
        for frame_path in frame_paths:
            add_preview("Sampled Frame", frame_path)

    return preview_items


def _render_visual_review_previews(review_package):
    preview_items = _visual_review_preview_items(review_package)
    if not preview_items:
        st.caption(tr("No Visual Review Previews"))
        return

    preview_columns = st.columns(min(3, len(preview_items)))
    for index, (label_key, image_path) in enumerate(preview_items):
        preview_columns[index % len(preview_columns)].image(
            image_path,
            caption=tr(label_key),
            width="stretch",
        )


def _render_history_visual_review(job, history_key_prefix):
    task_id = str(job.get("task_id") or "").strip() if isinstance(job, dict) else ""
    review_key = f"{history_key_prefix}_visual_review"
    if st.button(
        tr("Create Visual Review"),
        key=f"{history_key_prefix}_create_visual_review",
        disabled=not task_id,
    ):
        try:
            with st.spinner(tr("Creating Visual Review")):
                st.session_state[review_key] = (
                    render_quality.build_task_visual_review_package(task_id)
                )
        except Exception:
            st.session_state[review_key] = {"ok": False}

    review_package = st.session_state.get(review_key)
    if not isinstance(review_package, dict):
        return
    if review_package.get("ok"):
        st.success(tr("Visual Review Ready"))
        _render_render_quality_reports(
            review_package.get("quality_reports"),
            show_paths=False,
        )
        _render_visual_review_previews(review_package)
    else:
        st.warning(tr("Visual Review Failed"))


def _render_latest_single_result_visual_review():
    task_id = str(
        st.session_state.get("latest_single_result_task_id") or ""
    ).strip()
    if not task_id:
        return
    _render_history_visual_review(
        {"task_id": task_id},
        history_key_prefix=f"single_{task_id}",
    )


def _render_visual_pacing_report(report):
    """Show only actionable visual-rhythm feedback from a finished task."""
    if not isinstance(report, dict) or not report.get("available"):
        return

    try:
        seconds_per_visual = float(report.get("seconds_per_visual"))
        visual_count = int(report.get("planned_visual_count"))
    except (TypeError, ValueError):
        return
    if not 0 < seconds_per_visual < float("inf") or visual_count <= 0:
        return

    summary_values = {
        "seconds_per_visual": f"{seconds_per_visual:g}",
        "visual_count": visual_count,
    }
    if report.get("pacing_status") == "balanced":
        st.caption(tr("Visual Pacing Balanced").format(**summary_values))
    else:
        st.warning(tr("Visual Pacing Needs Review").format(**summary_values))

    try:
        scene_count = max(0, int(report.get("scene_count", 0)))
    except (TypeError, ValueError):
        scene_count = 0
    if report.get("scene_coverage_status") in {"partial", "sparse"}:
        st.warning(
            tr("Visual Pacing Scene Coverage Needs Review").format(
                scene_count=scene_count,
                visual_count=visual_count,
            )
        )

    try:
        cut_count = max(0, int(report.get("planned_cut_count", 0)))
        aligned_cuts = int(report.get("cue_alignment_opportunity_count", 0))
    except (TypeError, ValueError):
        return
    if cut_count:
        st.caption(
            tr("Visual Pacing Cue Alignment").format(
                aligned_cuts=max(0, min(cut_count, aligned_cuts)),
                cut_count=cut_count,
            )
        )


def _render_visual_reuse_report(report):
    """Render the optional cross-task scan without changing production settings."""
    if not isinstance(report, dict):
        return
    if report.get("status") == "no_final_videos":
        st.info(tr("Visual Reuse No Videos"))
        return
    if report.get("status") != "completed":
        return

    try:
        scanned_count = max(0, int(report.get("scanned_video_count", 0)))
        duplicate_count = max(0, int(report.get("duplicate_pair_count", 0)))
        unreadable_count = max(0, int(report.get("unreadable_video_count", 0)))
    except (TypeError, ValueError):
        return
    if not scanned_count:
        st.info(tr("Visual Reuse No Videos"))
        return

    st.caption(
        tr("Visual Reuse Summary").format(
            scanned_count=scanned_count,
            duplicate_count=duplicate_count,
        )
    )
    if duplicate_count:
        st.warning(tr("Visual Reuse Found"))
    else:
        st.success(tr("Visual Reuse Clear"))
    if unreadable_count:
        st.warning(tr("Visual Reuse Unreadable").format(count=unreadable_count))


def _safe_subtitle_download_path(job):
    if not isinstance(job, dict):
        return None

    subtitle_path = job.get("subtitle_path")
    if not isinstance(subtitle_path, str) or not subtitle_path.strip():
        return None

    candidate_path = os.path.realpath(subtitle_path)
    task_root = os.path.realpath(os.path.join(utils.storage_dir(), "tasks"))
    try:
        candidate_for_compare = os.path.normcase(candidate_path)
        root_for_compare = os.path.normcase(task_root)
        if (
            os.path.commonpath((candidate_for_compare, root_for_compare))
            != root_for_compare
        ):
            return None
    except ValueError:
        return None

    if os.path.splitext(candidate_path)[1].lower() not in {".srt", ".ass"}:
        return None
    if not os.path.isfile(candidate_path):
        return None
    return candidate_path


def _render_subtitle_download(job, key_prefix):
    subtitle_path = _safe_subtitle_download_path(job)
    if not subtitle_path:
        return

    try:
        with open(subtitle_path, "rb") as subtitle_file:
            subtitle_data = subtitle_file.read()
    except OSError:
        return

    st.download_button(
        tr("Download Subtitles"),
        data=subtitle_data,
        file_name=os.path.basename(subtitle_path),
        mime="text/plain",
        key=f"{key_prefix}_download_subtitles",
    )


def _load_subtitle_suspicion_report(job):
    subtitle_path = _safe_subtitle_download_path(job)
    if not subtitle_path:
        return None

    report_path = os.path.join(os.path.dirname(subtitle_path), "subtitle.review.json")
    try:
        with open(report_path, "r", encoding="utf-8") as report_file:
            report = json.load(report_file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        return None

    items = []
    for item in report["items"][:100]:
        if not isinstance(item, dict):
            continue
        subtitle_text = item.get("subtitle_text")
        if not isinstance(subtitle_text, str) or not subtitle_text.strip():
            continue
        time_range = item.get("time_range")
        suggested_text = item.get("suggested_text")
        items.append(
            {
                "time_range": str(time_range or "").strip()[:80],
                "subtitle_text": " ".join(subtitle_text.split())[:300],
                "suggested_text": (
                    " ".join(suggested_text.split())[:300]
                    if isinstance(suggested_text, str) and suggested_text.strip()
                    else "-"
                ),
            }
        )

    if not items:
        return None
    return {"suspicious_count": len(items), "items": items}


def _render_subtitle_suspicion_report(job):
    report = _load_subtitle_suspicion_report(job)
    if not report:
        return

    st.warning(
        tr("Subtitle Review Needed").format(
            count=report["suspicious_count"],
        )
    )
    st.dataframe(
        [
            {
                tr("Subtitle Time"): item["time_range"],
                tr("Subtitle Text"): item["subtitle_text"],
                tr("Suggested Text"): item["suggested_text"],
            }
            for item in report["items"]
        ],
        width="stretch",
        hide_index=True,
    )


def _load_subtitle_readability_report(job):
    subtitle_path = _safe_subtitle_download_path(job)
    if not subtitle_path:
        return None

    task_directory = os.path.dirname(subtitle_path)
    candidate_paths = (
        os.path.join(task_directory, "subtitle.render.srt"),
        os.path.join(task_directory, "subtitle.srt"),
    )
    checked_paths = set()
    for candidate_path in candidate_paths:
        safe_candidate_path = _safe_subtitle_download_path(
            {"subtitle_path": candidate_path}
        )
        if not safe_candidate_path or safe_candidate_path in checked_paths:
            continue
        checked_paths.add(safe_candidate_path)
        try:
            subtitle_items = subtitle.file_to_subtitles(safe_candidate_path)
            report = voice.inspect_subtitle_readability(subtitle_items)
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(report, dict) and report.get("checked_count", 0) > 0:
            return report
    return None


def _render_subtitle_readability_report(job):
    report = _load_subtitle_readability_report(job)
    if not report:
        return

    cue_count = report.get("checked_count", 0)
    issue_count = sum(
        report.get(key, 0)
        for key in (
            "too_many_lines_count",
            "overlong_line_count",
            "high_reading_speed_count",
        )
    )
    if issue_count:
        st.warning(
            tr("Subtitle Layout Needs Review").format(
                cue_count=cue_count,
                issue_count=issue_count,
            )
        )
    else:
        st.caption(tr("Subtitle Layout Ready").format(cue_count=cue_count))


def _material_review_provider(job):
    """Return a provider only when a task can be attributed unambiguously."""
    if not isinstance(job, dict):
        return None

    source = str(job.get("video_source") or "").strip().casefold()
    if source and source not in {"local", "multi"}:
        return source

    providers = set()
    for record in job.get("material_attributions") or []:
        if not isinstance(record, dict):
            continue
        provider = str(record.get("provider") or "").strip().casefold()
        if provider:
            providers.add(provider)
    return next(iter(providers)) if len(providers) == 1 else None


def _material_source_names(job):
    """Return the distinct material sources recorded for a generation result."""
    if not isinstance(job, dict):
        return []

    providers = set()
    for record in job.get("material_attributions") or []:
        if not isinstance(record, dict):
            continue
        provider = str(record.get("provider") or "").strip().casefold()
        if provider:
            providers.add(provider)

    if not providers:
        source = str(job.get("video_source") or "").strip().casefold()
        if source and source != "multi":
            providers.add(source)

    provider_labels = {
        "archive_org": "Internet Archive",
        "dvids": "DVIDS",
        "local": tr("Local Materials"),
        "loc": "Library of Congress",
        "nasa": "NASA",
        "noaa_ocean": "NOAA Ocean",
        "openmontage": "OpenMontage",
        "wikimedia": "Wikimedia Commons",
    }
    return sorted(
        {
            provider_labels.get(provider, provider.replace("_", " ").title())
            for provider in providers
        },
        key=str.casefold,
    )


def _render_material_sources(job):
    source_names = _material_source_names(job)
    if source_names:
        st.caption(
            f"{tr('Material Sources Used')}: {', '.join(source_names)}"
        )


def _render_material_review_feedback(job, key_prefix):
    task_id = str(job.get("task_id") or "").strip() if isinstance(job, dict) else ""
    if not task_id:
        return

    provider = _material_review_provider(job)
    reason_labels = {
        "unrelated_material": "Unrelated Material",
        "repeated_visual": "Repeated Visual",
        "poor_crop": "Poor Crop",
    }
    localized_reasons = {
        tr(label): reason for reason, label in reason_labels.items()
    }
    ui_language = st.session_state.get("ui_language", "en")
    with _render_history_detail_section("Material Review", "Material Review Help"):
        if provider:
            st.caption(tr("Material Review Provider").format(provider=provider))
        else:
            st.caption(tr("Material Review Provider Unavailable"))
        rejection_reason_label = st.selectbox(
            tr("Material Review Reason"),
            options=list(localized_reasons),
            key=f"{key_prefix}_material_review_reason_{ui_language}",
        )
        rejection_reason = localized_reasons[rejection_reason_label]
        approved_column, rejected_column = st.columns(2)
        if approved_column.button(
            tr("Materials Look Good"),
            key=f"{key_prefix}_approve_materials",
            width="stretch",
        ):
            result = review_feedback.record_review_decision(
                task_id,
                "approved",
                material_provider=provider,
            )
            if result.get("ok"):
                st.success(tr("Material Feedback Saved"))
            else:
                st.warning(tr("Material Feedback Save Failed"))
        if rejected_column.button(
            tr("Materials Need Improvement"),
            key=f"{key_prefix}_reject_materials",
            width="stretch",
        ):
            result = review_feedback.record_review_decision(
                task_id,
                "rejected",
                rejection_reason=rejection_reason,
                material_provider=provider,
            )
            if result.get("ok"):
                st.success(tr("Material Feedback Saved"))
            else:
                st.warning(tr("Material Feedback Save Failed"))


def _video_output_urls(value):
    """Return valid output paths from current and legacy history entries."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _render_recent_jobs_detail_list(recent_jobs):
    for index, job in enumerate(recent_jobs):
        history_key_prefix = f"history_{job.get('task_id', '')}"
        subject = job.get("subject") or job.get("task_id") or tr("Untitled")
        status = job.get("status", "")
        created_at = job.get("created_at", "")
        with st.expander(f"{subject} - {status}", expanded=index == 0):
            st.caption(f"{tr('Created At')}: {created_at}")
            _render_recent_job_detail_summary(job)
            if st.button(
                tr("Use as New Draft"),
                key=f"{history_key_prefix}_use_as_new_draft",
                width="stretch",
            ):
                draft_payload = _apply_history_job_as_draft(job)
                st.success(
                    tr("History Draft Applied").format(
                        subject=draft_payload.get("subject") or tr("Untitled")
                    )
                )
                st.rerun()
            output_tab, distribution_tab, quality_tab, metrics_tab = st.tabs(
                [
                    tr("Output"),
                    tr("Distribution"),
                    tr("Quality"),
                    tr("Metrics"),
                ]
            )
            videos = _video_output_urls(job.get("videos"))
            with output_tab:
                with _render_history_detail_section("Video Output"):
                    if videos:
                        for url in videos:
                            if isinstance(url, str) and url.strip():
                                st.video(url)
                    else:
                        st.caption(tr("No Video Output"))
                _render_material_sources(job)
                if _safe_subtitle_download_path(job):
                    with _render_history_detail_section("Subtitles"):
                        _render_subtitle_download(job, history_key_prefix)
                        _render_subtitle_readability_report(job)
                        _render_subtitle_suspicion_report(job)
                _render_partial_success_warning(job)
                _render_failed_generation_warning(job)
                encoder_results = job.get("video_encoder_results") or []
                if encoder_results:
                    with _render_history_detail_section("Video Encoder"):
                        _render_video_encoder_results(encoder_results)
                render_quality_reports = job.get("render_quality_reports") or []
                if render_quality_reports:
                    with _render_history_detail_section("Render Quality"):
                        _render_render_quality_reports(render_quality_reports)
                with _render_history_detail_section("Thumbnail Candidates"):
                    _render_thumbnail_candidates(
                        job.get("thumbnail_candidates"),
                        job.get("thumbnail_candidate_error", ""),
                        key_prefix=f"{history_key_prefix}_thumb",
                    )
                terms = job.get("terms") or []
                if isinstance(terms, str):
                    terms = [term.strip() for term in terms.split(",") if term.strip()]
                if terms:
                    with _render_history_detail_section("Search Queries"):
                        st.text_area(
                            tr("Search Queries"),
                            value="\n".join(str(term) for term in terms),
                            height=100,
                            key=f"history_terms_{job.get('task_id', '')}",
                        )
                cooldown_summary = _cooldown_summary_text(job.get("cooldown"))
                if cooldown_summary:
                    st.caption(cooldown_summary)
            with distribution_tab:
                metadata = job.get("metadata")
                if metadata:
                    with _render_history_detail_section("Social Metadata"):
                        st.text_input(
                            tr("Social Title"),
                            value=metadata.get("title", ""),
                            key=f"{history_key_prefix}_title",
                        )
                        st.text_area(
                            tr("Social Description"),
                            value=metadata.get("caption", ""),
                            height=90,
                            key=f"{history_key_prefix}_caption",
                        )
                        st.text_input(
                            tr("Social Hashtags"),
                            value=" ".join(metadata.get("hashtags", [])),
                            key=f"{history_key_prefix}_hashtags",
                        )
                with _render_history_detail_section("Pending Uploads"):
                    _render_pending_uploads(
                        job,
                        key_prefix=history_key_prefix,
                    )
            with quality_tab:
                visual_pacing = job.get("visual_pacing")
                if visual_pacing:
                    with _render_history_detail_section("Visual Pacing"):
                        _render_visual_pacing_report(visual_pacing)
                with _render_history_detail_section(
                    "Visual Review", "Visual Review Help"
                ):
                    _render_history_visual_review(job, history_key_prefix)
                viral_analysis = job.get("viral_analysis")
                if viral_analysis:
                    with _render_history_detail_section("Viral Analysis"):
                        _render_viral_analysis(
                            viral_analysis,
                            key_prefix=f"history_viral_{job.get('task_id', '')}",
                        )
                else:
                    st.caption(tr("Score Not Available"))
                _render_material_review_feedback(job, history_key_prefix)
            with metrics_tab:
                publish_metrics = job.get("publish_metrics") or {}
                with _render_history_detail_section("Publish Metrics"):
                    request_ids = _upload_post_request_ids(job)
                    sync_disabled = not bool(job.get("task_id")) or not bool(request_ids)
                    if st.button(
                        tr("Sync Metrics From Upload-Post"),
                        key=f"{history_key_prefix}_sync_upload_post_metrics",
                        disabled=sync_disabled,
                    ):
                        with st.spinner(tr("Syncing Upload-Post Metrics")):
                            sync_result, sync_message = _sync_upload_post_metrics_for_job(job)
                        if sync_result.outcome == metrics_sync.SYNC_OUTCOME_SYNCED:
                            st.success(sync_message)
                            st.rerun()
                        else:
                            history.update_metrics_sync_state(
                                job.get("task_id", ""),
                                sync_result.outcome,
                            )
                            st.warning(sync_message)
                    metric_cols = st.columns(5)
                    metric_values = {
                        "views": metric_cols[0].number_input(
                            tr("Views"),
                            min_value=0,
                            value=int(publish_metrics.get("views", 0) or 0),
                            step=1,
                            key=f"{history_key_prefix}_views",
                        ),
                        "likes": metric_cols[1].number_input(
                            tr("Likes"),
                            min_value=0,
                            value=int(publish_metrics.get("likes", 0) or 0),
                            step=1,
                            key=f"{history_key_prefix}_likes",
                        ),
                        "comments": metric_cols[2].number_input(
                            tr("Comments"),
                            min_value=0,
                            value=int(publish_metrics.get("comments", 0) or 0),
                            step=1,
                            key=f"{history_key_prefix}_comments",
                        ),
                        "shares": metric_cols[3].number_input(
                            tr("Shares"),
                            min_value=0,
                            value=int(publish_metrics.get("shares", 0) or 0),
                            step=1,
                            key=f"{history_key_prefix}_shares",
                        ),
                        "saves": metric_cols[4].number_input(
                            tr("Saves"),
                            min_value=0,
                            value=int(publish_metrics.get("saves", 0) or 0),
                            step=1,
                            key=f"{history_key_prefix}_saves",
                        ),
                    }
                    captured_at = st.text_input(
                        tr("Captured At"),
                        value=publish_metrics.get("captured_at", ""),
                        key=f"{history_key_prefix}_captured_at",
                    )
                    if st.button(
                        tr("Save Publish Metrics"),
                        key=f"{history_key_prefix}_save_publish_metrics",
                        disabled=not bool(job.get("task_id")),
                    ):
                        metric_values["captured_at"] = captured_at
                        if history.update_publish_metrics(
                            job.get("task_id", ""),
                            metric_values,
                        ):
                            st.success(tr("Publish Metrics Saved"))
                            st.rerun()
            if job.get("error"):
                st.error(job["error"])


def _render_last_metrics_sync_status(last_metrics_sync_run):
    if not last_metrics_sync_run:
        return
    if last_metrics_sync_run.get("status") == "not_configured":
        st.caption(
            tr("Last Metrics Sync Not Configured").format(
                recorded_at=last_metrics_sync_run.get("recorded_at", ""),
            )
        )
        return
    outcomes = last_metrics_sync_run.get("outcomes") or {}
    st.caption(
        tr("Last Metrics Sync Summary").format(
            recorded_at=last_metrics_sync_run.get("recorded_at", ""),
            synced=last_metrics_sync_run.get("synced", 0),
            no_data=outcomes.get("no_data", 0),
            transient=outcomes.get("transient_error", 0),
            permanent=outcomes.get("permanent_error", 0),
        )
    )


def _format_estimated_usd(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    if not 0 <= amount < float("inf"):
        amount = 0.0
    return f"${amount:,.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def _render_monthly_cost_estimate(history_jobs):
    summary = cost_estimate.summarize_monthly_history_costs(history_jobs)
    with st.container(border=True):
        st.write(tr("Monthly Cost Estimate"))
        st.caption(tr("Monthly Cost Estimate Help"))
        cost_cols = st.columns(3)
        cost_cols[0].metric(
            tr("Known Estimated Cost"),
            _format_estimated_usd(summary.get("known_total_usd")),
        )
        cost_cols[1].metric(
            tr("Cost Estimate Jobs"),
            str(summary.get("job_count", 0)),
        )
        unknown_job_count = int(summary.get("unknown_job_count", 0) or 0)
        cost_cols[2].metric(tr("Unknown Cost Jobs"), str(unknown_job_count))
        if unknown_job_count:
            st.warning(
                tr("Cost Estimate Needs Rates").format(count=unknown_job_count)
            )


def _render_recent_jobs_panel():
    _render_panel_intro("Recent Jobs", "Recent Jobs Panel Help")
    history_jobs = history.list_history()
    recent_jobs = history_jobs[:20]
    last_metrics_sync_run = history.get_last_metrics_sync_run()
    if not recent_jobs:
        _render_last_metrics_sync_status(last_metrics_sync_run)
        st.info(tr("No Recent Jobs"))
        return

    if st.button(tr("Clear Recent Jobs"), key="clear_recent_jobs"):
        history.clear_history()
        st.rerun()

    if st.button(
        tr("Sync All Pending Metrics"),
        key="sync_all_pending_upload_post_metrics",
    ):
        _sync_upload_post_service_from_config()
        if not upload_post.upload_post_service.is_configured():
            last_metrics_sync_run = history.record_metrics_sync_run(
                metrics_sync.empty_metrics_sync_summary(),
                status="not_configured",
            )
            st.warning(tr("Upload-Post Not Configured"))
        else:
            with st.spinner(tr("Syncing Upload-Post Metrics")):
                metrics_summary = metrics_sync.sync_pending_publish_metrics(
                    _sync_upload_post_metrics_for_job
                )
            last_metrics_sync_run = history.record_metrics_sync_run(metrics_summary)
            summary_message = tr("Pending Metrics Sync Summary").format(
                synced=metrics_summary.get("synced", 0),
                skipped=metrics_summary.get("skipped", 0),
                errors=len(metrics_summary.get("errors") or []),
            )
            if metrics_summary.get("errors"):
                st.warning(summary_message)
            else:
                st.success(summary_message)

    _render_last_metrics_sync_status(last_metrics_sync_run)

    _render_monthly_cost_estimate(history_jobs)
    _render_recent_jobs_compact_summary(recent_jobs)
    _render_history_performance_snapshot(recent_jobs)
    _render_publish_insights(history_jobs)
    st.write(tr("Visual Reuse Check"))
    st.caption(tr("Visual Reuse Check Help"))
    if st.button(tr("Check Visual Reuse"), key="check_visual_reuse"):
        with st.spinner(tr("Checking Visual Reuse")):
            st.session_state["visual_reuse_report"] = (
                visual_duplicates.find_cross_task_visual_duplicates()
            )
    _render_visual_reuse_report(st.session_state.get("visual_reuse_report"))
    _render_recent_jobs_summary_table(recent_jobs)

    calibration_report = quality_calibration.build_quality_gate_calibration_report(
        recent_jobs,
        current_threshold=st.session_state.get(
            "viral_quality_gate_threshold",
            content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
        ),
    )
    if calibration_report.get("sample_count"):
        st.write(tr("Quality Gate Calibration"))
        has_sufficient_samples = bool(
            calibration_report.get("has_sufficient_samples")
        )
        if has_sufficient_samples:
            st.caption(
                tr("Quality Gate Calibration Summary").format(
                    samples=calibration_report.get("sample_count", 0),
                    threshold=calibration_report.get(
                        "recommended_threshold",
                        content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
                    ),
                )
            )
        st.caption(
            f"{tr('Quality Gate Recommendation')}: "
            f"{calibration_report.get('recommendation', '')}"
        )
        strong_subjects = calibration_report.get("strong_subjects") or []
        weak_subjects = calibration_report.get("weak_subjects") or []
        if strong_subjects and weak_subjects:
            st.caption(
                tr("Content Performance Feedback").format(
                    strong=", ".join(strong_subjects),
                    weak=", ".join(weak_subjects),
                )
            )
        recommended_threshold = calibration_report.get("recommended_threshold")
        if st.button(
            tr("Apply Recommended Threshold"),
            key="apply_recommended_quality_gate_threshold",
            disabled=not has_sufficient_samples,
        ):
            if has_sufficient_samples and recommended_threshold is not None:
                st.session_state["viral_quality_gate_threshold"] = recommended_threshold
                config.app["viral_quality_gate_threshold"] = recommended_threshold
                st.success(
                    tr("Recommended Threshold Applied").format(
                        threshold=recommended_threshold
                    )
                )

    with st.expander(tr("Recent Jobs Details"), expanded=False):
        _render_recent_jobs_detail_list(recent_jobs)


def _upload_post_request_ids(job):
    request_ids = []
    pending_uploads = job.get("pending_uploads") if isinstance(job, dict) else None
    if not isinstance(pending_uploads, list):
        return request_ids
    for pending_upload in pending_uploads:
        if not isinstance(pending_upload, dict):
            continue
        result = pending_upload.get("result") or {}
        if not isinstance(result, dict):
            continue
        request_id = str(result.get("request_id") or "").strip()
        if request_id:
            request_ids.append(request_id)
    return request_ids


def _sync_upload_post_metrics_for_job(job):
    _sync_upload_post_service_from_config()
    if not upload_post.upload_post_service.is_configured():
        return (
            metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_PERMANENT_ERROR),
            tr("Upload-Post Not Configured"),
        )

    totals = {
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "saves": 0,
    }
    platform_totals = {}
    has_metrics = False
    saw_transient_error = False
    saw_permanent_error = False
    for request_id in _upload_post_request_ids(job):
        analytics = upload_post.upload_post_service.get_post_analytics(request_id)
        if not isinstance(analytics, dict) or not analytics.get("success"):
            logger.warning(f"Upload-Post metrics sync failed for {request_id}")
            if isinstance(analytics, dict) and analytics.get("retryable"):
                saw_transient_error = True
            else:
                saw_permanent_error = True
            continue
        if not upload_post.has_post_metrics(analytics):
            continue
        metrics = upload_post.aggregate_post_metrics(analytics)
        has_metrics = True
        for field in totals:
            totals[field] += int(metrics.get(field, 0) or 0)
        for platform, platform_metrics in upload_post.extract_post_platform_metrics(
            analytics
        ).items():
            target = platform_totals.setdefault(
                platform,
                {field: 0 for field in totals},
            )
            for field in totals:
                target[field] += int(platform_metrics.get(field, 0) or 0)

    metrics_payload = {**totals}
    if platform_totals:
        metrics_payload["platform_metrics"] = platform_totals
    if has_metrics and history.update_publish_metrics(
        job.get("task_id", ""), metrics_payload
    ):
        return (
            metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_SYNCED),
            tr("Upload-Post Metrics Synced"),
        )
    if has_metrics or saw_permanent_error:
        return (
            metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_PERMANENT_ERROR),
            tr("Sync Metrics Failed"),
        )
    if saw_transient_error:
        return (
            metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_TRANSIENT_ERROR),
            tr("Sync Metrics Retry Scheduled"),
        )
    return (
        metrics_sync.SyncJobResult(metrics_sync.SYNC_OUTCOME_NO_DATA),
        tr("Sync Metrics No Data"),
    )


def _render_content_intelligence_panel(params):
    _render_panel_intro("Content Intelligence", "Content Intelligence Panel Help")
    content_platforms = [
        ("TikTok", "tiktok"),
        ("YouTube Shorts", "youtube_shorts"),
        ("Instagram Reels", "instagram_reels"),
    ]
    content_cols = st.columns([0.45, 0.25, 0.3])
    with content_cols[0]:
        st.text_input(
            tr("Content Niche"),
            value=st.session_state.get("content_niche") or params.video_subject,
            key="content_niche",
            help=tr("Content Niche Help"),
        )
    with content_cols[1]:
        content_platform_index = st.selectbox(
            tr("Content Platform"),
            options=range(len(content_platforms)),
            format_func=lambda x: content_platforms[x][0],
            key="content_platform_select",
        )
    with content_cols[2]:
        st.text_input(
            tr("Target Audience"),
            key="content_target_audience",
        )

    plan_cols = st.columns([0.24, 0.16, 0.16, 0.2, 0.24])
    with plan_cols[0]:
        st.text_input(
            tr("Content Tone"),
            key="content_tone",
        )
    with plan_cols[1]:
        st.selectbox(
            tr("Content Days"),
            options=[7, 14],
            key="content_plan_days",
        )
    with plan_cols[2]:
        st.selectbox(
            tr("Daily Content Count"),
            options=[1, 2, 3],
            key="content_daily_count",
        )
    with plan_cols[3]:
        st.slider(
            tr("Idea Count"),
            min_value=1,
            max_value=30,
            key="content_idea_count",
        )
    with plan_cols[4]:
        st.checkbox(
            tr("Use Static Trend Context"),
            key="content_use_trend_context",
            help=tr("Use Static Trend Context Help"),
        )
        trend_source_options = [
            content_intelligence.TREND_SOURCE_STATIC,
            content_intelligence.TREND_SOURCE_RSS,
        ]
        if st.session_state.get("content_trend_source") not in trend_source_options:
            st.session_state["content_trend_source"] = (
                content_intelligence.TREND_SOURCE_STATIC
            )
        trend_source_labels = {
            content_intelligence.TREND_SOURCE_STATIC: tr("Static Planning Context"),
            content_intelligence.TREND_SOURCE_RSS: tr("RSS Headlines Context"),
        }
        st.selectbox(
            tr("Trend Context Source"),
            options=trend_source_options,
            format_func=lambda source: trend_source_labels.get(source, source),
            key="content_trend_source",
            help=tr("Trend Context Source Help"),
            disabled=not st.session_state.get("content_use_trend_context", False),
        )

    if st.button(tr("Generate Content Plan"), key="generate_content_plan"):
        content_subject = (
            st.session_state.get("content_niche") or params.video_subject or ""
        ).strip()
        if not content_subject and not params.video_script:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            with st.spinner(tr("Generating Content Plan")):
                st.session_state["content_plan"] = (
                    content_intelligence.generate_content_plan(
                        video_subject=content_subject,
                        video_script=params.video_script,
                        language=params.video_language or "auto",
                        platform=content_platforms[content_platform_index][1],
                        target_audience=st.session_state.get(
                            "content_target_audience", ""
                        ),
                        tone=st.session_state.get("content_tone", ""),
                        days=st.session_state.get("content_plan_days", 7),
                        daily_count=st.session_state.get("content_daily_count", 1),
                        idea_count=st.session_state.get("content_idea_count", 7),
                        use_trend_context=st.session_state.get(
                            "content_use_trend_context", False
                        ),
                        trend_source=st.session_state.get(
                            "content_trend_source",
                            content_intelligence.TREND_SOURCE_STATIC,
                        )
                        if st.session_state.get("content_use_trend_context", False)
                        else "none",
                    )
                )

    content_plan = st.session_state.get("content_plan")
    if not content_plan:
        return

    _render_content_plan_readiness(content_plan)

    warnings = content_plan.get("warnings") or []
    if warnings:
        st.write(tr("Planning Warnings"))
        for warning in warnings:
            st.caption(warning)

    ideas = content_plan.get("ideas") or []
    if ideas:
        st.write(tr("Content Ideas"))
        _render_content_ideas_summary_table(ideas)
        for index, idea in enumerate(ideas, start=1):
            with st.expander(f"{index}. {idea.get('subject', '')}", expanded=False):
                st.caption(f"{tr('Content Angle')}: {idea.get('angle', '')}")
                st.text_area(
                    tr("Content Hook"),
                    value=idea.get("hook", ""),
                    height=70,
                    key=f"content_idea_hook_{index}",
                )
                st.text_area(
                    tr("Content Script Prompt"),
                    value=idea.get("script_prompt", ""),
                    height=100,
                    key=f"content_idea_prompt_{index}",
                )
                st.text_input(
                    tr("Content Search Terms"),
                    value=", ".join(idea.get("search_terms") or []),
                    key=f"content_idea_terms_{index}",
                )
                st.caption(
                    f"{tr('Content Rationale')}: {idea.get('rationale', '')}"
                )

    calendar = content_plan.get("calendar") or []
    if not calendar:
        return

    st.write(tr("Content Calendar"))
    st.dataframe(
        [
            {
                tr("Content Day"): item.get("day", ""),
                tr("Created At"): item.get("date", ""),
                tr("Content Ideas"): item.get("subject", ""),
                tr("Content Format"): item.get("format", ""),
                tr("Content Goal"): item.get("goal", ""),
            }
            for item in calendar
        ],
        width="stretch",
        hide_index=True,
    )

    apply_cols = st.columns(2)
    with apply_cols[0]:
        if st.button(
            tr("Apply Calendar to Batch"),
            key="apply_content_calendar_to_batch",
        ):
            st.session_state["batch_subjects"] = "\n".join(
                item.get("subject", "") for item in calendar if item.get("subject")
            )
            st.session_state["use_manual_batch_scripts"] = False
            st.success(tr("Content Plan Applied to Batch"))
            st.rerun()
    with apply_cols[1]:
        if ideas and st.button(
            tr("Apply First Idea to Current Topic"),
            key="apply_first_content_idea",
        ):
            st.session_state["_pending_content_topic"] = {
                "subject": ideas[0].get("subject", ""),
                "script_prompt": ideas[0].get("script_prompt", ""),
            }
            st.success(tr("Content Topic Applied"))
            st.rerun()


def _content_plan_readiness_state(content_plan):
    if not isinstance(content_plan, dict):
        content_plan = {}
    ideas = content_plan.get("ideas") or []
    calendar = content_plan.get("calendar") or []
    warnings = content_plan.get("warnings") or []
    source = str(content_plan.get("source") or tr("Unknown"))

    if not ideas:
        level = "info"
        message = tr("Content Plan Needs Ideas")
        action = tr("Generate Content Plan")
    elif warnings:
        level = "warning"
        message = tr("Content Plan Needs Review")
        action = tr("Review Planning Warnings")
    else:
        level = "success"
        message = tr("Content Plan Ready")
        action = tr("Apply First Idea to Current Topic")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Source"),
                "value": source,
            },
            {
                "label": tr("Content Ideas"),
                "value": str(len(ideas)),
            },
            {
                "label": tr("Content Calendar"),
                "value": str(len(calendar)),
            },
            {
                "label": tr("Planning Warnings"),
                "value": str(len(warnings)),
            },
        ],
    }


def _render_content_plan_readiness(content_plan):
    _render_readiness_review(
        "Content Plan Readiness",
        "Content Plan Readiness Help",
        _content_plan_readiness_state(content_plan),
    )


def _content_ideas_summary_rows(ideas, limit=5):
    rows = []
    for index, idea in enumerate(ideas[:limit], start=1):
        rows.append(
            {
                "#": index,
                tr("Subject"): idea.get("subject", ""),
                tr("Content Angle"): idea.get("angle", ""),
                tr("Content Hook"): idea.get("hook", ""),
            }
        )
    return rows


def _render_content_ideas_summary_table(ideas):
    rows = _content_ideas_summary_rows(ideas)
    if not rows:
        return
    st.write(tr("Content Ideas Summary"))
    st.dataframe(
        rows,
        width="stretch",
        hide_index=True,
    )


def _render_preflight_status_summary(preflight_report, preflight_warning):
    if not preflight_report:
        st.info(f"{tr('Preflight Status')}: {tr('Not Run')}")
    elif preflight_warning:
        st.warning(f"{tr('Preflight Status')}: {tr('Needs Refresh')}")
    else:
        st.success(f"{tr('Preflight Status')}: {tr('Ready')}")


def _preflight_report_summary_items(preflight_report):
    if not isinstance(preflight_report, dict) or not preflight_report:
        return []

    content_plan = preflight_report.get("content_plan") or {}
    script_analysis = preflight_report.get("script_analysis") or {}
    ideas = content_plan.get("ideas") or []
    repeat_matches = preflight_report.get("repeat_matches") or []
    warnings = []
    warnings.extend(content_plan.get("warnings") or [])
    warnings.extend(script_analysis.get("warnings") or [])

    overall_score = _score_value(script_analysis.get("overall_score"))
    return [
        {
            "label": tr("Viral Score"),
            "value": f"{overall_score}/100"
            if overall_score is not None
            else tr("Score Not Available"),
        },
        {
            "label": tr("Content Ideas"),
            "value": str(len(ideas)),
        },
        {
            "label": tr("Repeat Matches"),
            "value": str(len(repeat_matches)),
        },
        {
            "label": tr("Planning Warnings"),
            "value": str(len(warnings)),
        },
    ]


def _render_preflight_compact_summary(preflight_report):
    items = _preflight_report_summary_items(preflight_report)
    if not items:
        return
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _render_content_preflight_panel(params, selected_social_platform):
    _render_panel_intro("Content Preflight", "Content Preflight Help")
    preflight_batch_items = _get_batch_items()
    preflight_inputs = _preflight_input_values(params, preflight_batch_items)
    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        selected_social_platform,
        preflight_inputs["language"],
    )
    _render_preflight_status_summary(preflight_report, preflight_warning)

    if st.button(tr("Analyze Topic"), key="analyze_content_preflight"):
        if not preflight_inputs["video_subject"] and not preflight_inputs["video_script"]:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            with st.spinner(tr("Generating Content Preflight")):
                st.session_state["content_preflight_report"] = (
                    content_quality.build_preflight_report(
                        video_subject=preflight_inputs["video_subject"],
                        video_script=preflight_inputs["video_script"],
                        platform=selected_social_platform,
                        language=preflight_inputs["language"],
                        target_audience=st.session_state.get(
                            "content_target_audience", ""
                        ),
                        tone=st.session_state.get("content_tone", ""),
                        use_trend_context=st.session_state.get(
                            "content_use_trend_context", False
                        ),
                        trend_source=st.session_state.get(
                            "content_trend_source",
                            content_intelligence.TREND_SOURCE_STATIC,
                        ),
                    )
                )

    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        selected_social_platform,
        preflight_inputs["language"],
    )
    if preflight_report and preflight_warning:
        st.warning(preflight_warning)
    script_repeat_warning = _script_repeat_warning_text(
        preflight_report.get("script_repeat_matches")
        if isinstance(preflight_report, dict)
        else []
    )
    if script_repeat_warning:
        st.warning(script_repeat_warning)
    _render_preflight_compact_summary(preflight_report)
    if preflight_report:
        with st.expander(tr("Preflight Details"), expanded=False):
            _render_content_preflight_report(
                preflight_report,
                key_prefix="current_preflight",
            )


def _render_viral_analysis_controls():
    control_cols = st.columns([0.34, 0.34, 0.32])
    with control_cols[0]:
        st.checkbox(
            tr("Auto Viral Analysis After Video"),
            key="auto_viral_analysis_after_video",
            help=tr("Auto Viral Analysis After Video Help"),
        )
    with control_cols[1]:
        st.checkbox(
            tr("Viral Quality Gate Enabled"),
            key="viral_quality_gate_enabled",
            help=tr("Viral Quality Gate Enabled Help"),
        )
    with control_cols[2]:
        st.slider(
            tr("Viral Quality Gate Threshold"),
            min_value=0,
            max_value=100,
            key="viral_quality_gate_threshold",
            help=tr("Viral Quality Gate Threshold Help"),
        )
    config.app["viral_quality_gate_enabled"] = st.session_state[
        "viral_quality_gate_enabled"
    ]
    config.app["viral_quality_gate_threshold"] = st.session_state[
        "viral_quality_gate_threshold"
    ]


def _viral_analysis_summary_items(viral_analysis):
    if not isinstance(viral_analysis, dict) or not viral_analysis:
        return []

    def _score_text(score_key):
        score = _score_value(viral_analysis.get(score_key))
        return f"{score}/100" if score is not None else tr("Score Not Available")

    warnings = viral_analysis.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings] if warnings.strip() else []
    return [
        {
            "label": tr("Viral Score"),
            "value": _score_text("overall_score"),
        },
        {
            "label": tr("Hook Score"),
            "value": _score_text("hook_score"),
        },
        {
            "label": tr("Pacing Score"),
            "value": _score_text("pacing_score"),
        },
        {
            "label": tr("Top Quality Warnings"),
            "value": str(len(warnings)),
        },
    ]


def _render_viral_analysis_compact_summary(viral_analysis):
    items = _viral_analysis_summary_items(viral_analysis)
    if not items:
        return
    summary_cols = st.columns(len(items))
    for col, item in zip(summary_cols, items):
        col.metric(item["label"], item["value"])


def _render_viral_analysis_panel(params, selected_social_platform):
    _render_panel_intro("Viral Analysis", "Viral Analysis Panel Help")
    _render_viral_analysis_controls()

    viral_batch_items = _get_batch_items()
    viral_inputs = _viral_analysis_input_values(params, viral_batch_items)

    if st.button(tr("Generate Viral Analysis"), key="generate_viral_analysis"):
        if not viral_inputs["video_subject"] and not viral_inputs["video_script"]:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            current_metadata = st.session_state.get("social_metadata") or {}
            with st.spinner(tr("Generating Viral Analysis")):
                st.session_state["viral_analysis"] = (
                    viral_analyzer.analyze_viral_potential(
                        video_subject=viral_inputs["video_subject"],
                        video_script=viral_inputs["video_script"],
                        title=current_metadata.get("title", ""),
                        video_duration_sec=None,
                        target_platforms=[selected_social_platform],
                        language=viral_inputs["language"],
                        social_caption=current_metadata.get("caption", ""),
                        hashtags=current_metadata.get("hashtags"),
                    )
                )

    current_viral_analysis = st.session_state.get("viral_analysis")
    _render_viral_analysis_compact_summary(current_viral_analysis)
    if current_viral_analysis:
        with st.expander(tr("Viral Analysis Details"), expanded=False):
            _render_viral_analysis(
                current_viral_analysis,
                key_prefix="current_viral",
            )

    if current_viral_analysis and params.video_script:
        if st.button(
            tr("Improve Script"),
            key="improve_script",
            help=tr("Improve Script Help"),
        ):
            with st.spinner(tr("Improving Script")):
                current_metadata = st.session_state.get("social_metadata") or {}
                rewrite_suggestion = content_quality.suggest_improved_script(
                    video_subject=params.video_subject,
                    video_script=params.video_script,
                    viral_analysis=current_viral_analysis,
                    platform=selected_social_platform,
                    language=params.video_language or "auto",
                    title=current_metadata.get("title", ""),
                    social_caption=current_metadata.get("caption", ""),
                    hashtags=current_metadata.get("hashtags"),
                )
                st.session_state["script_rewrite_suggestion"] = rewrite_suggestion
                st.session_state["script_rewrite_preview"] = rewrite_suggestion.get(
                    "improved_script", ""
                )

    rewrite_suggestion = st.session_state.get("script_rewrite_suggestion")
    if rewrite_suggestion:
        rewrite_error = rewrite_suggestion.get("error")
        if rewrite_error:
            st.warning(tr("Improve Script Unavailable").format(error=rewrite_error))
        else:
            _render_script_rewrite_decision_panel(rewrite_suggestion)
            _render_script_rewrite_comparison(rewrite_suggestion)
            _render_script_rewrite_text_comparison(rewrite_suggestion)
            st.session_state.setdefault(
                "script_rewrite_preview",
                rewrite_suggestion.get("improved_script", ""),
            )
            st.text_area(
                tr("Improved Script Suggestion"),
                height=240,
                key="script_rewrite_preview",
            )
            decision_cols = st.columns(2)
            if decision_cols[0].button(
                tr("Apply Improved Script"),
                key="apply_improved_script",
            ):
                applied_script = st.session_state.get(
                    "script_rewrite_preview",
                    rewrite_suggestion.get("improved_script", ""),
                )
                st.session_state["video_script"] = applied_script
                improved_analysis = rewrite_suggestion.get("improved_analysis")
                if _should_apply_improved_analysis(applied_script, rewrite_suggestion):
                    st.session_state["viral_analysis"] = improved_analysis
                st.session_state["script_rewrite_suggestion"] = None
                st.success(tr("Improved Script Applied"))
                st.rerun()
            if decision_cols[1].button(
                tr("Keep Original Script"),
                key="keep_original_script",
            ):
                st.session_state["script_rewrite_suggestion"] = None
                st.session_state.pop("script_rewrite_preview", None)
                st.rerun()


def _enabled_video_source_count():
    enabled_sources = config.app.get("enabled_video_sources", [])
    if isinstance(enabled_sources, str):
        enabled_sources = [source.strip() for source in enabled_sources.split(",")]
    return len([source for source in enabled_sources if source])


def _has_config_secret(section, key="api_key"):
    value = section.get(key, "") if isinstance(section, dict) else ""
    return bool(str(value or "").strip())


def _llm_provider_key_name(provider):
    provider = str(provider or "").strip().lower()
    return {
        "openai": "openai_api_key",
        "gemini": "gemini_api_key",
        "grok": "grok_api_key",
        "groq": "groq_api_key",
        "qwen": "qwen_api_key",
        "moonshot": "moonshot_api_key",
        "deepseek": "deepseek_api_key",
        "azure": "azure_api_key",
        "oneapi": "oneapi_api_key",
        "pollinations": "pollinations_api_key",
        "aimlapi": "aimlapi_api_key",
        "aihubmix": "aihubmix_api_key",
        "minimax": "minimax_api_key",
        "modelscope": "modelscope_api_key",
        "volcengine": "volcengine_api_key",
        "mimo": "mimo_api_key",
        "evolink": "evolink_api_key",
    }.get(provider, "")


def _accounts_api_readiness_state():
    llm_provider = str(config.app.get("llm_provider", "") or "").strip() or "openai"
    llm_key_name = _llm_provider_key_name(llm_provider)
    llm_ready = bool(str(config.app.get(llm_key_name, "") or "").strip()) if llm_key_name else False
    pexels_count = len(config.app.get("pexels_api_keys") or [])
    pixabay_count = len(config.app.get("pixabay_api_keys") or [])
    coverr_count = len(config.app.get("coverr_api_keys") or [])
    dvids_count = len(config.app.get("dvids_api_keys") or [])
    vecteezy_count = (
        len(config.app.get("vecteezy_api_keys") or [])
        if str(config.app.get("vecteezy_account_id") or "").strip().isdigit()
        else 0
    )
    media_key_count = (
        pexels_count + pixabay_count + coverr_count + dvids_count + vecteezy_count
    )
    source_health = provider_health.build_video_source_health()
    video_source_count = source_health["enabled_count"]
    ready_source_count = source_health["ready_count"]
    missing_source_rows = [
        row
        for row in source_health["sources"]
        if row["enabled"] and row["status"] == "needs_configuration"
    ]
    elevenlabs_ready = _has_config_secret(getattr(config, "elevenlabs", {}))
    chatterbox_ready = bool(
        str((getattr(config, "chatterbox", {}) or {}).get("base_url", "") or "").strip()
    )
    tts_ready = elevenlabs_ready or chatterbox_ready
    upload_enabled = bool(st.session_state.get("upload_post_enabled"))
    upload_ready = bool(str(config.app.get("upload_post_api_key", "") or "").strip())

    if not llm_ready:
        level = "warning"
        message = tr("Accounts LLM Needs Key")
        action = tr("Review LLM Settings")
    elif not media_key_count and video_source_count <= 0:
        level = "warning"
        message = tr("Accounts Media Needs Source")
        action = tr("Review Media Sources")
    elif missing_source_rows:
        level = "warning"
        message = tr("Accounts Source Needs Configuration").format(
            sources=", ".join(tr(row["label"]) for row in missing_source_rows)
        )
        action = tr("Review Media Sources")
    elif upload_enabled and not upload_ready:
        level = "warning"
        message = tr("Accounts Upload Needs Key")
        action = tr("Review Publishing Settings")
    else:
        level = "success"
        message = tr("Accounts Ready")
        action = tr("Optional")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("LLM Provider"),
                "value": tr("Ready") if llm_ready else tr("Needs Input"),
            },
            {
                "label": tr("Media API Keys"),
                "value": str(media_key_count),
            },
            {
                "label": tr("Video Sources"),
                "value": f"{ready_source_count}/{video_source_count} {tr('Ready')}",
            },
            {
                "label": tr("TTS Providers"),
                "value": tr("Ready") if tts_ready else tr("Optional"),
            },
            {
                "label": tr("Upload-Post"),
                "value": (
                    tr("Ready")
                    if upload_enabled and upload_ready
                    else tr("Enabled")
                    if upload_enabled
                    else tr("Disabled")
                ),
            },
        ],
        "source_health": source_health,
    }


def _render_accounts_api_readiness():
    readiness = _accounts_api_readiness_state()
    _render_readiness_review(
        "Accounts API Readiness",
        "Accounts API Readiness Help",
        readiness,
    )
    source_rows = [
        row
        for row in readiness["source_health"]["sources"]
        if row["enabled"]
    ]
    with st.expander(tr("Video Source Configuration"), expanded=False):
        st.caption(tr("Video Source Configuration Help"))
        status_labels = {
            "ready": tr("Ready"),
            "needs_configuration": tr("Needs Input"),
        }
        st.dataframe(
            [
                {
                    tr("Source"): tr(row["label"]),
                    tr("Status"): status_labels.get(
                        row["status"],
                        tr("Needs Input"),
                    ),
                    tr("API Requirement"): (
                        tr("API Key Required")
                        if row["requires_api_key"]
                        else tr("No API Key Required")
                    ),
                }
                for row in source_rows
            ],
            hide_index=True,
            width="stretch",
        )


def _brand_kit_profiles():
    return [
        {
            "id": "default",
            "name": tr("Brand Kit Default"),
            "tone": tr("Brand Tone Balanced"),
            "quality_threshold": 60,
            "video_preset": tr("Balanced Quality Preset"),
            "cta": tr("Brand CTA Soft"),
        },
        {
            "id": "growth",
            "name": tr("Brand Kit Growth"),
            "tone": tr("Brand Tone Energetic"),
            "quality_threshold": 70,
            "video_preset": tr("High Quality Preset"),
            "cta": tr("Brand CTA Direct"),
        },
        {
            "id": "archive",
            "name": tr("Brand Kit Archive"),
            "tone": tr("Brand Tone Documentary"),
            "quality_threshold": 55,
            "video_preset": tr("Archive Quality Preset"),
            "cta": tr("Brand CTA Informational"),
        },
    ]


def _selected_brand_kit_profile():
    profiles = _brand_kit_profiles()
    selected_id = st.session_state.get("brand_kit_profile", "default")
    for profile in profiles:
        if profile["id"] == selected_id:
            return profile
    return profiles[0]


def _brand_kit_summary_items(profile):
    profile = profile if isinstance(profile, dict) else {}
    return [
        {
            "label": tr("Brand Kit"),
            "value": profile.get("name", tr("Brand Kit Default")),
        },
        {
            "label": tr("Content Tone"),
            "value": profile.get("tone", tr("Brand Tone Balanced")),
        },
        {
            "label": tr("Quality Threshold"),
            "value": str(profile.get("quality_threshold", 60)),
        },
        {
            "label": tr("Video Quality Preset"),
            "value": profile.get("video_preset", tr("Balanced Quality Preset")),
        },
    ]


def _render_brand_kit_panel():
    profiles = _brand_kit_profiles()
    selected_profile = _selected_brand_kit_profile()
    st.selectbox(
        tr("Brand Kit"),
        options=[profile["id"] for profile in profiles],
        format_func=lambda profile_id: next(
            profile["name"] for profile in profiles if profile["id"] == profile_id
        ),
        key="brand_kit_profile",
    )
    selected_profile = _selected_brand_kit_profile()
    _render_workspace_summary_metrics(_brand_kit_summary_items(selected_profile))
    st.caption(f"{tr('Preferred CTA')}: {selected_profile.get('cta', '')}")


def _setup_preset_readiness_state(preset_count, builtin_preset_count):
    source_count = _enabled_video_source_count()
    if preset_count:
        level = "success"
        message = tr("Preset Ready")
        action = tr("Apply Preset")
    elif builtin_preset_count:
        level = "info"
        message = tr("Built-in Presets Available")
        action = tr("Apply Suggested Preset")
    else:
        level = "info"
        message = tr("Preset Needs Save")
        action = tr("Save Current Preset")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Presets"),
                "value": f"{preset_count} {tr('Saved')}",
            },
            {
                "label": tr("Suggested Preset"),
                "value": str(builtin_preset_count),
            },
            {
                "label": tr("Video Sources"),
                "value": f"{source_count} {tr('Enabled')}",
            },
        ],
    }


def _render_setup_preset_readiness(preset_count, builtin_preset_count):
    _render_readiness_review(
        "Preset Readiness",
        "Preset Readiness Help",
        _setup_preset_readiness_state(preset_count, builtin_preset_count),
    )


def _render_setup_workspace_summary():
    try:
        preset_count = len(presets.list_presets())
    except Exception:
        preset_count = 0
    try:
        builtin_preset_count = len(presets.list_builtin_presets())
    except Exception:
        builtin_preset_count = 0

    _render_setup_preset_readiness(preset_count, builtin_preset_count)


def _render_planning_quality_summary(params, selected_social_platform):
    content_plan = st.session_state.get("content_plan") or {}
    idea_count = len(content_plan.get("ideas") or [])
    preflight_status, viral_status, rewrite_status = _quality_check_status_values(
        params,
        selected_social_platform,
    )
    _render_workspace_summary_metrics(
        [
            {
                "label": "Content Ideas",
                "value": str(idea_count),
            },
            {
                "label": "Preflight Status",
                "value": preflight_status,
            },
            {
                "label": "Viral Analysis Status",
                "value": viral_status,
            },
            {
                "label": "Rewrite Status",
                "value": rewrite_status,
            },
        ]
    )


def _render_distribution_workspace_summary():
    _render_distribution_readiness_review()


def _current_distribution_input_values():
    return {
        "video_subject": _session_text_value("video_subject"),
        "video_script": _session_text_value("video_script"),
        "language": st.session_state.get("video_language", "auto") or "auto",
    }


def _distribution_social_metadata_state():
    social_metadata = st.session_state.get("social_metadata") or {}
    has_social_metadata = bool(social_metadata) if isinstance(social_metadata, dict) else False
    current_inputs = _current_distribution_input_values()
    social_metadata_warning = _social_metadata_warning_text(
        st.session_state.get("social_metadata_fingerprint"),
        current_inputs["video_subject"],
        current_inputs["video_script"],
        _current_social_platform(),
        current_inputs["language"],
    )
    if has_social_metadata and social_metadata_warning:
        return {
            "ready": False,
            "status": tr("Needs Refresh"),
            "action": tr("Generate Social Metadata"),
            "message": tr("Distribution Needs Social Metadata"),
        }
    if has_social_metadata:
        return {
            "ready": True,
            "status": tr("Ready"),
            "action": tr("Optional"),
            "message": "",
        }
    return {
        "ready": False,
        "status": tr("Not Run"),
        "action": tr("Generate Social Metadata"),
        "message": tr("Distribution Needs Social Metadata"),
    }


def _distribution_workflow_steps():
    social_metadata_state = _distribution_social_metadata_state()
    upload_enabled = bool(st.session_state.get("upload_post_enabled"))
    auto_upload_enabled = bool(st.session_state.get("upload_post_auto_upload"))
    youtube_privacy_labels = {
        upload_post.YOUTUBE_PRIVACY_PRIVATE: tr("Private Upload"),
        upload_post.YOUTUBE_PRIVACY_UNLISTED: tr("Unlisted Upload"),
        upload_post.YOUTUBE_PRIVACY_PUBLIC: tr("Public Upload"),
    }
    youtube_privacy = st.session_state.get(
        "upload_post_youtube_privacy_status",
        config.app.get(
            "upload_post_youtube_privacy_status",
            upload_post.YOUTUBE_PRIVACY_UNLISTED,
        ),
    )
    public_upload_needs_approval = (
        upload_enabled
        and youtube_privacy == upload_post.YOUTUBE_PRIVACY_PUBLIC
        and not st.session_state.get("upload_post_allow_public_youtube")
    )

    if public_upload_needs_approval:
        privacy_status = tr("Needs Approval")
        privacy_action = tr("Allow Public YouTube Upload")
    else:
        privacy_status = youtube_privacy_labels.get(youtube_privacy, tr("Unlisted Upload"))
        privacy_action = tr("Review Publishing Settings")

    return [
        {
            "label": tr("Social Metadata"),
            "status": social_metadata_state["status"],
            "action": social_metadata_state["action"],
        },
        {
            "label": tr("Publishing Settings"),
            "status": tr("Enabled") if upload_enabled else tr("Disabled"),
            "action": tr("Review Publishing Settings"),
        },
        {
            "label": tr("Auto Upload After Video"),
            "status": tr("Enabled") if auto_upload_enabled else tr("Disabled"),
            "action": tr("Review Publishing Settings"),
        },
        {
            "label": tr("YouTube Privacy Status"),
            "status": privacy_status,
            "action": privacy_action,
        },
    ]


def _render_distribution_workflow_steps():
    steps = _distribution_workflow_steps()
    with st.container(border=True):
        st.write(tr("Distribution Workflow"))
        st.caption(tr("Distribution Workflow Help"))
        cols = st.columns(len(steps))
        for index, step in enumerate(steps, start=1):
            with cols[index - 1]:
                st.metric(f"{index}. {step['label']}", step["status"])
                st.caption(f"{tr('Next Action')}: {step['action']}")


def _distribution_readiness_state():
    social_metadata_state = _distribution_social_metadata_state()
    upload_enabled = bool(st.session_state.get("upload_post_enabled"))
    auto_upload_enabled = bool(st.session_state.get("upload_post_auto_upload"))
    youtube_privacy_labels = {
        upload_post.YOUTUBE_PRIVACY_PRIVATE: tr("Private Upload"),
        upload_post.YOUTUBE_PRIVACY_UNLISTED: tr("Unlisted Upload"),
        upload_post.YOUTUBE_PRIVACY_PUBLIC: tr("Public Upload"),
    }
    youtube_privacy = st.session_state.get(
        "upload_post_youtube_privacy_status",
        config.app.get(
            "upload_post_youtube_privacy_status",
            upload_post.YOUTUBE_PRIVACY_UNLISTED,
        ),
    )
    public_upload_needs_approval = (
        upload_enabled
        and youtube_privacy == upload_post.YOUTUBE_PRIVACY_PUBLIC
        and not st.session_state.get("upload_post_allow_public_youtube")
    )

    if not social_metadata_state["ready"]:
        level = "info"
        message = social_metadata_state["message"]
        action = social_metadata_state["action"]
    elif public_upload_needs_approval:
        level = "warning"
        message = tr("Distribution Public Upload Needs Approval")
        action = tr("Allow Public YouTube Upload")
    elif not upload_enabled:
        level = "info"
        message = tr("Distribution Publishing Optional")
        action = tr("Review Publishing Settings")
    else:
        level = "success"
        message = tr("Distribution Ready")
        action = tr("Optional")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Social Metadata"),
                "value": social_metadata_state["status"],
            },
            {
                "label": tr("Publishing Settings"),
                "value": tr("Enabled") if upload_enabled else tr("Disabled"),
            },
            {
                "label": tr("Auto Upload After Video"),
                "value": tr("Enabled") if auto_upload_enabled else tr("Disabled"),
            },
            {
                "label": tr("YouTube Privacy Status"),
                "value": youtube_privacy_labels.get(youtube_privacy, tr("Unlisted Upload")),
            },
        ],
    }


def _render_distribution_readiness_review():
    readiness = _distribution_readiness_state()
    _render_readiness_review(
        "Distribution Readiness",
        "Distribution Readiness Help",
        readiness,
    )
    _render_distribution_action_controls(readiness)


def _distribution_action_focus_target(action):
    action_text = str(action or "").strip().lower()
    if not action_text:
        return ""
    action_targets = {
        tr("Generate Social Metadata").lower(): "social_metadata",
        tr("Review Publishing Settings").lower(): "publishing",
        tr("Allow Public YouTube Upload").lower(): "publishing",
    }
    return action_targets.get(action_text, "")


def _render_distribution_action_controls(readiness):
    if not isinstance(readiness, dict):
        return
    action = readiness.get("action")
    target = _distribution_action_focus_target(action)
    if not action or not target:
        return
    if st.button(action, key=f"distribution_focus_{target}", width="stretch"):
        st.session_state["distribution_focus_panel"] = target
        st.rerun()


def _batch_history_readiness_state(batch_items, recent_jobs, calibration_report):
    batch_count = len(batch_items or [])
    recent_count = len(recent_jobs or [])
    video_count = sum(_job_output_count(job, "videos") for job in (recent_jobs or []))
    pending_upload_count = sum(
        _job_output_count(job, "pending_uploads") for job in (recent_jobs or [])
    )
    thumbnail_count = sum(
        _job_output_count(job, "thumbnail_candidates") for job in (recent_jobs or [])
    )
    try:
        calibration_samples = int(calibration_report.get("sample_count", 0))
    except (AttributeError, TypeError, ValueError):
        calibration_samples = 0

    if pending_upload_count:
        level = "warning"
        message = tr("Recent Jobs Pending Uploads")
        action = tr("Review Pending Uploads")
    elif batch_count:
        level = "success"
        message = tr("Batch Ready for Generation")
        action = tr("Generate Batch")
    elif recent_count:
        level = "info"
        message = tr("Batch History Has Recent Jobs")
        action = tr("Review Recent Jobs")
    else:
        level = "info"
        message = tr("Batch Needs Ideas")
        action = tr("Add Batch Subjects")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Batch Generation"),
                "value": f"{batch_count} {tr('Ready')}",
            },
            {
                "label": tr("Recent Jobs"),
                "value": str(recent_count),
            },
            {
                "label": tr("Videos"),
                "value": str(video_count),
            },
            {
                "label": tr("Pending Uploads"),
                "value": str(pending_upload_count),
            },
            {
                "label": tr("Thumbnail Candidates"),
                "value": str(thumbnail_count),
            },
            {
                "label": tr("Quality Gate Calibration"),
                "value": str(calibration_samples),
            },
        ],
    }


def _render_batch_history_readiness_review(batch_items, recent_jobs, calibration_report):
    _render_readiness_review(
        "Batch History Readiness",
        "Batch History Readiness Help",
        _batch_history_readiness_state(
            batch_items,
            recent_jobs,
            calibration_report,
        ),
    )


def _render_batch_history_workspace_summary():
    batch_items = _get_batch_items()
    recent_jobs = history.list_history(limit=20)
    calibration_report = quality_calibration.build_quality_gate_calibration_report(
        recent_jobs,
        current_threshold=st.session_state.get(
            "viral_quality_gate_threshold",
            content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
        ),
    )
    _render_batch_history_readiness_review(batch_items, recent_jobs, calibration_report)


def _render_quality_planning_column(params):
    with st.expander(tr("Content Intelligence"), expanded=False):
        _render_content_intelligence_panel(params)


def _render_quality_analysis_column(params, selected_social_platform):
    _render_quality_check_workspace_intro(params, selected_social_platform)
    quality_focus_panel = st.session_state.pop("quality_focus_panel", "")

    with st.expander(
        tr("Content Preflight"),
        expanded=(
            quality_focus_panel == "preflight"
            or not bool(st.session_state.get("content_preflight_report"))
        ),
    ):
        _render_content_preflight_panel(params, selected_social_platform)

    with st.expander(
        tr("Viral Analysis"),
        expanded=(
            quality_focus_panel == "viral"
            or bool(st.session_state.get("viral_analysis"))
        ),
    ):
        _render_viral_analysis_panel(params, selected_social_platform)


def _render_planning_quality_workspace(
    params,
    selected_social_platform,
    tab_spec,
):
    _render_workspace_tab_intro(*tab_spec)
    _render_planning_quality_summary(params, selected_social_platform)
    _render_quality_workflow_steps(params, selected_social_platform)

    planning_col, analysis_col = st.columns([0.44, 0.56])
    with planning_col:
        _render_quality_planning_column(params)
    with analysis_col:
        _render_quality_analysis_column(params, selected_social_platform)


def _render_batch_generation_column(batch_items):
    _render_batch_workflow_steps(batch_items)
    with st.expander(tr("Batch Generation"), expanded=False):
        _render_batch_generation_panel()


def _render_recent_jobs_column():
    with st.expander(tr("Recent Jobs"), expanded=False):
        _render_recent_jobs_panel()


def _render_batch_history_workspace(tab_spec):
    _render_workspace_tab_intro(*tab_spec)
    _render_batch_history_workspace_summary()
    batch_items = _get_batch_items()
    batch_col, history_col = st.columns([0.48, 0.52])

    with batch_col:
        _render_batch_generation_column(batch_items)

    with history_col:
        _render_recent_jobs_column()


def _render_distribution_metadata_column(params, focus_panel=""):
    with st.expander(
        tr("Social Metadata"),
        expanded=focus_panel == "social_metadata",
    ):
        return _render_social_metadata_panel(params)


def _render_distribution_publishing_column(focus_panel=""):
    _render_distribution_workflow_steps()
    with st.expander(
        tr("Publishing Settings"),
        expanded=focus_panel == "publishing",
    ):
        _render_publishing_settings_panel()


def _render_distribution_workspace(params, tab_spec):
    _render_workspace_tab_intro(*tab_spec)
    _render_distribution_workspace_summary()
    distribution_focus_panel = st.session_state.pop("distribution_focus_panel", "")
    metadata_col, publishing_col = st.columns([0.52, 0.48])

    with metadata_col:
        selected_social_platform = _render_distribution_metadata_column(
            params,
            distribution_focus_panel,
        )

    with publishing_col:
        _render_distribution_publishing_column(distribution_focus_panel)

    return selected_social_platform


def _render_setup_workspace(params, tab_spec):
    _render_workspace_tab_intro(*tab_spec)
    _render_setup_workspace_summary()

    with st.expander(tr("Presets"), expanded=False):
        _render_presets_panel(params)

    with st.expander(tr("Brand Kit"), expanded=False):
        _render_brand_kit_panel()

    with st.expander(tr("Accounts & API"), expanded=False):
        _render_accounts_api_readiness()
        _render_video_source_api_key_management_panel("setup_accounts_api")


def _render_script_project_panel(params):
    with st.container(border=True):
        _render_video_script_settings_panel(params)


def _render_media_settings_group(params):
    with st.container(border=True):
        return _render_video_settings_panel(params)


def _render_audio_settings_group(params):
    with st.container(border=True):
        st.write(tr("Audio Settings"))
        _render_tts_settings_panel(params)
        return _render_custom_audio_and_bgm_panel(params)


def _render_output_safety_group(params):
    _render_quality_assistant_compact()

    with st.container(border=True):
        _render_subtitle_settings_panel(params)

    with st.expander(tr("Click to show API Key management"), expanded=False):
        _render_video_source_api_key_management_panel("output_safety_api")


def _render_production_desk(params):
    script_col, media_col, output_col = st.columns([0.48, 0.29, 0.23])
    uploaded_files = []
    uploaded_audio_file = None
    uploaded_bgm_file = None

    with script_col:
        _render_script_project_panel(params)

    with media_col:
        uploaded_files = _render_media_settings_group(params)
        uploaded_audio_file, uploaded_bgm_file = _render_audio_settings_group(params)

    with output_col:
        _render_output_safety_group(params)

    return uploaded_files, uploaded_audio_file, uploaded_bgm_file


def _render_main_workspace_tabs(params, selected_social_platform):
    workspace_tab_specs = _workspace_tab_specs()
    workspace_tabs = st.tabs(
        [tr(title_key) for title_key, _help_key in workspace_tab_specs]
    )
    setup_tab, planning_quality_tab, distribution_tab, batch_history_tab = workspace_tabs

    with setup_tab:
        _render_setup_workspace(params, workspace_tab_specs[0])

    with batch_history_tab:
        _render_batch_history_workspace(workspace_tab_specs[3])

    with distribution_tab:
        selected_social_platform = _render_distribution_workspace(
            params,
            workspace_tab_specs[2],
        )

    with planning_quality_tab:
        _render_planning_quality_workspace(
            params,
            selected_social_platform,
            workspace_tab_specs[1],
        )

    return selected_social_platform


def _initialize_video_params():
    params = VideoParams(
        video_subject="",
        outro_image_file=config.ui.get("outro_image_file", ""),
        outro_duration=config.ui.get("outro_duration", 2.0),
    )
    params.match_materials_to_script = bool(
        st.session_state.get("match_materials_to_script", False)
    )
    params.smart_scene_queries = bool(st.session_state.get("smart_scene_queries", False))
    return params


def _render_production_workspace(params, selected_social_platform):
    workspace_status_container = st.container()
    uploaded_files, uploaded_audio_file, uploaded_bgm_file = _render_production_desk(
        params
    )

    with workspace_status_container:
        _render_production_workspace_status(params, selected_social_platform)

    selected_social_platform = _render_main_workspace_tabs(
        params,
        selected_social_platform,
    )

    start_button, batch_button = _render_preview_generate_workspace(
        params,
        selected_social_platform,
    )

    return (
        selected_social_platform,
        uploaded_files,
        uploaded_audio_file,
        uploaded_bgm_file,
        start_button,
        batch_button,
    )


_VIDEO_SOURCE_KEYS = {
    "pexels",
    "pixabay",
    "coverr",
    "vecteezy",
    "nasa",
    "noaa_ocean",
    "loc",
    "wikimedia",
    "archive_org",
    "local",
}


def _plain_value(value):
    return getattr(value, "value", value)


def _output_aspect_summary(params, default="-"):
    values = []
    raw_aspects = [getattr(params, "video_aspect", None)]
    raw_aspects.extend(getattr(params, "video_aspects", None) or [])
    for raw_value in raw_aspects:
        value = str(_plain_value(raw_value) or "").strip()
        if value and value not in values:
            values.append(value)
    return ", ".join(values) or default


def _output_render_count(params):
    try:
        video_count = max(1, int(getattr(params, "video_count", 1) or 1))
    except (TypeError, ValueError):
        video_count = 1

    raw_aspects = [getattr(params, "video_aspect", None)]
    raw_aspects.extend(getattr(params, "video_aspects", None) or [])
    aspect_count = len(
        {
            str(_plain_value(aspect) or "").strip()
            for aspect in raw_aspects
            if str(_plain_value(aspect) or "").strip()
        }
    )
    return video_count * max(1, aspect_count)


def _find_option_index(options, selected_value, default_index=0):
    selected_value = _plain_value(selected_value)
    for index, option in enumerate(options):
        option_value = option[1] if isinstance(option, tuple) else option
        if _plain_value(option_value) == selected_value:
            return index
    return default_index


def _config_int(section, key, default):
    try:
        return int(section.get(key, default))
    except (TypeError, ValueError):
        return default


def _set_ui_value(key, value):
    if value is not None:
        config.ui[key] = _plain_value(value)


def _current_preset_app_config():
    return {
        "video_codec": _normalize_video_codec(
            config.app.get("video_codec", "libx264")
        ),
        "video_cooldown_enabled": bool(
            st.session_state.get("video_cooldown_enabled", False)
        ),
        "video_cooldown_days": int(
            st.session_state.get("video_cooldown_days", 7) or 7
        ),
        "video_deband_enabled": _config_bool(
            st.session_state.get(
                "video_deband_enabled",
                config.app.get("video_deband_enabled", False),
            )
        ),
        "video_crf": _normalize_video_crf_value(
            st.session_state.get("video_crf", config.app.get("video_crf", 20))
        ),
        "video_encoder_preset": _normalize_libx264_preset(
            st.session_state.get(
                "video_encoder_preset",
                config.app.get("video_encoder_preset", "medium"),
            )
        ),
        "video_fps": _normalize_video_fps_value(
            st.session_state.get("video_fps", config.app.get("video_fps", 30))
        ),
        "audio_bitrate": "{}k".format(
            _normalize_audio_bitrate_kbps(
                st.session_state.get(
                    "audio_bitrate_kbps",
                    config.app.get("audio_bitrate", "192k"),
                )
            )
        ),
    }


def _apply_video_quality_params(params):
    params.video_codec = _normalize_video_codec(
        config.app.get("video_codec", "libx264")
    )
    params.video_crf = _normalize_video_crf_value(
        st.session_state.get("video_crf", config.app.get("video_crf", 20))
    )
    params.video_encoder_preset = _normalize_libx264_preset(
        st.session_state.get(
            "video_encoder_preset",
            config.app.get("video_encoder_preset", "medium"),
        )
    )
    params.video_fps = _normalize_video_fps_value(
        st.session_state.get("video_fps", config.app.get("video_fps", 30))
    )
    params.audio_bitrate = "{}k".format(
        _normalize_audio_bitrate_kbps(
            st.session_state.get(
                "audio_bitrate_kbps",
                config.app.get("audio_bitrate", "192k"),
            )
        )
    )
    return params


def _apply_video_preset(payload):
    params_data = payload.get("params", {})
    app_config_data = payload.get("app_config") or {}
    preset_name = payload.get("name", "")

    st.session_state["video_subject"] = params_data.get("video_subject", "")
    st.session_state["video_script"] = params_data.get("video_script", "")

    video_terms = params_data.get("video_terms", "")
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)
    st.session_state["video_terms"] = video_terms or ""

    st.session_state["paragraph_number_input"] = params_data.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params_data.get("video_script_prompt", "")

    custom_system_prompt = params_data.get("custom_system_prompt", "")
    st.session_state["use_custom_system_prompt"] = bool(custom_system_prompt)
    st.session_state["custom_system_prompt"] = (
        custom_system_prompt or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )
    st.session_state["match_materials_to_script"] = bool(
        params_data.get("match_materials_to_script", False)
    )
    st.session_state["custom_bgm_file_input"] = params_data.get("bgm_file", "")

    if "video_cooldown_enabled" in app_config_data:
        video_cooldown_enabled = bool(app_config_data["video_cooldown_enabled"])
        st.session_state["video_cooldown_enabled"] = video_cooldown_enabled
        config.app["video_cooldown_enabled"] = video_cooldown_enabled
    if "video_cooldown_days" in app_config_data:
        try:
            video_cooldown_days = int(app_config_data["video_cooldown_days"])
        except (TypeError, ValueError):
            video_cooldown_days = 7
        st.session_state["video_cooldown_days"] = video_cooldown_days
        config.app["video_cooldown_days"] = video_cooldown_days
    if "video_deband_enabled" in app_config_data:
        video_deband_enabled = _config_bool(app_config_data["video_deband_enabled"])
        st.session_state["video_deband_enabled"] = video_deband_enabled
        config.app["video_deband_enabled"] = video_deband_enabled

    if "video_codec" in app_config_data:
        config.app["video_codec"] = _normalize_video_codec(app_config_data["video_codec"])
    if "video_crf" in app_config_data:
        video_crf = _normalize_video_crf_value(app_config_data["video_crf"])
        st.session_state["video_crf"] = video_crf
        config.app["video_crf"] = video_crf
    if "video_encoder_preset" in app_config_data:
        video_encoder_preset = _normalize_libx264_preset(
            app_config_data["video_encoder_preset"]
        )
        st.session_state["video_encoder_preset"] = video_encoder_preset
        config.app["video_encoder_preset"] = video_encoder_preset
    if "video_fps" in app_config_data:
        video_fps = _normalize_video_fps_value(app_config_data["video_fps"])
        st.session_state["video_fps"] = video_fps
        config.app["video_fps"] = video_fps
    if "audio_bitrate" in app_config_data:
        audio_bitrate_kbps = _normalize_audio_bitrate_kbps(
            app_config_data["audio_bitrate"]
        )
        st.session_state["audio_bitrate_kbps"] = audio_bitrate_kbps
        config.app["audio_bitrate"] = f"{audio_bitrate_kbps}k"

    video_source = params_data.get("video_source")
    if video_source:
        config.app["video_source"] = video_source
        if video_source in _VIDEO_SOURCE_KEYS:
            config.app["enabled_video_sources"] = [video_source]
        elif video_source == "multi" and not config.app.get("enabled_video_sources"):
            config.app["enabled_video_sources"] = ["pexels", "pixabay", "coverr"]

    _set_ui_value("video_concat_mode", params_data.get("video_concat_mode"))
    if params_data.get("video_transition_mode") is None:
        config.ui.pop("video_transition_mode", None)
    else:
        _set_ui_value("video_transition_mode", params_data.get("video_transition_mode"))
    _set_ui_value("video_aspect", params_data.get("video_aspect"))
    preset_aspects = params_data.get("video_aspects", [])
    if not isinstance(preset_aspects, (list, tuple)):
        preset_aspects = []
    restored_aspects = _resolve_output_aspects(
        params_data.get("video_aspect"), preset_aspects
    )
    restored_additional_aspects = [aspect.value for aspect in restored_aspects[1:]]
    restored_source = str(
        params_data.get("video_source") or config.app.get("video_source", "pexels")
    ).strip()
    st.session_state[
        f"video_additional_aspects_for_{restored_source}"
    ] = restored_additional_aspects
    if restored_additional_aspects:
        config.ui["video_aspects"] = [
            aspect.value for aspect in restored_aspects
        ]
    else:
        config.ui.pop("video_aspects", None)
    _set_ui_value("video_clip_duration", params_data.get("video_clip_duration"))
    _set_ui_value("video_count", params_data.get("video_count"))
    _set_ui_value("voice_name", params_data.get("voice_name"))
    _set_ui_value("voice_volume", params_data.get("voice_volume"))
    _set_ui_value("voice_rate", params_data.get("voice_rate"))
    _set_ui_value("bgm_type", params_data.get("bgm_type"))
    _set_ui_value("bgm_file", params_data.get("bgm_file"))
    _set_ui_value("bgm_volume", params_data.get("bgm_volume"))
    _set_ui_value("subtitle_enabled", params_data.get("subtitle_enabled"))
    _set_ui_value("subtitle_style", params_data.get("subtitle_style"))
    _set_ui_value("subtitle_position", params_data.get("subtitle_position"))
    _set_ui_value("custom_position", params_data.get("custom_position"))
    _set_ui_value("font_name", params_data.get("font_name"))
    _set_ui_value("text_fore_color", params_data.get("text_fore_color"))
    _set_ui_value("font_size", params_data.get("font_size"))
    _set_ui_value("stroke_color", params_data.get("stroke_color"))
    _set_ui_value("stroke_width", params_data.get("stroke_width"))
    _set_ui_value(
        "rounded_subtitle_background",
        params_data.get("rounded_subtitle_background"),
    )

    text_background_color = params_data.get("text_background_color", True)
    if text_background_color is False:
        config.ui["subtitle_background_enabled"] = False
    else:
        config.ui["subtitle_background_enabled"] = True
        if isinstance(text_background_color, str):
            config.ui["subtitle_background_color"] = text_background_color

    return preset_name


def _history_job_draft_payload(job):
    job = job if isinstance(job, dict) else {}
    terms = job.get("terms", "")
    if isinstance(terms, list):
        terms = ", ".join(str(term) for term in terms if str(term).strip())
    elif terms is None:
        terms = ""
    return {
        "subject": str(job.get("subject") or "").strip(),
        "script": str(job.get("script") or job.get("video_script") or "").strip(),
        "terms": str(terms or "").strip(),
    }


def _clear_analysis_state_for_new_draft():
    for key in (
        "content_preflight_report",
        "viral_analysis",
        "script_rewrite_suggestion",
        "social_metadata",
        "social_metadata_fingerprint",
        "_social_metadata_edit_signature",
    ):
        st.session_state[key] = None
    for key in (
        "script_rewrite_preview",
        "social_metadata_title",
        "social_metadata_caption",
        "social_metadata_hashtags",
    ):
        st.session_state.pop(key, None)


def _apply_history_job_as_draft(job):
    payload = _history_job_draft_payload(job)
    st.session_state["video_subject"] = payload["subject"]
    st.session_state["video_script"] = payload["script"]
    st.session_state["video_terms"] = payload["terms"]
    _clear_analysis_state_for_new_draft()
    return payload


def _parse_batch_subjects(raw_subjects):
    subjects = []
    seen = set()
    for line in (raw_subjects or "").splitlines():
        subject = line.strip()
        if not subject or subject in seen:
            continue
        seen.add(subject)
        subjects.append(subject)
    return subjects


def _split_batch_script_blocks(raw_blocks):
    blocks = []
    current = []
    for line in (raw_blocks or "").splitlines():
        if line.strip() == "---":
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
        else:
            current.append(line)

    block = "\n".join(current).strip()
    if block:
        blocks.append(block)
    return blocks


def _parse_batch_script_blocks(raw_blocks):
    items = []
    for block in _split_batch_script_blocks(raw_blocks):
        lines = [line.rstrip() for line in block.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        subject = lines[0].strip()
        script = "\n".join(lines[1:]).strip()
        if subject:
            items.append({"subject": subject, "script": script})
    return items


def _get_batch_items():
    if st.session_state.get("use_manual_batch_scripts"):
        return _parse_batch_script_blocks(
            st.session_state.get("batch_script_blocks", "")
        )
    return [
        {"subject": subject, "script": ""}
        for subject in _parse_batch_subjects(
            st.session_state.get("batch_subjects", "")
        )
    ]


def _clone_video_params(source_params):
    if hasattr(source_params, "model_dump"):
        return VideoParams(**source_params.model_dump())
    return source_params.copy(deep=True)


def _material_to_dict(item):
    return {
        "provider": getattr(item, "provider", ""),
        "url": getattr(item, "url", ""),
        "duration": int(getattr(item, "duration", 0) or 0),
        "width": int(getattr(item, "width", 0) or 0),
        "height": int(getattr(item, "height", 0) or 0),
    }


def _material_from_dict(item):
    m = MaterialInfo()
    m.provider = item.get("provider", "")
    m.url = item.get("url", "")
    m.duration = int(item.get("duration", 0) or 0)
    m.width = int(item.get("width", 0) or 0)
    m.height = int(item.get("height", 0) or 0)
    return m


def _is_vertical_high_resolution(item):
    width = int(item.get("width", 0) or 0)
    height = int(item.get("height", 0) or 0)
    return width > 0 and height > width and height >= 1080


def _manual_recommended_candidate_url(candidates):
    for item in candidates or []:
        if item.get("url") and _is_vertical_high_resolution(item):
            return item.get("url")
    return ""


def _manual_candidate_badges(item, is_system_recommendation=False):
    width = int(item.get("width", 0) or 0)
    height = int(item.get("height", 0) or 0)
    if width <= 0 or height <= 0:
        return []

    badges = []
    if is_system_recommendation:
        badges.append(tr("System Recommendation Badge"))
    if height > width:
        badges.append(tr("Vertical Video Badge"))
    if height < 720:
        badges.append(tr("Low Resolution Badge"))
    return badges


def _manual_candidate_label(url):
    candidates = st.session_state.get("manual_video_candidates", [])
    recommended_url = _manual_recommended_candidate_url(candidates)
    for index, item in enumerate(candidates, start=1):
        if item.get("url") == url:
            provider = item.get("provider") or "source"
            duration = item.get("duration") or 0
            badges = _manual_candidate_badges(
                item,
                is_system_recommendation=item.get("url") == recommended_url,
            )
            badge_text = f" - {' / '.join(badges)}" if badges else ""
            return f"{index}. {provider} - {duration}s{badge_text}"
    return url


def _selected_manual_video_materials():
    selected_urls = st.session_state.get("manual_video_selected_urls", [])
    candidates_by_url = {
        item.get("url"): item
        for item in st.session_state.get("manual_video_candidates", [])
        if item.get("url")
    }
    materials = []
    for url in selected_urls:
        item = candidates_by_url.get(url)
        if item:
            materials.append(_material_from_dict(item))
    return materials


def _list_bgm_files():
    if not os.path.isdir(song_dir):
        return []
    supported_exts = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
    return sorted(
        file
        for file in os.listdir(song_dir)
        if file.casefold().startswith("cc0_")
        and file.lower().endswith(supported_exts)
    )


def _prepare_task_params(
    task_id,
    source_params,
    uploaded_audio,
    uploaded_bgm,
    uploaded_video_files,
):
    run_params = _clone_video_params(source_params)

    if uploaded_audio:
        task_dir = utils.task_dir(task_id)
        _, audio_ext = os.path.splitext(os.path.basename(uploaded_audio.name))
        audio_ext = audio_ext.lower() or ".mp3"
        custom_audio_path = os.path.join(task_dir, f"custom-audio{audio_ext}")
        with open(custom_audio_path, "wb") as f:
            f.write(uploaded_audio.getbuffer())
        run_params.custom_audio_file = custom_audio_path

    if uploaded_bgm:
        task_dir = utils.task_dir(task_id)
        _, bgm_ext = os.path.splitext(os.path.basename(uploaded_bgm.name))
        bgm_ext = bgm_ext.lower() or ".mp3"
        custom_bgm_path = os.path.join(task_dir, f"custom-bgm{bgm_ext}")
        with open(custom_bgm_path, "wb") as file:
            file.write(uploaded_bgm.getbuffer())
        run_params.bgm_file = custom_bgm_path

    if uploaded_video_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        run_params.video_materials = []
        persisted_local_materials = []
        for file in uploaded_video_files:
            safe_name = file_security.sanitize_upload_filename(file.name)
            safe_file_id = re.sub(
                r"[^A-Za-z0-9_-]",
                "_",
                str(getattr(file, "file_id", "")),
            )[:64].strip("_") or uuid4().hex
            file_path = file_security.resolve_path_within_directory(
                local_videos_dir,
                f"{safe_file_id}_{safe_name}",
                require_file=False,
            )
            with open(file_path, "wb") as f:
                f.write(file.getbuffer())
            m = MaterialInfo()
            m.provider = "local"
            m.url = file_path
            run_params.video_materials.append(m)
            persisted_local_materials.append(
                {
                    "provider": m.provider,
                    "url": m.url,
                    "duration": m.duration,
                }
            )
        st.session_state["local_video_materials"] = persisted_local_materials
    elif (
        run_params.video_source == "local"
        and st.session_state["local_video_materials"]
    ):
        run_params.video_materials = []
        for material in st.session_state["local_video_materials"]:
            m = MaterialInfo()
            m.provider = material.get("provider", "local")
            m.url = material.get("url", "")
            m.duration = material.get("duration", 0)
            m.title = material.get("title", "")
            m.license = material.get("license", "")
            m.attribution = material.get("attribution", "")
            if m.url:
                run_params.video_materials.append(m)

    selected_manual_materials = _selected_manual_video_materials()
    if run_params.video_source != "local" and selected_manual_materials:
        run_params.video_materials = selected_manual_materials

    return run_params


def _prepare_reusable_voice_preview(task_id, params):
    cached = st.session_state.get("voice_preview_audio")
    script = str(params.video_script or "").strip()
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(
            selected_tts_server
        ),
    )
    if (
        not isinstance(cached, dict)
        or cached.get("preview_type") != "full"
        or cached.get("fingerprint") != expected_fingerprint
        or not cached.get("audio_bytes")
        or cached.get("sub_maker") is None
        or not isinstance(cached.get("duration"), (int, float))
        or cached.get("duration") <= 0
        or float(params.voice_volume) != 1.0
    ):
        return None

    preview_path = os.path.join(utils.task_dir(task_id), "voice-preview.mp3")
    with open(preview_path, "wb") as file:
        file.write(cached["audio_bytes"])
    return {
        "audio_file": preview_path,
        "duration": float(cached["duration"]),
        "sub_maker": cached["sub_maker"],
        "script": script,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }


def _generate_social_metadata_for_result(run_params, result, platform):
    metadata = llm.generate_social_metadata(
        video_subject=run_params.video_subject,
        video_script=(result or {}).get("script") or run_params.video_script,
        language=run_params.video_language or "auto",
        platform=platform,
    )
    metadata["caption"] = material.append_material_attributions(
        metadata.get("caption", ""),
        (result or {}).get("material_attributions"),
    )
    return metadata


def _generate_video_terms_for_ui(run_params, video_script):
    smart_scene_queries = bool(getattr(run_params, "smart_scene_queries", False))
    ordered_materials = (
        bool(getattr(run_params, "match_materials_to_script", False))
        or smart_scene_queries
    )
    amount = 8 if ordered_materials else 5
    if smart_scene_queries:
        terms = llm.generate_scene_queries(
            video_subject=run_params.video_subject,
            video_script=video_script,
            amount=amount,
            language=run_params.video_language or "",
        )
        if terms:
            return terms

    return llm.generate_terms(
        run_params.video_subject,
        video_script,
        amount=amount,
        match_script_order=ordered_materials,
    )


def _result_video_duration(result=None):
    video_paths = (result or {}).get("videos") or []
    if isinstance(video_paths, str):
        video_paths = [video_paths]

    for video_path in video_paths:
        video_path = str(video_path or "").strip()
        if not video_path:
            continue
        duration = video_service.get_video_duration(video_path)
        if duration is not None:
            return duration
    return None


def _generate_viral_analysis_for_result(
    run_params,
    result=None,
    metadata=None,
    platform=None,
):
    metadata = metadata or {}
    return viral_analyzer.analyze_viral_potential(
        video_subject=run_params.video_subject,
        video_script=(result or {}).get("script") or run_params.video_script,
        title=metadata.get("title", ""),
        video_duration_sec=_result_video_duration(result),
        target_platforms=[platform] if platform else None,
        language=run_params.video_language or "auto",
        social_caption=metadata.get("caption", ""),
        hashtags=metadata.get("hashtags"),
        material_attributions=(result or {}).get("material_attributions"),
    )


def _render_viral_analysis(analysis, key_prefix):
    if not analysis:
        return

    score_cols = st.columns(3)
    score_cols[0].metric(
        tr("Viral Score"),
        f"{analysis.get('overall_score', 0)}/100",
    )
    score_cols[1].metric(
        tr("Hook Score"),
        f"{analysis.get('hook_score', 0)}/100",
    )
    score_cols[2].metric(
        tr("Pacing Score"),
        f"{analysis.get('pacing_score', 0)}/100",
    )

    summary = analysis.get("summary")
    if summary:
        st.caption(summary)

    warnings = analysis.get("warnings") or []
    if warnings:
        st.warning(" | ".join(warnings))

    hook_suggestions = analysis.get("hook_suggestions") or []
    if hook_suggestions:
        st.write(tr("Hook Suggestions"))
        for suggestion in hook_suggestions:
            st.write(f"- {suggestion}")

    title_variants = analysis.get("title_variants") or []
    if title_variants:
        st.text_area(
            tr("Title Variants"),
            value="\n".join(title_variants),
            height=100,
            key=f"{key_prefix}_title_variants",
        )

    thumbnail_concepts = analysis.get("thumbnail_concepts") or []
    if thumbnail_concepts:
        st.text_area(
            tr("Thumbnail Concepts"),
            value="\n".join(thumbnail_concepts),
            height=100,
            key=f"{key_prefix}_thumbnail_concepts",
        )

    platform_fit = analysis.get("platform_fit") or {}
    if platform_fit:
        st.write(tr("Platform Fit"))
        for platform, score in platform_fit.items():
            score_value = min(1.0, max(0.0, float(score)))
            st.caption(f"{platform}: {score_value:.0%}")
            st.progress(score_value)


def _generate_thumbnail_candidates_for_result(task_id, result=None, viral_analysis=None):
    if not viral_analysis:
        return {"candidates": [], "error": ""}
    return thumbnail.generate_thumbnail_candidates(
        task_id=task_id,
        video_paths=(result or {}).get("videos", []),
        thumbnail_concepts=viral_analysis.get("thumbnail_concepts"),
        hook_timestamps=viral_analysis.get("thumbnail_timestamps"),
    )


def _enrich_batch_result_with_network_analysis(
    job,
    *,
    generate_social_metadata: bool,
    generate_viral_analysis: bool,
    platform,
):
    """Run only independent network enrichment after the render queue finishes."""
    run_params = job["run_params"]
    result = job["result"]
    metadata = None
    if generate_social_metadata:
        metadata = _generate_social_metadata_for_result(
            run_params,
            result,
            platform,
        )
    viral_analysis = None
    if generate_viral_analysis:
        viral_analysis = _generate_viral_analysis_for_result(
            run_params,
            result=result,
            metadata=metadata,
            platform=platform,
        )
    return {"metadata": metadata, "viral_analysis": viral_analysis}


def _attach_thumbnail_candidates(task_id, result=None, viral_analysis=None):
    result = result or {}
    thumbnail_result = _generate_thumbnail_candidates_for_result(
        task_id,
        result=result,
        viral_analysis=viral_analysis,
    )
    candidates = thumbnail_result.get("candidates") or []
    error = thumbnail_result.get("error") or ""
    if candidates:
        result["thumbnail_candidates"] = candidates
    if error:
        result["thumbnail_candidate_error"] = error
    return thumbnail_result


def _render_thumbnail_candidates(candidates=None, error="", key_prefix="thumbnail"):
    candidates = candidates or []
    if not candidates and not error:
        return
    st.write(tr("Thumbnail Candidates"))
    if error and not candidates:
        st.warning(tr("Thumbnail Candidate Error").format(error=error))
        return
    image_cols = st.columns(min(3, max(1, len(candidates))))
    for index, candidate in enumerate(candidates[:3]):
        image_path = candidate.get("path", "")
        timestamp = candidate.get("timestamp_sec")
        concept = candidate.get("concept", "")
        caption_parts = []
        if timestamp is not None:
            caption_parts.append(f"{timestamp:g}s")
        if concept:
            caption_parts.append(concept)
        caption = " - ".join(caption_parts)
        with image_cols[index % len(image_cols)]:
            if image_path and os.path.isfile(image_path):
                st.image(image_path, caption=caption, width="stretch")
            elif image_path:
                st.code(image_path)
            elif caption:
                st.caption(caption)


def _format_script_score_delta(change):
    if not isinstance(change, dict):
        return ""
    try:
        before = int(change.get("before"))
        after = int(change.get("after"))
    except (TypeError, ValueError):
        return ""
    try:
        delta = int(change.get("delta"))
    except (TypeError, ValueError):
        delta = after - before
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta} ({before} -> {after})"


def _should_apply_improved_analysis(applied_script, rewrite_suggestion):
    if not isinstance(rewrite_suggestion, dict):
        return False
    if not rewrite_suggestion.get("improved_analysis"):
        return False
    return str(applied_script or "") == str(
        rewrite_suggestion.get("improved_script", "") or ""
    )


def _script_rewrite_decision_state(rewrite_suggestion):
    comparison = rewrite_suggestion.get("score_comparison") if isinstance(rewrite_suggestion, dict) else {}
    overall_change = (comparison or {}).get("overall_score") if isinstance(comparison, dict) else {}
    try:
        delta = int(overall_change.get("delta"))
    except (AttributeError, TypeError, ValueError):
        return {
            "level": "info",
            "message": tr("Rewrite Score Unknown"),
            "action": tr("Review Rewrite"),
        }
    if delta >= 0:
        return {
            "level": "success",
            "message": tr("Rewrite Improves Score"),
            "action": tr("Apply Improved Script"),
        }
    return {
        "level": "warning",
        "message": tr("Rewrite Lowers Score"),
        "action": tr("Review Rewrite"),
    }


def _render_script_rewrite_decision_panel(rewrite_suggestion):
    state = _script_rewrite_decision_state(rewrite_suggestion)
    with st.container(border=True):
        st.write(tr("Script Rewrite Decision"))
        st.caption(f"{state['message']} {tr('Next Action')}: {state['action']}")


def _render_script_rewrite_comparison(rewrite_suggestion):
    if not isinstance(rewrite_suggestion, dict):
        return

    comparison = rewrite_suggestion.get("score_comparison") or {}
    improved_analysis = rewrite_suggestion.get("improved_analysis") or {}
    if not comparison and not improved_analysis:
        return

    st.write(tr("Script Improvement Comparison"))
    st.caption(tr("Score Change"))

    score_cols = st.columns(3)
    for index, (score_key, label_key) in enumerate(
        (
            ("overall_score", "Viral Score"),
            ("hook_score", "Hook Score"),
            ("pacing_score", "Pacing Score"),
        )
    ):
        change = comparison.get(score_key) or {}
        after_score = change.get("after") if isinstance(change, dict) else None
        if after_score is None and isinstance(improved_analysis, dict):
            after_score = improved_analysis.get(score_key)
        try:
            score_value = f"{int(after_score)}/100"
        except (TypeError, ValueError):
            score_value = tr("Score Not Available")
        score_cols[index].metric(
            tr(label_key),
            score_value,
            delta=_format_script_score_delta(change) or None,
        )

    summary = (
        improved_analysis.get("summary")
        if isinstance(improved_analysis, dict)
        else ""
    )
    if summary:
        st.caption(f"{tr('Improved Script Analysis')}: {summary}")

    warnings = (
        improved_analysis.get("warnings", [])
        if isinstance(improved_analysis, dict)
        else []
    )
    if warnings:
        st.warning(f"{tr('Improved Script Analysis')}: " + " | ".join(warnings[:3]))


def _render_script_rewrite_text_comparison(rewrite_suggestion):
    if not isinstance(rewrite_suggestion, dict):
        return

    original_script = rewrite_suggestion.get("original_script", "")
    improved_script = rewrite_suggestion.get("improved_script", "")
    if not original_script and not improved_script:
        return

    st.write(tr("Script Improvement Comparison"))
    st.caption(tr("Script Improvement Comparison Help"))
    text_cols = st.columns(2)
    with text_cols[0]:
        st.text_area(
            tr("Original Script"),
            value=original_script,
            height=180,
            disabled=True,
        )
    with text_cols[1]:
        st.text_area(
            tr("Improved Script"),
            value=improved_script,
            height=180,
            disabled=True,
        )


def _content_preflight_warning_text(
    report,
    video_subject,
    video_script,
    platform,
    language,
):
    if not report:
        return tr("Content Preflight Missing Warning")
    if content_quality.is_preflight_report_stale(
        report,
        video_subject=video_subject,
        video_script=video_script,
        platform=platform,
        language=language,
    ):
        return tr("Content Preflight Stale Warning")
    return ""


def _script_repeat_warning_text(matches):
    if not isinstance(matches, list) or not matches:
        return ""
    return tr("Preflight Script Repeat Warning")


def _session_text_value(key):
    try:
        return str(st.session_state.get(key, "") or "").strip()
    except Exception:
        return ""


def _preflight_input_values(params, batch_items=None):
    batch_items = batch_items or []
    video_subject = str(
        getattr(params, "video_subject", "") or _session_text_value("video_subject")
    ).strip()
    video_script = str(
        getattr(params, "video_script", "") or _session_text_value("video_script")
    ).strip()
    language = getattr(params, "video_language", "") or "auto"

    if batch_items and not video_subject:
        subjects = [
            item.get("subject", "").strip()
            for item in batch_items
            if item.get("subject", "").strip()
        ]
        scripts = [
            item.get("script", "").strip()
            for item in batch_items
            if item.get("script", "").strip()
        ]
        return {
            "video_subject": "\n".join(subjects[:5]),
            "video_script": "\n\n".join(scripts[:5]),
            "language": language,
        }
    return {
        "video_subject": video_subject,
        "video_script": video_script,
        "language": language,
    }


def _viral_analysis_input_values(params, batch_items=None):
    return _preflight_input_values(params, batch_items)


def _social_metadata_input_values(params, batch_items=None):
    return _preflight_input_values(params, batch_items)


def _quality_gate_warning_text(gate):
    if not gate or not gate.get("warn"):
        return ""
    return tr("Viral Quality Gate Warning").format(
        score=gate.get("score", 0),
        threshold=gate.get("threshold", content_quality.DEFAULT_QUALITY_GATE_THRESHOLD),
    )


def _production_final_readiness_state(params, platform):
    preflight_inputs = _preflight_input_values(params, _get_batch_items())
    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    viral_analysis = st.session_state.get("viral_analysis") or {}

    subject_ready = bool(preflight_inputs["video_subject"])
    script_ready = bool(preflight_inputs["video_script"])
    if not preflight_report:
        preflight_status = tr("Not Run")
        preflight_ready = False
    elif preflight_warning:
        preflight_status = tr("Needs Refresh")
        preflight_ready = False
    else:
        preflight_status = tr("Ready")
        preflight_ready = True

    viral_score = viral_analysis.get("overall_score")
    try:
        viral_status = f"{int(viral_score)}/100"
        viral_ready = True
    except (TypeError, ValueError):
        viral_status = tr("No Viral Score")
        viral_ready = False

    social_metadata = st.session_state.get("social_metadata")
    social_metadata_warning = _social_metadata_warning_text(
        st.session_state.get("social_metadata_fingerprint"),
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    social_metadata_ready = bool(social_metadata) and not bool(social_metadata_warning)
    social_metadata_status = (
        tr("Needs Refresh")
        if social_metadata and social_metadata_warning
        else tr("Ready")
        if social_metadata
        else tr("Not Run")
    )
    publishing_enabled = bool(st.session_state.get("upload_post_enabled"))
    output_ratio = _short_display_value(
        _output_aspect_summary(params, default="9:16")
    )
    voice_name = _short_display_value(_plain_value(getattr(params, "voice_name", "")) or tr("Auto Detect"))
    video_source = _short_display_value(
        _plain_value(getattr(params, "video_source", ""))
        or config.app.get("video_source", "multi")
    )

    if not subject_ready:
        level = "warning"
        message = tr("Final Readiness Needs Subject")
        action = tr("Enter Video Subject")
    elif not script_ready:
        level = "warning"
        message = tr("Final Readiness Needs Script")
        action = tr("Generate Video Script and Keywords")
    elif not preflight_ready:
        level = "info"
        message = tr("Final Readiness Needs Preflight")
        action = tr("Analyze Topic")
    elif not viral_ready:
        level = "info"
        message = tr("Final Readiness Needs Viral Analysis")
        action = tr("Generate Viral Analysis")
    elif not social_metadata_ready:
        level = "info"
        message = tr("Final Readiness Needs Social Metadata")
        action = tr("Generate Social Metadata")
    else:
        level = "success"
        message = tr("Final Readiness Ready")
        action = tr("Generate Video")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Subject"),
                "value": tr("Ready") if subject_ready else tr("Needs Input"),
            },
            {
                "label": tr("Script"),
                "value": tr("Ready") if script_ready else tr("Needs Input"),
            },
            {
                "label": tr("Content Preflight"),
                "value": preflight_status,
            },
            {
                "label": tr("Viral Analysis"),
                "value": viral_status,
            },
            {
                "label": tr("Social Metadata"),
                "value": social_metadata_status,
            },
            {
                "label": tr("Publishing Settings"),
                "value": tr("Enabled") if publishing_enabled else tr("Disabled"),
            },
            {
                "label": tr("Format"),
                "value": output_ratio,
            },
            {
                "label": tr("Source"),
                "value": video_source,
            },
            {
                "label": tr("Voice"),
                "value": voice_name,
            },
        ],
    }


def _render_production_readiness_summary(params, platform):
    _render_readiness_review(
        "Final Readiness",
        "Final Readiness Help",
        _production_final_readiness_state(params, platform),
    )


def _quality_check_status_values(params, platform):
    preflight_inputs = _preflight_input_values(params, _get_batch_items())
    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    if not preflight_report:
        preflight_status = tr("Not Run")
    elif preflight_warning:
        preflight_status = tr("Needs Refresh")
    else:
        preflight_status = tr("Ready")

    viral_analysis = st.session_state.get("viral_analysis") or {}
    viral_score = viral_analysis.get("overall_score")
    try:
        viral_status = f"{int(viral_score)}/100"
    except (TypeError, ValueError):
        viral_status = tr("No Viral Score")

    rewrite_suggestion = st.session_state.get("script_rewrite_suggestion")
    if isinstance(rewrite_suggestion, dict) and rewrite_suggestion.get("error"):
        rewrite_status = tr("Unavailable")
    elif rewrite_suggestion:
        rewrite_status = tr("Suggested")
    else:
        rewrite_status = tr("No Rewrite")
    return preflight_status, viral_status, rewrite_status


def _quality_workflow_steps(params, platform, batch_items=None):
    batch_items = batch_items if batch_items is not None else _get_batch_items()
    preflight_inputs = _preflight_input_values(params, batch_items)
    content_plan = st.session_state.get("content_plan")
    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    viral_analysis = st.session_state.get("viral_analysis") or {}
    viral_score = _score_value(viral_analysis.get("overall_score"))
    rewrite_suggestion = st.session_state.get("script_rewrite_suggestion")

    if content_plan:
        plan_status = tr("Ready")
        plan_action = tr("Optional")
    else:
        plan_status = tr("Not Run")
        plan_action = tr("Generate Content Plan")

    if not preflight_report:
        preflight_status = tr("Not Run")
        preflight_action = tr("Analyze Topic")
    elif preflight_warning:
        preflight_status = tr("Needs Refresh")
        preflight_action = tr("Analyze Topic")
    else:
        preflight_status = tr("Ready")
        preflight_action = tr("Optional")

    if viral_score is None:
        viral_status = tr("Not Run")
        viral_action = tr("Generate Viral Analysis")
    else:
        viral_status = f"{viral_score}/100"
        viral_action = tr("Optional")

    if isinstance(rewrite_suggestion, dict) and rewrite_suggestion.get("error"):
        rewrite_status = tr("Unavailable")
        rewrite_action = tr("Review Warnings")
    elif rewrite_suggestion:
        rewrite_status = tr("Suggested")
        rewrite_action = tr("Review Rewrite")
    elif viral_score is None:
        rewrite_status = tr("Waiting")
        rewrite_action = tr("Generate Viral Analysis")
    else:
        rewrite_status = tr("Optional")
        rewrite_action = tr("Improve Script")

    return [
        {
            "label": tr("Content Intelligence"),
            "status": plan_status,
            "action": plan_action,
        },
        {
            "label": tr("Content Preflight"),
            "status": preflight_status,
            "action": preflight_action,
        },
        {
            "label": tr("Viral Analysis"),
            "status": viral_status,
            "action": viral_action,
        },
        {
            "label": tr("Improve Script"),
            "status": rewrite_status,
            "action": rewrite_action,
        },
    ]


def _render_quality_workflow_steps(params, platform):
    steps = _quality_workflow_steps(params, platform)
    with st.container(border=True):
        st.write(tr("Quality Workflow"))
        st.caption(tr("Quality Workflow Help"))
        cols = st.columns(len(steps))
        for index, step in enumerate(steps, start=1):
            with cols[index - 1]:
                st.metric(f"{index}. {step['label']}", step["status"])
                st.caption(f"{tr('Next Action')}: {step['action']}")


def _warning_list(value):
    if isinstance(value, str):
        warning = value.strip()
        return [warning] if warning else []
    if isinstance(value, (list, tuple)):
        return [
            warning
            for warning in (str(item or "").strip() for item in value)
            if warning
        ]
    return []


def _quality_check_warning_summaries(limit=2):
    warnings = []
    preflight_report = st.session_state.get("content_preflight_report") or {}
    if isinstance(preflight_report, dict):
        content_plan = preflight_report.get("content_plan") or {}
        if isinstance(content_plan, dict):
            warnings.extend(_warning_list(content_plan.get("warnings")))
        script_analysis = preflight_report.get("script_analysis") or {}
        if isinstance(script_analysis, dict):
            warnings.extend(_warning_list(script_analysis.get("warnings")))

    viral_analysis = st.session_state.get("viral_analysis") or {}
    if isinstance(viral_analysis, dict):
        warnings.extend(_warning_list(viral_analysis.get("warnings")))

    unique_warnings = []
    seen = set()
    for warning in warnings:
        warning = warning.strip()
        if not warning or warning in seen:
            continue
        seen.add(warning)
        unique_warnings.append(warning)
        if len(unique_warnings) >= limit:
            break
    return unique_warnings


def _quality_warning_action(warning):
    text = str(warning or "").lower()
    if any(term in text for term in ("preflight", "stale", "repeat", "planning")):
        return tr("Analyze Topic")
    if any(term in text for term in ("viral", "score", "analysis")):
        return tr("Generate Viral Analysis")
    if any(term in text for term in ("cta", "hook", "pacing", "weak", "missing")):
        return tr("Improve Script")
    return tr("Review Warnings")


def _quality_warning_items(warnings):
    return [
        {
            "warning": warning,
            "action": _quality_warning_action(warning),
        }
        for warning in _warning_list(warnings)
    ]


def _score_value(value):
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, score))


def _quality_score_items(analysis):
    if not isinstance(analysis, dict):
        analysis = {}
    items = []
    for score_key, label_key in (
        ("overall_score", "Viral Score"),
        ("hook_score", "Hook Score"),
        ("pacing_score", "Pacing Score"),
    ):
        score = _score_value(analysis.get(score_key))
        if score is None:
            status_key = "Score Not Available"
            level = "neutral"
        elif score >= 75:
            status_key = "Score Strong"
            level = "success"
        elif score >= 60:
            status_key = "Score Watch"
            level = "warning"
        else:
            status_key = "Score Needs Work"
            level = "danger"
        items.append(
            {
                "label": tr(label_key),
                "score": score,
                "status": tr(status_key),
                "level": level,
            }
        )
    return items


def _active_quality_analysis():
    viral_analysis = st.session_state.get("viral_analysis")
    if isinstance(viral_analysis, dict) and viral_analysis:
        return viral_analysis
    preflight_report = st.session_state.get("content_preflight_report") or {}
    if isinstance(preflight_report, dict):
        script_analysis = preflight_report.get("script_analysis")
        if isinstance(script_analysis, dict):
            return script_analysis
    return {}


def _render_quality_score_breakdown(analysis):
    items = _quality_score_items(analysis)
    if not items:
        return
    st.write(tr("Quality Score Breakdown"))
    score_cols = st.columns(len(items))
    for index, item in enumerate(items):
        score = item.get("score")
        value = f"{score}/100" if score is not None else tr("Score Not Available")
        score_cols[index].metric(
            item["label"],
            value,
            delta=item["status"] if score is not None else None,
        )


def _render_quality_warning_checklist(warnings):
    st.write(tr("Quality Warning Checklist"))
    st.caption(tr("Quality Warning Checklist Help"))
    if not warnings:
        st.success(tr("No Critical Quality Warnings"))
        return

    for index, item in enumerate(_quality_warning_items(warnings), start=1):
        st.warning(
            f"{index}. {item['warning']} {tr('Next Action')}: {item['action']}"
        )


def _production_workspace_status_items(
    params,
    platform,
    batch_items=None,
    preset_count=0,
    recent_job_count=0,
):
    if batch_items is None:
        batch_items = _get_batch_items()

    preflight_inputs = _preflight_input_values(params, batch_items)
    preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    if not preflight_report:
        preflight_status = tr("Not Run")
        preflight_level = "neutral"
    elif preflight_warning:
        preflight_status = tr("Needs Refresh")
        preflight_level = "warning"
    else:
        preflight_status = tr("Ready")
        preflight_level = "success"

    viral_analysis = st.session_state.get("viral_analysis") or {}
    viral_score = _score_value(viral_analysis.get("overall_score"))
    viral_status = f"{viral_score}/100" if viral_score is not None else tr("Not Run")
    social_metadata = st.session_state.get("social_metadata")
    social_metadata_warning = _social_metadata_warning_text(
        st.session_state.get("social_metadata_fingerprint"),
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    if social_metadata and social_metadata_warning:
        social_metadata_status = tr("Needs Refresh")
        social_metadata_level = "warning"
        social_metadata_action = tr("Generate Social Metadata")
    elif social_metadata:
        social_metadata_status = tr("Ready")
        social_metadata_level = "success"
        social_metadata_action = tr("Optional")
    else:
        social_metadata_status = tr("Not Run")
        social_metadata_level = "neutral"
        social_metadata_action = tr("Generate Social Metadata")

    return [
        {
            "label": tr("Presets"),
            "status": (
                f"{int(preset_count)} {tr('Saved')}"
                if preset_count
                else tr("Optional")
            ),
            "level": "success" if preset_count else "neutral",
            "action": tr("Optional"),
        },
        {
            "label": tr("Batch Generation"),
            "status": (
                f"{len(batch_items)} {tr('Ready')}"
                if batch_items
                else tr("Empty")
            ),
            "level": "success" if batch_items else "neutral",
            "action": tr("Generate Batch") if batch_items else tr("Add Batch Subjects"),
        },
        {
            "label": tr("Content Intelligence"),
            "status": tr("Ready")
            if st.session_state.get("content_plan")
            else tr("Not Run"),
            "level": "success"
            if st.session_state.get("content_plan")
            else "neutral",
            "action": (
                tr("Optional")
                if st.session_state.get("content_plan")
                else tr("Generate Content Plan")
            ),
        },
        {
            "label": tr("Social Metadata"),
            "status": social_metadata_status,
            "level": social_metadata_level,
            "action": social_metadata_action,
        },
        {
            "label": tr("Publishing Settings"),
            "status": tr("Enabled")
            if st.session_state.get("upload_post_enabled")
            else tr("Disabled"),
            "level": "success"
            if st.session_state.get("upload_post_enabled")
            else "neutral",
            "action": tr("Review Publishing Settings"),
        },
        {
            "label": tr("Content Preflight"),
            "status": preflight_status,
            "level": preflight_level,
            "action": tr("Optional")
            if preflight_level == "success"
            else tr("Analyze Topic"),
        },
        {
            "label": tr("Viral Analysis"),
            "status": viral_status,
            "level": "success" if viral_score is not None else "neutral",
            "action": tr("Optional")
            if viral_score is not None
            else tr("Generate Viral Analysis"),
        },
        {
            "label": tr("Recent Jobs"),
            "status": str(int(recent_job_count or 0)),
            "level": "success" if recent_job_count else "neutral",
            "action": tr("Review Recent Jobs")
            if recent_job_count
            else tr("Generate Video"),
        },
    ]


def _production_workspace_status_groups(items):
    group_specs = [
        (
            tr("Production Step Setup"),
            {tr("Presets"), tr("Batch Generation")},
        ),
        (
            tr("Production Step Quality Check"),
            {
                tr("Content Intelligence"),
                tr("Content Preflight"),
                tr("Viral Analysis"),
            },
        ),
        (
            tr("Workspace Tab Distribution"),
            {tr("Social Metadata"), tr("Publishing Settings")},
        ),
        (
            tr("Workspace Tab Batch History"),
            {tr("Recent Jobs")},
        ),
    ]
    grouped = []
    used_labels = set()
    for title, labels in group_specs:
        group_items = [item for item in items if item.get("label") in labels]
        used_labels.update(item.get("label") for item in group_items)
        grouped.append({"title": title, "items": group_items})

    remaining_items = [item for item in items if item.get("label") not in used_labels]
    if remaining_items:
        grouped.append({"title": tr("Production Summary"), "items": remaining_items})
    return grouped


def _render_production_workspace_status(params, platform):
    try:
        preset_count = len(presets.list_presets())
    except Exception:
        preset_count = 0
    try:
        recent_job_count = len(history.list_history(limit=20))
    except Exception:
        recent_job_count = 0
    items = _production_workspace_status_items(
        params,
        platform,
        batch_items=_get_batch_items(),
        preset_count=preset_count,
        recent_job_count=recent_job_count,
    )

    with st.container(border=True):
        st.write(tr("Production Workspace"))
        st.caption(tr("Production Workspace Help"))
        groups = _production_workspace_status_groups(items)
        group_cols = st.columns(len(groups))
        for group_col, group in zip(group_cols, groups):
            with group_col:
                st.write(group["title"])
                for item in group["items"]:
                    st.metric(item["label"], item["status"])
                    if item.get("action"):
                        st.caption(f"{tr('Next Action')}: {item['action']}")


def _quality_check_decision_state(params, platform, batch_items=None):
    if batch_items is None:
        batch_items = _get_batch_items()
    preflight_inputs = _preflight_input_values(params, batch_items)
    has_content = bool(
        preflight_inputs["video_subject"] or preflight_inputs["video_script"]
    )

    def _decision(level, status_key, message_key, action_key=""):
        return {
            "level": level,
            "status": tr(status_key),
            "message": tr(message_key),
            "action": tr(action_key) if action_key else "",
        }

    if not has_content:
        return _decision(
            "warning",
            "Quality Decision Needs Input",
            "Quality Decision Needs Input Help",
        )

    preflight_report = st.session_state.get("content_preflight_report")
    if not preflight_report:
        return _decision(
            "info",
            "Quality Decision Needs Preflight",
            "Quality Decision Needs Preflight Help",
            "Analyze Topic",
        )

    preflight_warning = _content_preflight_warning_text(
        preflight_report,
        preflight_inputs["video_subject"],
        preflight_inputs["video_script"],
        platform,
        preflight_inputs["language"],
    )
    if preflight_warning:
        return _decision(
            "warning",
            "Quality Decision Refresh Preflight",
            "Quality Decision Refresh Preflight Help",
            "Analyze Topic",
        )

    rewrite_suggestion = st.session_state.get("script_rewrite_suggestion")
    if (
        isinstance(rewrite_suggestion, dict)
        and rewrite_suggestion.get("improved_script")
        and not rewrite_suggestion.get("error")
    ):
        return _decision(
            "info",
            "Quality Decision Review Rewrite",
            "Quality Decision Review Rewrite Help",
            "Apply Improved Script",
        )

    quality_gate = content_quality.evaluate_quality_gate(
        preflight_report,
        enabled=st.session_state.get("viral_quality_gate_enabled", False),
        threshold=st.session_state.get(
            "viral_quality_gate_threshold",
            content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
        ),
    )
    if quality_gate.get("warn"):
        return _decision(
            "warning",
            "Quality Decision Low Score",
            "Quality Decision Low Score Help",
            "Improve Script",
        )

    viral_analysis = st.session_state.get("viral_analysis")
    if preflight_inputs["video_script"] and not viral_analysis:
        return _decision(
            "info",
            "Quality Decision Needs Viral Analysis",
            "Quality Decision Needs Viral Analysis Help",
            "Generate Viral Analysis",
        )

    if _quality_check_warning_summaries(limit=1):
        return _decision(
            "warning",
            "Quality Decision Review Warnings",
            "Quality Decision Review Warnings Help",
        )

    return _decision(
        "success",
        "Quality Decision Ready",
        "Quality Decision Ready Help",
    )


def _render_quality_decision(decision):
    if not isinstance(decision, dict):
        return
    message = f"{decision.get('status', '')}: {decision.get('message', '')}".strip()
    action = decision.get("action")
    if action:
        message = f"{message} {tr('Next Action')}: {action}"
    level = decision.get("level")
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.info(message)


def _quality_action_focus_target(action):
    action_text = str(action or "").strip().lower()
    if not action_text:
        return ""
    action_targets = {
        tr("Analyze Topic").lower(): "preflight",
        tr("Generate Viral Analysis").lower(): "viral",
        tr("Improve Script").lower(): "viral",
        tr("Apply Improved Script").lower(): "viral",
    }
    return action_targets.get(action_text, "")


def _render_quality_action_controls(decision):
    if not isinstance(decision, dict):
        return
    action = decision.get("action")
    target = _quality_action_focus_target(action)
    if not action or not target:
        return
    if st.button(action, key=f"quality_focus_{target}", width="stretch"):
        st.session_state["quality_focus_panel"] = target
        st.rerun()


def _render_quality_gate_review(params, platform):
    preflight_status, viral_status, rewrite_status = _quality_check_status_values(
        params,
        platform,
    )
    decision = _quality_check_decision_state(params, platform)
    warnings = _quality_check_warning_summaries(limit=3)

    with st.container(border=True):
        st.write(tr("Quality Gate Review"))
        st.caption(tr("Quality Gate Review Help"))

        review_cols = st.columns([0.42, 0.58])
        with review_cols[0]:
            _render_quality_decision(decision)
            st.metric(
                tr("Next Action"),
                decision.get("action") or tr("Optional"),
            )
            _render_quality_action_controls(decision)
        with review_cols[1]:
            status_cols = st.columns(3)
            status_cols[0].metric(tr("Preflight Status"), preflight_status)
            status_cols[1].metric(tr("Viral Analysis Status"), viral_status)
            status_cols[2].metric(tr("Rewrite Status"), rewrite_status)

        _render_quality_score_breakdown(_active_quality_analysis())
        _render_quality_warning_checklist(warnings)


def _render_quality_check_workspace_intro(params, platform):
    _render_quality_gate_review(params, platform)


def _render_recent_generations_compact():
    recent_jobs = history.list_history(limit=5)
    with st.container(border=True):
        st.write(tr("Recent Generations"))
        if not recent_jobs:
            st.info(tr("No Recent Jobs"))
            return

        rows = []
        for job in recent_jobs:
            viral_analysis = job.get("viral_analysis") or {}
            viral_score = _score_value(viral_analysis.get("overall_score"))
            video_count = _job_output_count(job, "videos")
            pending_upload_count = _job_output_count(job, "pending_uploads")
            thumbnail_count = _job_output_count(job, "thumbnail_candidates")
            rows.append(
                {
                    tr("Subject"): job.get("subject")
                    or job.get("task_id")
                    or tr("Untitled"),
                    tr("Status"): job.get("status", ""),
                    tr("Videos"): str(video_count),
                    tr("Pending Uploads"): str(pending_upload_count),
                    tr("Viral Score"): (
                        f"{viral_score}/100"
                        if viral_score is not None
                        else tr("Score Not Available")
                    ),
                    tr("Next Action"): _recent_job_next_action(
                        video_count,
                        pending_upload_count,
                        thumbnail_count,
                        viral_score,
                    ),
                    tr("Created At"): job.get("created_at", ""),
                }
            )

        st.dataframe(rows, width="stretch", hide_index=True)


def _generate_actions_decision_state(params, platform, batch_items=None, recent_jobs=None):
    final_readiness = _production_final_readiness_state(params, platform)
    batch_items = batch_items if batch_items is not None else _get_batch_items()
    recent_jobs = recent_jobs if recent_jobs is not None else history.list_history(limit=5)
    batch_count = len(batch_items or [])
    recent_count = len(recent_jobs or [])

    if final_readiness.get("action") == tr("Generate Video"):
        level = "success"
        message = tr("Generate Decision Ready")
        action = tr("Generate Video")
    elif batch_count:
        level = "info"
        message = tr("Generate Decision Batch Ready")
        action = tr("Generate Batch")
    else:
        level = final_readiness.get("level", "info")
        message = final_readiness.get("message") or tr("Generate Decision Needs Review")
        action = final_readiness.get("action") or tr("Review Final Readiness")

    return {
        "level": level,
        "message": message,
        "action": action,
        "metrics": [
            {
                "label": tr("Single Video"),
                "value": final_readiness.get("action") or tr("Optional"),
            },
            {
                "label": tr("Batch Subjects"),
                "value": str(batch_count),
            },
            {
                "label": tr("Recent Jobs"),
                "value": str(recent_count),
            },
        ],
    }


def _render_generate_actions_decision(params, platform):
    _render_readiness_review(
        "Generate Decision",
        "Generate Decision Help",
        _generate_actions_decision_state(params, platform),
    )


def _render_generate_actions_panel():
    with st.container(border=True):
        st.write(tr("Generate Actions"))
        st.caption(tr("Generate Actions Help"))
        action_cols = st.columns(2)
        with action_cols[0]:
            start_button = st.button(
                tr("Generate Video"),
                width="stretch",
                type="primary",
            )
        with action_cols[1]:
            batch_button = st.button(
                tr("Generate Batch"),
                width="stretch",
            )
    return start_button, batch_button


def _render_preview_generate_workspace(params, selected_social_platform):
    _render_production_summary_compact(params)
    review_col, recent_col = st.columns([0.58, 0.42])
    with review_col:
        _render_production_readiness_summary(params, selected_social_platform)
        _render_generate_actions_decision(params, selected_social_platform)
        start_button, batch_button = _render_generate_actions_panel()
    with recent_col:
        _render_recent_generations_compact()
    return start_button, batch_button


def _render_script_stats(script):
    words = len(str(script or "").split())
    if not words:
        return

    estimated_seconds = max(1, int(round(words / 2.5)))
    minutes, seconds = divmod(estimated_seconds, 60)
    if minutes:
        duration = f"{minutes}m {seconds}s"
    else:
        duration = f"{seconds}s"
    st.caption(
        f"{tr('Script Words')}: {words} | "
        f"{tr('Estimated Duration')}: {duration}"
    )


def _short_display_value(value, max_length=38):
    text = str(value or "").strip()
    try:
        max_length = int(max_length)
    except (TypeError, ValueError):
        max_length = 38
    if max_length <= 0:
        return "-"
    if len(text) <= max_length:
        return text or "-"
    if max_length <= 3:
        return text[:max_length]
    return f"{text[: max_length - 3]}..."


def _render_production_summary_compact(params):
    enabled_sources = config.app.get("enabled_video_sources", [])
    if isinstance(enabled_sources, str):
        enabled_sources = [enabled_sources]
    source_text = ", ".join(str(source) for source in enabled_sources if source)
    if not source_text:
        source_text = _plain_value(getattr(params, "video_source", "")) or "-"

    ratio = _output_aspect_summary(params)
    voice_name = getattr(params, "voice_name", "") or tr("No Voice")
    output_count = _output_render_count(params)

    with st.container(border=True):
        st.write(tr("Production Summary"))
        summary_cols = st.columns(4)
        summary_cols[0].metric(tr("Format"), _short_display_value(ratio, 18))
        summary_cols[1].metric(tr("Source"), _short_display_value(source_text, 28))
        summary_cols[2].metric(tr("Voice"), _short_display_value(voice_name, 28))
        summary_cols[3].metric(tr("Outputs"), str(output_count))


def _render_quality_assistant_compact():
    preflight_report = st.session_state.get("content_preflight_report")
    viral_analysis = st.session_state.get("viral_analysis") or {}
    viral_score = viral_analysis.get("overall_score")
    try:
        viral_score = f"{int(viral_score)}/100"
    except (TypeError, ValueError):
        viral_score = tr("No Viral Score")

    with st.container(border=True):
        st.write(tr("Quality Assistant"))
        assistant_cols = st.columns(2)
        assistant_cols[0].metric(
            tr("Content Preflight"),
            tr("Ready") if preflight_report else tr("Not Run"),
        )
        assistant_cols[1].metric(tr("Viral Score"), viral_score)
        if viral_analysis:
            detail_cols = st.columns(2)
            detail_cols[0].metric(
                tr("Hook Score"),
                f"{int(viral_analysis.get('hook_score', 0) or 0)}/100",
            )
            detail_cols[1].metric(
                tr("Pacing Score"),
                f"{int(viral_analysis.get('pacing_score', 0) or 0)}/100",
            )

        warnings = _quality_check_warning_summaries()
        if warnings:
            st.caption(" | ".join(warnings))


def _render_content_preflight_report(report, key_prefix):
    if not report:
        return

    content_plan = report.get("content_plan") or {}
    source = content_plan.get("source")
    if source:
        st.caption(f"{tr('Content Preflight Source')}: {source}")

    repeat_matches = report.get("repeat_matches") or []
    if repeat_matches:
        st.write(tr("Preflight Repeat Matches"))
        for match in repeat_matches[:3]:
            subject = match.get("subject") or match.get("task_id") or tr("Untitled")
            similarity = match.get("similarity")
            created_at = match.get("created_at", "")
            similarity_text = (
                f" ({float(similarity):.0%})"
                if isinstance(similarity, (int, float))
                else ""
            )
            st.caption(f"{subject}{similarity_text} {created_at}".strip())

    script_repeat_matches = report.get("script_repeat_matches") or []
    if script_repeat_matches:
        st.write(tr("Preflight Script Repeat Matches"))
        for match in script_repeat_matches[:3]:
            subject = match.get("subject") or match.get("task_id") or tr("Untitled")
            similarity = match.get("similarity")
            created_at = match.get("created_at", "")
            similarity_text = (
                f" ({float(similarity):.0%})"
                if isinstance(similarity, (int, float))
                else ""
            )
            st.caption(f"{subject}{similarity_text} {created_at}".strip())

    warnings = content_plan.get("warnings") or []
    if warnings:
        st.write(tr("Planning Warnings"))
        for warning in warnings[:3]:
            st.caption(warning)

    ideas = content_plan.get("ideas") or []
    if ideas:
        st.write(tr("Preflight Content Ideas"))
        for index, idea in enumerate(ideas[:3], start=1):
            subject = idea.get("subject", "")
            hook = idea.get("hook", "")
            st.caption(f"{index}. {subject}")
            if hook:
                st.write(f"- {hook}")

    script_analysis = report.get("script_analysis")
    if script_analysis:
        st.write(tr("Preflight Script Analysis"))
        _render_viral_analysis(script_analysis, f"{key_prefix}_viral")


def _record_history(
    task_id,
    run_params,
    result=None,
    metadata=None,
    viral_analysis=None,
    error="",
):
    status = "failed" if error else "completed"
    raw_video_aspects = [getattr(run_params, "video_aspect", None)]
    additional_video_aspects = getattr(run_params, "video_aspects", None)
    if isinstance(additional_video_aspects, (list, tuple, set)):
        raw_video_aspects.extend(additional_video_aspects)
    elif additional_video_aspects:
        raw_video_aspects.append(additional_video_aspects)
    video_aspects = []
    for raw_aspect in raw_video_aspects:
        aspect = str(getattr(raw_aspect, "value", raw_aspect) or "").strip()
        if aspect and aspect not in video_aspects:
            video_aspects.append(aspect)
    history.add_history(
        {
            "task_id": task_id,
            "subject": run_params.video_subject,
            "script": run_params.video_script,
            "language": getattr(run_params, "video_language", "") or "",
            "video_aspect": video_aspects[0] if video_aspects else "",
            "video_aspects": video_aspects,
            "audio_duration": (result or {}).get("audio_duration"),
            "subtitle_path": (result or {}).get("subtitle_path", ""),
            "llm_provider": config.app.get("llm_provider", ""),
            "voice_name": run_params.voice_name,
            "custom_audio_file": run_params.custom_audio_file,
            "video_source": str(getattr(run_params, "video_source", "") or ""),
            "video_transition_mode": str(
                getattr(
                    getattr(run_params, "video_transition_mode", None),
                    "value",
                    getattr(run_params, "video_transition_mode", ""),
                )
                or ""
            ),
            "status": status,
            "videos": (result or {}).get("videos", []),
            "materials": (result or {}).get("materials", []),
            "material_attributions": (result or {}).get("material_attributions"),
            "terms": (result or {}).get("terms") or run_params.video_terms,
            "metadata": metadata,
            "viral_analysis": viral_analysis,
            "thumbnail_candidates": (result or {}).get("thumbnail_candidates"),
            "thumbnail_candidate_error": (result or {}).get(
                "thumbnail_candidate_error", ""
            ),
            "cooldown": (result or {}).get("cooldown"),
            "video_encoder_results": (result or {}).get("video_encoder_results"),
            "render_quality_reports": (result or {}).get("render_quality_reports"),
            "visual_pacing": (result or {}).get("visual_pacing"),
            "pending_uploads": (result or {}).get("pending_uploads"),
            "partial_success": bool((result or {}).get("partial_success")),
            "failed_aspects": (result or {}).get("failed_aspects"),
            "error": error,
        }
    )


def _result_failed_aspects(result):
    if not isinstance(result, dict):
        return []

    raw_aspects = result.get("failed_aspects")
    if isinstance(raw_aspects, str):
        raw_aspects = [raw_aspects]
    if not isinstance(raw_aspects, (list, tuple)):
        return []

    failed_aspects = []
    for aspect in raw_aspects:
        value = str(aspect or "").strip()
        if value and value not in failed_aspects:
            failed_aspects.append(value)
    return failed_aspects


def _failed_generation_history_result(task_id):
    try:
        task_state = state.get_task(task_id)
    except Exception:
        logger.warning("Video generation failure details are unavailable.")
        return None

    failed_aspects = _result_failed_aspects(task_state)
    if not failed_aspects:
        return None
    return {"failed_aspects": failed_aspects}


def _partial_success_failed_aspects(result):
    if not isinstance(result, dict) or not result.get("partial_success"):
        return []
    return _result_failed_aspects(result)


def _render_partial_success_warning(result):
    failed_aspects = _partial_success_failed_aspects(result)
    if not failed_aspects:
        return False

    st.warning(
        tr("Video Generation Partially Completed").format(
            failed_aspects=", ".join(failed_aspects)
        )
    )
    return True


def _render_generation_completion_status(result):
    if not _render_partial_success_warning(result):
        st.success(tr("Video Generation Completed"))


def _render_failed_generation_warning(result):
    if (
        not isinstance(result, dict)
        or result.get("partial_success")
        or result.get("status") != "failed"
    ):
        return

    failed_aspects = _result_failed_aspects(result)
    if failed_aspects:
        st.warning(
            tr("Video Generation Failed Formats").format(
                failed_aspects=", ".join(failed_aspects)
            )
        )


def _normalize_task_state(value):
    if value in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _render_generation_logs(task_id):
    if config.ui.get("hide_log", False):
        return
    records = webui_task.get_task_logs(task_id)
    if records:
        st.code("\n".join(records))


def _finalize_background_generation_once(task_id, task):
    """Run custom post-processing once after the official worker finishes."""
    contexts = st.session_state.get("background_generation_contexts")
    if not isinstance(contexts, dict):
        return dict(task or {})
    context = contexts.get(task_id)
    if not isinstance(context, dict):
        return dict(task or {})

    result = dict(task or {})
    if context.get("finalized"):
        result.update(context.get("enrichment") or {})
        return result

    state_value = _normalize_task_state(result.get("state"))
    if state_value not in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        return result

    context["finalized"] = True
    run_params = context.get("params")
    if not isinstance(run_params, VideoParams):
        return result

    if state_value == const.TASK_STATE_FAILED:
        _record_history(
            task_id,
            run_params,
            result=result,
            error=str(result.get("error") or tr("Video Generation Failed")),
        )
        return result

    metadata = None
    if context.get("generate_social_metadata"):
        try:
            metadata = _generate_social_metadata_for_result(
                run_params,
                result,
                context.get("platform"),
            )
        except Exception:
            logger.exception(
                f"Background social metadata generation failed: task_id={task_id}"
            )

    viral_analysis = None
    if context.get("generate_viral_analysis"):
        try:
            viral_analysis = _generate_viral_analysis_for_result(
                run_params,
                result=result,
                metadata=metadata,
                platform=context.get("platform"),
            )
            _attach_thumbnail_candidates(
                task_id,
                result=result,
                viral_analysis=viral_analysis,
            )
        except Exception:
            logger.exception(
                f"Background viral analysis failed: task_id={task_id}"
            )

    enrichment = {
        "metadata": metadata,
        "viral_analysis": viral_analysis,
        "thumbnail_candidates": result.get("thumbnail_candidates"),
        "thumbnail_candidate_error": result.get("thumbnail_candidate_error", ""),
    }
    context["enrichment"] = enrichment
    result.update(enrichment)
    _record_history(
        task_id,
        run_params,
        result=result,
        metadata=metadata,
        viral_analysis=viral_analysis,
    )
    if metadata:
        st.session_state["social_metadata"] = metadata
    if viral_analysis:
        st.session_state["viral_analysis"] = viral_analysis
    return result


def _render_generation_task_snapshot(task_id, task):
    task = _finalize_background_generation_once(task_id, task)
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state_value = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state_value == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(progress, text=f"{tr('Task Progress')}: {progress}%")
        _render_generation_logs(task_id)
        return

    if state_value == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_failed_generation_warning(task)
        _render_generation_logs(task_id)
        return

    video_files = _video_output_urls(task.get("videos"))
    if state_value != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    _render_generation_completion_status(task)
    try:
        player_cols = st.columns(len(video_files) * 2 + 1)
        for index, video_path in enumerate(video_files):
            player_cols[index * 2 + 1].video(video_path)
    except Exception:
        logger.exception(
            f"Failed to render generated videos in WebUI: task_id={task_id}"
        )

    _render_material_sources(task)
    encoder_results = task.get("video_encoder_results") or []
    if encoder_results:
        st.write(tr("Video Encoder"))
        _render_video_encoder_results(encoder_results)
    render_quality_reports = task.get("render_quality_reports") or []
    if render_quality_reports:
        st.write(tr("Render Quality"))
        _render_render_quality_reports(render_quality_reports)
    if task.get("visual_pacing"):
        st.write(tr("Visual Pacing"))
        _render_visual_pacing_report(task["visual_pacing"])
    cooldown_summary = _cooldown_summary_text(task.get("cooldown"))
    if cooldown_summary:
        st.caption(cooldown_summary)
    if task.get("metadata"):
        st.text_input(
            tr("Social Title"),
            value=task["metadata"].get("title", ""),
            key=f"background_title_{task_id}",
        )
        st.text_area(
            tr("Social Description"),
            value=task["metadata"].get("caption", ""),
            height=90,
            key=f"background_caption_{task_id}",
        )
        st.text_input(
            tr("Social Hashtags"),
            value=" ".join(task["metadata"].get("hashtags", [])),
            key=f"background_hashtags_{task_id}",
        )
    if task.get("viral_analysis"):
        _render_viral_analysis(
            task["viral_analysis"],
            key_prefix=f"background_viral_{task_id}",
        )
    _render_thumbnail_candidates(
        task.get("thumbnail_candidates"),
        task.get("thumbnail_candidate_error", ""),
        key_prefix=f"background_thumb_{task_id}",
    )
    _render_generation_logs(task_id)

    st.session_state["latest_single_result_task_id"] = task_id
    if st.session_state.get("opened_generation_task_id") != task_id:
        st.session_state["opened_generation_task_id"] = task_id
        open_task_folder(task_id)


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    try:
        task = state.get_task(task_id)
    except Exception:
        logger.exception(
            f"Failed to query WebUI generation task: task_id={task_id}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state_value = _normalize_task_state((task or {}).get("state"))
    if state_value in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        st.rerun(scope="app")
    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    task_id = str(st.session_state.get("current_generation_task_id") or "")
    if not task_id:
        return
    try:
        task = state.get_task(task_id)
    except Exception:
        logger.exception(
            f"Failed to query current WebUI task: task_id={task_id}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state_value = _normalize_task_state((task or {}).get("state"))
    if state_value in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _render_generation_task_snapshot(task_id, task)
        return
    _render_running_generation_task(task_id)


def _render_generation_controls(
    start_button=False,
    *,
    task_id="",
    params=None,
    platform="",
    generate_social_metadata=True,
    generate_viral_analysis=False,
    voice_preview=None,
):
    if start_button:
        config.save_config()
        contexts = st.session_state.setdefault("background_generation_contexts", {})
        contexts[task_id] = {
            "params": _clone_video_params(params),
            "platform": platform,
            "generate_social_metadata": bool(generate_social_metadata),
            "generate_viral_analysis": bool(generate_viral_analysis),
            "finalized": False,
        }
        st.session_state["current_generation_task_id"] = task_id
        try:
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=voice_preview,
            )
        except Exception:
            logger.exception(
                f"Failed to submit WebUI generation task: task_id={task_id}"
            )
            st.error(tr("Video Generation Failed"))
        else:
            st.toast(tr("Generating Video"))

    _render_current_generation_task()
    return start_button


def _render_application(start_button=False, **generation_kwargs):
    generation_submitted = _render_generation_controls(
        start_button,
        **generation_kwargs,
    )
    if not generation_submitted:
        config.save_config()
    return generation_submitted


def _cooldown_summary_text(cooldown):
    if not cooldown:
        return ""
    try:
        count = int(cooldown.get("moved_recent_count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return ""
    try:
        days = int(cooldown.get("days", 7) or 7)
    except (TypeError, ValueError):
        days = 7
    return tr("Cooldown Summary").format(count=count, days=days)


def _subject_repeat_warning_text(matches, days):
    if not matches:
        return ""
    first_match = matches[0] or {}
    subject = first_match.get("subject") or first_match.get("task_id") or tr("Untitled")
    created_at = first_match.get("created_at") or ""
    return tr("Similar Subject Warning").format(
        subject=subject,
        days=days,
        created_at=created_at,
    )


def _subject_repeat_suggestion_text(subject):
    subject = str(subject or "").strip()
    if not subject:
        return ""
    return tr("Similar Subject Fresh Angle Suggestions").format(subject=subject)


def _metadata_from_widget_state(key_prefix, fallback_metadata):
    metadata = fallback_metadata or {}
    hashtags_value = st.session_state.get(
        f"{key_prefix}_hashtags",
        " ".join(metadata.get("hashtags", [])),
    )
    if isinstance(hashtags_value, str):
        hashtags = [
            tag.strip()
            for tag in hashtags_value.replace(",", " ").split()
            if tag.strip()
        ]
    else:
        hashtags = list(metadata.get("hashtags", []))
    return {
        "title": st.session_state.get(
            f"{key_prefix}_title",
            metadata.get("title", ""),
        ),
        "caption": st.session_state.get(
            f"{key_prefix}_caption",
            metadata.get("caption", ""),
        ),
        "hashtags": hashtags,
    }


def _youtube_extra_for_upload(platforms, metadata):
    if not any(str(platform).startswith("youtube") for platform in platforms or []):
        return None
    metadata = metadata or {}
    return {
        "youtube_title": metadata.get("title", ""),
        "youtube_description": metadata.get("caption", ""),
        "tags": metadata.get("hashtags", []),
        "privacyStatus": upload_post.upload_post_service.youtube_privacy_status,
    }


def _sync_upload_post_service_from_config():
    for key in (
        "upload_post_enabled",
        "upload_post_auto_upload",
        "upload_post_allow_public_youtube",
        "upload_post_youtube_privacy_status",
    ):
        if key in st.session_state:
            config.app[key] = st.session_state[key]
    upload_post.upload_post_service.reload_config()


def _pending_upload_status_text(status):
    if status == "uploaded":
        return tr("Upload Status Uploaded")
    if status == "failed":
        return tr("Upload Status Failed")
    return tr("Upload Status Pending")


def _upload_button_label(platforms):
    if not any(str(platform).startswith("youtube") for platform in platforms or []):
        return tr("Confirm and Upload Now")
    privacy_status = upload_post.upload_post_service.youtube_privacy_status
    if privacy_status == upload_post.YOUTUBE_PRIVACY_PUBLIC:
        return tr("Publish Public Now")
    if privacy_status == upload_post.YOUTUBE_PRIVACY_UNLISTED:
        return tr("Upload Unlisted Now")
    return tr("Confirm and Upload Now")


def _needs_ai_disclosure_review(platforms):
    if not isinstance(platforms, (list, tuple, set)):
        return False
    return any(
        str(platform).strip().lower().startswith(("tiktok", "youtube"))
        for platform in platforms
    )


def _render_pending_uploads(job, key_prefix):
    _sync_upload_post_service_from_config()
    pending_uploads = job.get("pending_uploads") or []
    if not pending_uploads:
        return

    task_id = job.get("task_id", "")
    metadata = job.get("metadata") or {}
    st.write(tr("Pending Uploads"))
    if not upload_post.upload_post_service.is_configured():
        st.caption(tr("Upload-Post Not Configured"))

    for index, pending_upload in enumerate(pending_uploads):
        if not isinstance(pending_upload, dict):
            continue
        video_path = pending_upload.get("video_path", "")
        platforms = pending_upload.get("platforms") or upload_post.upload_post_service.platforms
        status = pending_upload.get("status", "pending")
        upload_key = f"{key_prefix}_upload_{index}"

        st.caption(
            tr("Pending Upload Card").format(
                status=_pending_upload_status_text(status),
                platforms=", ".join(platforms),
                title=pending_upload.get("title") or job.get("subject") or tr("Untitled"),
            )
        )
        st.code(video_path)

        result = pending_upload.get("result") or {}
        if status == "uploaded":
            st.success(tr("Upload Successful"))
            disclosure_review = pending_upload.get("disclosure_review") or {}
            if disclosure_review.get("reviewed"):
                st.caption(tr("AI Disclosure Review Recorded"))
            result_link = upload_post.extract_result_link(result)
            if result_link:
                st.markdown(f"[{tr('View Uploaded Video')}]({result_link})")
            request_id = result.get("request_id")
            if request_id:
                st.caption(f"Request ID: {request_id}")
            continue
        if status == "failed":
            st.error(
                tr("Upload Failed").format(
                    error=result.get("error") or result.get("message") or tr("Unknown Error")
                )
            )

        public_youtube_upload = False
        if any(str(platform).startswith("youtube") for platform in platforms):
            privacy_status = upload_post.upload_post_service.youtube_privacy_status
            st.caption(
                tr("YouTube Upload Privacy Summary").format(
                    privacy=privacy_status,
                )
            )
            if privacy_status == upload_post.YOUTUBE_PRIVACY_PUBLIC:
                public_youtube_upload = True
                st.warning(tr("Public YouTube Upload Warning"))
                st.checkbox(
                    tr("Confirm Public Upload"),
                    key=f"{upload_key}_public_confirm",
                    help=tr("Confirm Public Upload Help"),
                )

        public_confirmed = st.session_state.get(f"{upload_key}_public_confirm", False)
        disclosure_review_required = _needs_ai_disclosure_review(platforms)
        disclosure_reviewed = False
        if disclosure_review_required:
            st.caption(tr("AI Disclosure Review Help"))
            disclosure_reviewed = st.checkbox(
                tr("Confirm AI Disclosure Review"),
                key=f"{upload_key}_ai_disclosure_review",
                help=tr("Confirm AI Disclosure Review Help"),
            )
        video_file_exists = bool(video_path) and os.path.exists(video_path)
        if video_path and not video_file_exists:
            st.warning(tr("Upload Video Missing"))
        can_upload = (
            video_file_exists
            and upload_post.upload_post_service.is_configured()
            and (not public_youtube_upload or public_confirmed)
            and (not disclosure_review_required or disclosure_reviewed)
        )
        if st.button(
            _upload_button_label(platforms),
            key=upload_key,
            disabled=not can_upload,
        ):
            _sync_upload_post_service_from_config()
            upload_metadata = _metadata_from_widget_state(key_prefix, metadata)
            upload_title = (
                upload_metadata.get("title")
                or pending_upload.get("title")
                or job.get("subject")
                or "Check out this video! #shorts #viral"
            )
            youtube_extra = _youtube_extra_for_upload(platforms, upload_metadata)
            with st.spinner(tr("Uploading Video")):
                upload_result = upload_post.cross_post_video(
                    video_path=video_path,
                    title=upload_title,
                    platforms=platforms,
                    youtube_extra=youtube_extra,
                )
            if task_id:
                history.update_pending_upload_result(
                    task_id,
                    video_path,
                    upload_result,
                    disclosure_reviewed=(
                        disclosure_reviewed if disclosure_review_required else None
                    ),
                )
            if upload_result.get("success"):
                st.success(tr("Upload Successful"))
            else:
                st.error(
                    tr("Upload Failed").format(
                        error=upload_result.get("error")
                        or upload_result.get("message")
                        or tr("Unknown Error"),
                    )
                )
            st.rerun()


pending_video_preset = st.session_state.pop("_pending_video_preset", None)
if pending_video_preset:
    try:
        applied_preset_name = _apply_video_preset(pending_video_preset)
        st.success(f"{tr('Preset Loaded')}: {applied_preset_name}")
    except presets.PresetError as e:
        st.error(f"{tr('Preset Error')}: {str(e)}")


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _render_llm_provider_settings():
    st.write(tr("LLM Settings"))
    provider_ids = [provider.provider_id for provider in LLM_PROVIDER_REGISTRY]
    provider_labels = {
        provider.provider_id: tr(provider.label_key)
        if tr(provider.label_key) != provider.label_key
        else provider.default_label
        for provider in LLM_PROVIDER_REGISTRY
    }
    saved_provider = str(
        config.app.get("llm_provider", DEFAULT_LLM_PROVIDER_ID)
    ).casefold()
    if saved_provider not in provider_ids:
        saved_provider = DEFAULT_LLM_PROVIDER_ID
    provider_id = st.selectbox(
        tr("LLM Provider"),
        options=provider_ids,
        index=provider_ids.index(saved_provider),
        format_func=lambda value: provider_labels[value],
        key="llm_provider_select",
    )
    config.app["llm_provider"] = provider_id
    provider = get_llm_provider(provider_id)
    if provider is None:
        st.error(tr("Unsupported LLM Provider"))
        return

    tips = tr(provider.tips_key)
    if tips and tips != provider.tips_key:
        context = {
            "api_key_url": provider.api_key_url,
            "default_model": provider.default_model,
            "default_base_url": provider.default_base_url,
            "docker_hint": "",
            **{
                f"default_{field.config_suffix}": field.default_value
                for field in provider.extra_fields
            },
        }
        try:
            st.markdown(tips.format(**context))
        except (KeyError, ValueError):
            st.caption(tips)

    api_key_name = provider.config_key("api_key")
    api_key = str(config.app.get(api_key_name, "") or "")
    if provider.show_api_key:
        api_key = st.text_input(
            tr("API Key"),
            value=api_key,
            type="password",
            key=f"{provider_id}_api_key_input",
        )
        config.app[api_key_name] = api_key

    base_url_name = provider.config_key("base_url")
    base_url = provider.resolve_base_url(config.app.get(base_url_name, ""))
    if provider.show_base_url:
        base_url = st.text_input(
            tr("Base Url"),
            value=base_url,
            key=f"{provider_id}_base_url_input",
        )
        config.app[base_url_name] = normalize_provider_override(
            base_url, provider.default_base_url
        )

    model_name_key = provider.config_key("model_name")
    model_name = provider.resolve_model_name(config.app.get(model_name_key, ""))
    if provider.requires_model_name:
        if provider_id == "groq":
            groq_models = get_groq_model_ids(api_key, base_url)
        else:
            groq_models = []
        if groq_models:
            if model_name not in groq_models:
                groq_models.insert(0, model_name)
            model_name = st.selectbox(
                tr("Model Name"),
                options=groq_models,
                index=groq_models.index(model_name),
                key="groq_model_name_select",
            )
        else:
            model_name = st.text_input(
                tr("Model Name"),
                value=model_name,
                key=f"{provider_id}_model_name_input",
            )
        config.app[model_name_key] = normalize_provider_override(
            model_name, provider.default_model
        )

    for field in provider.extra_fields:
        config_key = provider.config_key(field.config_suffix)
        value = str(config.app.get(config_key, "") or field.default_value)
        entered = st.text_input(
            tr(field.label_key),
            value=value,
            type="password" if field.secret else "default",
            key=f"{provider_id}_{field.config_suffix}_input",
        )
        config.app[config_key] = normalize_provider_override(
            entered, field.default_value
        )

    if st.button(tr("Test LLM Connection"), key="test_llm_connection_button"):
        with st.spinner(tr("Testing LLM Connection")):
            success, error, elapsed = llm.test_connection()
        if success:
            st.success(
                tr("LLM Connection Test Succeeded").format(
                    provider=provider_labels[provider_id],
                    elapsed=f"{elapsed:.2f}",
                )
            )
        else:
            st.error(
                tr("LLM Connection Test Failed").format(
                    error=error,
                    elapsed=f"{elapsed:.2f}",
                )
            )

# åˆ›å»ºåŸºç¡€è®¾ç½®æŠ˜å æ¡†
if not config.app.get("hide_config", False):
    with st.expander(tr("Basic Settings"), expanded=False):
        config_panels = st.columns(3)
        left_config_panel = config_panels[0]
        middle_config_panel = config_panels[1]
        right_config_panel = config_panels[2]

        with left_config_panel:
            hide_config = st.checkbox(
                tr("Hide Basic Settings"), value=config.app.get("hide_config", False)
            )
            config.app["hide_config"] = hide_config

            hide_log = st.checkbox(
                tr("Hide Log"), value=config.ui.get("hide_log", False)
            )
            config.ui["hide_log"] = hide_log

        with middle_config_panel:
            _render_llm_provider_settings()

        with right_config_panel:
            _render_basic_video_source_settings_panel()

_render_production_step_header(active_step=_current_production_step())
selected_social_platform = _current_social_platform()
params = _initialize_video_params()
(
    selected_social_platform,
    uploaded_files,
    uploaded_audio_file,
    uploaded_bgm_file,
    start_button,
    batch_button,
) = _render_production_workspace(
    params,
    selected_social_platform,
)

show_latest_single_result_visual_review = False
if start_button or batch_button:
    if batch_button:
        config.save_config()

    batch_items = _get_batch_items()
    if batch_button and not batch_items:
        st.error(tr("Please Enter Batch Subjects"))
        scroll_to_bottom()
        st.stop()

    if start_button and not params.video_subject and not params.video_script:
        st.error(tr("Video Script and Subject Cannot Both Be Empty"))
        scroll_to_bottom()
        st.stop()

    # â”€â”€ Ã‡ok kaynaklÄ± kaynak doÄŸrulamasÄ± â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _val_sources = config.app.get("enabled_video_sources", ["pexels"])
    _val_api = [s for s in _val_sources if s not in ("local",)]
    if not _val_sources or (not _val_api and "local" not in _val_sources):
        st.error(tr("Please Select a Valid Video Source"))
        scroll_to_bottom()
        st.stop()

    if "pexels" in _val_sources and not config.app.get("pexels_api_keys", ""):
        st.error(tr("Please Enter the Pexels API Key"))
        scroll_to_bottom()
        st.stop()

    if "pixabay" in _val_sources and not config.app.get("pixabay_api_keys", ""):
        st.error(tr("Please Enter the Pixabay API Key"))
        scroll_to_bottom()
        st.stop()

    if "coverr" in _val_sources and not config.app.get("coverr_api_keys", ""):
        st.error(tr("Please Enter the Coverr API Key"))
        scroll_to_bottom()
        st.stop()
    if "dvids" in _val_sources and not config.app.get("dvids_api_keys", ""):
        st.error(tr("Please Enter the DVIDS API Key"))
        scroll_to_bottom()
        st.stop()
    if "vecteezy" in _val_sources and not config.app.get("vecteezy_api_keys", ""):
        st.error(tr("Please Enter the Vecteezy API Key"))
        scroll_to_bottom()
        st.stop()
    if "vecteezy" in _val_sources and not str(
        config.app.get("vecteezy_account_id") or ""
    ).strip().isdigit():
        st.error(tr("Please Enter the Vecteezy Account ID"))
        scroll_to_bottom()
        st.stop()
    # NASA, Wikimedia, Archive.org â†’ API key gerektirmez, doÄŸrulama gerekmez

    if (
        st.session_state.get("manual_video_selection_enabled")
        and _val_api
        and not st.session_state.get("manual_video_selected_urls")
    ):
        st.error(tr("Please Select at Least One Video Candidate"))
        scroll_to_bottom()
        st.stop()

    current_preflight_inputs = _preflight_input_values(
        params,
        batch_items if batch_button else [],
    )
    current_preflight_report = st.session_state.get("content_preflight_report")
    preflight_warning = _content_preflight_warning_text(
        current_preflight_report,
        current_preflight_inputs["video_subject"],
        current_preflight_inputs["video_script"],
        selected_social_platform,
        current_preflight_inputs["language"],
    )
    if preflight_warning:
        st.warning(preflight_warning)

    fresh_preflight_report = None if preflight_warning else current_preflight_report
    quality_gate_warning = _quality_gate_warning_text(
        content_quality.evaluate_quality_gate(
            fresh_preflight_report,
            enabled=st.session_state.get("viral_quality_gate_enabled", False),
            threshold=st.session_state.get(
                "viral_quality_gate_threshold",
                content_quality.DEFAULT_QUALITY_GATE_THRESHOLD,
            ),
        )
    )
    if quality_gate_warning:
        st.warning(quality_gate_warning)

    repeat_warning_days = history.DEFAULT_SUBJECT_LOOKBACK_DAYS
    repeat_matches = []
    if start_button and params.video_subject:
        repeat_matches = history.find_recent_similar_subjects(
            params.video_subject,
            days=repeat_warning_days,
        )
    elif batch_button:
        seen_repeat_tasks = set()
        for item in batch_items:
            subject_matches = history.find_recent_similar_subjects(
                item.get("subject", ""),
                days=repeat_warning_days,
                limit=1,
            )
            for match in subject_matches:
                task_key = match.get("task_id") or match.get("created_at")
                if task_key in seen_repeat_tasks:
                    continue
                seen_repeat_tasks.add(task_key)
                repeat_matches.append(match)
                if len(repeat_matches) >= 3:
                    break
            if len(repeat_matches) >= 3:
                break
    repeat_warning = _subject_repeat_warning_text(
        repeat_matches,
        days=repeat_warning_days,
    )
    if repeat_warning:
        st.warning(repeat_warning)
        suggestion_subject = params.video_subject if start_button else ""
        if not suggestion_subject and repeat_matches:
            suggestion_subject = repeat_matches[0].get("subject", "")
        repeat_suggestions = _subject_repeat_suggestion_text(suggestion_subject)
        if repeat_suggestions:
            st.info(repeat_suggestions)

    if start_button:
        task_id = str(uuid4())
        try:
            prepared_params = _prepare_task_params(
                task_id,
                params,
                uploaded_audio_file,
                uploaded_bgm_file,
                uploaded_files,
            )
            reusable_voice_preview = _prepare_reusable_voice_preview(
                task_id,
                prepared_params,
            )
        except Exception:
            logger.exception(
                f"Failed to prepare WebUI generation task: task_id={task_id}"
            )
            st.error(tr("Video Generation Failed"))
            scroll_to_bottom()
            st.stop()

        logger.info(tr("Start Generating Video"))
        logger.info(utils.to_json(prepared_params))
        _render_application(
            True,
            task_id=task_id,
            params=prepared_params,
            platform=selected_social_platform,
            generate_social_metadata=st.session_state.get(
                "auto_social_metadata_after_video",
                True,
            ),
            generate_viral_analysis=st.session_state.get(
                "auto_viral_analysis_after_video",
                False,
            ),
            voice_preview=reusable_voice_preview,
        )
        scroll_to_bottom()
        st.stop()

    log_container = st.empty()
    log_records = []
    log_lock = threading.Lock()
    ui_thread = threading.current_thread()

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_lock:
            log_records.append(msg)
            log_text = "\n".join(log_records)
        if threading.current_thread() is not ui_thread:
            return
        with log_container:
            st.code(log_text)

    logger.add(log_received)

    def run_generation(run_task_id, run_params, require_upload_review=None):
        prepared_params = _prepare_task_params(
            run_task_id,
            run_params,
            uploaded_audio_file,
            uploaded_bgm_file,
            uploaded_files,
        )
        reusable_voice_preview = _prepare_reusable_voice_preview(
            run_task_id,
            prepared_params,
        )
        logger.info(tr("Start Generating Video"))
        logger.info(utils.to_json(prepared_params))
        return tm.start(
            task_id=run_task_id,
            params=prepared_params,
            require_upload_review=require_upload_review,
            voice_preview=reusable_voice_preview,
        )

    scroll_to_bottom()

    if batch_button:
        st.toast(tr("Generating Batch Video"))
        batch_progress = st.progress(0)
        batch_results = []
        rendered_batch_jobs = []
        failed_subjects = []
        partial_subjects = []

        for index, item in enumerate(batch_items, start=1):
            subject = item["subject"]
            task_id = str(uuid4())
            run_params = _clone_video_params(params)
            run_params.video_subject = subject
            run_params.video_script = item.get("script", "")
            run_params.video_terms = None
            logger.info(
                f"{tr('Generating Batch Video')} "
                f"{index}/{len(batch_items)}: {subject}"
            )
            result = run_generation(
                task_id,
                run_params,
                require_upload_review=True,
            )
            if not result or "videos" not in result:
                failed_subjects.append(subject)
                logger.error(f"{tr('Video Generation Failed')}: {subject}")
                _record_history(
                    task_id,
                    run_params,
                    result=_failed_generation_history_result(task_id),
                    error=tr("Video Generation Failed"),
                )
            else:
                rendered_batch_jobs.append(
                    {
                        "subject": subject,
                        "task_id": task_id,
                        "run_params": run_params,
                        "result": result,
                    }
                )
            batch_progress.progress(index / len(batch_items))

        generate_social_metadata = st.session_state.get(
            "auto_social_metadata_after_video",
            True,
        )
        generate_viral_analysis = st.session_state.get(
            "auto_viral_analysis_after_video",
            False,
        )

        def enrich_rendered_batch_job(job):
            return _enrich_batch_result_with_network_analysis(
                job,
                generate_social_metadata=generate_social_metadata,
                generate_viral_analysis=generate_viral_analysis,
                platform=selected_social_platform,
            )

        if generate_social_metadata or generate_viral_analysis:
            spinner_text = (
                tr("Generating Social Metadata")
                if generate_social_metadata
                else tr("Generating Viral Analysis")
            )
            with st.spinner(spinner_text):
                enrichment_outcomes = batch_postprocessing.run_network_postprocessing(
                    rendered_batch_jobs,
                    enrich_rendered_batch_job,
                    max_workers=config.app.get("batch_network_workers", 2),
                )
        else:
            enrichment_outcomes = batch_postprocessing.run_network_postprocessing(
                rendered_batch_jobs,
                enrich_rendered_batch_job,
                max_workers=1,
            )

        for outcome in enrichment_outcomes:
            job = outcome["job"]
            task_id = job["task_id"]
            subject = job["subject"]
            run_params = job["run_params"]
            result = job["result"]
            enrichment = outcome.get("result") if outcome.get("ok") else {}
            if not outcome.get("ok"):
                logger.warning(
                    "Batch network post-processing failed; keeping rendered video."
                )
            metadata = (enrichment or {}).get("metadata")
            viral_analysis = (enrichment or {}).get("viral_analysis")
            if generate_viral_analysis and viral_analysis:
                with st.spinner(tr("Generating Thumbnail Candidates")):
                    _attach_thumbnail_candidates(task_id, result, viral_analysis)
            _record_history(
                task_id,
                run_params,
                result=result,
                metadata=metadata,
                viral_analysis=viral_analysis,
            )
            if result.get("partial_success"):
                partial_subjects.append(subject)
            batch_results.append(
                {
                    "subject": subject,
                    "task_id": task_id,
                    "videos": result.get("videos", []),
                    "material_attributions": result.get("material_attributions"),
                    "metadata": metadata,
                    "viral_analysis": viral_analysis,
                    "thumbnail_candidates": result.get("thumbnail_candidates"),
                    "thumbnail_candidate_error": result.get(
                        "thumbnail_candidate_error", ""
                    ),
                    "cooldown": result.get("cooldown"),
                    "video_encoder_results": result.get("video_encoder_results"),
                    "render_quality_reports": result.get("render_quality_reports"),
                    "visual_pacing": result.get("visual_pacing"),
                    "pending_uploads": result.get("pending_uploads"),
                    "partial_success": bool(result.get("partial_success")),
                    "failed_aspects": result.get("failed_aspects"),
                }
            )

        with log_lock:
            completed_batch_log = "\n".join(log_records)
        if completed_batch_log:
            with log_container:
                st.code(completed_batch_log)

        if failed_subjects:
            st.error(
                f"{tr('Batch Generation Finished With Failures')}: "
                f"{', '.join(failed_subjects)}"
            )
        elif partial_subjects:
            st.warning(
                tr("Batch Generation Completed With Partial Outputs").format(
                    subjects=", ".join(partial_subjects)
                )
            )
        else:
            st.success(tr("Batch Generation Completed"))

        for item in batch_results:
            with st.expander(item["subject"], expanded=False):
                _render_partial_success_warning(item)
                for url in item["videos"]:
                    st.video(url)
                _render_material_sources(item)
                encoder_results = item.get("video_encoder_results") or []
                if encoder_results:
                    st.write(tr("Video Encoder"))
                    _render_video_encoder_results(encoder_results)
                render_quality_reports = item.get("render_quality_reports") or []
                if render_quality_reports:
                    st.write(tr("Render Quality"))
                    _render_render_quality_reports(render_quality_reports)
                visual_pacing = item.get("visual_pacing")
                if visual_pacing:
                    st.write(tr("Visual Pacing"))
                    _render_visual_pacing_report(visual_pacing)
                cooldown_summary = _cooldown_summary_text(item.get("cooldown"))
                if cooldown_summary:
                    st.caption(cooldown_summary)
                if item.get("metadata"):
                    st.text_input(
                        tr("Social Title"),
                        value=item["metadata"].get("title", ""),
                        key=f"batch_title_{item['task_id']}",
                    )
                    st.text_area(
                        tr("Social Description"),
                        value=item["metadata"].get("caption", ""),
                        height=90,
                        key=f"batch_caption_{item['task_id']}",
                    )
                    st.text_input(
                        tr("Social Hashtags"),
                        value=" ".join(item["metadata"].get("hashtags", [])),
                        key=f"batch_hashtags_{item['task_id']}",
                    )
                if item.get("viral_analysis"):
                    _render_viral_analysis(
                        item["viral_analysis"],
                        key_prefix=f"batch_viral_{item['task_id']}",
                    )
                _render_thumbnail_candidates(
                    item.get("thumbnail_candidates"),
                    item.get("thumbnail_candidate_error", ""),
                    key_prefix=f"batch_thumb_{item['task_id']}",
                )
                if st.button(
                    tr("Open Task Folder"),
                    key=f"open_batch_task_{item['task_id']}",
                ):
                    open_task_folder(item["task_id"])
    else:
        st.toast(tr("Generating Video"))
        task_id = str(uuid4())
        result = run_generation(task_id, params)
        if not result or "videos" not in result:
            st.error(tr("Video Generation Failed"))
            logger.error(tr("Video Generation Failed"))
            _record_history(
                task_id,
                params,
                result=_failed_generation_history_result(task_id),
                error=tr("Video Generation Failed"),
            )
            scroll_to_bottom()
            st.stop()

        video_files = result.get("videos", [])
        metadata = None
        if st.session_state.get("auto_social_metadata_after_video", True):
            with st.spinner(tr("Generating Social Metadata")):
                metadata = _generate_social_metadata_for_result(
                    params,
                    result,
                    selected_social_platform,
                )
            st.session_state["social_metadata"] = metadata
        viral_analysis = None
        if st.session_state.get("auto_viral_analysis_after_video", False):
            with st.spinner(tr("Generating Viral Analysis")):
                viral_analysis = _generate_viral_analysis_for_result(
                    params,
                    result=result,
                    metadata=metadata,
                    platform=selected_social_platform,
                )
            with st.spinner(tr("Generating Thumbnail Candidates")):
                _attach_thumbnail_candidates(task_id, result, viral_analysis)
            st.session_state["viral_analysis"] = viral_analysis
        _record_history(
            task_id,
            params,
            result=result,
            metadata=metadata,
            viral_analysis=viral_analysis,
        )
        if video_files:
            st.session_state["latest_single_result_task_id"] = task_id
            show_latest_single_result_visual_review = True
        _render_generation_completion_status(result)
        cooldown_summary = _cooldown_summary_text(result.get("cooldown"))
        if cooldown_summary:
            st.caption(cooldown_summary)
        try:
            if video_files:
                player_cols = st.columns(len(video_files) * 2 + 1)
                for i, url in enumerate(video_files):
                    player_cols[i * 2 + 1].video(url)
        except Exception:
            pass
        _render_material_sources(result)
        encoder_results = result.get("video_encoder_results") or []
        if encoder_results:
            st.write(tr("Video Encoder"))
            _render_video_encoder_results(encoder_results)
        render_quality_reports = result.get("render_quality_reports") or []
        if render_quality_reports:
            st.write(tr("Render Quality"))
            _render_render_quality_reports(render_quality_reports)
        visual_pacing = result.get("visual_pacing")
        if visual_pacing:
            st.write(tr("Visual Pacing"))
            _render_visual_pacing_report(visual_pacing)

        if metadata:
            st.text_input(
                tr("Social Title"),
                value=metadata.get("title", ""),
                key=f"single_title_{task_id}",
            )
            st.text_area(
                tr("Social Description"),
                value=metadata.get("caption", ""),
                height=90,
                key=f"single_caption_{task_id}",
            )
            st.text_input(
                tr("Social Hashtags"),
                value=" ".join(metadata.get("hashtags", [])),
                key=f"single_hashtags_{task_id}",
            )
        if viral_analysis:
            _render_viral_analysis(
                viral_analysis,
                key_prefix=f"single_viral_{task_id}",
            )
        _render_thumbnail_candidates(
            result.get("thumbnail_candidates"),
            result.get("thumbnail_candidate_error", ""),
            key_prefix=f"single_thumb_{task_id}",
        )

        open_task_folder(task_id)
        logger.info(tr("Video Generation Completed"))

    scroll_to_bottom()

if show_latest_single_result_visual_review or not (start_button or batch_button):
    _render_latest_single_result_visual_review()

_render_application(False)
