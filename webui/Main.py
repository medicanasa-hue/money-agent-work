import json
import os
import sys
import webbrowser
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import (
    content_intelligence,
    history,
    llm,
    material,
    presets,
    upload_post,
    viral_analyzer,
    voice,
)
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
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
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]


def _parse_chatterbox_voices(voices):
    # Chatterbox 是自托管服务，音色列表由用户在 WebUI 中手动输入。
    # 这里统一兼容 TOML 数组和输入框里的逗号分隔字符串，避免下拉框、
    # 试听按钮和后续生成流程使用不同格式导致状态不一致。
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit 的按钮会触发整页 rerun，而 Chatterbox 配置输入框位于
    # “试听语音合成”按钮之后。如果试听时只读取 config.chatterbox，可能拿不到
    # 用户刚在输入框里填入的 base_url/model/voices。先从 session_state 同步一次，
    # 可以保证按钮逻辑和输入框显示逻辑使用同一份最新配置。
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
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # 即使请求 response_format=mp3，也会返回 WAV 内容。WebUI 试听如果固定
    # 使用 audio/mp3，浏览器可能无法播放，因此这里按文件头识别真实格式。
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


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


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
if "batch_subjects" not in st.session_state:
    st.session_state["batch_subjects"] = ""
if "use_manual_batch_scripts" not in st.session_state:
    st.session_state["use_manual_batch_scripts"] = False
if "batch_script_blocks" not in st.session_state:
    st.session_state["batch_script_blocks"] = ""
if "content_plan" not in st.session_state:
    st.session_state["content_plan"] = None
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
if "auto_viral_analysis_after_video" not in st.session_state:
    st.session_state["auto_viral_analysis_after_video"] = False
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

# 加载语言文件
locales = utils.load_locales(i18n_dir)

# 创建一个顶部栏，包含标题和语言选择
title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"MoneyPrinterTurbo v{config.project_version}")

with lang_col:
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
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
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        file_path = record["file"].path
        relative_path = os.path.relpath(file_path, root_dir)
        record["file"].path = f"./{relative_path}"
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get("Translation", {}).get(key, key)

_VIDEO_SOURCE_KEYS = {
    "pexels",
    "pixabay",
    "coverr",
    "nasa",
    "wikimedia",
    "archive_org",
    "local",
}


def _plain_value(value):
    return getattr(value, "value", value)


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
        if file.lower().endswith(supported_exts)
    )


