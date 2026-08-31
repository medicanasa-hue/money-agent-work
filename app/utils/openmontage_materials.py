"""Discover rendered OpenMontage videos bundled with the project."""

import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from app.utils import utils


PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ASPECT_OUTPUT_LABELS = {
    "16:9": "16x9",
    "9:16": "9x16",
    "4:5": "4x5",
    "1:1": "1x1",
}
_ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "4:5": 4 / 5,
    "1:1": 1.0,
}
_ASPECT_RATIO_TOLERANCE = 0.03


def is_openmontage_output_path(value: object) -> bool:
    """Return whether a finished video stays inside the bundled project library."""
    if not isinstance(value, (str, Path)):
        return False
    try:
        output_path = Path(value).expanduser().resolve(strict=True)
        projects_dir = (PROJECT_ROOT / "OpenMontage" / "projects").resolve(strict=True)
        output_path.relative_to(projects_dir)
    except (OSError, RuntimeError, ValueError):
        return False
    return output_path.suffix.lower() == ".mp4" and output_path.name.startswith("final")


def _probe_openmontage_video(path: Path) -> dict[str, Any] | None:
    ffprobe_binary = utils.get_ffprobe_binary()
    if not ffprobe_binary:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,bit_rate:format=duration,bit_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams") if isinstance(data, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else {}
        metadata = {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "avg_frame_rate": stream.get("avg_frame_rate"),
            "bit_rate": stream.get("bit_rate") or (data.get("format") or {}).get("bit_rate"),
            "duration": (data.get("format") or {}).get("duration"),
        }
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None
    return metadata


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _frame_rate(value: Any) -> float | None:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", maxsplit=1)
        parsed_numerator = _positive_float(numerator)
        parsed_denominator = _positive_float(denominator)
        if parsed_numerator is None or parsed_denominator is None:
            return None
        return parsed_numerator / parsed_denominator
    return _positive_float(value)


def _reference_bitrate_kbps(width: int, height: int, frame_rate: float | None) -> int | None:
    if frame_rate is None:
        return None
    bits_per_pixel_frame = 0.03
    reference_bps = int(width * height * frame_rate * bits_per_pixel_frame)
    return max(600, min(8000, round(reference_bps / 1000)))


