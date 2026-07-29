import json
import os.path
import re
import shutil
import unicodedata
from timeit import default_timer as timer

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from loguru import logger

from app.config import config
from app.utils import utils

model_size = config.whisper.get("model_size", "large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
_DEFAULT_MEMORY_FALLBACK_MODEL = "large-v3-turbo"
fallback_model_size = config.whisper.get(
    "fallback_model_size",
    _DEFAULT_MEMORY_FALLBACK_MODEL if model_size == "large-v3" else "",
)
model = None

_MEMORY_LOAD_ERROR_MARKERS = (
    "mkl_malloc",
    "failed to allocate memory",
    "out of memory",
    "not enough memory",
)


def _is_memory_load_error(error: Exception) -> bool:
    error_text = str(error).casefold()
    return any(marker in error_text for marker in _MEMORY_LOAD_ERROR_MARKERS)


def _model_path_for(model_name: str) -> str:
    model_path = f"{utils.root_dir()}/models/whisper-{model_name}"
    model_bin_file = f"{model_path}/model.bin"
    if os.path.isdir(model_path) and os.path.isfile(model_bin_file):
        return model_path
    return model_name


def _model_load_candidates() -> list[str]:
    primary_model = str(model_size or "").strip()
    fallback_model = str(fallback_model_size or "").strip()
    candidates = [primary_model] if primary_model else []
    if fallback_model and fallback_model not in candidates:
        candidates.append(fallback_model)
    return candidates


def _model_load_error_guidance(error: Exception) -> str:
    if _is_memory_load_error(error):
        return (
            "The Whisper model could not be loaded because available memory is insufficient.\n"
            "Close memory-heavy applications, restart the WebUI, or choose a smaller "
            "[whisper].model_size in config.toml."
        )
    return (
        "This may be caused by a network or model-download issue.\n"
        "Please download the model manually and put it in the 'models' folder.\n"
        "See the README FAQ for details."
    )


def _whisper_language_hint(language: str | None) -> str | None:
    normalized = unicodedata.normalize(
        "NFKD", str(language or "").strip().casefold()
    )
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).replace("_", "-")
    language_code = normalized.split("-", 1)[0]
    if language_code in {"tr", "turkce", "turkish"}:
        return "tr"
    if language_code in {"en", "english", "ingilizce"}:
        return "en"
    return None


def _write_word_timings(word_timings: list[dict], word_timing_file: str) -> None:
    parent_dir = os.path.dirname(word_timing_file)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(word_timing_file, "w", encoding="utf-8") as file:
        json.dump(word_timings, file, ensure_ascii=False)


def read_word_timings(word_timing_file: str) -> list[dict]:
    if not word_timing_file or not os.path.isfile(word_timing_file):
        return []
    try:
        with open(word_timing_file, "r", encoding="utf-8") as file:
            raw_word_timings = json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.warning("Whisper word timing file could not be read")
        return []

    if not isinstance(raw_word_timings, list):
        return []

    word_timings = []
    for raw_word_timing in raw_word_timings:
        if not isinstance(raw_word_timing, dict):
            continue
        try:
            text = str(raw_word_timing.get("text") or "").strip()
            start_time = float(raw_word_timing.get("start_time"))
            end_time = float(raw_word_timing.get("end_time"))
        except (TypeError, ValueError):
            continue
        if text and start_time >= 0 and end_time > start_time:
            word_timings.append(
                {
                    "text": text,
                    "start_time": start_time,
                    "end_time": end_time,
                }
            )
    return word_timings


