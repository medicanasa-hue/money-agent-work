import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import scheduled_job_health


class TestScheduledJobHealth(unittest.TestCase):
    def test_health_summary_flags_recent_failure_and_missing_history(self):
        report = scheduled_job_health.build_scheduled_job_health_summary(
            [
                {
                    "scheduled_job": "daily-finance",
                    "created_at": "2026-07-14T08:00:00+00:00",
                    "status": "completed",
                },
                {
                    "scheduled_job": "daily-finance",
                    "created_at": "2026-07-14T09:00:00+00:00",
                    "status": "failed",
                },
            ],
            [
                {"name": "daily-finance", "enabled": True},
                {"name": "weekly-science", "enabled": True},
            ],
            now=datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
        )

        by_name = {item["name"]: item for item in report["jobs"]}
        self.assertEqual(by_name["daily-finance"]["health"], "needs_attention")
        self.assertEqual(by_name["daily-finance"]["failed_run_count"], 1)
        self.assertEqual(by_name["daily-finance"]["last_status"], "failed")
        self.assertEqual(by_name["weekly-science"]["health"], "no_history")


if __name__ == "__main__":
    unittest.main()
