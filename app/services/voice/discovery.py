import json
import os

import requests
from loguru import logger

from app.config import config


_ELEVENLABS_SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
_ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v2/voices"
_ELEVENLABS_FREE_TIERS = frozenset(("free", "trial"))


def get_siliconflow_voices() -> list[str]:
    """
    获取硅基流动的声音列表

    Returns:
        声音列表，格式为 ["siliconflow:FunAudioLLM/CosyVoice2-0.5B:alex", ...]
    """
    # 硅基流动的声音列表和对应的性别（用于显示）
    voices_with_gender = [
        ("FunAudioLLM/CosyVoice2-0.5B", "alex", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "anna", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "bella", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "benjamin", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "charles", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "claire", "Female"),
        ("FunAudioLLM/CosyVoice2-0.5B", "david", "Male"),
        ("FunAudioLLM/CosyVoice2-0.5B", "diana", "Female"),
    ]

    # 添加siliconflow:前缀，并格式化为显示名称
    return [
        f"siliconflow:{model}:{voice}-{gender}"
        for model, voice, gender in voices_with_gender
    ]


def get_gemini_voices() -> list[str]:
    """Return Google's preset voices with style labels, not inferred genders.

    https://ai.google.dev/gemini-api/docs/speech-generation#voice-options
    Legacy saved gender labels remain accepted by the dispatch layer.
    """
    voices_with_style = (
        ("Zephyr", "Bright"), ("Puck", "Upbeat"), ("Charon", "Informative"),
        ("Kore", "Firm"), ("Fenrir", "Excitable"), ("Leda", "Youthful"),
        ("Orus", "Firm"), ("Aoede", "Breezy"), ("Callirrhoe", "Easy-going"),
        ("Autonoe", "Bright"), ("Enceladus", "Breathy"), ("Iapetus", "Clear"),
        ("Umbriel", "Easy-going"), ("Algieba", "Smooth"), ("Despina", "Smooth"),
        ("Erinome", "Clear"), ("Algenib", "Gravelly"), ("Rasalgethi", "Informative"),
        ("Laomedeia", "Upbeat"), ("Achernar", "Soft"), ("Alnilam", "Firm"),
        ("Schedar", "Even"), ("Gacrux", "Mature"), ("Pulcherrima", "Forward"),
        ("Achird", "Friendly"), ("Zubenelgenubi", "Casual"), ("Vindemiatrix", "Gentle"),
        ("Sadachbia", "Lively"), ("Sadaltager", "Knowledgeable"), ("Sulafat", "Warm"),
    )
    return [f"gemini:{name}-{style}" for name, style in voices_with_style]


def get_mimo_voices() -> list[str]:
    """
    获取 Xiaomi MiMo V2.5 TTS 的预置音色列表。

    当前只接入官方文档里的 `mimo-v2.5-tts` 预置音色模式。音色设计
    `mimo-v2.5-tts-voicedesign` 和音色复刻 `mimo-v2.5-tts-voiceclone`
    需要额外的输入表单和素材上传流程，先不混入普通 TTS 下拉框，避免
    用户误以为选择一个 voice id 就能完成所有高级能力。
    """
    voices_with_gender = [
        ("mimo_default", "Female"),
        ("冰糖", "Female"),
        ("茉莉", "Female"),
        ("苏打", "Male"),
        ("白桦", "Male"),
        ("Mia", "Female"),
        ("Chloe", "Female"),
        ("Milo", "Male"),
        ("Dean", "Male"),
    ]

    return [f"mimo:{voice}-{gender}" for voice, gender in voices_with_gender]


def _elevenlabs_subscription_tier(api_key: str) -> str:
    try:
        response = requests.get(
            _ELEVENLABS_SUBSCRIPTION_URL,
            headers={"xi-api-key": api_key},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning(f"ElevenLabs subscription fetch failed: {exc}")
        return ""
    if response.status_code != 200:
        logger.warning(
            "ElevenLabs subscription fetch failed with status "
            f"{response.status_code}"
        )
        return ""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tier") or "").strip().lower()


def _is_elevenlabs_voice_available(voice: dict, tier: str) -> bool:
    if str(voice.get("status") or "").strip().lower() == "disabled":
        return False
    if tier not in _ELEVENLABS_FREE_TIERS:
        return True

    # ElevenLabs exposes saved Voice Library entries in the catalog even
    # though its API does not permit free-tier TTS generation with them.
    if isinstance(voice.get("sharing"), dict):
        return False

    available_tiers = voice.get("available_for_tiers")
    if isinstance(available_tiers, list) and available_tiers:
        normalized_tiers = {
            str(available_tier or "").strip().lower()
            for available_tier in available_tiers
        }
        return bool(_ELEVENLABS_FREE_TIERS & normalized_tiers)
    return True


def get_elevenlabs_voice_catalog(api_key: str) -> dict:
    catalog = {"voices": [], "tier": "", "filtered_count": 0}
    if not api_key:
        return catalog

    tier = _elevenlabs_subscription_tier(api_key)
    catalog["tier"] = tier
    try:
        response = requests.get(
            _ELEVENLABS_VOICES_URL,
            params={"page_size": 100},
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning(
                "ElevenLabs voices fetch failed with status "
                f"{response.status_code}"
            )
            return catalog
        data = response.json()
        voices = data.get("voices", []) if isinstance(data, dict) else []
        for voice in voices:
            if (
                not isinstance(voice, dict)
                or not voice.get("voice_id")
                or not voice.get("name")
            ):
                continue
            if not _is_elevenlabs_voice_available(voice, tier):
                catalog["filtered_count"] += 1
                continue
            catalog["voices"].append(
                f"elevenlabs:{voice['voice_id']}:{voice['name']}"
            )
    except Exception as e:
        logger.warning(f"ElevenLabs voices fetch failed: {str(e)}")
    return catalog


def get_elevenlabs_voices(api_key: str) -> list[str]:
    return get_elevenlabs_voice_catalog(api_key)["voices"]


def get_chatterbox_voices() -> list[str]:
    """Return the configured Chatterbox voices.

    Chatterbox is self-hosted, so there is no global voice catalog. Operators
    list the voice names exposed by their server via ``[chatterbox] voices``
    (a TOML array, or a comma-separated string). Each entry is normalised to
    the ``chatterbox:<name>`` format used by the TTS dispatcher.
    """
    voices = config.chatterbox.get("voices", []) or []
    if isinstance(voices, str):
        voices = [v.strip() for v in voices.split(",") if v.strip()]
    result = []
    for v in voices:
        v = str(v).strip()
        if not v:
            continue
        result.append(v if v.startswith("chatterbox:") else f"chatterbox:{v}")
    if not result:
        # keep the dropdown usable even before any voice is configured
        result = ["chatterbox:default-Female"]
    return result


_AZURE_VOICES_DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "azure_voices.json"
)
_azure_voices_cache = None


def _load_azure_voices() -> list[dict]:
    global _azure_voices_cache
    if _azure_voices_cache is None:
        with open(_AZURE_VOICES_DATA_FILE, "r", encoding="utf-8") as f:
            _azure_voices_cache = json.load(f)
    return _azure_voices_cache


def get_all_azure_voices(filter_locals=None) -> list[str]:
    voices = []
    for item in _load_azure_voices():
        name = item["name"]
        gender = item["gender"]
        # 应用过滤条件
        if filter_locals and any(
            name.lower().startswith(fl.lower()) for fl in filter_locals
        ):
            voices.append(f"{name}-{gender}")
        elif not filter_locals:
            voices.append(f"{name}-{gender}")

    voices.sort()
    return voices