def _prepare_task_params(
    task_id,
    source_params,
    uploaded_audio,
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

    if uploaded_video_files:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        run_params.video_materials = []
        persisted_local_materials = []
        for file in uploaded_video_files:
            file_path = os.path.join(local_videos_dir, f"{file.file_id}_{file.name}")
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
            if m.url:
                run_params.video_materials.append(m)

    selected_manual_materials = _selected_manual_video_materials()
    if run_params.video_source != "local" and selected_manual_materials:
        run_params.video_materials = selected_manual_materials

    return run_params


def _generate_social_metadata_for_result(run_params, result, platform):
    return llm.generate_social_metadata(
        video_subject=run_params.video_subject,
        video_script=(result or {}).get("script") or run_params.video_script,
        language=run_params.video_language or "auto",
        platform=platform,
    )


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
        video_duration_sec=None,
        target_platforms=[platform] if platform else None,
        language=run_params.video_language or "auto",
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


def _record_history(
    task_id,
    run_params,
    result=None,
    metadata=None,
    viral_analysis=None,
    error="",
):
    status = "failed" if error else "completed"
    history.add_history(
        {
            "task_id": task_id,
            "subject": run_params.video_subject,
            "status": status,
            "videos": (result or {}).get("videos", []),
            "materials": (result or {}).get("materials", []),
            "material_attributions": (result or {}).get("material_attributions"),
            "terms": (result or {}).get("terms") or run_params.video_terms,
            "metadata": metadata,
            "viral_analysis": viral_analysis,
            "cooldown": (result or {}).get("cooldown"),
            "pending_uploads": (result or {}).get("pending_uploads"),
            "error": error,
        }
    )


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
        video_file_exists = bool(video_path) and os.path.exists(video_path)
        if video_path and not video_file_exists:
            st.warning(tr("Upload Video Missing"))
        can_upload = (
            video_file_exists
            and upload_post.upload_post_service.is_configured()
            and (not public_youtube_upload or public_confirmed)
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

# 创建基础设置折叠框
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
            st.write(tr("LLM Settings"))
            # 下拉框展示文本和后端 provider id 分开维护，避免 UI 文案变化
            # 污染 `config.app["llm_provider"]` 这类稳定配置值。
            aihubmix_label = f"AIHubMix ({tr('Recommended')})"
            if config.ui.get("language") == "zh":
                aihubmix_label = "AIHubMix（推荐）"
            llm_provider_options = [
                ("OpenAI", "openai"),
                (aihubmix_label, "aihubmix"),
                ("AIML API", "aimlapi"),
                ("EvoLink", "evolink"),
                ("VolcEngine", "volcengine"),
                ("Moonshot", "moonshot"),
                ("Azure", "azure"),
                ("Qwen", "qwen"),
                ("DeepSeek", "deepseek"),
                ("ModelScope", "modelscope"),
                ("Gemini", "gemini"),
                ("Grok", "grok"),
                ("Groq", "groq"),
                ("Ollama", "ollama"),
                ("G4f", "g4f"),
                ("OneAPI", "oneapi"),
                ("Cloudflare", "cloudflare"),
                ("ERNIE", "ernie"),
                ("MiniMax", "minimax"),
                ("MiMo", "mimo"),
                ("Pollinations", "pollinations"),
                ("LiteLLM", "litellm"),
            ]
            llm_provider_ids = [provider_id for _, provider_id in llm_provider_options]
            llm_provider_labels = {
                provider_id: label for label, provider_id in llm_provider_options
            }
            saved_llm_provider = config.app.get("llm_provider", "openai").lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = "openai"

            if st.session_state.get("llm_provider_select") not in (
                None,
                *llm_provider_ids,
            ):
                del st.session_state["llm_provider_select"]

            llm_provider = st.selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                index=llm_provider_ids.index(saved_llm_provider),
                format_func=lambda provider_id: llm_provider_labels[provider_id],
                key="llm_provider_select",
            )
            llm_helper = st.container()
            config.app["llm_provider"] = llm_provider

            llm_api_key = config.app.get(f"{llm_provider}_api_key", "")
            llm_secret_key = config.app.get(f"{llm_provider}_secret_key", "")
            llm_base_url = config.app.get(f"{llm_provider}_base_url", "")
            llm_model_name = config.app.get(f"{llm_provider}_model_name", "")
            llm_account_id = config.app.get(f"{llm_provider}_account_id", "")

            tips = ""
            if llm_provider == "ollama":
                if not llm_model_name:
                    llm_model_name = "qwen:7b"
                if not llm_base_url:
                    llm_base_url = config.get_default_ollama_base_url()

                with llm_helper:
                    docker_hint = ""
                    if config.is_running_in_container():
                        docker_hint = "\n                            > 检测到容器环境，未配置 Base Url 时会默认使用 `http://host.docker.internal:11434/v1`\n"
                    tips = f"""
                            ##### Ollama配置说明
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 一般为 http://localhost:11434/v1
                                - 如果 `MoneyPrinterTurbo` 和 `Ollama` **不在同一台机器上**，需要填写 `Ollama` 机器的IP地址
                                - 如果 `MoneyPrinterTurbo` 是 `Docker` 部署，建议填写 `http://host.docker.internal:11434/v1`{docker_hint}
                            - **Model Name**: 使用 `ollama list` 查看，比如 `qwen:7b`
                            """

            if llm_provider == "openai":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### OpenAI 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://platform.openai.com/api-keys)
                            - **Base Url**: 官方 OpenAI 可留空；如果使用 OpenAI 兼容供应商（例如 OpenRouter），请填写对应的兼容接口地址
                            - **Model Name**: 填写**有权限**的模型；如果使用兼容供应商，请填写该平台支持的模型 ID
                            """

            if llm_provider == "aihubmix":
                if not llm_model_name:
                    llm_model_name = "gpt-5.4-mini"
                if not llm_base_url:
                    llm_base_url = "https://aihubmix.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIHubMix 配置说明
                            - **API Key**: 在 AIHubMix 控制台创建 API Key
                            - **Base Url**: 预填 https://aihubmix.com/v1
                            - **Model Name**: 默认 gpt-5.4-mini，也可以填写 AIHubMix 支持的其它模型 ID
                            """

            if llm_provider == "aimlapi":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                if not llm_base_url:
                    llm_base_url = "https://api.aimlapi.com/v1"
                with llm_helper:
                    tips = """
                            ##### AIML API Configuration
                            - **API Key**: create one at https://aimlapi.com/app/keys
                            - **Base Url**: https://api.aimlapi.com/v1
                            - **Model Name**: for example `openai/gpt-4o-mini`, `openai/gpt-4o`, `anthropic/claude-sonnet-4.5`, or `google/gemini-3-flash-preview`
                            """

            if llm_provider == "evolink":
                if not llm_model_name:
                    llm_model_name = "gpt-5.5"
                if not llm_base_url:
                    llm_base_url = "https://direct.evolink.ai/v1"
                with llm_helper:
                    tips = """
                            ##### EvoLink 配置说明
                            - **API Key**: [点击到官网申请](https://evolink.ai/dashboard/keys)
                            - **Base Url**: 默认 https://direct.evolink.ai/v1
                            - **Model Name**: 默认 gpt-5.5，也可以填写 EvoLink 支持的其它模型 ID
                            """

            if llm_provider == "volcengine":
                if not llm_model_name:
                    llm_model_name = "doubao-seed-2-1-turbo-260628"
                if not llm_base_url:
                    llm_base_url = "https://ark.cn-beijing.volces.com/api/v3"
                with llm_helper:
                    tips = """
                            ##### VolcEngine Ark 配置说明
                            - **注册链接**: [点击注册 火山引擎](https://www.volcengine.com/activity/ai618?utm_campaign=hw&utm_content=hw&utm_medium=devrel_tool_web&utm_source=OWO&utm_term=MoneyPrinterTurbo)
                            - **API Key**: 在火山引擎方舟控制台创建 API Key
                            - **Base Url**: 默认 https://ark.cn-beijing.volces.com/api/v3
                            - **Model Name**: 填写 Ark 控制台已开通的模型 ID，例如 doubao-seed-2-1-turbo-260628
                            """

            if llm_provider == "moonshot":
                if not llm_model_name:
                    llm_model_name = "moonshot-v1-8k"
                with llm_helper:
                    tips = """
                            ##### Moonshot 配置说明
                            - **API Key**: [点击到官网申请](https://platform.moonshot.cn/console/api-keys)
                            - **Base Url**: 固定为 https://api.moonshot.cn/v1
                            - **Model Name**: 比如 moonshot-v1-8k，[点击查看模型列表](https://platform.moonshot.cn/docs/intro#%E6%A8%A1%E5%9E%8B%E5%88%97%E8%A1%A8)
                            """
            if llm_provider == "oneapi":
                if not llm_model_name:
                    llm_model_name = "claude-3-5-sonnet-20240620"
                with llm_helper:
                    tips = """
                        ##### OneAPI 配置说明
                        - **API Key**: 填写您的 OneAPI 密钥
                        - **Base Url**: 填写 OneAPI 的基础 URL
                        - **Model Name**: 填写您要使用的模型名称，例如 claude-3-5-sonnet-20240620
                        """

            if llm_provider == "qwen":
                if not llm_model_name:
                    llm_model_name = "qwen-max"
                with llm_helper:
                    tips = """
                            ##### 通义千问Qwen 配置说明
                            - **API Key**: [点击到官网申请](https://dashscope.console.aliyun.com/apiKey)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 qwen-max，[点击查看模型列表](https://help.aliyun.com/zh/dashscope/developer-reference/model-introduction#3ef6d0bcf91wy)
                            """

            if llm_provider == "g4f":
                if not llm_model_name:
                    llm_model_name = "gpt-3.5-turbo"
                with llm_helper:
                    tips = """
                            ##### gpt4free 配置说明
                            > [GitHub开源项目](https://github.com/xtekky/gpt4free)，可以免费使用GPT模型，但是**稳定性较差**
                            - **API Key**: 随便填写，比如 123
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gpt-3.5-turbo，[点击查看模型列表](https://github.com/xtekky/gpt4free/blob/main/g4f/models.py#L308)
                            """
            if llm_provider == "azure":
                with llm_helper:
                    tips = """
                            ##### Azure 配置说明
                            > [点击查看如何部署模型](https://learn.microsoft.com/zh-cn/azure/ai-services/openai/how-to/create-resource)
                            - **API Key**: [点击到Azure后台创建](https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI)
                            - **Base Url**: 留空
                            - **Model Name**: 填写你实际的部署名
                            """

            if llm_provider == "gemini":
                if not llm_model_name:
                    llm_model_name = "gemini-1.0-pro"

                with llm_helper:
                    tips = """
                            ##### Gemini 配置说明
                            > 需要VPN开启全局流量模式
                            - **API Key**: [点击到官网申请](https://ai.google.dev/)
                            - **Base Url**: 留空
                            - **Model Name**: 比如 gemini-1.0-pro
                            """

            if llm_provider == "grok":
                if not llm_model_name:
                    llm_model_name = "grok-4.3"
                if not llm_base_url:
                    llm_base_url = "https://api.x.ai/v1"

                with llm_helper:
                    tips = """
                            ##### Grok 配置说明
                            - **API Key**: 填写您的 GrokAPI 密钥
                            - **Base Url**: 填写 GrokAPI 的基础 URL
                            - **Model Name**: 比如 grok-4.3
                            """

            if llm_provider == "groq":
                if not llm_model_name:
                    llm_model_name = "llama-3.3-70b-versatile"
                if not llm_base_url:
                    llm_base_url = "https://api.groq.com/openai/v1"

                with llm_helper:
                    tips = """
                            ##### Groq 配置说明
                            - **API Key**: [点击到官网申请](https://console.groq.com/keys)
                            - **Base Url**: 固定为 https://api.groq.com/openai/v1
                            - **Model Name**: 比如 llama-3.3-70b-versatile
                            """

            if llm_provider == "deepseek":
                if not llm_model_name:
                    llm_model_name = "deepseek-chat"
                if not llm_base_url:
                    llm_base_url = "https://api.deepseek.com"
                with llm_helper:
                    tips = """
                            ##### DeepSeek 配置说明
                            - **API Key**: [点击到官网申请](https://platform.deepseek.com/api_keys)
                            - **Base Url**: 固定为 https://api.deepseek.com
                            - **Model Name**: 固定为 deepseek-chat
                            """

            if llm_provider == "mimo":
                if not llm_model_name:
                    llm_model_name = "mimo-v2.5-pro"
                if not llm_base_url:
                    llm_base_url = "https://api.xiaomimimo.com/v1"
                with llm_helper:
                    tips = """
                            ##### Xiaomi MiMo 配置说明
                            - **API Key**: [点击到官网申请](https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call)
                            - **Base Url**: 固定为 https://api.xiaomimimo.com/v1
                            - **Model Name**: 默认 mimo-v2.5-pro，也可以按官方文档填写其它可用模型
                            """

            if llm_provider == "modelscope":
                if not llm_model_name:
                    llm_model_name = "Qwen/Qwen3-32B"
                if not llm_base_url:
                    llm_base_url = "https://api-inference.modelscope.cn/v1/"
                with llm_helper:
                    tips = """
                            ##### ModelScope 配置说明
                            - **API Key**: [点击到官网申请](https://modelscope.cn/docs/model-service/API-Inference/intro)
                            - **Base Url**: 固定为 https://api-inference.modelscope.cn/v1/
                            - **Model Name**: 比如 Qwen/Qwen3-32B，[点击查看模型列表](https://modelscope.cn/models?filter=inference_type&page=1)
                            """

            if llm_provider == "ernie":
                with llm_helper:
                    tips = """
                            ##### 百度文心一言 配置说明
                            - **API Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Secret Key**: [点击到官网申请](https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application)
                            - **Base Url**: 填写 **请求地址** [点击查看文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/s/jlil56u11#%E8%AF%B7%E6%B1%82%E8%AF%B4%E6%98%8E)
                            """

            if llm_provider == "pollinations":
                if not llm_model_name:
                    llm_model_name = "default"
                with llm_helper:
                    tips = """
                            ##### Pollinations AI Configuration
                            - **API Key**: Optional - Leave empty for public access
                            - **Base Url**: Default is https://text.pollinations.ai/openai
                            - **Model Name**: Use 'openai-fast' or specify a model name
                            """

            if llm_provider == "litellm":
                if not llm_model_name:
                    llm_model_name = "openai/gpt-4o-mini"
                with llm_helper:
                    tips = """
                            ##### LiteLLM Configuration
                            > [LiteLLM](https://github.com/BerriAI/litellm) routes to 100+ LLM providers via a unified interface.
                            > Set your provider's API key as an env var: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.
                            - **Model Name**: LiteLLM format — `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`, `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0`, `gemini/gemini-2.5-flash`. See [full provider list](https://docs.litellm.ai/docs/providers)
                            """

            if tips and config.ui["language"] == "zh":
                st.info(tips)

            st_llm_api_key = st.text_input(
                tr("API Key"), value=llm_api_key, type="password"
            )
            st_llm_base_url = st.text_input(tr("Base Url"), value=llm_base_url)
            st_llm_model_name = ""
            if llm_provider != "ernie":
                if llm_provider == "groq":
                    effective_api_key = st_llm_api_key or llm_api_key
                    effective_base_url = st_llm_base_url or llm_base_url
                    groq_models = get_groq_model_ids(
                        api_key=effective_api_key,
                        base_url=effective_base_url,
                    )

                    if groq_models:
                        selected_index = 0
                        if llm_model_name in groq_models:
                            selected_index = groq_models.index(llm_model_name)

                        st_llm_model_name = st.selectbox(
                            tr("Model Name"),
                            options=groq_models,
                            index=selected_index,
                            key="groq_model_name_select",
                        )
                    else:
                        st_llm_model_name = st.text_input(
                            tr("Model Name"),
                            value=llm_model_name,
                            key="groq_model_name_input",
                        )
                        if effective_api_key:
                            st.caption(
                                "Unable to load Groq model list right now. You can still enter a model name manually — note it won't be validated until generation."
                            )
                        else:
                            st.caption(
                                "Add a Groq API key to load available models automatically."
                            )
                else:
                    st_llm_model_name = st.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key=f"{llm_provider}_model_name_input",
                    )
                if st_llm_model_name:
                    config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            else:
                st_llm_model_name = None

            if st_llm_api_key:
                config.app[f"{llm_provider}_api_key"] = st_llm_api_key
            if st_llm_base_url:
                config.app[f"{llm_provider}_base_url"] = st_llm_base_url
            if st_llm_model_name:
                config.app[f"{llm_provider}_model_name"] = st_llm_model_name
            if llm_provider == "ernie":
                st_llm_secret_key = st.text_input(
                    tr("Secret Key"), value=llm_secret_key, type="password"
                )
                config.app[f"{llm_provider}_secret_key"] = st_llm_secret_key

            if llm_provider == "cloudflare":
                st_llm_account_id = st.text_input(
                    tr("Account ID"), value=llm_account_id
                )
                if st_llm_account_id:
                    config.app[f"{llm_provider}_account_id"] = st_llm_account_id

        with right_config_panel:

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

