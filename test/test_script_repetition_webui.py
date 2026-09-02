import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


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
    namespace = {"tr": lambda key: key}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace[name], namespace


class TestScriptRepetitionWebUi(unittest.TestCase):
    def test_preflight_report_shows_recent_similar_scripts(self):
        render_report, namespace = _load_webui_helper("_render_content_preflight_report")
        write = Mock()
        caption = Mock()
        namespace["st"] = SimpleNamespace(
            write=write,
            caption=caption,
        )
        namespace["tr"] = {
            "Preflight Script Repeat Matches": "Recent similar scripts",
        }.get

        render_report(
            {
                "content_plan": {},
                "script_repeat_matches": [
                    {
                        "task_id": "previous-script",
                        "subject": "Why interest rates rise",
                        "created_at": "2026-07-04T10:00:00+00:00",
                        "similarity": 0.91,
                    }
                ],
            },
            "preflight",
        )

        write.assert_called_once_with("Recent similar scripts")
        caption.assert_called_once_with(
            "Why interest rates rise (91%) 2026-07-04T10:00:00+00:00"
        )

    def test_script_repeat_warning_is_advisory(self):
        warning_text, namespace = _load_webui_helper("_script_repeat_warning_text")
        namespace["tr"] = {
            "Preflight Script Repeat Warning": (
                "A recent script is very similar. Review it before generating."
            ),
        }.get

        self.assertEqual(warning_text([]), "")
        self.assertEqual(
            warning_text([{"task_id": "previous-script"}]),
            "A recent script is very similar. Review it before generating.",
        )


if __name__ == "__main__":
    unittest.main()
