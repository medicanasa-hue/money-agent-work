import unittest
from unittest.mock import call, patch

from app.services import metrics_sync


class TestMetricsSync(unittest.TestCase):
    @patch("app.services.metrics_sync.history.update_metrics_sync_state")
    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_reports_typed_outcomes(
        self, list_candidates, update_metrics_sync_state
    ):
        list_candidates.return_value = [
            {"task_id": "synced", "pending_uploads": [{"request_id": "one"}]},
            {"task_id": "no-data", "pending_uploads": [{"request_id": "two"}]},
            {
                "task_id": "transient",
                "pending_uploads": [{"request_id": "three"}],
            },
            {
                "task_id": "permanent",
                "pending_uploads": [{"request_id": "four"}],
            },
        ]

        outcomes = {
            "synced": metrics_sync.SYNC_OUTCOME_SYNCED,
            "no-data": metrics_sync.SYNC_OUTCOME_NO_DATA,
            "transient": metrics_sync.SYNC_OUTCOME_TRANSIENT_ERROR,
            "permanent": metrics_sync.SYNC_OUTCOME_PERMANENT_ERROR,
        }

        result = metrics_sync.sync_pending_publish_metrics(
            lambda job: metrics_sync.SyncJobResult(outcomes[job["task_id"]]),
            min_interval_seconds=0,
        )

        self.assertEqual(result["eligible"], 4)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(
            result["outcomes"],
            {
                "synced": 1,
                "no_data": 1,
                "transient_error": 1,
                "permanent_error": 1,
            },
        )
        self.assertEqual(
            result["errors"],
            ["transient: transient_error", "permanent: permanent_error"],
        )
        update_metrics_sync_state.assert_has_calls(
            [
                call("no-data", "no_data"),
                call("transient", "transient_error"),
                call("permanent", "permanent_error"),
            ]
        )

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_aggregates_results(self, list_candidates):
        list_candidates.return_value = [
            {"task_id": "synced", "pending_uploads": [{"request_id": "one"}]},
            {"task_id": "skipped", "pending_uploads": [{"request_id": "two"}]},
            {"task_id": "broken", "pending_uploads": "not-a-list"},
            {"task_id": "error", "pending_uploads": [{"request_id": "three"}]},
        ]

        def sync_fn(job):
            if job["task_id"] == "synced":
                return True, "updated"
            if job["task_id"] == "error":
                raise RuntimeError("network unavailable api_key=secret-value")
            return False, "no metrics"

        result = metrics_sync.sync_pending_publish_metrics(
            sync_fn,
            min_interval_seconds=0,
        )

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["errors"], ["error: RuntimeError"])
        self.assertNotIn("secret-value", " ".join(result["errors"]))

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_spaces_valid_sync_calls(self, list_candidates):
        list_candidates.return_value = [
            {"task_id": "first", "pending_uploads": [{"request_id": "one"}]},
            {"task_id": "second", "pending_uploads": [{"request_id": "two"}]},
        ]
        sleep_calls = []

        result = metrics_sync.sync_pending_publish_metrics(
            lambda job: True,
            min_interval_seconds=0.25,
            max_attempts=1,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(result["synced"], 2)
        self.assertEqual(sleep_calls, [0.25])

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_retries_transient_errors_with_backoff(
        self, list_candidates
    ):
        list_candidates.return_value = [
            {"task_id": "retry", "pending_uploads": [{"request_id": "one"}]},
        ]
        sleep_calls = []
        attempts = 0

        def sync_fn(job):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OSError("temporary network failure")
            return True

        result = metrics_sync.sync_pending_publish_metrics(
            sync_fn,
            min_interval_seconds=0.25,
            max_attempts=3,
            backoff_seconds=1.0,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(attempts, 3)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(sleep_calls, [1.0, 2.0])

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_transient_backoff_does_not_delay_the_next_job(self, list_candidates):
        list_candidates.return_value = [
            {"task_id": "exhausted", "pending_uploads": [{"request_id": "one"}]},
            {"task_id": "healthy", "pending_uploads": [{"request_id": "two"}]},
        ]
        sleep_calls = []

        def sync_fn(job):
            if job["task_id"] == "exhausted":
                raise OSError("temporary network failure")
            return True

        result = metrics_sync.sync_pending_publish_metrics(
            sync_fn,
            min_interval_seconds=0.25,
            max_attempts=3,
            backoff_seconds=1.0,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["outcomes"]["transient_error"], 1)
        self.assertEqual(sleep_calls, [1.0, 2.0, 0.25])

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_does_not_retry_nontransient_errors(
        self, list_candidates
    ):
        list_candidates.return_value = [
            {"task_id": "broken", "pending_uploads": [{"request_id": "one"}]},
        ]
        sleep_calls = []
        attempts = 0

        def sync_fn(job):
            nonlocal attempts
            attempts += 1
            raise ValueError("invalid request")

        result = metrics_sync.sync_pending_publish_metrics(
            sync_fn,
            max_attempts=3,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(result["errors"], ["broken: ValueError"])
        self.assertEqual(sleep_calls, [])

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_limits_candidate_jobs(self, list_candidates):
        list_candidates.return_value = [
            {"task_id": "first", "pending_uploads": [{"request_id": "one"}]},
            {"task_id": "second", "pending_uploads": [{"request_id": "two"}]},
            {"task_id": "third", "pending_uploads": [{"request_id": "three"}]},
        ]
        synced_task_ids = []

        def sync_fn(job):
            synced_task_ids.append(job["task_id"])
            return True

        result = metrics_sync.sync_pending_publish_metrics(
            sync_fn,
            max_jobs=2,
            min_interval_seconds=0,
        )

        self.assertEqual(synced_task_ids, ["first", "second"])
        self.assertEqual(result["synced"], 2)

    @patch("app.services.metrics_sync.history.list_jobs_pending_metrics_sync")
    def test_sync_pending_publish_metrics_skips_empty_tuple_results(
        self, list_candidates
    ):
        list_candidates.return_value = [
            {"task_id": "pending", "pending_uploads": [{"request_id": "one"}]},
        ]

        result = metrics_sync.sync_pending_publish_metrics(
            lambda job: (),
            min_interval_seconds=0,
        )

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["outcomes"]["no_data"], 1)