llm_provider = config.app.get("llm_provider", "").lower()
panel = st.columns(3)
left_panel = panel[0]
middle_panel = panel[1]
right_panel = panel[2]

params = VideoParams(video_subject="")
params.match_materials_to_script = bool(
    st.session_state.get("match_materials_to_script", False)
)
params.smart_scene_queries = bool(st.session_state.get("smart_scene_queries", False))
uploaded_files = []
uploaded_audio_file = None

with left_panel:
    with st.container(border=True):
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
            tr("Video Script"), height=280, key="video_script"
        ).strip()
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

with middle_panel:
    with st.container(border=True):
        st.write(tr("Video Settings"))
        video_concat_modes = [
            (tr("Sequential"), "sequential"),
            (tr("Random"), "random"),
        ]
        saved_video_concat_mode = config.ui.get(
            "video_concat_mode", VideoConcatMode.random.value
        )

        # ── Çok Kaynaklı Video Seçimi ────────────────────────────────────
        _API_SOURCES = {
            "pexels":       "Pexels",
            "pixabay":      "Pixabay",
            "coverr":       "Coverr",
            "nasa":         "NASA Image Library",
            "wikimedia":    "Wikimedia Commons",
            "archive_org":  "Internet Archive",
        }
        _ALL_SOURCES = {**_API_SOURCES, "local": tr("Local file")}

        _saved_sources = config.app.get("enabled_video_sources", ["pexels", "pixabay", "coverr"])
        if isinstance(_saved_sources, str):
            _saved_sources = [_saved_sources]
        _saved_sources = [s for s in _saved_sources if s in _ALL_SOURCES]
        if not _saved_sources:
            _saved_sources = ["pexels", "pixabay", "coverr"]

        enabled_video_sources = st.multiselect(
            tr("Video Sources"),
            options=list(_ALL_SOURCES.keys()),
            default=_saved_sources,
            format_func=lambda x: _ALL_SOURCES[x],
            help="Birden fazla kaynak seçildiğinde hepsi paralel aranır ve en uygun klipler otomatik seçilir.",
        )
        if not enabled_video_sources:
            enabled_video_sources = ["pexels"]
            st.caption("⚠️ En az bir kaynak seçin. Varsayılan olarak Pexels kullanılıyor.")

        config.app["enabled_video_sources"] = enabled_video_sources

        # task.py ile geriye dönük uyumluluk için params.video_source belirlenir
        _api_sel = [s for s in enabled_video_sources if s in _API_SOURCES]
        if len(_api_sel) > 1:
            params.video_source = "multi"
        elif len(_api_sel) == 1:
            params.video_source = _api_sel[0]
        elif "local" in enabled_video_sources:
            params.video_source = "local"
        else:
            params.video_source = "pexels"
        config.app["video_source"] = params.video_source

        # Yerel dosya yükleyici — sadece "local" seçiliyse göster
        if "local" in enabled_video_sources:
            local_file_types = ["mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"]
            uploaded_files = st.file_uploader(
                tr("Upload Local Files"),
                type=local_file_types + [file_type.upper() for file_type in local_file_types],
                accept_multiple_files=True,
            )

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

        # 视频转场模式
        video_transition_modes = [
            (tr("None"), VideoTransitionMode.none.value),
            (tr("Shuffle"), VideoTransitionMode.shuffle.value),
            (tr("FadeIn"), VideoTransitionMode.fade_in.value),
            (tr("FadeOut"), VideoTransitionMode.fade_out.value),
            (tr("SlideIn"), VideoTransitionMode.slide_in.value),
            (tr("SlideOut"), VideoTransitionMode.slide_out.value),
        ]
        saved_video_transition_mode = config.ui.get("video_transition_mode")
        selected_index = st.selectbox(
            tr("Video Transition Mode"),
            options=range(len(video_transition_modes)),
            format_func=lambda x: video_transition_modes[x][0],
            index=_find_option_index(
                video_transition_modes, saved_video_transition_mode, 0
            ),
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
            (tr("Landscape"), VideoAspect.landscape.value),
        ]
        # Sadece Coverr seçiliyse varsayılan Landscape (Coverr büyük çoğunlukla 16:9)
        saved_video_aspect = config.ui.get("video_aspect")
        default_aspect_index = _find_option_index(
            video_aspect_ratios,
            saved_video_aspect,
            1 if enabled_video_sources == ["coverr"] else 0,
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

        if _api_sel:
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
                    use_container_width=True,
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
                        st.caption(tr("Selected Video Preview"))
                        for preview_url in selected_urls[:3]:
                            st.video(preview_url)
                elif st.session_state.get("manual_video_candidates") == []:
                    st.caption(tr("No Video Candidates Found"))

        video_count_options = [1, 2, 3, 4, 5]
        saved_video_count = _config_int(config.ui, "video_count", 1)
        params.video_count = st.selectbox(
            tr("Number of Videos Generated Simultaneously"),
            options=video_count_options,
            index=_find_option_index(video_count_options, saved_video_count, 0),
        )
        config.ui["video_count"] = params.video_count

        with st.expander(tr("Advanced Video Settings"), expanded=False):
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
            st.selectbox(
                tr("Recently Used Video Window"),
                options=cooldown_day_options,
                index=cooldown_day_options.index(current_cooldown_days),
                format_func=lambda days: tr("Cooldown Days Label").format(days=days),
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
                current_preset = _normalize_libx264_preset(
                    st.session_state["video_encoder_preset"]
                )
                st.selectbox(
                    tr("Encoder Preset"),
                    options=preset_options,
                    index=preset_options.index(current_preset),
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
            _apply_video_quality_params(params)
    with st.container(border=True):
        st.write(tr("Audio Settings"))

        tts_servers = [
            (voice.NO_VOICE_NAME, tr("No Voice")),
            ("azure-tts-v1", "Azure TTS V1"),
            ("azure-tts-v2", "Azure TTS V2"),
            ("siliconflow", "SiliconFlow TTS"),
            ("gemini-tts", "Google Gemini TTS"),
            ("mimo-tts", "Xiaomi MiMo TTS"),
            ("elevenlabs", "ElevenLabs TTS"),
            ("chatterbox", "Chatterbox TTS"),
        ]

        saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
        saved_tts_server_index = 0
        for i, (server_value, _) in enumerate(tts_servers):
            if server_value == saved_tts_server:
                saved_tts_server_index = i
                break

        selected_tts_server_index = st.selectbox(
            tr("TTS Servers"),
            options=range(len(tts_servers)),
            format_func=lambda x: tts_servers[x][1],
            index=saved_tts_server_index,
        )

        selected_tts_server = tts_servers[selected_tts_server_index][0]
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
            cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
            if cache_key not in st.session_state:
                st.session_state[cache_key] = voice.get_elevenlabs_voices(
                    saved_elevenlabs_api_key
                )
            if st.button("🔄 Sesleri Yenile", key="refresh_elevenlabs"):
                for k in list(st.session_state.keys()):
                    if k.startswith("elevenlabs_voices_"):
                        del st.session_state[k]
                st.rerun()
            filtered_voices = st.session_state[cache_key]
        elif selected_tts_server == "chatterbox":
            # 自托管 Chatterbox 服务的预置音色（来自 [chatterbox] voices 配置）
            _sync_chatterbox_config_from_session_state()
            filtered_voices = voice.get_chatterbox_voices()
        else:
            all_voices = voice.get_all_azure_voices(filter_locals=None)

            for v in all_voices:
                if selected_tts_server == "azure-tts-v2":
                    if "V2" in v:
                        filtered_voices.append(v)
                else:
                    if "V2" not in v:
                        filtered_voices.append(v)

        if selected_tts_server == voice.NO_VOICE_NAME:
            friendly_names = {voice.NO_VOICE_NAME: tr("No Voice")}
        else:
            def _friendly(v):
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )
            friendly_names = {v: _friendly(v) for v in filtered_voices}

        saved_voice_name = config.ui.get("voice_name", "")
        saved_voice_name_index = 0

        if saved_voice_name in friendly_names:
            saved_voice_name_index = list(friendly_names.keys()).index(saved_voice_name)
        else:
            for i, v in enumerate(filtered_voices):
                if v.lower().startswith(st.session_state["ui_language"].lower()):
                    saved_voice_name_index = i
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

        if (
            friendly_names
            and selected_tts_server != voice.NO_VOICE_NAME
            and st.button(tr("Play Voice"))
        ):
            if selected_tts_server == "chatterbox":
                _sync_chatterbox_config_from_session_state()
            play_content = params.video_subject
            if not play_content:
                play_content = params.video_script
            if not play_content:
                if voice.is_elevenlabs_voice(voice_name):
                    parts = voice_name.split(":", 2)
                    display = parts[2] if len(parts) >= 3 else ""
                    _vi_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
                    if any(c in _vi_chars for c in display):
                        play_content = "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
                    else:
                        play_content = tr("Voice Example")
                else:
                    play_content = tr("Voice Example")
            with st.spinner(tr("Synthesizing Voice")):
                temp_dir = utils.storage_dir("temp", create=True)
                audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
                sub_maker = voice.tts(
                    text=play_content,
                    voice_name=voice_name,
                    voice_rate=params.voice_rate,
                    voice_file=audio_file,
                    voice_volume=params.voice_volume,
                )
                if not sub_maker:
                    play_content = "This is a example voice. if you hear this, the voice synthesis failed with the original content."
                    sub_maker = voice.tts(
                        text=play_content,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_file=audio_file,
                        voice_volume=params.voice_volume,
                    )

                if sub_maker and os.path.exists(audio_file):
                    with open(audio_file, "rb") as f:
                        audio_bytes = f.read()
                    if audio_bytes:
                        st.audio(
                            audio_bytes,
                            format=_detect_audio_mime(audio_file, audio_bytes),
                        )
                    else:
                        logger.error(f"voice preview audio file is empty: {audio_file}")
                    if os.path.exists(audio_file):
                        os.remove(audio_file)

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

            _elevenlabs_models = [
                "eleven_multilingual_v2",
                "eleven_flash_v2_5",
                "eleven_v3",
            ]
            saved_elevenlabs_model = config.elevenlabs.get(
                "model_id", "eleven_multilingual_v2"
            )
            if saved_elevenlabs_model not in _elevenlabs_models:
                saved_elevenlabs_model = "eleven_multilingual_v2"
            elevenlabs_model = st.selectbox(
                tr("ElevenLabs Model"),
                options=_elevenlabs_models,
                index=_elevenlabs_models.index(saved_elevenlabs_model),
                key="elevenlabs_model_select",
            )
            config.elevenlabs["model_id"] = elevenlabs_model

            st.info(
                "ElevenLabs TTS Settings:\n"
                "- Get your API key at https://elevenlabs.io/app/settings/api-keys\n"
                "- Mark voices as ★ Favorite in the ElevenLabs voice library to make them appear here"
            )

            if elevenlabs_api_key != saved_elevenlabs_api_key:
                for k in list(st.session_state.keys()):
                    if k.startswith("elevenlabs_voices_"):
                        del st.session_state[k]

            config.elevenlabs["api_key"] = elevenlabs_api_key

        # Chatterbox API settings section (self-hosted, OpenAI-compatible)
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

            _saved_chatterbox_voices = (
                _parse_chatterbox_voices(config.chatterbox.get("voices"))
                or DEFAULT_CHATTERBOX_VOICES
            )
            if isinstance(_saved_chatterbox_voices, list):
                _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
            chatterbox_voices = st.text_input(
                tr("Chatterbox Voices"),
                value=str(_saved_chatterbox_voices or ""),
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

        custom_audio_file_types = ["mp3", "wav", "m4a", "aac", "flac", "ogg"]
        uploaded_audio_file = st.file_uploader(
            tr("Custom Audio File"),
            type=custom_audio_file_types
            + [file_type.upper() for file_type in custom_audio_file_types],
            accept_multiple_files=False,
            key="custom_audio_file_uploader",
        )
        if uploaded_audio_file:
            st.audio(uploaded_audio_file, format="audio/mp3")
            st.info(
                tr(
                    "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                )
            )

        bgm_options = [
            (tr("No Background Music"), ""),
            (tr("Random Background Music"), "random"),
            (tr("Royalty-Free Music Library"), "library"),
            (tr("Custom Background Music"), "custom"),
        ]
        saved_bgm_type = config.ui.get("bgm_type", "random")
        selected_index = st.selectbox(
            tr("Background Music"),
            index=_find_option_index(bgm_options, saved_bgm_type, 1),
            options=range(len(bgm_options)),
            format_func=lambda x: bgm_options[x][0],
        )
        selected_bgm_type = bgm_options[selected_index][1]
        config.ui["bgm_type"] = selected_bgm_type
        params.bgm_type = selected_bgm_type
        params.bgm_file = ""

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
            if "custom_bgm_file_input" not in st.session_state:
                st.session_state["custom_bgm_file_input"] = config.ui.get(
                    "bgm_file", ""
                )
            custom_bgm_file = st.text_input(
                tr("Custom Background Music File"),
                key="custom_bgm_file_input",
            )
            if custom_bgm_file:
                params.bgm_file = custom_bgm_file.strip()
                config.ui["bgm_file"] = params.bgm_file
        else:
            config.ui["bgm_file"] = ""
        bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        saved_bgm_volume = config.ui.get("bgm_volume", 0.2)
        params.bgm_volume = st.selectbox(
            tr("Background Music Volume"),
            options=bgm_volume_options,
            index=_find_option_index(bgm_volume_options, saved_bgm_volume, 2),
        )
        config.ui["bgm_volume"] = params.bgm_volume

with right_panel:
    with st.container(border=True):
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
            "subtitle_background_enabled", True
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
    with st.expander(tr("Click to show API Key management"), expanded=False):
        st.subheader(tr("Manage Pexels, Pixabay and Coverr API Keys"))

        col1, col2, col3 = st.tabs([
            tr("Pexels API Keys"),
            tr("Pixabay API Keys"),
            tr("Coverr API Keys"),
        ])

        with col1:
            st.subheader(tr("Pexels API Keys"))
            if config.app["pexels_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pexels_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pexels API Keys currently"))

            new_key = st.text_input(tr("Add Pexels API Key"), key="pexels_new_key")
            if st.button(tr("Add Pexels API Key")):
                if new_key and new_key not in config.app["pexels_api_keys"]:
                    config.app["pexels_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pexels API Key added successfully"))
                elif new_key in config.app["pexels_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pexels_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pexels API Key to delete"), config.app["pexels_api_keys"], key="pexels_delete_key"
                )
                if st.button(tr("Delete Selected Pexels API Key")):
                    config.app["pexels_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pexels API Key deleted successfully"))

        with col2:
            st.subheader(tr("Pixabay API Keys"))

            if config.app["pixabay_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["pixabay_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Pixabay API Keys currently"))

            new_key = st.text_input(tr("Add Pixabay API Key"), key="pixabay_new_key")
            if st.button(tr("Add Pixabay API Key")):
                if new_key and new_key not in config.app["pixabay_api_keys"]:
                    config.app["pixabay_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key added successfully"))
                elif new_key in config.app["pixabay_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["pixabay_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Pixabay API Key to delete"), config.app["pixabay_api_keys"], key="pixabay_delete_key"
                )
                if st.button(tr("Delete Selected Pixabay API Key")):
                    config.app["pixabay_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Pixabay API Key deleted successfully"))

        with col3:
            st.subheader(tr("Coverr API Keys"))

            if "coverr_api_keys" not in config.app or config.app["coverr_api_keys"] is None:
                config.app["coverr_api_keys"] = []

            if config.app["coverr_api_keys"]:
                st.write(tr("Current Keys:"))
                for key in config.app["coverr_api_keys"]:
                    st.code(key)
            else:
                st.info(tr("No Coverr API Keys currently"))

            new_key = st.text_input(tr("Add Coverr API Key"), key="coverr_new_key")
            if st.button(tr("Add Coverr API Key")):
                if new_key and new_key not in config.app["coverr_api_keys"]:
                    config.app["coverr_api_keys"].append(new_key)
                    config.save_config()
                    st.success(tr("Coverr API Key added successfully"))
                elif new_key in config.app["coverr_api_keys"]:
                    st.warning(tr("This API Key already exists"))
                else:
                    st.error(tr("Please enter a valid API Key"))

            if config.app["coverr_api_keys"]:
                delete_key = st.selectbox(
                    tr("Select Coverr API Key to delete"), config.app["coverr_api_keys"], key="coverr_delete_key"
                )
                if st.button(tr("Delete Selected Coverr API Key")):
                    config.app["coverr_api_keys"].remove(delete_key)
                    config.save_config()
                    st.success(tr("Coverr API Key deleted successfully"))

