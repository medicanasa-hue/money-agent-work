import base64
import io
import os
import requests
import app.services.voice as voice_module
from datetime import datetime
from typing import Union
from xml.sax.saxutils import escape

import edge_tts
from edge_tts import SubMaker
from loguru import logger

from app.config import config
from app.utils import utils
from .dispatch import (
    convert_rate_to_percent,
    create_edge_tts_communicate,
    ensure_file_path_exists,
    ensure_legacy_submaker_fields,
    get_edge_tts_timeout_seconds,
    populate_legacy_submaker_with_full_text,
    stream_edge_tts_chunks,
)
from .naming import is_azure_v2_voice, parse_voice_name

_MIMO_DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
_MIMO_DEFAULT_TTS_MODEL = "mimo-v2.5-tts"


def _configure_pydub_ffmpeg(audio_segment_cls):
    configured_ffmpeg = utils.get_ffmpeg_binary()
    if configured_ffmpeg:
        audio_segment_cls.converter = configured_ffmpeg


def azure_tts_v1(
    text: str, voice_name: str, voice_rate: float, voice_file: str
) -> Union[SubMaker, None]:
    voice_name = parse_voice_name(voice_name)
    text = text.strip()
    rate_str = convert_rate_to_percent(voice_rate)
    for i in range(3):
        try:
            logger.info(f"start, voice name: {voice_name}, try: {i + 1}")

            # 这里同时兼容 edge_tts 7.x 和旧版便携包里可能残留的老依赖：
            # 1. 新版支持 `boundary` + `stream_sync()`
            # 2. 旧版不支持 `boundary`，且通常只暴露异步 `stream()`
            ensure_file_path_exists(voice_file)
            communicate = create_edge_tts_communicate(text, voice_name, rate_str)
            sub_maker = edge_tts.SubMaker()
            timeout_seconds = get_edge_tts_timeout_seconds()

            with open(voice_file, "wb") as file:
                def _handle_chunk(chunk):
                    chunk_type = chunk["type"]
                    if chunk_type == "audio":
                        file.write(chunk["data"])
                    elif chunk_type in ["WordBoundary", "SentenceBoundary"]:
                        # 无论来自 7.x 的同步流，还是旧版异步流，只要事件结构
                        # 里仍有边界信息，就统一喂给 SubMaker，保证后续字幕链路
                        # 仍然走项目现有逻辑。
                        sub_maker.feed(chunk)

                stream_edge_tts_chunks(
                    communicate, _handle_chunk, timeout_seconds=timeout_seconds
                )

            if not sub_maker.get_srt():
                logger.warning("failed, sub_maker.get_srt() is empty")
                continue

            logger.info(f"completed, output file: {voice_file}")
            return sub_maker
        except Exception as e:
            logger.error(f"failed, error: {str(e)}")
            # TTS 流式写入如果在首包前超时或网络异常，会留下 0 字节音频文件。
            # 这种文件既不可播放，也可能误导后续排查，因此失败后只清理空文件；
            # 如果已经写入了部分数据，则保留现场文件，便于分析服务端返回内容。
            if os.path.exists(voice_file) and os.path.getsize(voice_file) == 0:
                try:
                    os.remove(voice_file)
                except Exception as remove_error:
                    logger.warning(
                        "failed to remove empty tts file: "
                        f"{voice_file}, error: {str(remove_error)}"
                    )
    return None


