import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import video_cooldown


class TestVideoCooldownStore(unittest.TestCase):
    def _store_path(self, temp_dir):
        return str(Path(temp_dir) / video_cooldown.COOLDOWN_FILENAME)

    def test_normalize_url_strips_query_and_spaces(self):
        self.assertEqual(
            video_cooldown.normalize_url(
                " https://cdn.example/video.mp4?token=temporary "
            ),
            "https://cdn.example/video.mp4",
        )

    def test_normalize_url_strips_fragment_variants(self):
        self.assertEqual(
            video_cooldown.normalize_url(
                "https://cdn.example/video.mp4#preview"
            ),
            "https://cdn.example/video.mp4",
        )

    def test_normalize_url_lowercases_scheme_and_host(self):
        self.assertEqual(
            video_cooldown.normalize_url(
                " HTTPS://CDN.EXAMPLE/video.mp4?token=temporary "
            ),
            "https://cdn.example/video.mp4",
        )

    def test_mark_used_deduplicates_normalized_urls(self):
        now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._store_path(temp_dir)
            with (
                patch.object(video_cooldown, "get_cooldown_path", return_value=path),
                patch.object(video_cooldown, "_utc_now", return_value=now),
            ):
                video_cooldown.mark_used(
                    "https://cdn.example/video.mp4?token=one",
                    provider="pexels",
                )
                video_cooldown.mark_used(
                    "https://cdn.example/video.mp4?token=two",
                    provider="pixabay",
                )
                records = video_cooldown.list_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], "https://cdn.example/video.mp4")
        self.assertEqual(records[0]["provider"], "pixabay")

    def test_recent_urls_ignores_stale_and_invalid_records(self):
        now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
        records = [
            {
                "url": "https://cdn.example/recent.mp4?x=1",
                "used_at": "2026-06-29T12:00:00+00:00",
            },
            {
                "url": "https://cdn.example/old.mp4",
                "used_at": "2026-06-01T12:00:00+00:00",
            },
            {
                "url": "https://cdn.example/bad-date.mp4",
                "used_at": "not-a-date",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._store_path(temp_dir)
            Path(path).write_text(json.dumps(records), encoding="utf-8")

            with (
                patch.object(video_cooldown, "get_cooldown_path", return_value=path),
                patch.object(video_cooldown, "_utc_now", return_value=now),
            ):
                urls = video_cooldown.recent_urls(days=7)

        self.assertEqual(urls, {"https://cdn.example/recent.mp4"})

    def test_filter_recently_used_splits_available_and_skipped_items(self):
        fresh = SimpleNamespace(url="https://cdn.example/fresh.mp4")
        used = SimpleNamespace(url="https://cdn.example/used.mp4?token=temporary")

        with patch.object(
            video_cooldown,
            "recent_urls",
            return_value={"https://cdn.example/used.mp4"},
        ):
            available, skipped = video_cooldown.filter_recently_used(
                [fresh, used],
                days=7,
            )

        self.assertEqual(available, [fresh])
        self.assertEqual(skipped, [used])


if __name__ == "__main__":
    unittest.main()
