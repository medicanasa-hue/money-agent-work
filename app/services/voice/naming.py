import re
import unicodedata

from app.utils import utils

NO_VOICE_NAME = "no-voice"
_NO_VOICE_ALIASES = {NO_VOICE_NAME, "none"}


def parse_voice_name(name: str):
    # zh-CN-XiaoyiNeural-Female
    # zh-CN-YunxiNeural-Male
    # zh-CN-XiaoxiaoMultilingualNeural-V2-Female
    name = name.replace("-Female", "").replace("-Male", "").strip()
    return name


def is_azure_v2_voice(voice_name: str):
    voice_name = parse_voice_name(voice_name)
    if voice_name.endswith("-V2"):
        return voice_name.replace("-V2", "").strip()
    return ""


def is_siliconflow_voice(voice_name: str):
    """检查是否是硅基流动的声音"""
    return voice_name.startswith("siliconflow:")


def is_gemini_voice(voice_name: str):
    """检查是否是Gemini TTS的声音"""
    return voice_name.startswith("gemini:")


def is_mimo_voice(voice_name: str):
    """检查是否是 Xiaomi MiMo TTS 的声音"""
    return voice_name.startswith("mimo:")


def is_elevenlabs_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("elevenlabs:")


def is_chatterbox_voice(voice_name: str) -> bool:
    return (voice_name or "").startswith("chatterbox:")


def is_no_voice(voice_name: str | None) -> bool:
    """
    判断用户是否明确选择了“无配音”模式。

    这里刻意不把空字符串当成无配音：空 voice 更可能是配置损坏、旧版本
    WebUI 状态丢失或接口参数缺失。只有明确的 sentinel 才进入静音分支，
    这样可以避免把真实错误伪装成正常生成。
    """
    return str(voice_name or "").strip().lower() in _NO_VOICE_ALIASES


def estimate_no_voice_duration(text: str) -> float:
    """
    为无配音模式估算一个稳定的视频时间轴长度。

    无配音仍需要一个音频占位来驱动现有素材裁剪、字幕时间轴和最终合成。
    估算策略尽量简单：
    1. 中文等 CJK 字符按约 4.2 字/秒估算；
    2. 英文/数字按约 2.7 词/秒估算；
    3. 其他语种文字按约 4.0 字符/秒兜底估算，覆盖俄语、阿拉伯语、
       日文假名、韩文等非 ASCII 文本；
    4. 每个断句补一点停顿，让字幕切换不至于过于紧凑；
    5. 最少 3 秒，避免极短脚本生成 0 秒音频。
    """
    normalized_text = (text or "").strip()
    if not normalized_text:
        return 3.0

    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized_text))
    words = len(re.findall(r"[A-Za-z0-9]+", normalized_text))
    ascii_word_chars = sum(len(word) for word in re.findall(r"[A-Za-z0-9]+", normalized_text))
    other_text_chars = 0
    for char in normalized_text:
        # Unicode category 以 L 开头表示各语种字母，N 表示数字。前面已经单独
        # 统计了 CJK 和 ASCII 单词，这里只统计剩余文字，避免英文被重复计时。
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            other_text_chars += 1
    other_text_chars = max(other_text_chars - cjk_chars - ascii_word_chars, 0)
    sentence_count = max(len(utils.split_string_by_punctuations(normalized_text)), 1)

    cjk_duration = cjk_chars / 4.2
    word_duration = words / 2.7
    other_text_duration = other_text_chars / 4.0
    pause_duration = max(sentence_count - 1, 0) * 0.35
    return max(3.0, cjk_duration + word_duration + other_text_duration + pause_duration)