def siliconflow_tts(
    text: str,
    model: str,
    voice: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    使用硅基流动的API生成语音

    Args:
        text: 要转换为语音的文本
        model: 模型名称，如 "FunAudioLLM/CosyVoice2-0.5B"
        voice: 声音名称，如 "FunAudioLLM/CosyVoice2-0.5B:alex"
        voice_rate: 语音速度，范围[0.25, 4.0]
        voice_file: 输出的音频文件路径
        voice_volume: 语音音量，范围[0.6, 5.0]，需要转换为硅基流动的增益范围[-10, 10]

    Returns:
        SubMaker对象或None
    """
    text = text.strip()
    api_key = config.siliconflow.get("api_key", "")

    if not api_key:
        logger.error("SiliconFlow API key is not set")
        return None

    # 将voice_volume转换为硅基流动的增益范围
    # 默认voice_volume为1.0，对应gain为0
    gain = voice_volume - 1.0
    # 确保gain在[-10, 10]范围内
    gain = max(-10, min(10, gain))

    url = "https://api.siliconflow.cn/v1/audio/speech"

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        "sample_rate": 32000,
        "stream": False,
        "speed": voice_rate,
        "gain": gain,
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for i in range(3):  # 尝试3次
        try:
            logger.info(
                f"start siliconflow tts, model: {model}, voice: {voice}, try: {i + 1}"
            )

            response = requests.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                # 保存音频文件
                with open(voice_file, "wb") as f:
                    f.write(response.content)

                # 这里仍然沿用项目原有的字幕结构，因此需要补齐旧字段。
                sub_maker = ensure_legacy_submaker_fields(SubMaker())

                # 获取音频文件的实际长度
                try:
                    # 尝试使用moviepy获取音频长度
                    audio_clip = voice_module.AudioFileClip(voice_file)
                    audio_duration = audio_clip.duration
                    audio_clip.close()

                    # 将音频长度转换为100纳秒单位（与edge_tts兼容）
                    audio_duration_100ns = int(audio_duration * 10000000)

                    # 使用文本分割来创建更准确的字幕
                    # 将文本按标点符号分割成句子
                    sentences = utils.split_string_by_punctuations(text)

                    if sentences:
                        # 计算每个句子的大致时长（按字符数比例分配）
                        total_chars = sum(len(s) for s in sentences)
                        char_duration = (
                            audio_duration_100ns / total_chars if total_chars > 0 else 0
                        )

                        current_offset = 0
                        for sentence in sentences:
                            if not sentence.strip():
                                continue

                            # 计算当前句子的时长
                            sentence_chars = len(sentence)
                            sentence_duration = int(sentence_chars * char_duration)

                            # 添加到SubMaker
                            sub_maker.subs.append(sentence)
                            sub_maker.offset.append(
                                (current_offset, current_offset + sentence_duration)
                            )

                            # 更新偏移量
                            current_offset += sentence_duration
                    else:
                        # 如果无法分割，则使用整个文本作为一个字幕
                        sub_maker.subs = [text]
                        sub_maker.offset = [(0, audio_duration_100ns)]

                except Exception as e:
                    logger.warning(f"Failed to create accurate subtitles: {str(e)}")
                    # 回退到简单的字幕
                    sub_maker.subs = [text]
                    # 使用音频文件的实际长度，如果无法获取，则假设为10秒
                    sub_maker.offset = [
                        (
                            0,
                            audio_duration_100ns
                            if "audio_duration_100ns" in locals()
                            else 10000000,
                        )
                    ]

                logger.success(f"siliconflow tts succeeded: {voice_file}")
                logger.debug(
                    "siliconflow subtitle timeline generated, "
                    f"subs: {len(sub_maker.subs)}, offsets: {len(sub_maker.offset)}"
                )
                return sub_maker
            else:
                logger.error(
                    f"siliconflow tts failed with status code {response.status_code}: {response.text}"
                )
        except Exception as e:
            logger.error(f"siliconflow tts failed: {str(e)}")

    return None


def _build_azure_v2_ssml(text: str, voice_name: str, voice_rate: float) -> str:
    """Build safe Azure Speech SSML with a normalized speaking rate."""
    try:
        normalized_rate = float(voice_rate)
    except (TypeError, ValueError):
        normalized_rate = 1.0
    normalized_rate = max(0.25, min(4.0, normalized_rate))

    locale_parts = voice_name.split("-", 2)
    locale = "-".join(locale_parts[:2]) if len(locale_parts) >= 2 else "en-US"
    escaped_voice_name = escape(voice_name, {'"': "&quot;"})
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{locale}"><voice name="{escaped_voice_name}">'
        f'<prosody rate="{normalized_rate:g}">{escape(text)}</prosody>'
        "</voice></speak>"
    )


def azure_tts_v2(
    text: str,
    voice_name: str,
    voice_file: str,
    voice_rate: float = 1.0,
) -> Union[SubMaker, None]:
    voice_name = is_azure_v2_voice(voice_name)
    if not voice_name:
        logger.error(f"invalid voice name: {voice_name}")
        raise ValueError(f"invalid voice name: {voice_name}")
    text = text.strip()
    ssml = _build_azure_v2_ssml(text, voice_name, voice_rate)

    def _format_duration_to_offset(duration) -> int:
        if isinstance(duration, str):
            time_obj = datetime.strptime(duration, "%H:%M:%S.%f")
            milliseconds = (
                (time_obj.hour * 3600000)
                + (time_obj.minute * 60000)
                + (time_obj.second * 1000)
                + (time_obj.microsecond // 1000)
            )
            return milliseconds * 10000

        if isinstance(duration, int):
            return duration

        return 0

    for i in range(3):
        try:
            logger.info(
                f"start, voice name: {voice_name}, rate: {voice_rate}, try: {i + 1}"
            )

            import azure.cognitiveservices.speech as speechsdk

            sub_maker = ensure_legacy_submaker_fields(SubMaker())

            def speech_synthesizer_word_boundary_cb(evt: speechsdk.SessionEventArgs):
                # print('WordBoundary event:')
                # print('\tBoundaryType: {}'.format(evt.boundary_type))
                # print('\tAudioOffset: {}ms'.format((evt.audio_offset + 5000)))
                # print('\tDuration: {}'.format(evt.duration))
                # print('\tText: {}'.format(evt.text))
                # print('\tTextOffset: {}'.format(evt.text_offset))
                # print('\tWordLength: {}'.format(evt.word_length))

                duration = _format_duration_to_offset(str(evt.duration))
                offset = _format_duration_to_offset(evt.audio_offset)
                sub_maker.subs.append(evt.text)
                sub_maker.offset.append((offset, offset + duration))

            # Creates an instance of a speech config with specified subscription key and service region.
            speech_key = config.azure.get("speech_key", "")
            service_region = config.azure.get("speech_region", "")
            if not speech_key or not service_region:
                logger.error("Azure speech key or region is not set")
                return None

            audio_config = speechsdk.audio.AudioOutputConfig(
                filename=voice_file, use_default_speaker=True
            )
            speech_config = speechsdk.SpeechConfig(
                subscription=speech_key, region=service_region
            )
            speech_config.speech_synthesis_voice_name = voice_name
            # speech_config.set_property(property_id=speechsdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary,
            #                            value='true')
            speech_config.set_property(
                property_id=speechsdk.PropertyId.SpeechServiceResponse_RequestWordBoundary,
                value="true",
            )

            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
            )
            speech_synthesizer = speechsdk.SpeechSynthesizer(
                audio_config=audio_config, speech_config=speech_config
            )
            speech_synthesizer.synthesis_word_boundary.connect(
                speech_synthesizer_word_boundary_cb
            )

            result = speech_synthesizer.speak_ssml_async(ssml).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                logger.success(f"azure v2 speech synthesis succeeded: {voice_file}")
                return sub_maker
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                logger.error(
                    f"azure v2 speech synthesis canceled: {cancellation_details.reason}"
                )
                if cancellation_details.reason == speechsdk.CancellationReason.Error:
                    logger.error(
                        f"azure v2 speech synthesis error: {cancellation_details.error_details}"
                    )
            logger.info(f"completed, output file: {voice_file}")
        except Exception as e:
            logger.error(f"failed, error: {str(e)}")
    return None


def gemini_tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    使用Google Gemini TTS生成语音

    Args:
        text: 要转换的文本
        voice_name: 语音名称，如 "Zephyr", "Puck" 等
        voice_rate: 语音速率（当前未使用）
        voice_file: 输出音频文件路径
        voice_volume: 音频音量（当前未使用）

    Returns:
        SubMaker对象或None
    """
    import base64
    import io
    from pydub import AudioSegment
    from google import genai
    from google.genai import types
    _configure_pydub_ffmpeg(AudioSegment)

    try:
        # 配置Gemini API
        api_key = config.app.get("gemini_api_key", "")
        if not api_key:
            logger.error("Gemini API key is not set")
            return None

        logger.info(f"start, voice name: {voice_name}, try: 1")

        generation_config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        )
        client = genai.Client(api_key=api_key)
        if hasattr(client, "__enter__"):
            with client:
                response = client.models.generate_content(
                    model="gemini-2.5-flash-preview-tts",
                    contents=text,
                    config=generation_config,
                )
        else:
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash-preview-tts",
                    contents=text,
                    config=generation_config,
                )
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        # 检查响应
        if not response.candidates or not response.candidates[0].content:
            logger.error("No audio content received from Gemini TTS")
            return None

        # 获取音频数据
        audio_data = None
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                audio_data = part.inline_data.data
                break

        if not audio_data:
            logger.error("No audio data found in response")
            return None

        # 音频数据已经是原始字节，不需要base64解码
        if isinstance(audio_data, str):
            # 如果是字符串，则需要base64解码
            audio_bytes = base64.b64decode(audio_data)
        else:
            # 如果已经是字节，直接使用
            audio_bytes = audio_data

        # 尝试不同的音频格式 - Gemini可能返回不同的格式
        audio_segment = None

        # Gemini返回Linear PCM格式，按照文档参数解析
        try:
            audio_segment = AudioSegment.from_file(
                io.BytesIO(audio_bytes),
                format="raw",
                frame_rate=24000,  # Gemini TTS默认采样率
                channels=1,        # 单声道
                sample_width=2     # 16-bit
            )
        except Exception as e:
            logger.error(f"Failed to load PCM audio: {e}")
            return None

        # 导出为MP3格式
        ensure_file_path_exists(voice_file)
        export_handle = audio_segment.export(voice_file, format="mp3")
        if export_handle is not None:
            export_handle.close()

        logger.info(f"completed, output file: {voice_file}")

        # Gemini 拿不到 edge_tts 那种逐词边界事件，因此这里退回到
        # 项目原有的 `subs/offset` 兼容结构，至少保证后续字幕与时长
        # 计算链路可继续工作。
        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        audio_duration = len(audio_segment) / 1000.0  # 转换为秒
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=audio_duration,
        )

    except ImportError as e:
        logger.error(f"Missing required package for Gemini TTS: {str(e)}. Please install: pip install pydub")
        return None
    except Exception as e:
        logger.error(f"Gemini TTS failed, error: {str(e)}")
        return None


