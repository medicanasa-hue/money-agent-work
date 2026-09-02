import ast
from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _load_webui_helpers(*names):
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"tr": lambda key: key}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


class TestPublishInsightsWebUi(unittest.TestCase):
    def test_insufficient_data_message_explains_the_sample_count(self):
        helpers = _load_webui_helpers("_publish_insights_status_text")
        helpers["tr"] = {
            "Publish Insights Insufficient Data": (
                "{samples} of {minimum} metric samples are available."
            ),
            "Publish Insights Advisory": "Read-only insights.",
        }.__getitem__

        text = helpers["_publish_insights_status_text"](
            {"status": "insufficient_data", "sample_size": 1, "minimum_sample_size": 3}
        )

        self.assertEqual(text, "1 of 3 metric samples are available.")

    def test_suggestion_text_uses_a_localized_read_only_message(self):
        helpers = _load_webui_helpers("_publish_insight_suggestion_text")
        helpers["tr"] = {
            "Publish Insight Quality Alignment": "Quality scores are aligned with engagement.",
            "Publish Insight Fallback": "Review the available publish metrics manually.",
        }.get

        self.assertEqual(
            helpers["_publish_insight_suggestion_text"](
                {"type": "quality_gate_alignment"}
            ),
            "Quality scores are aligned with engagement.",
        )
        self.assertEqual(
            helpers["_publish_insight_suggestion_text"]({"type": "unknown"}),
            "Review the available publish metrics manually.",
        )


if __name__ == "__main__":
    unittest.main()
