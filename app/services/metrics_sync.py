"""Batch orchestration for refreshing published-video metrics."""

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

from app.services import history


DEFAULT_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
SYNC_OUTCOME_SYNCED = "synced"
SYNC_OUTCOME_NO_DATA = "no_data"
SYNC_OUTCOME_TRANSIENT_ERROR = "transient_error"
SYNC_OUTCOME_PERMANENT_ERROR = "permanent_error"
SYNC_OUTCOMES = (
    SYNC_OUTCOME_SYNCED,
    SYNC_OUTCOME_NO_DATA,
    SYNC_OUTCOME_TRANSIENT_ERROR,
    SYNC_OUTCOME_PERMANENT_ERROR,
)


@dataclass(frozen=True)
class SyncJobResult:
    """Typed result returned by a provider-specific metrics sync function."""

    outcome: str

    def __post_init__(self):
        if self.outcome not in SYNC_OUTCOMES:
            raise ValueError(f"unsupported metrics-sync outcome: {self.outcome}")


def empty_metrics_sync_summary(*, eligible: int = 0) -> dict:
    """Create a backward-compatible batch summary with typed outcome counts."""
    return {
        "eligible": max(0, int(eligible)),
        "synced": 0,
        "skipped": 0,
        "errors": [],
        "outcomes": {outcome: 0 for outcome in SYNC_OUTCOMES},
    }


def _sync_error_summary(task_id: str, error: Exception) -> str:
    """Return a safe, non-sensitive error summary for batch results."""
    return f"{task_id}: {type(error).__name__}"


def _sync_outcome(sync_result: Any) -> str:
    value = sync_result[0] if isinstance(sync_result, tuple) and sync_result else sync_result
    if isinstance(value, SyncJobResult):
        return value.outcome
    return SYNC_OUTCOME_SYNCED if value else SYNC_OUTCOME_NO_DATA


def _record_outcome(
    result: dict,
    task_id: str,
    outcome: str,
    *,
    include_error: bool = True,
):
    result["outcomes"][outcome] += 1
    if outcome == SYNC_OUTCOME_SYNCED:
        result["synced"] += 1
        return
    if outcome == SYNC_OUTCOME_NO_DATA:
        result["skipped"] += 1
    elif include_error:
        result["errors"].append(f"{task_id}: {outcome}")
    history.update_metrics_sync_state(task_id, outcome)


def sync_pending_publish_metrics(
    sync_fn: Callable[[dict], Any],
    *,
    max_jobs: int | None = None,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Refresh metrics for eligible history jobs using an injected sync function."""
    interval_seconds = max(0.0, min_interval_seconds)
    attempt_limit = max(1, max_attempts)
    retry_backoff_seconds = max(0.0, backoff_seconds)
    delay_before_next_call = 0.0
    candidates = history.list_jobs_pending_metrics_sync()
    if max_jobs is not None:
        candidates = candidates[: max(0, int(max_jobs))]
    result = empty_metrics_sync_summary(eligible=len(candidates))

    for job in candidates:
        task_id = job.get("task_id")
        pending_uploads = job.get("pending_uploads")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(pending_uploads, list)
            or not pending_uploads
        ):
            result["skipped"] += 1
            continue

        outcome = None
        error_recorded = False
        for attempt in range(attempt_limit):
            if delay_before_next_call:
                sleep_fn(delay_before_next_call)

            try:
                sync_result = sync_fn(job)
            except OSError as exc:
                delay_before_next_call = max(
                    interval_seconds,
                    retry_backoff_seconds * (2**attempt),
                )
                if attempt + 1 == attempt_limit:
                    outcome = SYNC_OUTCOME_TRANSIENT_ERROR
                    result["errors"].append(_sync_error_summary(task_id, exc))
                    error_recorded = True
                    break
                continue
            except Exception as exc:
                delay_before_next_call = interval_seconds
                outcome = SYNC_OUTCOME_PERMANENT_ERROR
                result["errors"].append(_sync_error_summary(task_id, exc))
                error_recorded = True
                break
            else:
                delay_before_next_call = interval_seconds
                outcome = _sync_outcome(sync_result)
                break

        if outcome is None:
            continue
        _record_outcome(
            result,
            task_id,
            outcome,
            include_error=not error_recorded,
        )
        # Retry backoff belongs only to this job. Subsequent jobs need only
        # the normal provider request interval.
        delay_before_next_call = interval_seconds

    return result