def mimo_tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    """
    使用 Xiaomi MiMo V2.5 TTS 生成语音。

    官方接口兼容 OpenAI Chat Completions，但 TTS 有两个关键差异：
    1. 待合成文本必须放在 `assistant` 消息里；
    2. 音频以 `message.audio.data` 的 base64 字符串返回。

    MiMo 当前没有返回逐词时间轴，因此这里复用项目已有的 legacy
    SubMaker 兜底方案：根据最终音频时长和脚本文本断句生成字幕时间轴。
    """
    from pydub import AudioSegment

    text = (text or "").strip()
    if not text:
        logger.error("MiMo TTS text is empty")
        return None

    api_key = config.app.get("mimo_api_key", "")
    if not api_key:
        logger.error("MiMo API key is not set")
        return None

    base_url = config.app.get("mimo_base_url", "") or _MIMO_DEFAULT_BASE_URL
    model_name = config.app.get("mimo_tts_model_name", "") or _MIMO_DEFAULT_TTS_MODEL
    style_prompt = config.app.get(
        "mimo_tts_style_prompt",
        "请用自然、清晰、适合短视频旁白的语气朗读。",
    )

    _configure_pydub_ffmpeg(AudioSegment)

    for i in range(3):
        try:
            logger.info(
                f"start mimo tts, model: {model_name}, voice: {voice_name}, try: {i + 1}"
            )
            ensure_file_path_exists(voice_file)

            client = voice_module.OpenAI(api_key=api_key, base_url=base_url)
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "user", "content": style_prompt},
                    {"role": "assistant", "content": text},
                ],
                audio={
                    "format": "wav",
                    "voice": voice_name,
                },
            )

            if not completion or not getattr(completion, "choices", None):
                raise ValueError("MiMo TTS returned empty response")

            message = completion.choices[0].message
            audio = getattr(message, "audio", None)
            audio_data = None
            if isinstance(audio, dict):
                audio_data = audio.get("data")
            elif audio is not None:
                audio_data = getattr(audio, "data", None)

            if not audio_data:
                raise ValueError("MiMo TTS returned empty audio data")

            audio_bytes = base64.b64decode(audio_data)
            audio_segment = AudioSegment.from_file(io.BytesIO(audio_bytes), format="wav")

            output_format = utils.parse_extension(voice_file) or "mp3"
            if output_format == "wav":
                with open(voice_file, "wb") as f:
                    f.write(audio_bytes)
            else:
                audio_segment.export(voice_file, format=output_format)

            audio_duration = len(audio_segment) / 1000.0
            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"mimo tts succeeded: {voice_file}")
            logger.debug(
                "mimo subtitle timeline generated, "
                f"duration: {audio_duration:.3f}s, output_format: {output_format}"
            )
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"mimo tts failed: {str(e)}")

    return None


