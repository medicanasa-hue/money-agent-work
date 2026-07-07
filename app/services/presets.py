import json
import os
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.models.schema import VideoParams
from app.utils import utils
from app.utils.file_security import resolve_path_within_directory

PRESET_VERSION = 1
PRESET_EXTENSION = ".json"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
_VIDEO_PARAM_FIELDS = set(VideoParams.model_fields)
_VIDEO_PARAM_DEFAULTS = {
    "video_subject": "",
}
_APP_CONFIG_FIELDS = {
    "video_codec",
    "video_cooldown_enabled",
    "video_cooldown_days",
    "video_crf",
    "video_encoder_preset",
    "video_fps",
    "audio_bitrate",
}
_COOLDOWN_DAY_OPTIONS = {3, 7, 14, 30}
_VIDEO_CODEC_OPTIONS = {
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
}
_LIBX264_PRESET_OPTIONS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}

_BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "Finance Shorts TR": {
        "video_language": "tr-TR",
        "video_aspect": "9:16",
        "video_concat_mode": "random",
        "video_clip_duration": 4,
        "match_materials_to_script": True,
        "video_count": 1,
        "subtitle_enabled": True,
        "font_size": 64,
        "stroke_width": 1.5,
        "bgm_type": "random",
        "bgm_volume": 0.18,
        "paragraph_number": 1,
        "video_script_prompt": (
            "Kisa, net ve izleyiciyi ilk cumlede yakalayan bir finans short'u yaz."
        ),
    },
    "Motivation Shorts EN": {
        "video_language": "en-US",
        "video_aspect": "9:16",
        "video_concat_mode": "random",
        "video_clip_duration": 4,
        "match_materials_to_script": True,
        "video_count": 1,
        "subtitle_enabled": True,
        "font_size": 62,
        "stroke_width": 1.5,
        "bgm_type": "random",
        "bgm_volume": 0.2,
        "paragraph_number": 1,
        "video_script_prompt": (
            "Write a punchy motivational short with a strong hook and a practical ending."
        ),
    },
    "Silent Caption Video": {
        "video_language": "auto",
        "video_aspect": "9:16",
        "video_concat_mode": "sequential",
        "video_clip_duration": 5,
        "match_materials_to_script": True,
        "video_count": 1,
        "voice_name": "no-voice",
        "subtitle_enabled": True,
        "font_size": 58,
        "stroke_width": 1.5,
        "bgm_type": "random",
        "bgm_volume": 0.25,
        "paragraph_number": 1,
    },
}


class PresetError(ValueError):
    pass


def get_preset_dir(create: bool = False) -> str:
    return utils.storage_dir("presets", create=create)


def normalize_preset_name(name: str) -> str:
    clean_name = " ".join(str(name or "").strip().split())
    if clean_name.lower().endswith(PRESET_EXTENSION):
        clean_name = clean_name[: -len(PRESET_EXTENSION)].strip()

    if not clean_name:
        raise PresetError("preset name is required")
    if not _SAFE_NAME_RE.fullmatch(clean_name):
        raise PresetError(
            "preset name can only use letters, numbers, spaces, dots, dashes, and underscores"
        )
    return clean_name


def _preset_filename(name: str) -> str:
    return f"{normalize_preset_name(name)}{PRESET_EXTENSION}"


def _preset_path(name: str, preset_dir: str | None = None, *, require_file: bool) -> str:
    base_dir = preset_dir or get_preset_dir(create=not require_file)
    if not os.path.isdir(base_dir):
        raise PresetError("preset directory does not exist")

    try:
        return resolve_path_within_directory(
            base_dir,
            _preset_filename(name),
            require_file=require_file,
        )
    except ValueError as exc:
        raise PresetError(str(exc)) from exc


def _validate_params(params: VideoParams | Mapping[str, Any]) -> VideoParams:
    if isinstance(params, VideoParams):
        return params

    if not isinstance(params, Mapping):
        raise PresetError("preset params must be an object")

    unknown_fields = sorted(set(params) - _VIDEO_PARAM_FIELDS)
    if unknown_fields:
        raise PresetError(f"unsupported preset fields: {', '.join(unknown_fields)}")

    try:
        return VideoParams.model_validate({**_VIDEO_PARAM_DEFAULTS, **dict(params)})
    except ValidationError as exc:
        raise PresetError("preset params are invalid") from exc


def _normalize_int_app_config(
    app_config: Mapping[str, Any],
    field_name: str,
    *,
    min_value: int,
    max_value: int,
) -> int | None:
    if field_name not in app_config:
        return None
    value = app_config[field_name]
    if isinstance(value, bool):
        raise PresetError(f"{field_name} must be between {min_value} and {max_value}")
    try:
        normalized_value = int(value)
    except (TypeError, ValueError) as exc:
        raise PresetError(
            f"{field_name} must be between {min_value} and {max_value}"
        ) from exc
    if not min_value <= normalized_value <= max_value:
        raise PresetError(f"{field_name} must be between {min_value} and {max_value}")
    return normalized_value