def validate_openmontage_output(
    value: object,
    *,
    video_aspect: object | None = None,
    probe: Callable[[Path], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Inspect a local render before it is selected as an OpenMontage material.

    The helper is deliberately read-only. It never triggers Manim and only
    reports whether an existing native output matches the requested frame.
    """
    raw_aspect = getattr(video_aspect, "value", video_aspect)
    expected_aspect = str(raw_aspect) if isinstance(raw_aspect, str) else None
    if expected_aspect not in _ASPECT_RATIOS:
        expected_aspect = None
    report: dict[str, Any] = {
        "valid": False,
        "path": str(value or ""),
        "expected_aspect": expected_aspect,
        "resolution": "",
        "duration_seconds": None,
        "frame_rate": None,
        "bitrate_kbps": None,
        "quality_reference_kbps": None,
        "quality_warnings": [],
        "issues": [],
    }
    if not is_openmontage_output_path(value):
        report["issues"].append("invalid_output_path")
        return report

    path = Path(value).expanduser().resolve()
    try:
        if path.stat().st_size <= 0:
            report["issues"].append("empty_file")
            return report
    except OSError:
        report["issues"].append("unreadable_file")
        return report

    try:
        metadata = (probe or _probe_openmontage_video)(path)
    except Exception:
        metadata = None
    if not isinstance(metadata, dict):
        report["issues"].append("metadata_unavailable")
        return report

    width = _positive_int(metadata.get("width"))
    height = _positive_int(metadata.get("height"))
    duration = _positive_float(metadata.get("duration"))
    bit_rate = _positive_int(metadata.get("bit_rate"))
    frame_rate = _frame_rate(metadata.get("avg_frame_rate") or metadata.get("frame_rate"))
    if width is None or height is None:
        report["issues"].append("invalid_resolution")
    else:
        report["resolution"] = f"{width}x{height}"
        if expected_aspect and abs((width / height) - _ASPECT_RATIOS[expected_aspect]) > _ASPECT_RATIO_TOLERANCE:
            report["issues"].append("aspect_mismatch")
        reference_kbps = _reference_bitrate_kbps(width, height, frame_rate)
        if reference_kbps is not None:
            report["quality_reference_kbps"] = reference_kbps
            if bit_rate is not None:
                bitrate_kbps = round(bit_rate / 1000)
                report["bitrate_kbps"] = bitrate_kbps
                if bitrate_kbps < reference_kbps * 0.6:
                    report["quality_warnings"].append("low_bitrate_review_recommended")
    if frame_rate is not None:
        report["frame_rate"] = round(frame_rate, 3)
    elif bit_rate is not None:
        report["bitrate_kbps"] = round(bit_rate / 1000)
    if duration is None:
        report["issues"].append("invalid_duration")
    else:
        report["duration_seconds"] = round(duration, 3)

    report["valid"] = not report["issues"]
    return report


def _output_aspect_from_filename(filename: str) -> str | None:
    normalized = filename.casefold()
    for aspect, label in _ASPECT_OUTPUT_LABELS.items():
        if f"_{label}_" in normalized or normalized.endswith(f"_{label}.mp4"):
            return aspect
    return None


def _output_language_from_filename(filename: str) -> str:
    return "tr" if filename.casefold().startswith("final_silent_tr_") else "en"


def validate_openmontage_library(
    projects_dir: Path | None = None,
    *,
    probe: Callable[[Path], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Validate manifests and existing silent outputs without invoking Manim.

    The report is intended for a local maintenance check. It verifies that each
    declared language has a usable silent output and reuses the per-file video
    validator for resolution, duration, and native-aspect checks.
    """
    library_dir = projects_dir or PROJECT_ROOT / "OpenMontage" / "projects"
    report: dict[str, Any] = {
        "valid": False,
        "project_count": 0,
        "projects": [],
        "issues": [],
    }
    if not library_dir.is_dir():
        report["issues"].append("projects_directory_missing")
        return report

    project_reports = []
    for project_dir in sorted(
        (path for path in library_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name.casefold(),
    ):
        issues: list[str] = []
        manifest_path = project_dir / "openmontage.json"
        manifest: dict[str, Any] | None = None
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = raw_manifest if isinstance(raw_manifest, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest = None
        if manifest is None:
            issues.append("invalid_manifest")
            languages: list[str] = []
        else:
            raw_languages = manifest.get("languages")
            languages = []
            if not isinstance(raw_languages, list):
                issues.append("invalid_languages")
            else:
                for language in raw_languages:
                    normalized_language = _normalized_language(language)
                    if normalized_language not in {"en", "tr"}:
                        issues.append("invalid_languages")
                        continue
                    if normalized_language not in languages:
                        languages.append(normalized_language)
                if not languages:
                    issues.append("invalid_languages")
            keywords = manifest.get("keywords")
            if not isinstance(keywords, list) or not any(
                isinstance(keyword, str) and keyword.strip()
                for keyword in keywords
            ):
                issues.append("invalid_keywords")

        output_reports = []
        usable_languages = set()
        output_paths = sorted(
            project_dir.glob("final_silent*.mp4"),
            key=lambda path: path.name.casefold(),
        )
        if not output_paths:
            issues.append("missing_silent_output")
        for output_path in output_paths:
            output_report = validate_openmontage_output(
                output_path,
                video_aspect=_output_aspect_from_filename(output_path.name),
                probe=probe,
            )
            output_report["filename"] = output_path.name
            output_reports.append(output_report)
            if output_report["valid"]:
                usable_languages.add(_output_language_from_filename(output_path.name))
            else:
                issue_codes = ",".join(output_report["issues"])
                issues.append(f"invalid_output:{output_path.name}:{issue_codes}")

        for language in languages:
            if language not in usable_languages:
                issues.append(f"missing_language_output:{language}")
        project_reports.append(
            {
                "project": project_dir.name,
                "valid": not issues,
                "languages": languages,
                "outputs": output_reports,
                "issues": issues,
            }
        )

    report["project_count"] = len(project_reports)
    report["projects"] = project_reports
    report["valid"] = bool(project_reports) and all(
        project["valid"] for project in project_reports
    )
    if not project_reports:
        report["issues"].append("no_projects_found")
    return report


def _keyword_tokens(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return set(re.findall(r"[\w]+", value.lower()))


def _project_keywords(project_dir: Path) -> set[str]:
    """Include optional local aliases without requiring a separate service."""
    keywords = _keyword_tokens(project_dir.name)
    manifest_path = project_dir / "openmontage.json"
    if not manifest_path.is_file():
        return keywords
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return keywords
    if not isinstance(manifest, dict):
        return keywords
    aliases = manifest.get("keywords")
    if not isinstance(aliases, list):
        return keywords
    for alias in aliases:
        keywords.update(_keyword_tokens(alias))
    return keywords


def _normalized_language(value: object | None) -> str | None:
    value = getattr(value, "value", value)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-")
    if not normalized or normalized == "auto":
        return None
    return normalized.split("-", maxsplit=1)[0]


def _output_filenames(
    prefer_silent: bool,
    video_aspect: object | None,
    language: object | None = None,
) -> list[str]:
    prefix = "final_silent" if prefer_silent else "final"
    aspect = getattr(video_aspect, "value", video_aspect)
    aspect_label = _ASPECT_OUTPUT_LABELS.get(str(aspect))
    language_code = _normalized_language(language)
    if language_code and language_code not in {"en", "tr"}:
        return []
    if not aspect_label:
        language_filenames = (
            [f"{prefix}_{language_code}_720p.mp4", f"{prefix}_{language_code}.mp4"]
            if language_code
            else []
        )
        if language_code == "tr":
            return language_filenames
        return language_filenames + [f"{prefix}_720p.mp4"]

    language_filenames = (
        [
            f"{prefix}_{language_code}_{aspect_label}_1080p.mp4",
            f"{prefix}_{language_code}_{aspect_label}_720p.mp4",
            f"{prefix}_{language_code}_{aspect_label}.mp4",
        ]
        if language_code
        else []
    )
    if language_code == "tr":
        return language_filenames
    return language_filenames + [
        f"{prefix}_{aspect_label}_1080p.mp4",
        f"{prefix}_{aspect_label}_720p.mp4",
        f"{prefix}_{aspect_label}.mp4",
    ]


def find_openmontage_output(
    subject_or_keyword: str,
    prefer_silent: bool = False,
    video_aspect: object | None = None,
    language: object | None = None,
) -> str | None:
    """Return the best matching OpenMontage output for the requested aspect.

    Portrait, 4:5, and square requests only accept a matching native output
    (for example ``final_silent_9x16_720p.mp4``). This prevents a landscape
    animation from being cropped down to a narrow social-video frame.
    """
    keywords = _keyword_tokens(subject_or_keyword)
    projects_dir = PROJECT_ROOT / "OpenMontage" / "projects"
    if not keywords or not projects_dir.is_dir():
        return None

    matches = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        project_words = _project_keywords(project_dir)
        overlap = keywords & project_words
        if overlap:
            matches.append((len(overlap), project_dir))

    if not matches:
        return None

    filenames = _output_filenames(prefer_silent, video_aspect, language)
    for _, project_dir in sorted(
        matches,
        key=lambda match: (-match[0], match[1].name.casefold()),
    ):
        for filename in filenames:
            output_path = project_dir / filename
            if output_path.is_file():
                return str(output_path)
    return None