def elevenlabs_tts(
    text: str,
    voice_id: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    text = (text or "").strip()
    if not text:
        logger.error("ElevenLabs TTS text is empty")
        return None

    api_key = voice_module.config.elevenlabs.get("api_key", "")
    if not api_key:
        logger.error("ElevenLabs API key is not set")
        return None

    if not model_id:
        model_id = voice_module.config.elevenlabs.get(
            "model_id", "eleven_multilingual_v2"
        )

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    # Errors where retrying will never help (auth/access/validation failures).
    _NON_RETRYABLE_CODES = {401, 403, 422}
    _NON_RETRYABLE_STATUSES = {"voice_disabled", "voice_access_denied", "unauthorized"}

    for i in range(3):
        try:
            logger.info(f"start elevenlabs tts, voice_id: {voice_id}, try: {i + 1}")
            ensure_file_path_exists(voice_file)

            response = requests.post(url, json=payload, headers=headers, timeout=60)
            if response.status_code != 200:
                error_status = ""
                try:
                    detail = response.json().get("detail", {})
                    if isinstance(detail, dict):
                        error_status = detail.get("status", "")
                except Exception as e:
                    logger.warning(
                        f"failed to parse ElevenLabs error response: {str(e)}"
                    )

                if response.status_code in _NON_RETRYABLE_CODES or error_status in _NON_RETRYABLE_STATUSES:
                    logger.error(
                        f"ElevenLabs TTS failed (non-retryable) — voice_id: {voice_id}, "
                        f"status: {response.status_code}, error: {error_status or response.text[:200]}. "
                        "Please select a different ElevenLabs voice."
                    )
                    return None

                logger.error(
                    f"elevenlabs tts failed with status {response.status_code}: {response.text[:200]}"
                )
                continue

            with open(voice_file, "wb") as f:
                f.write(response.content)

            audio_clip = voice_module.AudioFileClip(voice_file)
            audio_duration = audio_clip.duration
            audio_clip.close()

            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"elevenlabs tts succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"elevenlabs tts failed: {str(e)}")

    return None


def chatterbox_tts(
    text: str,
    voice: str,
    voice_file: str,
    voice_rate: float = 1.0,
    voice_volume: float = 1.0,
    model_id: str = "",
) -> Union[SubMaker, None]:
    """Generate speech with a self-hosted Chatterbox TTS server.

    Chatterbox (Resemble AI, MIT) is an open-source, locally hosted TTS model
    with zero-shot voice cloning — a self-hostable alternative to ElevenLabs.
    This talks to an OpenAI-compatible ``/audio/speech`` endpoint, so it works
    with the common community servers (e.g. devnen/Chatterbox-TTS-Server,
    travisvn/chatterbox-tts-api). Configure ``[chatterbox] base_url`` (and an
    optional ``api_key``).

    Like ElevenLabs, Chatterbox does not return word-level timestamps, so the
    subtitle path falls back to the full-text SubMaker. For tighter subtitle
    sync set ``subtitle_provider = "whisper"``.
    """
    text = (text or "").strip()
    if not text:
        logger.error("Chatterbox TTS text is empty")
        return None

    base_url = (config.chatterbox.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        logger.error(
            "Chatterbox base_url is not set, please configure [chatterbox] base_url in config.toml"
        )
        return None

    api_key = config.chatterbox.get("api_key", "")
    if not model_id:
        model_id = config.chatterbox.get("model_id", "chatterbox") or "chatterbox"

    url = f"{base_url}/audio/speech"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model_id,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        # OpenAI speech API accepts speed 0.25-4.0; MoneyPrinterTurbo's rate is a
        # 1.0-centred multiplier, so it maps directly (clamped to the valid range).
        "speed": max(0.25, min(4.0, float(voice_rate or 1.0))),
    }
    # voice_volume is accepted for parity with the other TTS providers but is
    # intentionally not sent: the OpenAI /audio/speech contract has no volume
    # field, so Chatterbox servers ignore it. Adjust loudness via voice_rate
    # (speed) or in post-processing instead.

    for i in range(3):
        try:
            logger.info(f"start chatterbox tts, voice: {voice}, try: {i + 1}")
            ensure_file_path_exists(voice_file)

            response = requests.post(url, json=payload, headers=headers, timeout=120)
            if response.status_code != 200:
                logger.error(
                    f"chatterbox tts failed with status {response.status_code}: {response.text[:200]}"
                )
                continue

            with open(voice_file, "wb") as f:
                f.write(response.content)

            audio_clip = voice_module.AudioFileClip(voice_file)
            audio_duration = audio_clip.duration
            audio_clip.close()

            sub_maker = ensure_legacy_submaker_fields(SubMaker())
            logger.success(f"chatterbox tts succeeded: {voice_file}")
            return populate_legacy_submaker_with_full_text(
                sub_maker=sub_maker,
                text=text,
                audio_duration_seconds=audio_duration,
            )
        except Exception as e:
            logger.error(f"chatterbox tts failed: {str(e)}")

    return None
