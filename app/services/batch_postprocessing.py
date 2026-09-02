"""Bounded parallel work for batch steps that do not render video."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable


DEFAULT_NETWORK_WORKERS = 2
MAX_NETWORK_WORKERS = 3


def normalize_network_workers(value: object) -> int:
    """Clamp network-only batch concurrency without affecting video rendering."""
    try:
        workers = int(value)
    except (TypeError, ValueError):
        return DEFAULT_NETWORK_WORKERS
    return max(1, min(MAX_NETWORK_WORKERS, workers))


def _run_one(job: Any, processor: Callable[[Any], Any]) -> dict[str, Any]:
    try:
        return {"job": job, "ok": True, "result": processor(job), "error": ""}
    except Exception:
        # A metadata/analysis issue must never discard an already rendered video.
        return {
            "job": job,
            "ok": False,
            "result": None,
            "error": "postprocessing_failed",
        }


def run_network_postprocessing(
    jobs: Iterable[Any],
    processor: Callable[[Any], Any],
    *,
    max_workers: object = DEFAULT_NETWORK_WORKERS,
) -> list[dict[str, Any]]:
    """Process independent network jobs with stable output order.

    The helper is deliberately unsuitable for render/TTS work: callers should
    pass only independent network calls such as LLM metadata enrichment.
    """
    job_list = list(jobs)
    if not job_list:
        return []

    workers = min(normalize_network_workers(max_workers), len(job_list))
    if workers == 1:
        return [_run_one(job, processor) for job in job_list]

    outcomes: list[dict[str, Any] | None] = [None] * len(job_list)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_one, job, processor): index
            for index, job in enumerate(job_list)
        }
        for future in as_completed(futures):
            outcomes[futures[future]] = future.result()

    return [outcome for outcome in outcomes if outcome is not None]
