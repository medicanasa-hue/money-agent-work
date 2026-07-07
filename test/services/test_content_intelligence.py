import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import ContentIntelligenceRequest
from app.services import content_intelligence


class TestContentIntelligence(unittest.TestCase):
    def test_trend_context_disabled_by_default(self):
        result = content_intelligence.get_trend_context(
            video_subject="finance tips",
            platform="tiktok",
            enabled=False,
            source="static",
        )

        self.assertIsInstance(result, content_intelligence.TrendContextResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.source, "none")

    def test_static_trend_context_returns_planning_input(self):
        result = content_intelligence.get_trend_context(
            video_subject="personal finance for beginners",
            platform="tiktok",
            enabled=True,
            source="static",
        )

        self.assertIsInstance(result, content_intelligence.TrendContextResult)
        self.assertEqual(result.source, "static")
        self.assertIn("Personal finance clarity", result.text)
        self.assertIn("not live trend", result.warnings[0])
        with self.assertRaises(AttributeError):
            result.warnings.append("mutated")

    def test_trend_context_handles_invalid_adapter_items(self):
        class InvalidAdapter:
            def fetch(self, query, platform="tiktok", limit=3):
                return [object(), {"title": "Broken item"}]

        result = content_intelligence.get_trend_context(
            video_subject="personal finance",
            platform="tiktok",
            enabled=True,
            source="static",
            adapter=InvalidAdapter(),
        )

        self.assertIsInstance(result, content_intelligence.TrendContextResult)
        self.assertEqual(result.text, "")
        self.assertEqual(result.source, "static")
        self.assertIn("no usable items", result.warnings[0])

    def test_rss_trend_context_returns_planning_input(self):
        with patch.object(
            content_intelligence.rss_trend,
            "fetch_rss_trend",
            return_value="Headline A; Headline B",
        ):
            result = content_intelligence.get_trend_context(
                video_subject="personal finance",
                platform="tiktok",
                enabled=True,
                source="rss",
            )

        self.assertIsInstance(result, content_intelligence.TrendContextResult)
        self.assertEqual(result.source, "rss")
        self.assertIn("Headline A", result.text)
        self.assertIn("not ranking", result.warnings[0])

    def test_rss_trend_context_warns_when_empty(self):
        with patch.object(
            content_intelligence.rss_trend,
            "fetch_rss_trend",
            return_value="",
        ):
            result = content_intelligence.get_trend_context(
                video_subject="personal finance",
                platform="tiktok",
                enabled=True,
                source="rss",
            )

        self.assertEqual(result.text, "")
        self.assertEqual(result.source, "rss")
        self.assertIn("RSS trend context returned no usable items", result.warnings[0])

    def test_build_prompt_disallows_live_trend_claims(self):
        prompt = content_intelligence.build_content_plan_prompt(
            video_subject="budgeting",
            language="en",
            platform="youtube_shorts",
        )

        self.assertIn("Do not claim live trends", prompt)
        self.assertIn("guaranteed virality", prompt)
        self.assertIn("Do not imply that you used web", prompt)

    def test_build_prompt_includes_static_trend_context_without_live_claims(self):
        prompt = content_intelligence.build_content_plan_prompt(
            video_subject="budgeting",
            language="en",
            platform="youtube_shorts",
            trend_context="Static planning note",
        )

        self.assertIn("Optional external planning context, not popularity data", prompt)
        self.assertIn("Static planning note", prompt)
        self.assertIn("Do not claim live trends", prompt)

    def test_generate_content_plan_uses_llm_json(self):
        payload = """
        {
          "ideas": [
            {
              "subject": "Budgeting mistakes",
              "angle": "common mistake",
              "hook": "This tiny mistake ruins budgets.",
              "script_prompt": "Write about budgeting mistakes.",
              "search_terms": ["budgeting", "money planning", "saving"],
              "platform": "tiktok",
              "rationale": "Useful for beginners."
            }
          ],
          "calendar": [
            {
              "day": 1,
              "date": "",
              "subject": "Budgeting mistakes",
              "format": "short_video",
              "goal": "teach one mistake",
              "script_prompt": "Write about budgeting mistakes."
            }
          ],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=payload):
            result = content_intelligence.generate_content_plan(
                video_subject="budgeting",
                platform="tiktok",
                days=7,
                daily_count=1,
                idea_count=1,
            )

        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["ideas"][0]["subject"], "Budgeting mistakes")
        self.assertEqual(len(result["calendar"]), 7)
        self.assertIn("No live trend data was used", result["warnings"][0])

    def test_generate_content_plan_normalizes_string_warning(self):
        payload = """
        {
          "ideas": [
            {
              "subject": "Coffee prices",
              "angle": "quick explainer",
              "hook": "Coffee got expensive for a reason.",
              "script_prompt": "Write about coffee prices.",
              "search_terms": ["coffee", "inflation", "shipping"],
              "platform": "tiktok",
              "rationale": "Useful explainer."
            }
          ],
          "calendar": [],
          "warnings": "No live trend data was used."
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=payload):
            result = content_intelligence.generate_content_plan(
                video_subject="coffee prices",
                platform="tiktok",
                days=7,
                daily_count=1,
                idea_count=1,
            )

        self.assertTrue(
            any("No live trend data was used" in warning for warning in result["warnings"])
        )
        self.assertLess(len(result["warnings"]), 4)

    def test_generate_content_plan_adds_static_trend_context_to_prompt(self):
        payload = """
        {
          "ideas": [
            {
              "subject": "Budgeting checklist",
              "angle": "quick checklist",
              "hook": "Use this before payday.",
              "script_prompt": "Write about a budgeting checklist.",
              "search_terms": ["budgeting", "saving", "money"],
              "platform": "tiktok",
              "rationale": "Useful for planning."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=payload) as generate:
            result = content_intelligence.generate_content_plan(
                video_subject="personal finance",
                platform="tiktok",
                days=7,
                daily_count=1,
                idea_count=1,
                use_trend_context=True,
                trend_source="static",
            )

        prompt = generate.call_args.args[0]
        self.assertIn("Personal finance clarity", prompt)
        self.assertIn("not popularity data", prompt)
        self.assertIn("Static trend context was used", result["warnings"][-1])

    def test_generate_content_plan_adds_rss_trend_context_to_prompt(self):
        payload = """
        {
          "ideas": [
            {
              "subject": "Budgeting headline lesson",
              "angle": "headline explainer",
              "hook": "This money headline has a simple lesson.",
              "script_prompt": "Write about a budgeting headline.",
              "search_terms": ["budgeting", "finance headline", "money"],
              "platform": "tiktok",
              "rationale": "Useful for planning."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(
            content_intelligence.rss_trend,
            "fetch_rss_trend",
            return_value="Headline A; Headline B",
        ):
            with patch.object(content_intelligence.llm, "_generate_response", return_value=payload) as generate:
                result = content_intelligence.generate_content_plan(
                    video_subject="personal finance",
                    platform="tiktok",
                    days=7,
                    daily_count=1,
                    idea_count=1,
                    use_trend_context=True,
                    trend_source="rss",
                )

        prompt = generate.call_args.args[0]
        self.assertIn("Headline A", prompt)
        self.assertIn("not popularity data", prompt)
        self.assertNotIn("No live trend data was used", result["warnings"][0])
        self.assertIn("RSS headlines were used", result["warnings"][0])
        self.assertIn("RSS trend context was used", result["warnings"][-1])

    def test_generate_content_plan_continues_when_trend_adapter_fails(self):
        class FailingAdapter:
            def fetch(self, query, platform="tiktok", limit=3):
                raise RuntimeError("adapter unavailable")

        payload = """
        {
          "ideas": [
            {
              "subject": "Coffee guide",
              "angle": "save-worthy list",
              "hook": "Save this coffee checklist.",
              "script_prompt": "Write about coffee.",
              "search_terms": ["coffee", "cafe", "morning"],
              "platform": "tiktok",
              "rationale": "Useful for planning."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=payload) as generate:
            result = content_intelligence.generate_content_plan(
                video_subject="coffee",
                platform="tiktok",
                days=7,
                daily_count=1,
                idea_count=1,
                use_trend_context=True,
                trend_source="static",
                trend_adapter=FailingAdapter(),
            )

        prompt = generate.call_args.args[0]
        self.assertNotIn("Coffee guide:", prompt)
        self.assertIn("Trend context was unavailable", result["warnings"][-1])
        self.assertEqual(result["source"], "llm")

    def test_generate_content_plan_falls_back_on_bad_json(self):
        with patch.object(
            content_intelligence.llm,
            "_generate_response",
            return_value="Error: api_key is not set",
        ):
            result = content_intelligence.generate_content_plan(
                video_subject="coffee",
                platform="instagram_reels",
                days=7,
                daily_count=2,
                idea_count=3,
            )

        self.assertEqual(result["source"], "fallback")
        self.assertEqual(len(result["ideas"]), 3)
        self.assertEqual(len(result["calendar"]), 14)
        self.assertIn("No live trend data was used", result["warnings"][0])

    def test_generate_content_plan_fallback_keeps_trend_warning(self):
        with patch.object(
            content_intelligence.llm,
            "_generate_response",
            return_value="Error: api_key is not set",
        ):
            result = content_intelligence.generate_content_plan(
                video_subject="personal finance",
                platform="tiktok",
                days=7,
                daily_count=1,
                idea_count=1,
                use_trend_context=True,
                trend_source="static",
            )

        self.assertEqual(result["source"], "fallback")
        self.assertIn("No live trend data was used", result["warnings"][0])
        self.assertTrue(
            any("Static trend context was used" in warning for warning in result["warnings"])
        )

    def test_generate_content_plan_fallback_keeps_rss_warning(self):
        with patch.object(
            content_intelligence.rss_trend,
            "fetch_rss_trend",
            return_value="Headline A; Headline B",
        ):
            with patch.object(
                content_intelligence.llm,
                "_generate_response",
                return_value="Error: api_key is not set",
            ):
                result = content_intelligence.generate_content_plan(
                    video_subject="personal finance",
                    platform="tiktok",
                    days=7,
                    daily_count=1,
                    idea_count=1,
                    use_trend_context=True,
                    trend_source="rss",
                )

        self.assertEqual(result["source"], "fallback")
        self.assertNotIn("No live trend data was used", result["warnings"][0])
        self.assertIn("RSS headlines were used", result["warnings"][0])
        self.assertTrue(
            any("RSS trend context was used" in warning for warning in result["warnings"])
        )

    def test_request_model_rejects_oversized_content_fields(self):
        with self.assertRaises(ValidationError):
            ContentIntelligenceRequest(video_subject="x" * 501)

        with self.assertRaises(ValidationError):
            ContentIntelligenceRequest(video_script="x" * 8001)

        with self.assertRaises(ValidationError):
            ContentIntelligenceRequest(days=6)

        with self.assertRaises(ValidationError):
            ContentIntelligenceRequest(daily_count=4)

    def test_content_intelligence_endpoint_response_shape(self):
        from fastapi.testclient import TestClient

        from app.asgi import app

        request_body = {
            "video_subject": "Tokyo coffee shops",
            "language": "en",
            "platform": "youtube_shorts",
            "days": 7,
            "daily_count": 1,
            "idea_count": 1,
        }
        llm_response = """
        {
          "ideas": [
            {
              "subject": "Quiet Tokyo coffee shops",
              "angle": "save-worthy list",
              "hook": "Save these quiet Tokyo coffee shops.",
              "script_prompt": "Write a short video about quiet Tokyo coffee shops.",
              "search_terms": ["Tokyo coffee", "quiet cafe", "morning"],
              "platform": "youtube_shorts",
              "rationale": "Useful travel planning angle."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=llm_response):
            response = TestClient(app).post(
                "/api/v1/content-intelligence",
                json=request_body,
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        self.assertEqual(body["data"]["source"], "llm")
        self.assertEqual(
            body["data"]["ideas"][0]["subject"],
            "Quiet Tokyo coffee shops",
        )
        self.assertEqual(len(body["data"]["calendar"]), 7)

    def test_content_intelligence_endpoint_uses_static_trend_context(self):
        from fastapi.testclient import TestClient

        from app.asgi import app

        request_body = {
            "video_subject": "personal finance for beginners",
            "language": "en",
            "platform": "tiktok",
            "days": 7,
            "daily_count": 1,
            "idea_count": 1,
            "use_trend_context": True,
            "trend_source": "static",
        }
        llm_response = """
        {
          "ideas": [
            {
              "subject": "Budgeting checklist",
              "angle": "quick checklist",
              "hook": "Use this before payday.",
              "script_prompt": "Write about a budgeting checklist.",
              "search_terms": ["budgeting", "saving", "money"],
              "platform": "tiktok",
              "rationale": "Useful for planning."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(content_intelligence.llm, "_generate_response", return_value=llm_response) as generate:
            response = TestClient(app).post(
                "/api/v1/content-intelligence",
                json=request_body,
            )

        self.assertEqual(response.status_code, 200)
        prompt = generate.call_args.args[0]
        body = response.json()
        self.assertIn("Personal finance clarity", prompt)
        self.assertIn("not popularity data", prompt)
        self.assertTrue(
            any(
                "Static trend context was used" in warning
                for warning in body["data"]["warnings"]
            )
        )

    def test_content_intelligence_endpoint_uses_rss_trend_context(self):
        from fastapi.testclient import TestClient

        from app.asgi import app

        request_body = {
            "video_subject": "personal finance for beginners",
            "language": "en",
            "platform": "tiktok",
            "days": 7,
            "daily_count": 1,
            "idea_count": 1,
            "use_trend_context": True,
            "trend_source": "rss",
        }
        llm_response = """
        {
          "ideas": [
            {
              "subject": "Budgeting headline lesson",
              "angle": "headline explainer",
              "hook": "This money headline has a simple lesson.",
              "script_prompt": "Write about a budgeting headline.",
              "search_terms": ["budgeting", "finance headline", "money"],
              "platform": "tiktok",
              "rationale": "Useful for planning."
            }
          ],
          "calendar": [],
          "warnings": ["No live trend data was used."]
        }
        """

        with patch.object(
            content_intelligence.rss_trend,
            "fetch_rss_trend",
            return_value="Headline A; Headline B",
        ):
            with patch.object(content_intelligence.llm, "_generate_response", return_value=llm_response) as generate:
                response = TestClient(app).post(
                    "/api/v1/content-intelligence",
                    json=request_body,
                )

        self.assertEqual(response.status_code, 200)
        prompt = generate.call_args.args[0]
        body = response.json()
        self.assertIn("Headline A", prompt)
        self.assertIn("not popularity data", prompt)
        self.assertEqual(body["data"]["source"], "llm")
        self.assertNotIn("No live trend data was used", body["data"]["warnings"][0])
        self.assertIn("RSS headlines were used", body["data"]["warnings"][0])
        self.assertTrue(
            any(
                "RSS trend context was used" in warning
                for warning in body["data"]["warnings"]
            )
        )


if __name__ == "__main__":
    unittest.main()
