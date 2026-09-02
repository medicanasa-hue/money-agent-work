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
    namespace = {"tr": lambda key: key}
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace[name], namespace


class TestMaterialBenchmarkWebUi(unittest.TestCase):
    def test_rows_keep_only_aggregated_provider_quality_signals(self):
        build_rows, namespace = _load_webui_helper("_material_benchmark_rows")
        namespace["tr"] = {
            "Material Benchmark Provider": "Provider",
            "Material Benchmark Candidates": "Candidates",
            "Material Benchmark Aspect Fit": "Aspect fit",
            "Material Benchmark Preview Quality": "Preview quality",
        }.get

        rows = build_rows(
            {
                "providers": [
                    {
                        "provider": "pexels",
                        "candidate_count": 4,
                        "average_aspect_fit": 0.875,
                        "average_preview_quality": 0.9,
                    }
                ]
            }
        )

        self.assertEqual(
            rows,
            [
                {
                    "Provider": "pexels",
                    "Candidates": 4,
                    "Aspect fit": "88%",
                    "Preview quality": "90%",
                }
            ],
        )

    def test_rows_ignore_malformed_provider_entries(self):
        build_rows, _namespace = _load_webui_helper("_material_benchmark_rows")

        self.assertEqual(build_rows({"providers": [None, "not-a-row"]}), [])


if __name__ == "__main__":
    unittest.main()
