import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import MaterialInfo, VideoAspect
from app.services import material_benchmark


class TestMaterialBenchmark(unittest.TestCase):
    def test_summarize_candidates_groups_quality_signals_by_provider(self):
        summary = material_benchmark.summarize_material_candidates(
            [
                MaterialInfo(
                    provider="pexels",
                    url="https://example.invalid/one.mp4",
                    width=1080,
                    height=1920,
                    duration=8,
                    preview_quality_score=0.9,
                ),
                MaterialInfo(
                    provider="pexels",
                    url="https://example.invalid/two.mp4",
                    width=1080,
                    height=1920,
                    duration=6,
                    preview_quality_score=0.7,
                ),
                MaterialInfo(
                    provider="pixabay",
                    url="https://example.invalid/three.mp4",
                    width=1920,
                    height=1080,
                    duration=5,
                    preview_quality_score=0.0,
                ),
            ],
            VideoAspect.portrait,
        )

        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(summary["provider_count"], 2)
        providers = {item["provider"]: item for item in summary["providers"]}
        self.assertEqual(providers["pexels"]["candidate_count"], 2)
        self.assertEqual(providers["pexels"]["average_preview_quality"], 0.8)
        self.assertEqual(providers["pixabay"]["average_preview_quality"], 0.0)
        self.assertGreater(
            providers["pexels"]["average_aspect_fit"],
            providers["pixabay"]["average_aspect_fit"],
        )

    def test_benchmark_rejects_empty_topic_without_provider_search(self):
        with patch.object(material_benchmark.material, "search_video_candidates") as search:
            result = material_benchmark.benchmark_material_providers("  ")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_topic")
        search.assert_not_called()

    def test_scene_inspection_reports_queries_without_candidates(self):
        matching_item = MaterialInfo(
            provider="pexels",
            url="https://example.invalid/grocery.mp4",
            search_query="grocery prices",
            title="Grocery price labels in a market",
            tags=["grocery", "prices", "market"],
            duration=5,
            width=1080,
            height=1920,
        )
        relevance_report = {
            "candidate_count": 1,
            "queries": [{"query": "grocery prices", "has_substantive_candidate": True}],
        }
        with (
            patch.object(
                material_benchmark.material,
                "search_video_candidates",
                side_effect=[[], [matching_item]],
            ) as search,
            patch.object(
                material_benchmark,
                "build_material_relevance_report",
                return_value=relevance_report,
            ),
        ):
            result = material_benchmark.inspect_scene_material_relevance(
                ["interest rates", "grocery prices"]
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["scene_count"], 2)
        self.assertEqual(result["covered_scene_count"], 1)
        self.assertEqual(result["scene_coverage_ratio"], 0.5)
        self.assertEqual(result["scene_coverage_status"], "partial")
        self.assertEqual(result["uncovered_scene_queries"], ["interest rates"])
        self.assertEqual(search.call_count, 2)