def _normalize_audio_bitrate(value: Any) -> str:
    if isinstance(value, bool):
        raise PresetError("audio_bitrate must be between 32k and 512k")
    normalized = str(value).strip().lower()
    if normalized.endswith("k"):
        normalized = normalized[:-1]
    try:
        kbps = int(normalized)
    except (TypeError, ValueError) as exc:
        raise PresetError("audio_bitrate must be between 32k and 512k") from exc
    if not 32 <= kbps <= 512:
        raise PresetError("audio_bitrate must be between 32k and 512k")
    return f"{kbps}k"


def _validate_app_config(app_config: Mapping[str, Any] | None) -> dict[str, Any]:
    if app_config is None:
        return {}
    if not isinstance(app_config, Mapping):
        raise PresetError("preset app_config must be an object")

    unknown_fields = sorted(set(app_config) - _APP_CONFIG_FIELDS)
    if unknown_fields:
        raise PresetError(
            f"unsupported preset app_config fields: {', '.join(unknown_fields)}"
        )

    normalized: dict[str, Any] = {}
    if "video_cooldown_enabled" in app_config:
        value = app_config["video_cooldown_enabled"]
        if isinstance(value, bool):
            normalized["video_cooldown_enabled"] = value
        elif isinstance(value, str):
            normalized["video_cooldown_enabled"] = value.strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        else:
            raise PresetError("video_cooldown_enabled must be a boolean")

    if "video_cooldown_days" in app_config:
        value = app_config["video_cooldown_days"]
        if isinstance(value, bool):
            raise PresetError("video_cooldown_days must be one of 3, 7, 14, 30")
        try:
            days = int(value)
        except (TypeError, ValueError) as exc:
            raise PresetError(
                "video_cooldown_days must be one of 3, 7, 14, 30"
            ) from exc
        if days not in _COOLDOWN_DAY_OPTIONS:
            raise PresetError("video_cooldown_days must be one of 3, 7, 14, 30")
        normalized["video_cooldown_days"] = days

    if "video_codec" in app_config:
        codec = str(app_config["video_codec"]).strip().lower()
        if codec not in _VIDEO_CODEC_OPTIONS:
            raise PresetError("video_codec is not supported")
        normalized["video_codec"] = codec

    video_crf = _normalize_int_app_config(
        app_config,
        "video_crf",
        min_value=0,
        max_value=51,
    )
    if video_crf is not None:
        normalized["video_crf"] = video_crf

    video_fps = _normalize_int_app_config(
        app_config,
        "video_fps",
        min_value=1,
        max_value=120,
    )
    if video_fps is not None:
        normalized["video_fps"] = video_fps

    if "video_encoder_preset" in app_config:
        preset = str(app_config["video_encoder_preset"]).strip().lower()
        if preset not in _LIBX264_PRESET_OPTIONS:
            raise PresetError("video_encoder_preset is not supported")
        normalized["video_encoder_preset"] = preset

    if "audio_bitrate" in app_config:
        normalized["audio_bitrate"] = _normalize_audio_bitrate(
            app_config["audio_bitrate"]
        )

    return normalized


def build_preset_payload(
    name: str,
    params: VideoParams | Mapping[str, Any],
    app_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    video_params = _validate_params(params)
    payload = {
        "version": PRESET_VERSION,
        "name": normalize_preset_name(name),
        "params": video_params.model_dump(mode="json", warnings=False),
    }
    normalized_app_config = _validate_app_config(app_config)
    if normalized_app_config:
        payload["app_config"] = normalized_app_config
    return payload


def import_preset_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise PresetError("preset payload must be an object")

    version = payload.get("version", PRESET_VERSION)
    if version != PRESET_VERSION:
        raise PresetError(f"unsupported preset version: {version}")

    return build_preset_payload(
        name=payload.get("name", ""),
        params=payload.get("params", {}),
        app_config=payload.get("app_config"),
    )


def list_builtin_presets() -> list[str]:
    return sorted(_BUILTIN_PRESETS, key=str.casefold)


def load_builtin_preset(name: str) -> dict[str, Any]:
    normalized_name = normalize_preset_name(name)
    for preset_name, params in _BUILTIN_PRESETS.items():
        if preset_name.casefold() == normalized_name.casefold():
            return build_preset_payload(preset_name, params)
    raise PresetError("built-in preset does not exist")


def save_preset(
    name: str,
    params: VideoParams | Mapping[str, Any],
    preset_dir: str | None = None,
    app_config: Mapping[str, Any] | None = None,
) -> str:
    payload = build_preset_payload(name, params, app_config=app_config)
    path = _preset_path(payload["name"], preset_dir=preset_dir, require_file=False)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return path


def load_preset(name: str, preset_dir: str | None = None) -> dict[str, Any]:
    path = _preset_path(name, preset_dir=preset_dir, require_file=True)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except json.JSONDecodeError as exc:
        raise PresetError("preset file is not valid JSON") from exc
    return import_preset_payload(payload)


def delete_preset(name: str, preset_dir: str | None = None) -> None:
    path = _preset_path(name, preset_dir=preset_dir, require_file=True)
    os.remove(path)


def list_presets(preset_dir: str | None = None) -> list[str]:
    base_dir = preset_dir or get_preset_dir(create=False)
    if not os.path.isdir(base_dir):
        return []

    names = []
    for filename in os.listdir(base_dir):
        if not filename.lower().endswith(PRESET_EXTENSION):
            continue
        try:
            names.append(normalize_preset_name(filename))
        except PresetError:
            continue
    return sorted(names, key=str.casefold)
