import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import visual_duplicates


class TestVisualDuplicates(unittest.TestCase):
    def test_frame_hash_has_zero_distance_for_same_frame(self):
        frame = np.full((32, 32, 3), 120, dtype=np.uint8)

        first_hash = visual_duplicates._frame_hash(frame)
        second_hash = visual_duplicates._frame_hash(frame.copy())

        self.assertIsNotNone(first_hash)
        self.assertEqual(visual_duplicates._hamming_distance(first_hash, second_hash), 0)

    def test_cross_task_scan_reports_matching_sample_hashes(self):
        video_paths = [
            "C:/storage/tasks/old-task/final-1.mp4",
            "C:/storage/tasks/new-task/final-1.mp4",
        ]
        matching_hash = "0" * 256
        with (
            patch.object(
                visual_duplicates,
                "_recent_final_video_paths",
                return_value=video_paths,
            ),
            patch.object(
                visual_duplicates,
                "_sample_video_hashes",
                side_effect=[
                    [{"sample_index": 0, "hash": matching_hash}],
                    [{"sample_index": 1, "hash": matching_hash}],
                ],
            ),
        ):
            report = visual_duplicates.find_cross_task_visual_duplicates()

        self.assertTrue(report["ok"])
        self.assertEqual(report["duplicate_pair_count"], 1)
        self.assertEqual(report["duplicates"][0]["left"]["task_id"], "old-task")
        self.assertEqual(report["duplicates"][0]["right"]["task_id"], "new-task")
