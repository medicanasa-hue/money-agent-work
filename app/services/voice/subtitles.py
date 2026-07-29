import asyncio
import math
import os
import re
import unicodedata
from typing import Union
from xml.sax.saxutils import unescape

from edge_tts import SubMaker
from loguru import logger
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.tools import subtitles

from app.models.schema import VideoAspect
from app.utils import utils
from .discovery import get_all_azure_voices
from .dispatch import ensure_file_path_exists
from .naming import is_azure_v2_voice, parse_voice_name
from .providers import azure_tts_v2

_DEFAULT_ASS_MARGIN_V = 80
_PORTRAIT_ASS_SAFE_MARGIN_RATIO = 0.16
_DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_SECOND = 17.0
_DEFAULT_SUBTITLE_MAX_LINES = 2
_DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_LINE = 42


def mktimestamp(time_unit: float) -> str:
    hour = math.floor(time_unit / 10**7 / 3600)
    minute = math.floor((time_unit / 10**7 / 60) % 60)
    seconds = (time_unit / 10**7) % 60
    return f"{hour:02d}:{minute:02d}:{seconds:06.3f}"


def _format_text(text: str) -> str:
    """
    清理字幕对齐前的脚本文本。

    这里不能只在 LLM 生成阶段处理，因为用户也可能手动粘贴脚本，或通过
    API 直接传入包含 Markdown 标记的文本。TTS 通常不会朗读 `---`、
    `___`、`***` 这类分隔符行，也不会朗读 `_` 这种强调标记；如果字幕
    对齐仍保留这些字符，`create_subtitle()` 会一直等待不存在的 cue，
    最终导致字幕文件缺失并在 Whisper fallback 校正时补出全 0 时间轴。
    """
    text = text.replace("[", " ")
    text = text.replace("]", " ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = text.replace("{", " ")
    text = text.replace("}", " ")
    return utils.normalize_script_for_subtitle_matching(text)


def _build_subtitle_formatter():
    """
    返回统一的 SRT 行格式化函数。

    这里单独拆成一个小工具，是为了让 edge_tts 7.x 的 cues 路径
    和项目原有的 legacy `subs/offset` 路径共用同一套字幕落盘格式，
    避免两套逻辑各自产生细微格式差异。
    """

    def formatter(idx: int, start_time: float, end_time: float, sub_text: str) -> str:
        start_t = mktimestamp(start_time).replace(".", ",")
        end_t = mktimestamp(end_time).replace(".", ",")
        return f"{idx}\n{start_t} --> {end_t}\n{sub_text}\n"

    return formatter


# 阿拉伯语变音符号和 Tatweel 拉长符在 edge-tts 返回文本中可能出现，
# 这些字符不影响语义，但会导致脚本文本和字幕 cue 字符串精确匹配失败。
_ARABIC_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u0640\u06D6-\u06ED]")


def _normalize_arabic(text: str) -> str:
    """统一阿拉伯语常见字母变体，提升字幕 cue 与脚本行的匹配容错率。

    edge-tts 对阿拉伯语可能返回与原脚本不同的字母形态，例如把 أ/إ/آ
    归一成 ا，或者携带变音符号。这里仅在最后一层匹配兜底中使用，
    不改变原始字幕文本，避免影响最终展示内容。
    """
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in (
        ("أإآٱ", "ا"),
        ("ىئ", "ي"),
        ("ة", "ه"),
        ("ؤ", "و"),
    ):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def _match_script_line(script_lines: list[str], current_text: str, sub_index: int) -> str:
    """
    尝试把当前累计的字幕文本，与脚本中的某一条标准断句匹配起来。

    这里复用了项目原有的“按标点拆脚本，再逐段比对”的思路：
    1. 优先精确匹配；
    2. 再做一次去标点和 Markdown `_` 格式符后的匹配；
    3. 最后做一次阿拉伯语字符形态归一化匹配。

    这样可以兼容：
    - TTS 返回里可能缺失或单独拆分的标点；
    - 中文场景下词边界和脚本文本不完全一一对应的情况。
    """
    if len(script_lines) <= sub_index:
        return ""

    target_line = script_lines[sub_index]
    if current_text == target_line:
        return target_line.strip()

    current_text_normalized = re.sub(r"[_\W]+", "", current_text)
    target_line_normalized = re.sub(r"[_\W]+", "", target_line)
    if current_text_normalized == target_line_normalized:
        return target_line.strip()

    # 最后一层阿拉伯语容错：edge-tts 返回的字母形态、变音符号或 Tatweel
    # 可能和脚本不同。只在常规匹配失败后归一化比较，非阿拉伯语文本不会受影响。
    current_ar = re.sub(r"[_\W]+", "", _normalize_arabic(current_text))
    target_ar = re.sub(r"[_\W]+", "", _normalize_arabic(target_line))
    if current_ar and current_ar == target_ar:
        return target_line.strip()

    return ""


def _write_subtitle_items(sub_items: list[str], subtitle_file: str) -> bool:
    """
    将已经聚合好的字幕段写入到 SRT 文件，并做一次基本可读性验证。

    返回值：
    - `True`：字幕文件成功落盘且可被 moviepy 解析；
    - `False`：字幕文件写入或解析失败。
    """
    try:
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write("\n".join(sub_items) + "\n")

        sbs = subtitles.file_to_subtitles(subtitle_file, encoding="utf-8")
        duration = max([tb for ((ta, tb), txt) in sbs]) if sbs else 0
        logger.info(
            f"completed, subtitle file created: {subtitle_file}, duration: {duration}"
        )
        return True
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False


def _build_subtitle_items_from_edge_cues(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    """
    将 edge_tts 7.x 的细粒度 `cues` 聚合为按脚本断句的 SRT 片段。

    背景：
    edge_tts 7.x 的 `SubMaker.get_srt()` 更偏向逐词/逐短语的时间轴。
    对英文做逐词高亮尚可，但中文短视频字幕如果直接照搬，会出现
    “金钱 / 是 / 一种 / 社会 / 工具” 这种阅读体验很差的效果。

    实现策略：
    1. 逐个消费 cues 中的 `content`；
    2. 累积成一段候选文本；
    3. 当候选文本与脚本里当前目标断句匹配时，收敛为一个完整字幕段；
    4. 使用第一条 cue 的开始时间和最后一条 cue 的结束时间，保证时间轴连续。
    """
    formatter = _build_subtitle_formatter()
    sub_items = []
    sub_index = 0
    current_text = ""
    current_start_time = None

    for cue in sub_maker.cues:
        cue_text = unescape(cue.content)
        if current_start_time is None:
            current_start_time = int(cue.start.total_seconds() * 10000000)

        current_end_time = int(cue.end.total_seconds() * 10000000)
        current_text += cue_text

        matched_text = _match_script_line(script_lines, current_text, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=current_start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        current_text = ""
        current_start_time = None

    if current_text.strip():
        logger.warning(
            f"edge cues still have unmatched text after aggregation: {current_text}"
        )

    return sub_items


def _build_subtitle_items_from_legacy_submaker(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    """
    将项目原有 `subs/offset` 结构聚合为按脚本断句的 SRT 片段。

    这部分保留了原来的核心思路，只是拆成独立函数，便于与 edge_tts 7.x
    的 cues 聚合逻辑共享同一套断句匹配与落盘流程。
    """
    formatter = _build_subtitle_formatter()
    start_time = -1.0
    sub_items = []
    sub_index = 0
    sub_line = ""

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for _, (offset, sub) in enumerate(zip(legacy_offsets, legacy_subs)):
        current_start_time, current_end_time = offset
        if start_time < 0:
            start_time = current_start_time

        sub_line += unescape(sub)
        matched_text = _match_script_line(script_lines, sub_line, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        start_time = -1.0
        sub_line = ""

    if sub_line.strip():
        logger.warning(
            f"legacy subtitle items still have unmatched text after aggregation: {sub_line}"
        )

    return sub_items


_TURKISH_ASCII_EQUIVALENTS = str.maketrans(
    {
        "Ç": "C",
        "Ğ": "G",
        "İ": "I",
        "Ö": "O",
        "Ş": "S",
        "Ü": "U",
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
    }
)


def _normalize_karaoke_alignment_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).translate(_TURKISH_ASCII_EQUIVALENTS)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[_\W]+", "", text).casefold()


def _karaoke_cue_entries(
    sub_maker: SubMaker,
    text: str = "",
    subtitle_items=None,
) -> list[tuple[object, str]]:
    cue_entries = []
    for cue in getattr(sub_maker, "cues", []):
        cue_text = unescape(getattr(cue, "content", "")).strip()
        if not cue_text:
            continue
        if _cue_seconds(cue.end) <= _cue_seconds(cue.start):
            continue
        cue_entries.append((cue, cue_text))

    if (
        isinstance(subtitle_items, (list, tuple))
        and len(subtitle_items) == len(cue_entries)
    ):
        corrected_entries = []
        for subtitle_item, (cue, _) in zip(subtitle_items, cue_entries):
            if not isinstance(subtitle_item, (list, tuple)) or len(subtitle_item) < 3:
                corrected_entries = []
                break
            time_range = _subtitle_item_time_range(subtitle_item[1])
            subtitle_text = str(subtitle_item[2] or "").strip()
            if (
                time_range is None
                or not subtitle_text
                or abs(time_range[0] - _cue_seconds(cue.start)) > 0.05
                or abs(time_range[1] - _cue_seconds(cue.end)) > 0.05
            ):
                corrected_entries = []
                break
            corrected_entries.append((cue, subtitle_text))
        if corrected_entries:
            return corrected_entries

    script_tokens = _format_text(text or "").split()
    if (
        script_tokens
        and len(script_tokens) == len(cue_entries)
        and _normalize_karaoke_alignment_text(" ".join(script_tokens))
        == _normalize_karaoke_alignment_text(
            " ".join(cue_text for _, cue_text in cue_entries)
        )
    ):
        return [
            (cue, script_token)
            for (cue, _), script_token in zip(cue_entries, script_tokens)
        ]

    return cue_entries


def _build_karaoke_subtitle_items_from_edge_cues(
    sub_maker: SubMaker,
    text: str = "",
) -> list[str]:
    formatter = _build_subtitle_formatter()
    sub_items = []

    for cue, cue_text in _karaoke_cue_entries(sub_maker, text):
        start_time = int(_cue_seconds(cue.start) * 10000000)
        end_time = int(_cue_seconds(cue.end) * 10000000)

        sub_items.append(
            formatter(
                idx=len(sub_items) + 1,
                start_time=start_time,
                end_time=end_time,
                sub_text=cue_text,
            )
        )

    return sub_items


def _build_karaoke_subtitle_items_from_legacy_submaker(
    sub_maker: SubMaker,
) -> list[str]:
    formatter = _build_subtitle_formatter()
    sub_items = []

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for offset, sub_text in zip(legacy_offsets, legacy_subs):
        start_time, end_time = offset
        clean_text = unescape(sub_text).strip()
        if not clean_text or end_time <= start_time:
            continue

        sub_items.append(
            formatter(
                idx=len(sub_items) + 1,
                start_time=start_time,
                end_time=end_time,
                sub_text=clean_text,
            )
        )

    return sub_items


def _ass_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0

    total_centiseconds = int(round(seconds * 100))
    hour = total_centiseconds // 360000
    total_centiseconds %= 360000
    minute = total_centiseconds // 6000
    total_centiseconds %= 6000
    second = total_centiseconds // 100
    centisecond = total_centiseconds % 100
    return f"{hour}:{minute:02d}:{second:02d}.{centisecond:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\N")
    )


def _cue_seconds(value) -> float:
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value) / 10**7


def _ass_play_resolution(video_aspect=None) -> tuple[int, int]:
    try:
        return VideoAspect(video_aspect or VideoAspect.portrait).to_resolution()
    except (TypeError, ValueError):
        return VideoAspect.portrait.to_resolution()


def _ass_subtitle_margin_v(video_aspect=None) -> int:
    try:
        aspect = VideoAspect(video_aspect or VideoAspect.portrait)
    except (TypeError, ValueError):
        return _DEFAULT_ASS_MARGIN_V
    if aspect != VideoAspect.portrait:
        return _DEFAULT_ASS_MARGIN_V
    _, height = aspect.to_resolution()
    return int(round(height * _PORTRAIT_ASS_SAFE_MARGIN_RATIO))


def _build_ass_header(video_aspect=None) -> str:
    play_res_x, play_res_y = _ass_play_resolution(video_aspect)
    margin_v = _ass_subtitle_margin_v(video_aspect)
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {play_res_x}",
            f"PlayResY: {play_res_y}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding"
            ),
            (
                "Style: Karaoke,Arial,56,&H00FFFFFF,&H0000D7FF,&H8A000000,"
                f"&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,80,80,{margin_v},1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )


def create_karaoke_ass_variant(
    source_subtitle_file: str,
    subtitle_file: str,
    video_aspect=None,
) -> bool:
    """Copy karaoke events into an ASS file with aspect-appropriate styling."""
    try:
        with open(source_subtitle_file, "r", encoding="utf-8") as file:
            source_text = file.read()

        _, marker, event_section = source_text.partition("[Events]")
        if not marker:
            logger.warning("ASS karaoke variant requested, but events were unavailable")
            return False

        event_lines = event_section.splitlines()
        format_index = next(
            (
                index
                for index, line in enumerate(event_lines)
                if line.startswith("Format:")
            ),
            None,
        )
        if format_index is None:
            logger.warning("ASS karaoke variant requested, but dialogue events were unavailable")
            return False

        event_body = "\n".join(event_lines[format_index + 1 :]).strip()
        if not event_body:
            logger.warning("ASS karaoke variant requested, but dialogue events were unavailable")
            return False
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write(f"{_build_ass_header(video_aspect)}\n{event_body}\n")
        logger.info(f"completed, ASS karaoke subtitle variant created: {subtitle_file}")
        return True
    except Exception as e:
        logger.error(f"failed to create ASS karaoke subtitle variant, error: {str(e)}")
        return False


def _srt_timestamp_seconds(timestamp: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2}),(\d{3})\s*", timestamp)
    if not match:
        return None
    hour, minute, second, millisecond = (int(part) for part in match.groups())
    return hour * 3600 + minute * 60 + second + millisecond / 1000


