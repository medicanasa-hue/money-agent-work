"""Validation helpers for named CLI jobs defined in ``config.toml``."""

from typing import Any

from app.config import config
from app.models.schema import VideoTransitionMode


class ScheduledJobError(ValueError):
    """Raised when a configured scheduled job cannot be used safely."""


def _job_name(value: Any) -> str:
    return str(value or "").strip()


def _config_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _optional_config_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _config_bool(value)


def _normalize_subject_pool(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ScheduledJobError("scheduled job video_subject_pool must be a list")

    subjects = []
    seen_subjects = set()
    for item in value:
        subject = _job_name(item)
        subject_key = subject.casefold()
        if not subject or subject_key in seen_subjects:
            continue
        seen_subjects.add(subject_key)
        subjects.append(subject)
    return subjects


def _normalize_video_transition_mode(value: Any) -> str | None:
    if value is None:
        return None

    requested = _job_name(value)
    if not requested or requested.casefold() == "none":
        return None

    for transition_mode in VideoTransitionMode:
        if (
            transition_mode.value
            and requested.casefold() == transition_mode.value.casefold()
        ):
            return transition_mode.value

    raise ScheduledJobError("scheduled job video_transition_mode is invalid")


def _normalize_scheduled_job(job: dict[str, Any]) -> dict[str, Any]:
    name = _job_name(job.get("name"))
    if not name:
        raise ScheduledJobError("scheduled job must have a name")

    script = job.get("video_script", "")
    if script is None:
        script = ""
    if not isinstance(script, str):
        raise ScheduledJobError("scheduled job video_script must be text")

    subject = _job_name(job.get("video_subject"))
    subject_pool = _normalize_subject_pool(job.get("video_subject_pool"))
    rss_trend_query = _job_name(job.get("rss_trend_query"))
    if not subject and not subject_pool and not rss_trend_query:
        raise ScheduledJobError(
            "scheduled job must have a video_subject, video_subject_pool, or rss_trend_query"
        )
    if (subject_pool or rss_trend_query) and script.strip():
        raise ScheduledJobError(
            "scheduled job dynamic subjects cannot use a fixed video_script"
        )

    return {
        "name": name,
        "enabled": _config_bool(job.get("enabled"), default=True),
        "video_subject": subject,
        "video_subject_pool": subject_pool,
        "rss_trend_query": rss_trend_query,
        "rss_trend_language": _job_name(job.get("rss_trend_language")),
        "video_script": script.strip(),
        "video_script_prompt": _job_name(job.get("video_script_prompt")),
        "voice_name": _job_name(job.get("voice_name")),
        "match_materials_to_script": _optional_config_bool(
            job.get("match_materials_to_script")
        ),
        "smart_scene_queries": _optional_config_bool(
            job.get("smart_scene_queries")
        ),
        "skip_if_recent_duplicate": _config_bool(
            job.get("skip_if_recent_duplicate"), default=False
        ),
        "openmontage_auto_materials": _config_bool(
            job.get("openmontage_auto_materials"), default=False
        ),
        "video_transition_mode": _normalize_video_transition_mode(
            job.get("video_transition_mode")
        ),
    }


def list_scheduled_jobs(jobs=None) -> list[dict[str, Any]]:
    configured_jobs = (
        config.app.get("scheduled_jobs", []) if jobs is None else jobs
    )
    if not isinstance(configured_jobs, (list, tuple)):
        raise ScheduledJobError("scheduled_jobs must be a list")

    normalized_jobs = []
    seen_names = set()
    for job in configured_jobs:
        if not isinstance(job, dict):
            raise ScheduledJobError("scheduled job must be an object")
        normalized = _normalize_scheduled_job(job)
        name_key = normalized["name"].casefold()
        if name_key in seen_names:
            raise ScheduledJobError("scheduled job name must be unique")
        seen_names.add(name_key)
        normalized_jobs.append(normalized)
    return normalized_jobs


def get_scheduled_job(name: str, jobs=None) -> dict[str, Any]:
    requested_name = _job_name(name)
    if not requested_name:
        raise ScheduledJobError("scheduled job name is required")

    configured_jobs = (
        config.app.get("scheduled_jobs", []) if jobs is None else jobs
    )
    if not isinstance(configured_jobs, (list, tuple)):
        raise ScheduledJobError("scheduled_jobs must be a list")

    matching_jobs = [
        job
        for job in configured_jobs
        if isinstance(job, dict)
        and _job_name(job.get("name")).casefold() == requested_name.casefold()
    ]
    if not matching_jobs:
        raise ScheduledJobError("scheduled job was not found")
    if len(matching_jobs) > 1:
        raise ScheduledJobError("scheduled job name must be unique")

    normalized = _normalize_scheduled_job(matching_jobs[0])
    if not normalized["enabled"]:
        raise ScheduledJobError("scheduled job is disabled")
    return normalized


def scheduled_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"],
        "enabled": bool(job["enabled"]),
        "video_subject": job["video_subject"],
        "subject_pool_size": len(job.get("video_subject_pool") or []),
        "has_rss_trend_query": bool(job.get("rss_trend_query")),
        "has_custom_script": bool(job["video_script"]),
        "match_materials_to_script": job.get("match_materials_to_script"),
        "smart_scene_queries": job.get("smart_scene_queries"),
        "skip_if_recent_duplicate": bool(job.get("skip_if_recent_duplicate")),
        "openmontage_auto_materials": bool(
            job.get("openmontage_auto_materials")
        ),
        "video_transition_mode": job.get("video_transition_mode"),
    }
