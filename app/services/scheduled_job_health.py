"""Read-only health summaries for configured scheduled production jobs."""

from datetime import datetime, timedelta, timezone


DEFAULT_LOOKBACK_DAYS = 7


def _as_utc_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc
    )


def _lookback_days(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_DAYS


def _configured_jobs(jobs) -> list[dict]:
    try:
        values = list(jobs or [])
    except TypeError:
        return []
    configured = []
    seen = set()
    for job in values:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        configured.append({"name": name, "enabled": bool(job.get("enabled", True))})
    return configured


def build_scheduled_job_health_summary(
    entries,
    jobs,
    *,
    now: datetime | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """Summarize recent local scheduled-job results without running any jobs."""
    current_time = _as_utc_datetime(now) or datetime.now(timezone.utc)
    window_days = _lookback_days(lookback_days)
    cutoff = current_time - timedelta(days=window_days)
    grouped_entries: dict[str, list[tuple[datetime, dict]]] = {}
    try:
        raw_entries = list(entries or [])
    except TypeError:
        raw_entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("scheduled_job") or "").strip()
        created_at = _as_utc_datetime(entry.get("created_at"))
        if not name or created_at is None or created_at < cutoff:
            continue
        grouped_entries.setdefault(name, []).append((created_at, entry))

    configured_jobs = _configured_jobs(jobs)
    configured_names = {job["name"] for job in configured_jobs}
    for name in sorted(set(grouped_entries) - configured_names):
        configured_jobs.append({"name": name, "enabled": False})

    summaries = []
    for job in configured_jobs:
        name = job["name"]
        job_entries = sorted(
            grouped_entries.get(name, []),
            key=lambda item: item[0],
        )
        completed_count = sum(
            str(entry.get("status") or "").casefold() == "completed"
            for _, entry in job_entries
        )
        failed_count = sum(
            str(entry.get("status") or "").casefold() == "failed"
            for _, entry in job_entries
        )
        partial_count = sum(bool(entry.get("partial_success")) for _, entry in job_entries)
        last_time, last_entry = job_entries[-1] if job_entries else (None, {})
        last_status = str(last_entry.get("status") or "").casefold() or None
        if not job["enabled"]:
            health = "disabled"
        elif not job_entries:
            health = "no_history"
        elif last_status == "failed" or failed_count or partial_count:
            health = "needs_attention"
        else:
            health = "healthy"
        summaries.append(
            {
                "name": name,
                "enabled": job["enabled"],
                "health": health,
                "recent_run_count": len(job_entries),
                "completed_run_count": completed_count,
                "failed_run_count": failed_count,
                "partial_success_count": partial_count,
                "last_run_at": last_time.isoformat(timespec="seconds")
                if last_time is not None
                else None,
                "last_status": last_status,
            }
        )

    return {
        "lookback_days": window_days,
        "job_count": len(summaries),
        "healthy_count": sum(item["health"] == "healthy" for item in summaries),
        "attention_count": sum(
            item["health"] == "needs_attention" for item in summaries
        ),
        "no_history_count": sum(item["health"] == "no_history" for item in summaries),
        "jobs": summaries,
    }