def _subtitle_item_time_range(time_range: str) -> tuple[float, float] | None:
    if not isinstance(time_range, str) or " --> " not in time_range:
        return None
    start_text, end_text = time_range.split(" --> ", 1)
    start_seconds = _srt_timestamp_seconds(start_text)
    end_seconds = _srt_timestamp_seconds(end_text)
    if start_seconds is None or end_seconds is None or end_seconds <= start_seconds:
        return None
    return start_seconds, end_seconds


def inspect_subtitle_readability(
    subtitle_items,
    *,
    max_characters_per_second: float = _DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_SECOND,
    max_lines: int = _DEFAULT_SUBTITLE_MAX_LINES,
    max_characters_per_line: int = _DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_LINE,
) -> dict:
    """Report subtitle readability risks without changing text or timing."""
    report = {
        "ok": True,
        "checked_count": 0,
        "skipped_count": 0,
        "high_reading_speed_count": 0,
        "too_many_lines_count": 0,
        "overlong_line_count": 0,
        "limits": {
            "max_characters_per_second": max_characters_per_second,
            "max_lines": max_lines,
            "max_characters_per_line": max_characters_per_line,
        },
        "items": [],
    }

    for subtitle_item in subtitle_items or []:
        if not isinstance(subtitle_item, (tuple, list)) or len(subtitle_item) < 3:
            report["skipped_count"] += 1
            continue

        time_range = _subtitle_item_time_range(subtitle_item[1])
        if time_range is None:
            report["skipped_count"] += 1
            continue

        start_seconds, end_seconds = time_range
        lines = [line.strip() for line in str(subtitle_item[2] or "").splitlines()]
        lines = [line for line in lines if line]
        text = " ".join(lines)
        characters_per_second = len(text) / (end_seconds - start_seconds)
        overlong_lines = [
            line for line in lines if len(line) > max_characters_per_line
        ]
        issues = []
        if characters_per_second > max_characters_per_second:
            issues.append("reading_speed")
            report["high_reading_speed_count"] += 1
        if len(lines) > max_lines:
            issues.append("line_count")
            report["too_many_lines_count"] += 1
        if overlong_lines:
            issues.append("line_length")
            report["overlong_line_count"] += len(overlong_lines)

        report["checked_count"] += 1
        report["items"].append(
            {
                "subtitle_id": subtitle_item[0],
                "characters_per_second": round(characters_per_second, 2),
                "line_count": len(lines),
                "longest_line_length": max((len(line) for line in lines), default=0),
                "issues": issues,
            }
        )

    report["ok"] = not (
        report["high_reading_speed_count"]
        or report["too_many_lines_count"]
        or report["overlong_line_count"]
    )
    return report