with st.expander(tr("Presets"), expanded=False):
    preset_names = presets.list_presets()
    builtin_preset_names = presets.list_builtin_presets()

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
                        st.session_state["_pending_video_preset"] = presets.load_preset(
                            selected_preset_name
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
                    st.success(f"{tr('Preset Saved')}: {presets.normalize_preset_name(preset_name)}")
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
                except (UnicodeDecodeError, json.JSONDecodeError, presets.PresetError) as e:
                    st.error(f"{tr('Preset Error')}: {str(e)}")

with st.expander(tr("Recent Jobs"), expanded=False):
    recent_jobs = history.list_history(limit=20)
    if not recent_jobs:
        st.info(tr("No Recent Jobs"))
    else:
        if st.button(tr("Clear Recent Jobs"), key="clear_recent_jobs"):
            history.clear_history()
            st.rerun()

        for job in recent_jobs:
            history_key_prefix = f"history_{job.get('task_id', '')}"
            subject = job.get("subject") or job.get("task_id") or tr("Untitled")
            status = job.get("status", "")
            created_at = job.get("created_at", "")
            with st.expander(f"{subject} - {status}", expanded=False):
                st.caption(f"{tr('Created At')}: {created_at}")
                videos = job.get("videos") or []
                if videos:
                    st.write(tr("Videos"))
                    for url in videos:
                        st.code(url)
                terms = job.get("terms") or []
                if isinstance(terms, str):
                    terms = [term.strip() for term in terms.split(",") if term.strip()]
                if terms:
                    st.write(tr("Search Queries"))
                    st.text_area(
                        tr("Search Queries"),
                        value="\n".join(str(term) for term in terms),
                        height=100,
                        key=f"history_terms_{job.get('task_id', '')}",
                    )
                cooldown_summary = _cooldown_summary_text(job.get("cooldown"))
                if cooldown_summary:
                    st.caption(cooldown_summary)
                metadata = job.get("metadata")
                if metadata:
                    st.write(tr("Social Metadata"))
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
                _render_pending_uploads(
                    job,
                    key_prefix=history_key_prefix,
                )
                viral_analysis = job.get("viral_analysis")
                if viral_analysis:
                    st.write(tr("Viral Analysis"))
                    _render_viral_analysis(
                        viral_analysis,
                        key_prefix=f"history_viral_{job.get('task_id', '')}",
                    )
                if job.get("error"):
                    st.error(job["error"])

with st.expander(tr("Batch Generation"), expanded=False):
    use_manual_batch_scripts = st.checkbox(
        tr("Use Manual Batch Scripts"),
        key="use_manual_batch_scripts",
    )
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
    batch_items = _get_batch_items()
    if batch_items:
        st.caption(f"{len(batch_items)} {tr('Batch Subjects Ready')}")

with st.expander(tr("Content Intelligence"), expanded=False):
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
    if content_plan:
        warnings = content_plan.get("warnings") or []
        if warnings:
            st.write(tr("Planning Warnings"))
            for warning in warnings:
                st.caption(warning)

        ideas = content_plan.get("ideas") or []
        if ideas:
            st.write(tr("Content Ideas"))
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
                    st.caption(f"{tr('Content Rationale')}: {idea.get('rationale', '')}")

        calendar = content_plan.get("calendar") or []
        if calendar:
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
                use_container_width=True,
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

with st.expander(tr("Social Metadata"), expanded=False):
    auto_social_metadata_after_video = st.checkbox(
        tr("Auto Social Metadata After Video"),
        key="auto_social_metadata_after_video",
    )
    social_platforms = [
        ("TikTok", "tiktok"),
        ("YouTube Shorts", "youtube_shorts"),
        ("Instagram Reels", "instagram_reels"),
    ]
    selected_social_platform_index = st.selectbox(
        tr("Social Platform"),
        options=range(len(social_platforms)),
        format_func=lambda x: social_platforms[x][0],
        key="social_platform_select",
    )
    selected_social_platform = social_platforms[selected_social_platform_index][1]

    if st.button(tr("Generate Social Metadata"), key="generate_social_metadata"):
        if not params.video_subject and not params.video_script:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            with st.spinner(tr("Generating Social Metadata")):
                st.session_state["social_metadata"] = llm.generate_social_metadata(
                    video_subject=params.video_subject,
                    video_script=params.video_script,
                    language=params.video_language or "auto",
                    platform=selected_social_platform,
                )

    social_metadata = st.session_state.get("social_metadata")
    if social_metadata:
        st.text_input(
            tr("Social Title"),
            value=social_metadata.get("title", ""),
        )
        st.text_area(
            tr("Social Description"),
            value=social_metadata.get("caption", ""),
            height=120,
        )
        st.text_input(
            tr("Social Hashtags"),
            value=" ".join(social_metadata.get("hashtags", [])),
        )

with st.expander(tr("Publishing Settings"), expanded=False):
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
    current_youtube_privacy = st.session_state.get(
        "upload_post_youtube_privacy_status",
        upload_post.YOUTUBE_PRIVACY_UNLISTED,
    )
    if current_youtube_privacy not in youtube_privacy_options:
        current_youtube_privacy = upload_post.YOUTUBE_PRIVACY_UNLISTED
        st.session_state["upload_post_youtube_privacy_status"] = current_youtube_privacy

    st.selectbox(
        tr("YouTube Privacy Status"),
        options=youtube_privacy_options,
        index=youtube_privacy_options.index(current_youtube_privacy),
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

with st.expander(tr("Viral Analysis"), expanded=False):
    st.checkbox(
        tr("Auto Viral Analysis After Video"),
        key="auto_viral_analysis_after_video",
        help=tr("Auto Viral Analysis After Video Help"),
    )

    if st.button(tr("Generate Viral Analysis"), key="generate_viral_analysis"):
        if not params.video_subject and not params.video_script:
            st.error(tr("Please Enter the Video Subject or Script"))
        else:
            current_metadata = st.session_state.get("social_metadata") or {}
            with st.spinner(tr("Generating Viral Analysis")):
                st.session_state["viral_analysis"] = viral_analyzer.analyze_viral_potential(
                    video_subject=params.video_subject,
                    video_script=params.video_script,
                    title=current_metadata.get("title", ""),
                    video_duration_sec=None,
                    target_platforms=[selected_social_platform],
                    language=params.video_language or "auto",
                )

    _render_viral_analysis(
        st.session_state.get("viral_analysis"),
        key_prefix="current_viral",
    )

action_cols = st.columns(2)
with action_cols[0]:
    start_button = st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
    )
with action_cols[1]:
    batch_button = st.button(
        tr("Generate Batch"),
        use_container_width=True,
    )

if start_button or batch_button:
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

    # ── Çok kaynaklı kaynak doğrulaması ─────────────────────────────────
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
    # NASA, Wikimedia, Archive.org → API key gerektirmez, doğrulama gerekmez

    if (
        st.session_state.get("manual_video_selection_enabled")
        and _val_api
        and not st.session_state.get("manual_video_selected_urls")
    ):
        st.error(tr("Please Select at Least One Video Candidate"))
        scroll_to_bottom()
        st.stop()

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

    log_container = st.empty()
    log_records = []

    def log_received(msg):
        if config.ui["hide_log"]:
            return
        with log_container:
            log_records.append(msg)
            st.code("\n".join(log_records))

    logger.add(log_received)

    def run_generation(run_task_id, run_params):
        prepared_params = _prepare_task_params(
            run_task_id,
            run_params,
            uploaded_audio_file,
            uploaded_files,
        )
        logger.info(tr("Start Generating Video"))
        logger.info(utils.to_json(prepared_params))
        return tm.start(task_id=run_task_id, params=prepared_params)

    scroll_to_bottom()

    if batch_button:
        st.toast(tr("Generating Batch Video"))
        batch_progress = st.progress(0)
        batch_results = []
        failed_subjects = []

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
            result = run_generation(task_id, run_params)
            if not result or "videos" not in result:
                failed_subjects.append(subject)
                logger.error(f"{tr('Video Generation Failed')}: {subject}")
                _record_history(
                    task_id,
                    run_params,
                    error=tr("Video Generation Failed"),
                )
            else:
                metadata = None
                if st.session_state.get("auto_social_metadata_after_video", True):
                    with st.spinner(tr("Generating Social Metadata")):
                        metadata = _generate_social_metadata_for_result(
                            run_params,
                            result,
                            selected_social_platform,
                        )
                viral_analysis = None
                if st.session_state.get("auto_viral_analysis_after_video", False):
                    with st.spinner(tr("Generating Viral Analysis")):
                        viral_analysis = _generate_viral_analysis_for_result(
                            run_params,
                            result=result,
                            metadata=metadata,
                            platform=selected_social_platform,
                        )
                _record_history(
                    task_id,
                    run_params,
                    result=result,
                    metadata=metadata,
                    viral_analysis=viral_analysis,
                )
                batch_results.append(
                    {
                        "subject": subject,
                        "task_id": task_id,
                        "videos": result.get("videos", []),
                        "metadata": metadata,
                        "viral_analysis": viral_analysis,
                        "cooldown": result.get("cooldown"),
                        "pending_uploads": result.get("pending_uploads"),
                    }
                )
            batch_progress.progress(index / len(batch_items))

        if failed_subjects:
            st.error(
                f"{tr('Batch Generation Finished With Failures')}: "
                f"{', '.join(failed_subjects)}"
            )
        else:
            st.success(tr("Batch Generation Completed"))

        for item in batch_results:
            with st.expander(item["subject"], expanded=False):
                for url in item["videos"]:
                    st.video(url)
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
            st.session_state["viral_analysis"] = viral_analysis
        _record_history(
            task_id,
            params,
            result=result,
            metadata=metadata,
            viral_analysis=viral_analysis,
        )
        st.success(tr("Video Generation Completed"))
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

        open_task_folder(task_id)
        logger.info(tr("Video Generation Completed"))

    scroll_to_bottom()

config.save_config()