def create(
    audio_file,
    subtitle_file: str = "",
    language: str | None = None,
    word_timing_file: str = "",
):
    global model
    if WhisperModel is None:
        logger.warning("faster_whisper not available, skipping whisper subtitle generation")
        return ""
    if not model:
        candidates = _model_load_candidates()
        for candidate_index, candidate in enumerate(candidates):
            model_path = _model_path_for(candidate)
            logger.info(
                f"loading model: {model_path}, device: {device}, compute_type: {compute_type}"
            )
            try:
                model = WhisperModel(
                    model_size_or_path=model_path,
                    device=device,
                    compute_type=compute_type,
                )
                break
            except Exception as error:
                has_fallback = candidate_index + 1 < len(candidates)
                if _is_memory_load_error(error) and has_fallback:
                    logger.warning(
                        "Whisper model '{}' could not be loaded due to memory pressure; "
                        "retrying with '{}'.".format(candidate, candidates[candidate_index + 1])
                    )
                    continue
                logger.error(
                    f"failed to load model: {error} \n\n"
                    f"********************************************\n"
                    f"{_model_load_error_guidance(error)}\n"
                    f"********************************************\n\n"
                )
                return None
        if not model:
            return None

    logger.info(f"start, output file: {subtitle_file}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    transcribe_kwargs = {
        "beam_size": 5,
        "word_timestamps": True,
        "vad_filter": True,
        "vad_parameters": dict(min_silence_duration_ms=500),
    }
    language_hint = _whisper_language_hint(language)
    if language_hint:
        transcribe_kwargs["language"] = language_hint

    segments, info = model.transcribe(audio_file, **transcribe_kwargs)

    logger.info(
        f"detected language: '{info.language}', probability: {info.language_probability:.2f}"
    )

    start = timer()
    subtitles = []
    word_timings = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return

        msg = "[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text)
        logger.debug(msg)

        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    for segment in segments:
        segment_words = getattr(segment, "words", None) or []
        if not segment_words:
            try:
                segment_start = float(getattr(segment, "start", 0))
                segment_end = float(getattr(segment, "end", 0))
            except (TypeError, ValueError):
                continue
            if segment_start >= 0 and segment_end > segment_start:
                recognized(
                    str(getattr(segment, "text", "") or ""),
                    segment_start,
                    segment_end,
                )
            continue
        words_idx = 0
        words_len = len(segment_words)

        seg_start = 0
        seg_end = 0
        seg_text = ""

        if segment_words:
            is_segmented = False
            for word in segment_words:
                word_text = str(getattr(word, "word", "") or "").strip()
                try:
                    word_start = float(word.start)
                    word_end = float(word.end)
                except (AttributeError, TypeError, ValueError):
                    word_start = word_end = 0
                if word_text and word_start >= 0 and word_end > word_start:
                    word_timings.append(
                        {
                            "text": word_text,
                            "start_time": word_start,
                            "end_time": word_end,
                        }
                    )
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True

                seg_end = word.end
                # If it contains punctuation, then break the sentence.
                seg_text += word.word

                if utils.str_contains_punctuation(word.word):
                    # remove last char
                    seg_text = seg_text[:-1]
                    if not seg_text:
                        continue

                    recognized(seg_text, seg_start, seg_end)

                    is_segmented = False
                    seg_text = ""

                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if not seg_text:
            continue

        recognized(seg_text, seg_start, seg_end)

    end = timer()

    diff = end - start
    logger.info(f"complete, elapsed: {diff:.2f} s")

    idx = 1
    lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            lines.append(
                utils.text_to_srt(
                    idx, text, subtitle.get("start_time"), subtitle.get("end_time")
                )
            )
            idx += 1

    sub = "\n".join(lines) + "\n"
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write(sub)
    logger.info(f"subtitle file created: {subtitle_file}")
    if word_timing_file:
        _write_word_timings(word_timings, word_timing_file)
        logger.info(f"Whisper word timing file created: {word_timing_file}")


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


_SUBTITLE_CORRECTION_MIN_SIMILARITY = 0.8
_SUBTITLE_CORRECTION_LOOKAHEAD_WORDS = 12
_SUBTITLE_CORRECTION_WORD_COUNT_DELTA = 1


def _subtitle_correction_tokens(text: str) -> list[str]:
    return re.findall(r"\S+", str(text or ""))


def _subtitle_correction_key(text: str) -> str:
    return re.sub(r"[^\w]+", "", str(text or "").casefold())


def _best_script_match_details(
    subtitle_text: str, script_words: list[str], script_index: int
) -> dict | None:
    subtitle_words = _subtitle_correction_tokens(subtitle_text)
    subtitle_key = _subtitle_correction_key(subtitle_text)
    word_count = len(subtitle_words)
    if not subtitle_key or not word_count:
        return None

    max_start = min(
        len(script_words) - 1,
        script_index + _SUBTITLE_CORRECTION_LOOKAHEAD_WORDS,
    )
    best_match = None
    best_score = 0.0
    best_word_count_delta = None
    for candidate_start in range(script_index, max_start + 1):
        minimum_word_count = max(
            1,
            word_count - _SUBTITLE_CORRECTION_WORD_COUNT_DELTA,
        )
        maximum_word_count = min(
            len(script_words) - candidate_start,
            word_count + _SUBTITLE_CORRECTION_WORD_COUNT_DELTA,
        )
        for candidate_word_count in range(
            minimum_word_count,
            maximum_word_count + 1,
        ):
            candidate_end = candidate_start + candidate_word_count
            candidate_text = " ".join(script_words[candidate_start:candidate_end])
            candidate_key = _subtitle_correction_key(candidate_text)
            if not candidate_key:
                continue
            candidate_score = similarity(subtitle_key, candidate_key)
            candidate_word_count_delta = abs(candidate_word_count - word_count)
            if (
                candidate_score > best_score
                or (
                    candidate_score == best_score
                    and (
                        best_word_count_delta is None
                        or candidate_word_count_delta < best_word_count_delta
                    )
                )
            ):
                best_score = candidate_score
                best_word_count_delta = candidate_word_count_delta
                best_match = {
                    "text": candidate_text,
                    "end": candidate_end,
                    "similarity": best_score,
                }

    if best_score < _SUBTITLE_CORRECTION_MIN_SIMILARITY:
        return None
    return best_match


