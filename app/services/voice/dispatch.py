import asyncio
import inspect
import os
import queue
import subprocess
import threading
import time
from typing import Union

import edge_tts
from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.utils import utils
from .naming import (
    estimate_no_voice_duration,
    is_azure_v2_voice,
    is_chatterbox_voice,
    is_elevenlabs_voice,
    is_gemini_voice,
    is_mimo_voice,
    is_no_voice,
    is_siliconflow_voice,
)

_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS = 30.0


def _configured_fallback_voice_names(primary_voice_name: str) -> list[str]:
    """Return explicit backup voices without changing the default TTS path."""
    configured = config.app.get("tts_fallback_voice_names", [])
    if isinstance(configured, str):
        configured = configured.split(",")
    if not isinstance(configured, (list, tuple)):
        return []

    primary_key = str(primary_voice_name or "").strip().casefold()
    seen = {primary_key} if primary_key else set()
    fallbacks = []
    for value in configured:
        voice_name = str(value or "").strip()
        voice_key = voice_name.casefold()
        if not voice_name or voice_key in seen or is_no_voice(voice_name):
            continue
        seen.add(voice_key)
        fallbacks.append(voice_name)
    return fallbacks


def _remove_failed_voice_output(voice_file: str) -> None:
    try:
        if os.path.isfile(voice_file):
            os.remove(voice_file)
    except OSError:
        logger.warning("Could not remove incomplete audio before a TTS fallback.")