def _subtitle_display_lines(text: str, max_characters_per_line: int) -> list[str]:
    words = re.findall(r"\S+", text)
    if not words:
        return []

    lines = []
    current_line = ""
    for word in words:
        candidate = word if not current_line else f"{current_line} {word}"
        if current_line and len(candidate) > max_characters_per_line:
            lines.append(current_line)
            current_line = word
        else:
            current_line = candidate

    if current_line:
        lines.append(current_line)
    return lines


def _format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remaining = divmod(total_milliseconds, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    whole_seconds, milliseconds = divmod(remaining, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _reflowed_time_ranges(
    start_seconds: float,
    end_seconds: float,
    caption_chunks: list[str],
    word_timings=None,
) -> list[str] | None:
    total_milliseconds = int(round((end_seconds - start_seconds) * 1000))
    if len(caption_chunks) < 2 or total_milliseconds < len(caption_chunks):
        return None

    word_timing_ranges = _word_timing_reflow_time_ranges(
        start_seconds,
        end_seconds,
        caption_chunks,
        word_timings,
    )
    if word_timing_ranges is not None:
        return word_timing_ranges

    weights = [max(1, len(" ".join(chunk.split()))) for chunk in caption_chunks]
    total_weight = sum(weights)
    elapsed_milliseconds = 0
    time_ranges = []

    for index, weight in enumerate(weights):
        remaining_chunks = len(weights) - index - 1
        if remaining_chunks:
            ideal_end = round(total_milliseconds * sum(weights[: index + 1]) / total_weight)
            chunk_end = min(
                total_milliseconds - remaining_chunks,
                max(elapsed_milliseconds + 1, ideal_end),
            )
        else:
            chunk_end = total_milliseconds

        chunk_start_seconds = start_seconds + elapsed_milliseconds / 1000
        chunk_end_seconds = start_seconds + chunk_end / 1000
        time_ranges.append(
            f"{_format_srt_timestamp(chunk_start_seconds)} --> "
            f"{_format_srt_timestamp(chunk_end_seconds)}"
        )
        elapsed_milliseconds = chunk_end

    return time_ranges


def reflow_subtitle_items(
    subtitle_items,
    *,
    max_lines: int = _DEFAULT_SUBTITLE_MAX_LINES,
    max_characters_per_line: int = _DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_LINE,
    word_timings=None,
) -> list:
    """Split crowded subtitle cues into compact, sequential display events.

    The original words and punctuation stay intact. Only whitespace and cue
    boundaries change, and split cues keep the source interval without gaps or
    overlaps. Reading speed is deliberately not changed because doing so would
    desynchronise the narration.
    """
    source_items = list(subtitle_items or [])
    if max_lines < 1 or max_characters_per_line < 1:
        return source_items

    reflowed_items = []
    for subtitle_item in source_items:
        if not isinstance(subtitle_item, (tuple, list)) or len(subtitle_item) < 3:
            return source_items

        time_range = _subtitle_item_time_range(subtitle_item[1])
        subtitle_text = subtitle_item[2]
        if time_range is None or not isinstance(subtitle_text, str):
            return source_items

        source_lines = [line.strip() for line in subtitle_text.splitlines() if line.strip()]
        if (
            not source_lines
            or (
                len(source_lines) <= max_lines
                and all(len(line) <= max_characters_per_line for line in source_lines)
            )
        ):
            reflowed_items.append(tuple(subtitle_item[:3]))
            continue

        display_lines = _subtitle_display_lines(
            subtitle_text,
            max_characters_per_line,
        )
        caption_chunks = [
            "\n".join(display_lines[index : index + max_lines])
            for index in range(0, len(display_lines), max_lines)
        ]
        if not caption_chunks:
            reflowed_items.append(tuple(subtitle_item[:3]))
            continue

        if len(caption_chunks) == 1:
            reflowed_items.append(
                (subtitle_item[0], subtitle_item[1], caption_chunks[0])
            )
            continue

        time_ranges = _reflowed_time_ranges(
            *time_range,
            caption_chunks,
            word_timings=word_timings,
        )
        if time_ranges is None:
            reflowed_items.append(tuple(subtitle_item[:3]))
            continue
        reflowed_items.extend(
            (subtitle_item[0], chunk_time_range, caption_text)
            for chunk_time_range, caption_text in zip(time_ranges, caption_chunks)
        )

    return [
        (index, item[1], item[2])
        for index, item in enumerate(reflowed_items, start=1)
        if isinstance(item, (tuple, list)) and len(item) >= 3
    ]


def _word_timing_entries_for_range(word_timings, start_seconds, end_seconds):
    entries = []
    for word_timing in word_timings:
        if not isinstance(word_timing, dict):
            continue
        try:
            text = str(word_timing.get("text") or "").strip()
            word_start = float(word_timing.get("start_time"))
            word_end = float(word_timing.get("end_time"))
        except (TypeError, ValueError):
            continue
        if (
            text
            and word_end > word_start
            and word_end > start_seconds
            and word_start < end_seconds
        ):
            entries.append((word_start, word_end))
    return sorted(entries)


def _word_timing_reflow_time_ranges(
    start_seconds: float,
    end_seconds: float,
    caption_chunks: list[str],
    word_timings,
) -> list[str] | None:
    if not word_timings:
        return None

    token_counts = [len(re.findall(r"\S+", chunk)) for chunk in caption_chunks]
    if not token_counts or any(count == 0 for count in token_counts):
        return None

    entries = _word_timing_entries_for_range(
        word_timings,
        start_seconds,
        end_seconds,
    )
    if len(entries) != sum(token_counts):
        return None

    boundaries = [start_seconds]
    token_index = 0
    for token_count in token_counts[:-1]:
        token_index += token_count
        next_start_seconds = entries[token_index][0]
        if not boundaries[-1] < next_start_seconds < end_seconds:
            return None
        boundaries.append(next_start_seconds)
    boundaries.append(end_seconds)

    formatted_boundaries = [_format_srt_timestamp(boundary) for boundary in boundaries]
    if any(
        formatted_boundaries[index] == formatted_boundaries[index + 1]
        for index in range(len(formatted_boundaries) - 1)
    ):
        return None

    return [
        f"{start_timestamp} --> {end_timestamp}"
        for start_timestamp, end_timestamp in zip(
            formatted_boundaries,
            formatted_boundaries[1:],
        )
    ]


def _karaoke_timing_for_subtitle_line(
    tokens: list[str],
    word_timings,
    start_seconds: float,
    end_seconds: float,
) -> tuple[float, float, list[int]] | None:
    entries = _word_timing_entries_for_range(
        word_timings, start_seconds, end_seconds
    )
    if not entries:
        return None

    if len(entries) == len(tokens):
        dialogue_start = max(start_seconds, entries[0][0])
        dialogue_end = min(end_seconds, entries[-1][1])
        if dialogue_end <= dialogue_start:
            return None
        durations = []
        for index, (word_start, word_end) in enumerate(entries):
            next_start = entries[index + 1][0] if index + 1 < len(entries) else word_end
            word_start = max(dialogue_start, word_start)
            word_end = min(dialogue_end, max(word_end, next_start))
            durations.append(max(1, int(round((word_end - word_start) * 100))))
        return dialogue_start, dialogue_end, durations

    total_centiseconds = max(len(tokens), int(round((end_seconds - start_seconds) * 100)))
    base_duration, extra = divmod(total_centiseconds, len(tokens))
    durations = [base_duration + (1 if index < extra else 0) for index in range(len(tokens))]
    return start_seconds, end_seconds, durations


def _build_karaoke_ass_from_word_timings(
    subtitle_items,
    word_timings,
    video_aspect=None,
) -> str:
    events = []
    for subtitle_item in subtitle_items:
        if not isinstance(subtitle_item, (tuple, list)) or len(subtitle_item) < 3:
            continue
        time_range = _subtitle_item_time_range(subtitle_item[1])
        tokens = re.findall(r"\S+", str(subtitle_item[2] or ""))
        if time_range is None or not tokens:
            continue
        karaoke_timing = _karaoke_timing_for_subtitle_line(
            tokens,
            word_timings,
            *time_range,
        )
        if karaoke_timing is None:
            return ""

        dialogue_start, dialogue_end, durations = karaoke_timing
        text_parts = []
        previous_text = ""
        for duration, token in zip(durations, tokens):
            token_text = _escape_ass_text(token)
            spacing = ""
            if (
                text_parts
                and token_text[:1] not in ",.!?;:)]"
                and previous_text[-1:] not in " (["
            ):
                spacing = " "
            text_parts.append(f"{spacing}{{\\kf{duration}}}{token_text}")
            previous_text = token_text

        events.append(
            (
                f"Dialogue: 0,{_ass_timestamp(dialogue_start)},{_ass_timestamp(dialogue_end)},"
                f"Karaoke,,0,0,0,,{''.join(text_parts)}"
            )
        )

    if not events:
        return ""
    return f"{_build_ass_header(video_aspect)}\n" + "\n".join(events) + "\n"


def create_karaoke_ass_from_word_timings(
    subtitle_items,
    word_timings,
    subtitle_file: str,
    video_aspect=None,
) -> bool:
    try:
        subtitle_text = _build_karaoke_ass_from_word_timings(
            subtitle_items,
            word_timings,
            video_aspect=video_aspect,
        )
        if not subtitle_text:
            logger.warning(
                "ASS karaoke subtitle requested, but Whisper word timings were unavailable"
            )
            return False
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write(subtitle_text)
        logger.info(
            f"completed, Whisper ASS karaoke subtitle file created: {subtitle_file}"
        )
        return True
    except Exception as e:
        logger.error(f"failed to create Whisper ASS karaoke subtitle, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False


def _build_karaoke_ass_from_edge_cues(
    sub_maker: SubMaker,
    video_aspect=None,
    text: str = "",
    subtitle_items=None,
) -> str:
    events = []
    current_tokens = []
    current_start = None
    current_end = None

    def exceeds_caption_capacity(tokens) -> bool:
        caption_parts = []
        previous_text = ""
        for _, token_text in tokens:
            spacing = ""
            if (
                caption_parts
                and token_text[:1] not in ",.!?;:)]"
                and previous_text[-1:] not in " (["
            ):
                spacing = " "
            caption_parts.append(f"{spacing}{token_text}")
            previous_text = token_text

        return len(
            _subtitle_display_lines(
                "".join(caption_parts),
                _DEFAULT_SUBTITLE_MAX_CHARACTERS_PER_LINE,
            )
        ) > _DEFAULT_SUBTITLE_MAX_LINES

    def flush_current_line():
        nonlocal current_tokens, current_start, current_end
        if not current_tokens or current_start is None or current_end is None:
            return

        text_parts = []
        previous_text = ""
        for duration_cs, token_text in current_tokens:
            spacing = ""
            if (
                text_parts
                and token_text[:1] not in ",.!?;:)]"
                and previous_text[-1:] not in " (["
            ):
                spacing = " "
            text_parts.append(f"{spacing}{{\\kf{duration_cs}}}{token_text}")
            previous_text = token_text

        events.append(
            (
                f"Dialogue: 0,{_ass_timestamp(current_start)},{_ass_timestamp(current_end)},"
                f"Karaoke,,0,0,0,,{''.join(text_parts)}"
            )
        )
        current_tokens = []
        current_start = None
        current_end = None

    for cue, source_text in _karaoke_cue_entries(
        sub_maker,
        text,
        subtitle_items=subtitle_items,
    ):
        cue_text = _escape_ass_text(source_text)
        start_seconds = _cue_seconds(cue.start)
        end_seconds = _cue_seconds(cue.end)
        duration_cs = max(1, int(round((end_seconds - start_seconds) * 100)))
        next_token = (duration_cs, cue_text)

        if current_tokens and exceeds_caption_capacity(current_tokens + [next_token]):
            flush_current_line()

        if current_start is None:
            current_start = start_seconds
        current_end = end_seconds
        current_tokens.append(next_token)

        if cue_text[-1:] in ".!?;:" or len(current_tokens) >= 8:
            flush_current_line()

    flush_current_line()
    if not events:
        return ""

    return f"{_build_ass_header(video_aspect)}\n" + "\n".join(events) + "\n"


def create_karaoke_ass_subtitle(
    sub_maker: SubMaker,
    subtitle_file: str,
    video_aspect=None,
    text: str = "",
    subtitle_items=None,
) -> bool:
    try:
        if not getattr(sub_maker, "cues", []):
            logger.warning("ASS karaoke subtitle requested, but no edge cues found")
            return False

        subtitle_text = _build_karaoke_ass_from_edge_cues(
            sub_maker,
            video_aspect=video_aspect,
            text=text,
            subtitle_items=subtitle_items,
        )
        if not subtitle_text:
            logger.warning("ASS karaoke subtitle requested, but no valid cues found")
            return False

        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write(subtitle_text)
        logger.info(f"completed, ASS karaoke subtitle file created: {subtitle_file}")
        return True
    except Exception as e:
        logger.error(f"failed to create ASS karaoke subtitle, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False


def create_karaoke_subtitle(sub_maker: SubMaker, text: str, subtitle_file: str) -> bool:
    """
    Create a word/phrase-timed subtitle file for karaoke-style highlighting.
    Falls back cleanly by returning False when the TTS provider has no timing cues.
    """
    try:
        if hasattr(sub_maker, "cues") and sub_maker.cues:
            sub_items = _build_karaoke_subtitle_items_from_edge_cues(sub_maker, text)
        else:
            sub_items = _build_karaoke_subtitle_items_from_legacy_submaker(sub_maker)

        if not sub_items:
            logger.warning("karaoke subtitle requested, but no subtitle cues found")
            return False

        return _write_subtitle_items(sub_items, subtitle_file)
    except Exception as e:
        logger.error(f"failed to create karaoke subtitle, error: {str(e)}")
        return False


def create_subtitle(sub_maker: SubMaker, text: str, subtitle_file: str):
    """
    优化字幕文件
    1. 将字幕文件按照标点符号分割成多行
    2. 逐行匹配字幕文件中的文本
    3. 生成新的字幕文件
    """
    text = _format_text(text)
    script_lines = utils.split_string_by_punctuations(text)
    try:
        if hasattr(sub_maker, "cues") and sub_maker.cues:
            sub_items = _build_subtitle_items_from_edge_cues(sub_maker, script_lines)
        else:
            sub_items = _build_subtitle_items_from_legacy_submaker(
                sub_maker, script_lines
            )

        if len(sub_items) != len(script_lines):
            logger.warning(
                f"failed, sub_items len: {len(sub_items)}, script_lines len: {len(script_lines)}"
            )
            return

        _write_subtitle_items(sub_items, subtitle_file)
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")


def _get_audio_duration_from_submaker(sub_maker: SubMaker):
    """
    获取音频时长
    """
    # 优先兼容 edge_tts 7.x 的 cues 结构；
    # 如果是项目里其他 TTS 手工填充的旧结构，则继续读取 offset。
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        return sub_maker.cues[-1].end.total_seconds()

    legacy_offsets = getattr(sub_maker, "offset", [])
    if not legacy_offsets:
        return 0.0
    return legacy_offsets[-1][1] / 10000000

def _get_audio_duration_from_audio_file(audio_file: str) -> float:
    """
    获取MP3音频时长
    """
    if not os.path.exists(audio_file):
        logger.error(f"audio file does not exist: {audio_file}")
        return 0.0

    try:
        # Use MoviePy so normalized WAV and existing MP3 narration are both supported.
        with AudioFileClip(audio_file) as audio:
            return audio.duration  # Duration in seconds
    except Exception as e:
        logger.error(f"Failed to get audio duration: {str(e)}")
        return 0.0

def get_audio_duration(target: Union[str, SubMaker]) -> float:
    """
    获取音频时长
    如果是SubMaker对象，则从SubMaker中获取时长
    如果是MP3文件，则从MP3文件中获取时长
    """
    if isinstance(target, SubMaker):
        return _get_audio_duration_from_submaker(target)
    elif isinstance(target, str):
        return _get_audio_duration_from_audio_file(target)
    else:
        logger.error(f"Invalid target type: {type(target)}")
        return 0.0

if __name__ == "__main__":
    voice_name = "zh-CN-XiaoxiaoMultilingualNeural-V2-Female"
    voice_name = parse_voice_name(voice_name)
    voice_name = is_azure_v2_voice(voice_name)
    print(voice_name)

    voices = get_all_azure_voices()
    print(len(voices))

    async def _do():
        temp_dir = utils.storage_dir("temp")

        voice_names = [
            "zh-CN-XiaoxiaoMultilingualNeural",
            # 女性
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-XiaoyiNeural",
            # 男性
            "zh-CN-YunyangNeural",
            "zh-CN-YunxiNeural",
        ]
        text = """
        静夜思是唐代诗人李白创作的一首五言古诗。这首诗描绘了诗人在寂静的夜晚，看到窗前的明月，不禁想起远方的家乡和亲人，表达了他对家乡和亲人的深深思念之情。全诗内容是：“床前明月光，疑是地上霜。举头望明月，低头思故乡。”在这短短的四句诗中，诗人通过“明月”和“思故乡”的意象，巧妙地表达了离乡背井人的孤独与哀愁。首句“床前明月光”设景立意，通过明亮的月光引出诗人的遐想；“疑是地上霜”增添了夜晚的寒冷感，加深了诗人的孤寂之情；“举头望明月”和“低头思故乡”则是情感的升华，展现了诗人内心深处的乡愁和对家的渴望。这首诗简洁明快，情感真挚，是中国古典诗歌中非常著名的一首，也深受后人喜爱和推崇。
            """

        text = """
        What is the meaning of life? This question has puzzled philosophers, scientists, and thinkers of all kinds for centuries. Throughout history, various cultures and individuals have come up with their interpretations and beliefs around the purpose of life. Some say it's to seek happiness and self-fulfillment, while others believe it's about contributing to the welfare of others and making a positive impact in the world. Despite the myriad of perspectives, one thing remains clear: the meaning of life is a deeply personal concept that varies from one person to another. It's an existential inquiry that encourages us to reflect on our values, desires, and the essence of our existence.
        """

        text = """
               预计未来3天深圳冷空气活动频繁，未来两天持续阴天有小雨，出门带好雨具；
               10-11日持续阴天有小雨，日温差小，气温在13-17℃之间，体感阴凉；
               12日天气短暂好转，早晚清凉；
                   """

        text = "[Opening scene: A sunny day in a suburban neighborhood. A young boy named Alex, around 8 years old, is playing in his front yard with his loyal dog, Buddy.]\n\n[Camera zooms in on Alex as he throws a ball for Buddy to fetch. Buddy excitedly runs after it and brings it back to Alex.]\n\nAlex: Good boy, Buddy! You're the best dog ever!\n\n[Buddy barks happily and wags his tail.]\n\n[As Alex and Buddy continue playing, a series of potential dangers loom nearby, such as a stray dog approaching, a ball rolling towards the street, and a suspicious-looking stranger walking by.]\n\nAlex: Uh oh, Buddy, look out!\n\n[Buddy senses the danger and immediately springs into action. He barks loudly at the stray dog, scaring it away. Then, he rushes to retrieve the ball before it reaches the street and gently nudges it back towards Alex. Finally, he stands protectively between Alex and the stranger, growling softly to warn them away.]\n\nAlex: Wow, Buddy, you're like my superhero!\n\n[Just as Alex and Buddy are about to head inside, they hear a loud crash from a nearby construction site. They rush over to investigate and find a pile of rubble blocking the path of a kitten trapped underneath.]\n\nAlex: Oh no, Buddy, we have to help!\n\n[Buddy barks in agreement and together they work to carefully move the rubble aside, allowing the kitten to escape unharmed. The kitten gratefully nuzzles against Buddy, who responds with a friendly lick.]\n\nAlex: We did it, Buddy! We saved the day again!\n\n[As Alex and Buddy walk home together, the sun begins to set, casting a warm glow over the neighborhood.]\n\nAlex: Thanks for always being there to watch over me, Buddy. You're not just my dog, you're my best friend.\n\n[Buddy barks happily and nuzzles against Alex as they disappear into the sunset, ready to face whatever adventures tomorrow may bring.]\n\n[End scene.]"

        text = "大家好，我是乔哥，一个想帮你把信用卡全部还清的家伙！\n今天我们要聊的是信用卡的取现功能。\n你是不是也曾经因为一时的资金紧张，而拿着信用卡到ATM机取现？如果是，那你得好好看看这个视频了。\n现在都2024年了，我以为现在不会再有人用信用卡取现功能了。前几天一个粉丝发来一张图片，取现1万。\n信用卡取现有三个弊端。\n一，信用卡取现功能代价可不小。会先收取一个取现手续费，比如这个粉丝，取现1万，按2.5%收取手续费，收取了250元。\n二，信用卡正常消费有最长56天的免息期，但取现不享受免息期。从取现那一天开始，每天按照万5收取利息，这个粉丝用了11天，收取了55元利息。\n三，频繁的取现行为，银行会认为你资金紧张，会被标记为高风险用户，影响你的综合评分和额度。\n那么，如果你资金紧张了，该怎么办呢？\n乔哥给你支一招，用破思机摩擦信用卡，只需要少量的手续费，而且还可以享受最长56天的免息期。\n最后，如果你对玩卡感兴趣，可以找乔哥领取一本《卡神秘籍》，用卡过程中遇到任何疑惑，也欢迎找乔哥交流。\n别忘了，关注乔哥，回复用卡技巧，免费领取《2024用卡技巧》，让我们一起成为用卡高手！"

        text = """
        2023全年业绩速览
公司全年累计实现营业收入1476.94亿元，同比增长19.01%，归母净利润747.34亿元，同比增长19.16%。EPS达到59.49元。第四季度单季，营业收入444.25亿元，同比增长20.26%，环比增长31.86%；归母净利润218.58亿元，同比增长19.33%，环比增长29.37%。这一阶段
的业绩表现不仅突显了公司的增长动力和盈利能力，也反映出公司在竞争激烈的市场环境中保持了良好的发展势头。
2023年Q4业绩速览
第四季度，营业收入贡献主要增长点；销售费用高增致盈利能力承压；税金同比上升27%，扰动净利率表现。
业绩解读
利润方面，2023全年贵州茅台，>归母净利润增速为19%，其中营业收入正贡献18%，营业成本正贡献百分之一，管理费用正贡献百分之一点四。(注：归母净利润增速值=营业收入增速+各科目贡献，展示贡献/拖累的前四名科目，且要求贡献值/净利润增速>15%)
"""
        text = "静夜思是唐代诗人李白创作的一首五言古诗。这首诗描绘了诗人在寂静的夜晚，看到窗前的明月，不禁想起远方的家乡和亲人"

        text = _format_text(text)
        lines = utils.split_string_by_punctuations(text)
        print(lines)

        for voice_name in voice_names:
            voice_file = f"{temp_dir}/tts-{voice_name}.mp3"
            subtitle_file = f"{temp_dir}/tts.mp3.srt"
            sub_maker = azure_tts_v2(
                text=text, voice_name=voice_name, voice_file=voice_file
            )
            create_subtitle(sub_maker=sub_maker, text=text, subtitle_file=subtitle_file)
            audio_duration = get_audio_duration(sub_maker)
            print(f"voice: {voice_name}, audio duration: {audio_duration}s")

    loop = asyncio.get_event_loop_policy().get_event_loop()
    try:
        loop.run_until_complete(_do())
    finally:
        loop.close()