def _best_script_match_for_subtitle(
    subtitle_text: str, script_words: list[str], script_index: int
) -> tuple[str, int] | None:
    match_details = _best_script_match_details(
        subtitle_text,
        script_words,
        script_index,
    )
    if not match_details:
        return None
    return match_details["text"], match_details["end"]


def build_subtitle_suspicion_report(
    subtitle_file: str,
    video_script: str,
    language: str | None,
) -> dict | None:
    language_code = _whisper_language_hint(language)
    if language_code not in {"tr", "en"}:
        return None

    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_words = _subtitle_correction_tokens(normalized_script)
    report = {
        "language": language_code,
        "subtitle_count": len(subtitle_items),
        "suspicious_count": 0,
        "items": [],
    }
    if not subtitle_items or not script_words:
        return report

    script_index = 0
    for subtitle_index, (_, time_range, subtitle_text) in enumerate(subtitle_items):
        match_details = _best_script_match_details(
            subtitle_text,
            script_words,
            script_index,
        )
        if not match_details:
            report["items"].append(
                {
                    "index": subtitle_index + 1,
                    "time_range": time_range,
                    "subtitle_text": subtitle_text,
                    "suggested_text": None,
                    "reason": "no_script_match",
                    "similarity": None,
                }
            )
            continue

        script_index = match_details["end"]
        suggested_text = match_details["text"]
        if _subtitle_correction_key(subtitle_text) == _subtitle_correction_key(
            suggested_text
        ):
            continue

        report["items"].append(
            {
                "index": subtitle_index + 1,
                "time_range": time_range,
                "subtitle_text": subtitle_text,
                "suggested_text": suggested_text,
                "reason": "script_mismatch",
                "similarity": round(match_details["similarity"], 3),
            }
        )

    report["suspicious_count"] = len(report["items"])
    return report


def write_subtitle_suspicion_report(report: dict | None, report_file: str) -> bool:
    if not isinstance(report, dict) or not report_file:
        return False

    parent_dir = os.path.dirname(report_file)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    return True


_SUBTITLE_CORRECTIONS_FILE_VERSION = 1


def _write_subtitle_items(subtitle_file: str, subtitle_items) -> bool:
    if not subtitle_file or not subtitle_items:
        return False

    parent_dir = os.path.dirname(subtitle_file)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(subtitle_file, "w", encoding="utf-8") as file:
        for index, (_, time_range, subtitle_text) in enumerate(subtitle_items, start=1):
            file.write(f"{index}\n{time_range}\n{subtitle_text}\n\n")
    return True


def write_subtitle_items(subtitle_file: str, subtitle_items) -> bool:
    """Persist parsed subtitle items as an SRT file for a render-only variant."""
    return _write_subtitle_items(subtitle_file, subtitle_items)


def _write_manual_subtitle_corrections(corrections_file: str, corrections) -> bool:
    if not corrections_file:
        return False

    parent_dir = os.path.dirname(corrections_file)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(corrections_file, "w", encoding="utf-8") as file:
        json.dump(
            {
                "version": _SUBTITLE_CORRECTIONS_FILE_VERSION,
                "corrections": corrections,
            },
            file,
            ensure_ascii=False,
            indent=2,
        )
    return True