def generate_silent_audio(duration_seconds: float, output_file: str) -> bool:
    """
    生成 MP3 静音音频，作为“无配音”模式的时间轴占位。

    使用 FFmpeg 的 anullsrc 直接生成静音，比先构造临时 WAV 再转码更少中间
    文件。失败时返回 False，让上层按普通 TTS 失败路径处理并记录日志。
    """
    ensure_file_path_exists(output_file)
    duration_seconds = max(float(duration_seconds or 0), 0.1)
    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        f"{duration_seconds:.3f}",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        output_file,
    ]

    logger.info(
        f"generating silent audio for no-voice mode, duration: {duration_seconds:.2f}s"
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "failed to generate silent audio: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
        return False
    if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
        logger.error(
            "silent audio output file is missing or empty, "
            f"file: {output_file}, duration: {duration_seconds:.2f}s"
        )
        return False
    return True


def _tts_once(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    import app.services.voice as voice_module

    if is_no_voice(voice_name):
        duration_seconds = estimate_no_voice_duration(text)
        if not generate_silent_audio(duration_seconds, voice_file):
            return None

        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=duration_seconds,
        )

    if is_azure_v2_voice(voice_name):
        return voice_module.azure_tts_v2(
            text,
            voice_name,
            voice_file,
            voice_rate=voice_rate,
        )
    elif is_siliconflow_voice(voice_name):
        # 从voice_name中提取模型和声音
        # 格式: siliconflow:model:voice-Gender
        parts = voice_name.split(":")
        if len(parts) >= 3:
            model = parts[1]
            # 移除性别后缀，例如 "alex-Male" -> "alex"
            voice_with_gender = parts[2]
            voice = voice_with_gender.split("-")[0]
            # 构建完整的voice参数，格式为 "model:voice"
            full_voice = f"{model}:{voice}"
            return voice_module.siliconflow_tts(
                text, model, full_voice, voice_rate, voice_file, voice_volume
            )
        else:
            logger.error(f"Invalid siliconflow voice name format: {voice_name}")
            return None
    elif is_gemini_voice(voice_name):
        # 从voice_name中提取声音名称
        # 格式: gemini:voice-Gender
        parts = voice_name.split(":")
        if len(parts) >= 2:
            # 移除性别后缀，例如 "Zephyr-Female" -> "Zephyr"
            voice_with_gender = parts[1]
            voice = voice_with_gender.split("-")[0]
            return voice_module.gemini_tts(
                text, voice, voice_rate, voice_file, voice_volume
            )
        else:
            logger.error(f"Invalid gemini voice name format: {voice_name}")
            return None
    elif is_mimo_voice(voice_name):
        # 从voice_name中提取声音名称
        # 格式: mimo:voice-Gender；如果调用方已执行 parse_voice_name，
        # 则可能是 mimo:voice。两种格式都兼容。
        parts = voice_name.split(":")
        if len(parts) >= 2:
            voice_with_gender = parts[1]
            voice = voice_with_gender.split("-")[0]
            return voice_module.mimo_tts(text, voice, voice_rate, voice_file, voice_volume)
        else:
            logger.error(f"Invalid mimo voice name format: {voice_name}")
            return None
    elif is_elevenlabs_voice(voice_name):
        # 格式: elevenlabs:{voice_id}:{name}
        parts = voice_name.split(":")
        if len(parts) >= 2:
            voice_id = parts[1]
            return voice_module.elevenlabs_tts(
                text, voice_id, voice_file, voice_rate, voice_volume
            )
        else:
            logger.error(f"Invalid elevenlabs voice name format: {voice_name}")
            return None
    elif is_chatterbox_voice(voice_name):
        # 格式: chatterbox:<voice>，voice 可带显示用的 -Female/-Male 后缀
        parts = voice_name.split(":", 1)
        if len(parts) >= 2 and parts[1].strip():
            chatterbox_voice = parts[1].strip()
            if chatterbox_voice.endswith(("-Female", "-Male")):
                chatterbox_voice = chatterbox_voice.rsplit("-", 1)[0]
            return voice_module.chatterbox_tts(
                text, chatterbox_voice, voice_file, voice_rate, voice_volume
            )
        else:
            logger.error(f"Invalid chatterbox voice name format: {voice_name}")
            return None
    return voice_module.azure_tts_v1(text, voice_name, voice_rate, voice_file)


def tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """Generate TTS once, then try explicitly configured backup voices.

    Fallback is disabled unless ``tts_fallback_voice_names`` is set. The
    no-voice mode remains terminal because it intentionally requests silence.
    """
    if is_no_voice(voice_name):
        return _tts_once(text, voice_name, voice_rate, voice_file, voice_volume)

    fallback_voice_names = _configured_fallback_voice_names(voice_name)
    if not fallback_voice_names:
        return _tts_once(text, voice_name, voice_rate, voice_file, voice_volume)

    try:
        result = _tts_once(text, voice_name, voice_rate, voice_file, voice_volume)
    except Exception as error:
        logger.warning(
            "Primary TTS attempt raised "
            f"{type(error).__name__}; trying a configured fallback voice."
        )
        result = None
    if result is not None:
        return result

    for fallback_voice_name in fallback_voice_names:
        _remove_failed_voice_output(voice_file)
        try:
            result = _tts_once(
                text,
                fallback_voice_name,
                voice_rate,
                voice_file,
                voice_volume,
            )
        except Exception as error:
            logger.warning(
                "Configured TTS fallback raised "
                f"{type(error).__name__}; trying the next fallback if available."
            )
            continue
        if result is not None:
            return result
    return None


def convert_rate_to_percent(rate: float) -> str:
    # edge-tts requires a sign-prefixed percentage (e.g. "+0%", "-20%").
    # Rounding can yield 0 for rates near but not equal to 1.0 (e.g. 1.004,
    # 0.997); those must still be returned as "+0%", not the unsigned "0%"
    # which edge-tts rejects with ValueError: Invalid rate '0%'.
    # API 或批处理调用可能传入 0、0.0、None 或无法转换的空值；这些值不代表
    # 合法语速，直接计算会变成 -100% 或抛异常。这里统一回退到正常语速，
    # 避免生成极慢音频或让 TTS 流程在边界输入下失败。
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    percent = round((rate - 1.0) * 100)
    if percent >= 0:
        return f"+{percent}%"
    return f"{percent}%"


def ensure_file_path_exists(file_path: str) -> None:
    """
    确保输出文件所在目录一定存在。

    这里单独做一层兜底，是因为 edge_tts 7.x 在真正发起网络请求之前，
    就会先打开目标音频文件；如果目录不存在，会直接因为本地文件路径报错，
    从而掩盖真正的 TTS 行为结果。
    """
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)


def ensure_legacy_submaker_fields(sub_maker: SubMaker) -> SubMaker:
    """
    为项目里仍然沿用旧字幕结构的调用方补齐兼容字段。

    edge_tts 7.x 的 `SubMaker` 主要暴露 `cues/get_srt()`，但项目里 Azure v2、
    Gemini、SiliconFlow 这些路径仍然会直接读写 `subs/offset`。这里统一补齐，
    避免升级 edge_tts 后这些非 edge 路径被连带破坏。
    """
    if not hasattr(sub_maker, "subs"):
        sub_maker.subs = []
    if not hasattr(sub_maker, "offset"):
        sub_maker.offset = []
    return sub_maker


