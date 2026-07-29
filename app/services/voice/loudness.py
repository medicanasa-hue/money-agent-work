import os
import subprocess

from loguru import logger

from app.utils import utils


_NARRATION_LOUDNESS_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"


def normalize_narration_loudness(
    input_path: str, output_path: str | None = None
) -> str:
    """Create a normalized narration copy, preserving the original on failure."""
    original_path = input_path
    source_path = os.path.realpath(input_path or "")
    if not source_path or not os.path.isfile(source_path):
        logger.warning("narration loudness normalization skipped: source is unavailable")
        return original_path

    normalized_path = os.path.realpath(
        output_path or os.path.splitext(source_path)[0] + ".normalized.wav"
    )
    if normalized_path == source_path:
        logger.warning("narration loudness normalization skipped: output matches source")
        return original_path

    os.makedirs(os.path.dirname(normalized_path), exist_ok=True)
    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-i",
        source_path,
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        _NARRATION_LOUDNESS_FILTER,
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        normalized_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"narration loudness normalization unavailable: {exc}")
        return original_path

    if result.returncode != 0:
        logger.warning("narration loudness normalization failed; using original audio")
        return original_path
    if not os.path.isfile(normalized_path) or os.path.getsize(normalized_path) <= 0:
        logger.warning("narration loudness normalization produced no audio; using original")
        return original_path
    return normalized_path