def _read_manual_subtitle_corrections(corrections_file: str) -> list[dict]:
    if not corrections_file or not os.path.isfile(corrections_file):
        return []
    try:
        with open(corrections_file, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(f"manual subtitle corrections could not be read: {error}")
        return []

    if not isinstance(payload, dict) or not isinstance(payload.get("corrections"), list):
        return []
    return [item for item in payload["corrections"] if isinstance(item, dict)]


def save_subtitle_generated_baseline(subtitle_file: str, baseline_file: str) -> bool:
    """Save the generated SRT before any user-authored correction is restored."""
    if not file_to_subtitles(subtitle_file) or not baseline_file:
        return False
    try:
        parent_dir = os.path.dirname(baseline_file)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        shutil.copyfile(subtitle_file, baseline_file)
        return True
    except OSError as error:
        logger.warning(f"subtitle baseline could not be saved: {error}")
        return False


def capture_manual_subtitle_corrections(
    subtitle_file: str,
    baseline_file: str,
    corrections_file: str,
) -> int:
    """Persist text edits made to a task SRT before it is regenerated.

    A correction is tied to both its time range and the generated source text.
    This lets a later render retain a deliberate edit without applying it to a
    newly generated, different line. Existing tasks that predate the baseline
    file are retained once by timestamp so their user work is not discarded.
    """
    subtitle_items = file_to_subtitles(subtitle_file)
    if not subtitle_items:
        return 0

    baseline_items = file_to_subtitles(baseline_file)
    baseline_by_time = {time_range: text for _, time_range, text in baseline_items}
    corrections = []
    for subtitle_index, (_, time_range, subtitle_text) in enumerate(
        subtitle_items, start=1
    ):
        if baseline_items:
            baseline_text = baseline_by_time.get(time_range)
            if baseline_text is None or subtitle_text == baseline_text:
                continue
        else:
            baseline_text = None

        corrections.append(
            {
                "index": subtitle_index,
                "time_range": time_range,
                "original_text": baseline_text,
                "replacement_text": subtitle_text,
            }
        )

    _write_manual_subtitle_corrections(corrections_file, corrections)
    return len(corrections)


def apply_manual_subtitle_corrections(subtitle_file: str, corrections_file: str) -> int:
    """Apply persisted manual edits only when the generated source still matches."""
    subtitle_items = file_to_subtitles(subtitle_file)
    corrections = _read_manual_subtitle_corrections(corrections_file)
    if not subtitle_items or not corrections:
        return 0

    corrections_by_time = {
        correction.get("time_range"): correction
        for correction in corrections
        if isinstance(correction.get("time_range"), str)
        and isinstance(correction.get("replacement_text"), str)
    }
    restored_count = 0
    restored_items = []
    for subtitle_index, time_range, subtitle_text in subtitle_items:
        correction = corrections_by_time.get(time_range)
        if not correction:
            restored_items.append((subtitle_index, time_range, subtitle_text))
            continue

        original_text = correction.get("original_text")
        replacement_text = correction["replacement_text"]
        if original_text is not None and original_text != subtitle_text:
            logger.info(
                "manual subtitle correction skipped because generated text changed, "
                f"time range: {time_range}"
            )
            restored_items.append((subtitle_index, time_range, subtitle_text))
            continue
        if replacement_text != subtitle_text:
            subtitle_text = replacement_text
            restored_count += 1
        restored_items.append((subtitle_index, time_range, subtitle_text))

    if restored_count:
        _write_subtitle_items(subtitle_file, restored_items)
    return restored_count


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_words = _subtitle_correction_tokens(normalized_script)
    if not subtitle_items or not script_words:
        return

    script_units = utils.split_string_by_punctuations(normalized_script)
    combined_subtitle_text = " ".join(item[2] for item in subtitle_items)
    if (
        len(script_units) == 1
        and len(subtitle_items) > 1
        and _subtitle_correction_key(combined_subtitle_text)
        == _subtitle_correction_key(script_units[0])
    ):
        start_time = subtitle_items[0][1].split(" --> ", 1)[0]
        end_time = subtitle_items[-1][1].split(" --> ", 1)[-1]
        _write_subtitle_items(
            subtitle_file,
            [(1, f"{start_time} --> {end_time}", script_units[0])],
        )
        logger.info("Subtitle corrected")
        return

    corrected = False
    new_subtitle_items = []
    script_index = 0
    matched_count = 0
    for subtitle_index, (_, times, subtitle_text) in enumerate(subtitle_items):
        corrected_text = subtitle_text
        script_match = _best_script_match_for_subtitle(
            subtitle_text, script_words, script_index
        )
        if script_match:
            matched_count += 1
            script_text, script_index = script_match
            if script_text != subtitle_text:
                logger.warning(
                    "Corrected subtitle text from script while preserving timing, "
                    f"subtitle index: {subtitle_index + 1}"
                )
                corrected_text = script_text
                corrected = True
        new_subtitle_items.append((subtitle_index + 1, times, corrected_text))

    if matched_count == 0 and len(script_units) > len(subtitle_items):
        new_subtitle_items = []
        for index, script_text in enumerate(script_units):
            time_range = (
                subtitle_items[index][1]
                if index < len(subtitle_items)
                else "00:00:00,000 --> 00:00:00,000"
            )
            new_subtitle_items.append((index + 1, time_range, script_text))
        corrected = True

    if corrected:
        _write_subtitle_items(subtitle_file, new_subtitle_items)
        logger.info("Subtitle corrected")
    else:
        logger.success("Subtitle is correct")


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