def populate_legacy_submaker_with_full_text(
    sub_maker: SubMaker, text: str, audio_duration_seconds: float
) -> SubMaker:
    """
    用整段文本填充项目历史沿用的 `subs/offset` 字幕结构。

    背景：
    1. edge_tts 7.x 的 `SubMaker` 不再提供旧版本里的 `create_sub()`；
    2. 项目里 Gemini、SiliconFlow 等非 edge 路径依然需要返回一个
       带 `subs/offset` 的对象，供后续统一计算音频时长和生成字幕；
    3. 对于拿不到逐词边界的 TTS 服务，需要至少按脚本断句切成多个片段，
       这样后续 `subtitle_provider=edge` 的聚合逻辑才能继续工作，而不是
       因为整段文本无法和脚本断句逐行匹配而回退 Whisper。

    Args:
        sub_maker: 需要写入兼容字段的字幕对象
        text: 原始脚本文本
        audio_duration_seconds: 音频总时长，单位秒

    Returns:
        已填充兼容字幕数据的 SubMaker 对象
    """
    sub_maker = ensure_legacy_submaker_fields(sub_maker)

    # 清空旧值，避免调用方重复复用对象时出现脏数据叠加。
    sub_maker.subs = []
    sub_maker.offset = []

    normalized_text = (text or "").strip()
    if not normalized_text:
        return sub_maker

    audio_duration_100ns = max(int(audio_duration_seconds * 10000000), 1)

    # Gemini / SiliconFlow 这类路径拿不到逐词边界时，仍然尽量沿用项目
    # 原来的“按标点断句 + 按字符数比例分配时长”的策略。这样既能让
    # create_subtitle() 匹配脚本断句，也能避免再次回退 Whisper。
    sentences = utils.split_string_by_punctuations(normalized_text)
    if not sentences:
        sentences = [normalized_text]

    total_chars = sum(len(sentence) for sentence in sentences)
    if total_chars <= 0:
        sub_maker.subs.append(normalized_text)
        sub_maker.offset.append((0, audio_duration_100ns))
        return sub_maker

    current_offset = 0
    for index, sentence in enumerate(sentences):
        cleaned_sentence = sentence.strip()
        if not cleaned_sentence:
            continue

        # 前面的句子按字符数比例分配时长，最后一句兜底吃掉剩余时长，
        # 避免整数取整导致总时长丢失或字幕结束时间短于音频。
        if index == len(sentences) - 1:
            sentence_end = audio_duration_100ns
        else:
            sentence_chars = len(cleaned_sentence)
            sentence_duration = max(
                int(audio_duration_100ns * (sentence_chars / total_chars)),
                1,
            )
            sentence_end = min(current_offset + sentence_duration, audio_duration_100ns)

        sub_maker.subs.append(cleaned_sentence)
        sub_maker.offset.append((current_offset, sentence_end))
        current_offset = sentence_end

    return sub_maker


def create_edge_tts_communicate(
    text: str, voice_name: str, rate_str: str
) -> edge_tts.Communicate:
    """
    按当前已安装的 edge_tts 版本构造 Communicate 对象。

    背景：
    1. 主线代码已经升级到 edge_tts 7.x，并使用 `boundary` 参数拿到更细的边界事件；
    2. 但 Windows 便携包如果更新失败，现场环境可能仍然停留在旧版 edge_tts；
    3. 旧版 `Communicate.__init__()` 不接受 `boundary`，会直接抛出
       `unexpected keyword argument 'boundary'`，导致整个 TTS 链路失败。

    因此这里先根据构造函数签名探测当前版本支持的参数，再决定是否传入
    `boundary`，让同一份代码同时兼容旧版和新版依赖。
    """
    communicate_kwargs = {"rate": rate_str}
    communicate_signature = inspect.signature(edge_tts.Communicate)

    if "boundary" in communicate_signature.parameters:
        communicate_kwargs["boundary"] = "WordBoundary"

    return edge_tts.Communicate(text, voice_name, **communicate_kwargs)


