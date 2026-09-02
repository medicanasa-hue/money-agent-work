import ast
from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _load_webui_helper(name):
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace[name]


class TestUploadDisclosureReviewWebUi(unittest.TestCase):
    def test_ai_disclosure_review_is_only_needed_for_supported_platforms(self):
        needs_review = _load_webui_helper("_needs_ai_disclosure_review")

        self.assertTrue(needs_review(["tiktok"]))
        self.assertTrue(needs_review(["youtube_shorts"]))
        self.assertFalse(needs_review(["instagram"]))
        self.assertFalse(needs_review([]))


if __name__ == "__main__":
    unittest.main()
