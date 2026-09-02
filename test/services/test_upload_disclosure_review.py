import tempfile
import unittest
from unittest.mock import patch

from app.services import history


class TestUploadDisclosureReview(unittest.TestCase):
    def test_upload_result_records_completed_disclosure_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app.services.history.utils.storage_dir", return_value=temp_dir):
                history.save_history(
                    [
                        {
                            "task_id": "upload-review",
                            "pending_uploads": [
                                {
                                    "video_path": "C:/videos/final.mp4",
                                    "platforms": ["tiktok", "youtube"],
                                    "status": "pending",
                                }
                            ],
                        }
                    ]
                )

                updated = history.update_pending_upload_result(
                    "upload-review",
                    "C:/videos/final.mp4",
                    {"success": True, "request_id": "request-1"},
                    disclosure_reviewed=True,
                )
                pending_upload = history.list_history()[0]["pending_uploads"][0]

        self.assertTrue(updated)
        self.assertEqual(pending_upload["status"], "uploaded")
        self.assertEqual(
            pending_upload["disclosure_review"].get("reviewed"),
            True,
        )
        self.assertTrue(pending_upload["disclosure_review"].get("reviewed_at"))


if __name__ == "__main__":
    unittest.main()