def get_edge_tts_timeout_seconds() -> Union[float, None]:
    """
    获取 Azure TTS V1 单次流式请求的超时时间。

    背景：
    Edge consumer TTS 在网络不通、服务端限流、voice 与文本语言不匹配等场景下，
    可能长时间卡在 `stream_sync()` 内部，日志只停留在 `start`。这里提供一个
    默认超时，避免 WebUI 任务长期无反馈。

    使用方式：
    - 默认 30 秒，覆盖常见短视频脚本的首包等待时间；
    - 如用户处于慢网络或代理环境，可在 `config.toml` 里设置
      `edge_tts_timeout = 60`；
    - 设置为 0 或负数表示显式禁用超时，保留完全向后兼容。
    """
    raw_timeout = config.app.get(
        "edge_tts_timeout", _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(
            "invalid edge_tts_timeout: "
            f"{raw_timeout}, fallback to {_DEFAULT_EDGE_TTS_TIMEOUT_SECONDS}s"
        )
        timeout_seconds = _DEFAULT_EDGE_TTS_TIMEOUT_SECONDS

    if timeout_seconds <= 0:
        return None

    return timeout_seconds


def _stream_edge_tts_sync_with_timeout(
    communicate, on_chunk, timeout_seconds: float
) -> None:
    """
    带总超时地消费 edge_tts 7.x 的同步流。

    实现原因：
    `stream_sync()` 本身是阻塞迭代器，网络层卡住时主线程无法及时恢复。
    这里把阻塞迭代放到 daemon 线程中，主线程通过 Queue 获取 chunk，
    到达超时时间后直接抛出 TimeoutError，让外层重试和错误日志继续工作。

    注意：
    daemon 线程只作为兜底保护使用，最多随 Azure TTS V1 的 3 次重试产生
    少量残留线程；进程退出时会自动回收。相比 WebUI 任务永久卡住，这是
    更可控的失败模式。
    """
    stream_queue = queue.Queue()
    done_marker = object()

    def _produce_chunks():
        try:
            for chunk in communicate.stream_sync():
                stream_queue.put(("chunk", chunk))
            stream_queue.put(("done", done_marker))
        except Exception as e:
            logger.warning(f"edge tts stream producer failed: {str(e)}")
            stream_queue.put(("error", e))

    thread = threading.Thread(target=_produce_chunks, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError(
                f"edge_tts stream timed out after {timeout_seconds:g}s"
            )

        try:
            item_type, payload = stream_queue.get(
                timeout=min(0.5, remaining_seconds)
            )
        except queue.Empty:
            continue

        if item_type == "chunk":
            on_chunk(payload)
        elif item_type == "error":
            raise payload
        elif item_type == "done":
            return


def stream_edge_tts_chunks(
    communicate, on_chunk, timeout_seconds: Union[float, None] = None
) -> None:
    """
    统一消费 edge_tts 的同步流和旧版异步流。

    edge_tts 7.x 提供 `stream_sync()`，可以在同步函数里直接迭代；
    更早的版本通常只有异步 `stream()`。为了让 `azure_tts_v1()` 在
    旧依赖残留场景下仍能继续工作，这里统一做一层流式兼容。

    Args:
        communicate: edge_tts.Communicate 实例
        on_chunk: 每拿到一个事件块时执行的回调
        timeout_seconds: 单次流式请求总超时；为 None 时不启用超时。
    """
    if hasattr(communicate, "stream_sync"):
        if timeout_seconds:
            _stream_edge_tts_sync_with_timeout(
                communicate, on_chunk, timeout_seconds
            )
            return

        for chunk in communicate.stream_sync():
            on_chunk(chunk)
        return

    if not hasattr(communicate, "stream"):
        raise AttributeError("edge_tts communicate object has no stream method")

    async def _consume_async_stream():
        async for chunk in communicate.stream():
            on_chunk(chunk)

    # 这里显式创建独立事件循环，而不是复用外部上下文，目的是避免
    # 在同步调用栈里遇到“当前线程没有事件循环”或跨线程复用循环的问题。
    loop = asyncio.new_event_loop()
    try:
        if timeout_seconds:
            loop.run_until_complete(
                asyncio.wait_for(_consume_async_stream(), timeout=timeout_seconds)
            )
        else:
            loop.run_until_complete(_consume_async_stream())
    finally:
        loop.close()
