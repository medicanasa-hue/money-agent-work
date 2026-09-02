import unittest

from app.services import batch_postprocessing


class TestBatchPostprocessing(unittest.TestCase):
    def test_runs_each_job_and_preserves_input_order(self):
        jobs = [{"subject": "first"}, {"subject": "second"}, {"subject": "third"}]

        outcomes = batch_postprocessing.run_network_postprocessing(
            jobs,
            lambda job: {"subject": job["subject"], "ready": True},
            max_workers=2,
        )

        self.assertEqual(
            [outcome["result"]["subject"] for outcome in outcomes],
            ["first", "second", "third"],
        )
        self.assertTrue(all(outcome["ok"] for outcome in outcomes))

    def test_isolates_one_postprocessing_failure(self):
        def process(job):
            if job["subject"] == "broken":
                raise RuntimeError("provider error")
            return {"subject": job["subject"]}

        outcomes = batch_postprocessing.run_network_postprocessing(
            [{"subject": "ready"}, {"subject": "broken"}],
            process,
            max_workers=2,
        )

        self.assertTrue(outcomes[0]["ok"])
        self.assertFalse(outcomes[1]["ok"])
        self.assertEqual(outcomes[1]["error"], "postprocessing_failed")

    def test_normalizes_worker_count_to_safe_bounds(self):
        self.assertEqual(batch_postprocessing.normalize_network_workers("invalid"), 2)
        self.assertEqual(batch_postprocessing.normalize_network_workers(0), 1)
        self.assertEqual(batch_postprocessing.normalize_network_workers(99), 3)
