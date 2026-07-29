"""On-demand AMF encoder calibration using a short local synthetic clip."""

import os
import re
import subprocess
import tempfile

from app.models.schema import VideoAspect
from app.services import video
from app.utils import utils


_CALIBRATION_DURATION_SECONDS = 2
_CALIBRATION_FPS = 30
_AMF_QP_STEP = 2
_AMF_MAX_QP_I = 49
_SSIM_PATTERN = re.compile(r"SSIM .*?All:([0-9.]+)", re.IGNORECASE)
_PSNR_PATTERN = re.compile(r"PSNR .*?average:([0-9.]+)", re.IGNORECASE)


def _configured_amf_qp_i() -> int:
    quality_args = video._ffmpeg_quality_args("h264_amf")
    try:
        qp_i_index = quality_args.index("-qp_i") + 1
        qp_i = int(quality_args[qp_i_index])
    except (ValueError, IndexError, TypeError):
        return 12
    return max(0, min(_AMF_MAX_QP_I, qp_i))


def _amf_qp_candidates(baseline_qp_i: int) -> list[int]:
    candidates = {
        max(0, min(_AMF_MAX_QP_I, baseline_qp_i - _AMF_QP_STEP)),
        max(0, min(_AMF_MAX_QP_I, baseline_qp_i)),
        max(0, min(_AMF_MAX_QP_I, baseline_qp_i + _AMF_QP_STEP)),
    }
    return sorted(candidates)


def build_amf_calibration_plan(
    video_aspect: VideoAspect | str = VideoAspect.portrait,
    *,
    baseline_qp_i: int | None = None,
) -> dict:
    """Return the local AMF measurements that would be run without changing config."""
    aspect = VideoAspect(video_aspect)
    width, height = aspect.to_resolution()
    baseline = _configured_amf_qp_i() if baseline_qp_i is None else int(baseline_qp_i)
    candidates = [
        {"qp_i": qp_i, "qp_p": min(51, qp_i + 2)}
        for qp_i in _amf_qp_candidates(baseline)
    ]
    return {
        "video_aspect": aspect.value,
        "resolution": [width, height],
        "fps": _CALIBRATION_FPS,
        "duration_seconds": _CALIBRATION_DURATION_SECONDS,
        "baseline_qp_i": max(0, min(_AMF_MAX_QP_I, baseline)),
        "candidates": candidates,
    }


def _parse_measurement_output(output: str) -> dict[str, float | None]:
    normalized_output = str(output or "")
    ssim_match = _SSIM_PATTERN.search(normalized_output)
    psnr_match = _PSNR_PATTERN.search(normalized_output)
    return {
        "ssim": float(ssim_match.group(1)) if ssim_match else None,
        "psnr": float(psnr_match.group(1)) if psnr_match else None,
    }


def _run_ffmpeg_command(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return result.returncode == 0, "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )


def _reference_command(
    ffmpeg_binary: str,
    output_path: str,
    *,
    width: int,
    height: int,
) -> list[str]:
    return [
        ffmpeg_binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={_CALIBRATION_FPS}",
        "-t",
        str(_CALIBRATION_DURATION_SECONDS),
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "ffv1",
        "-an",
        output_path,
    ]


def _encode_command(
    ffmpeg_binary: str,
    source_path: str,
    output_path: str,
    *,
    qp_i: int,
) -> list[str]:
    return [
        ffmpeg_binary,
        "-y",
        "-i",
        source_path,
        "-c:v",
        "h264_amf",
        "-preset",
        "quality",
        "-rc",
        "cqp",
        "-qp_i",
        str(qp_i),
        "-qp_p",
        str(min(51, qp_i + 2)),
        "-g",
        str(_CALIBRATION_FPS * 2),
        "-pix_fmt",
        "yuv420p",
        "-an",
        output_path,
    ]


def _measurement_command(
    ffmpeg_binary: str,
    source_path: str,
    encoded_path: str,
) -> list[str]:
    return [
        ffmpeg_binary,
        "-v",
        "info",
        "-i",
        source_path,
        "-i",
        encoded_path,
        "-lavfi",
        "[0:v][1:v]ssim;[0:v][1:v]psnr",
        "-f",
        "null",
        "-",
    ]


def _encoded_bitrate_kbps(encoded_path: str) -> float | None:
    try:
        size_bytes = os.path.getsize(encoded_path)
    except OSError:
        return None
    if size_bytes <= 0:
        return None
    return round(
        size_bytes * 8 / _CALIBRATION_DURATION_SECONDS / 1000,
        2,
    )


def run_amf_calibration(
    video_aspect: VideoAspect | str = VideoAspect.portrait,
) -> dict:
    """Measure AMF QP candidates locally; never writes back to configuration."""
    configured_codec = video._get_configured_video_codec()
    plan = build_amf_calibration_plan(video_aspect)
    if configured_codec != "h264_amf":
        return {
            "ok": False,
            "status": "not_amf_configured",
            "configured_codec": configured_codec,
            **plan,
        }

    try:
        ffmpeg_binary = str(utils.get_ffmpeg_binary() or "").strip()
    except Exception:
        ffmpeg_binary = ""
    if not ffmpeg_binary:
        return {
            "ok": False,
            "status": "ffmpeg_unavailable",
            "configured_codec": configured_codec,
            **plan,
        }

    width, height = plan["resolution"]
    with tempfile.TemporaryDirectory(prefix="mpt-amf-calibration-") as temp_dir:
        source_path = os.path.join(temp_dir, "reference.mkv")
        reference_ok, _ = _run_ffmpeg_command(
            _reference_command(
                ffmpeg_binary,
                source_path,
                width=width,
                height=height,
            )
        )
        if not reference_ok:
            return {
                "ok": False,
                "status": "reference_generation_failed",
                "configured_codec": configured_codec,
                **plan,
            }

        measured_candidates = []
        for candidate in plan["candidates"]:
            encoded_path = os.path.join(temp_dir, f"amf-qp{candidate['qp_i']}.mp4")
            encode_ok, _ = _run_ffmpeg_command(
                _encode_command(
                    ffmpeg_binary,
                    source_path,
                    encoded_path,
                    qp_i=candidate["qp_i"],
                )
            )
            result = dict(candidate)
            if not encode_ok:
                result["status"] = "encode_failed"
                measured_candidates.append(result)
                continue

            measurement_ok, output = _run_ffmpeg_command(
                _measurement_command(ffmpeg_binary, source_path, encoded_path)
            )
            result["bitrate_kbps"] = _encoded_bitrate_kbps(encoded_path)
            if not measurement_ok:
                result["status"] = "measurement_failed"
                measured_candidates.append(result)
                continue
            result.update(_parse_measurement_output(output))
            result["status"] = "measured"
            measured_candidates.append(result)

    measured_count = sum(
        candidate.get("status") == "measured" for candidate in measured_candidates
    )
    return {
        "ok": measured_count > 0,
        "status": "completed" if measured_count else "encoder_unavailable",
        "configured_codec": configured_codec,
        **plan,
        "candidates": measured_candidates,
    }
