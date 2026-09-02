import base64
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import scheduled_job_notifications


class TestScheduledJobNotifications(unittest.TestCase):
    def test_notifies_attention_status_with_native_windows_toast(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))

        with patch.object(
            scheduled_job_notifications, "_is_windows", return_value=True
        ), patch.object(
            scheduled_job_notifications.config,
            "app",
            {"scheduled_job_windows_notifications": True},
        ), patch.object(scheduled_job_notifications.subprocess, "run", run):
            notified = scheduled_job_notifications.notify_scheduled_job_attention(
                "morning-finance",
                "failed",
            )

        self.assertTrue(notified)
        command = run.call_args.args[0]
        self.assertIn("-EncodedCommand", command)
        encoded_script = command[command.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded_script).decode("utf-16le")
        encoded_values = re.findall(r"FromBase64String\('([^']+)'\)", script)
        decoded_values = [
            base64.b64decode(value).decode("utf-8") for value in encoded_values
        ]
        self.assertIn("morning-finance", decoded_values[1])
        self.assertIn("başarısız oldu", decoded_values[1])

    def test_ignores_completed_job_and_disabled_notifications(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))

        with patch.object(
            scheduled_job_notifications, "_is_windows", return_value=True
        ), patch.object(
            scheduled_job_notifications.config,
            "app",
            {"scheduled_job_windows_notifications": False},
        ), patch.object(scheduled_job_notifications.subprocess, "run", run):
            self.assertFalse(
                scheduled_job_notifications.notify_scheduled_job_attention(
                    "morning-finance", "failed"
                )
            )
            self.assertFalse(
                scheduled_job_notifications.notify_scheduled_job_attention(
                    "morning-finance", "completed"
                )
            )

        run.assert_not_called()

    def test_notifies_render_quality_attention_when_enabled(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))

        with patch.object(
            scheduled_job_notifications, "_is_windows", return_value=True
        ), patch.object(
            scheduled_job_notifications.config,
            "app",
            {"render_quality_windows_notifications": True},
        ), patch.object(scheduled_job_notifications.subprocess, "run", run):
            notified = scheduled_job_notifications.notify_render_quality_attention(
                "Son 5 videoda ses seviyesi düzensiz."
            )

        self.assertTrue(notified)
        command = run.call_args.args[0]
        encoded_script = command[command.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded_script).decode("utf-16le")
        encoded_values = re.findall(r"FromBase64String\('([^']+)'\)", script)
        decoded_values = [
            base64.b64decode(value).decode("utf-8") for value in encoded_values
        ]
        self.assertIn("kalite", decoded_values[0].casefold())
        self.assertIn("ses seviyesi", decoded_values[1])


if __name__ == "__main__":
    unittest.main()
