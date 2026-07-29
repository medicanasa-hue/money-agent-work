import unittest

from app.config import (
    DEFAULT_LOG_RETENTION_DAYS,
    daily_log_file_path,
    normalized_log_retention_days,
    redact_log_message,
)


class TestLogRedaction(unittest.TestCase):
    def test_redacts_common_secret_formats_before_a_record_reaches_a_sink(self):
        message = (
            "api_key=secret-value Authorization: Bearer bearer-value "
            "https://example.test/?token=query-value"
        )

        redacted = redact_log_message(message)

        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("bearer-value", redacted)
        self.assertNotIn("query-value", redacted)
        self.assertIn("<redacted>", redacted)

    def test_daily_log_path_and_retention_are_bounded(self):
        self.assertTrue(
            daily_log_file_path("C:/temporary/logs").endswith(
                "server_{time:YYYY-MM-DD}.log"
            )
        )
        self.assertEqual(normalized_log_retention_days("bad"), DEFAULT_LOG_RETENTION_DAYS)
        self.assertEqual(normalized_log_retention_days(9999), 365)


if __name__ == "__main__":
    unittest.main()
