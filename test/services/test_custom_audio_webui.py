import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
TIMING_WARNING_KEY = "Subtitle Timing Approximate"
TIMING_WARNING = (
    "Subtitle timing is approximate because speech recognition was unavailable. "
    "Check the timing before publishing."
)
HELPER_NAMES = {
    "_safe_subtitle_download_path",
    "_load_subtitle_suspicion_report",
    "_render_subtitle_suspicion_report",
    "_result_failed_aspects",
    "_partial_success_failed_aspects",
    "_render_partial_success_warning",
    "_render_generation_completion_status",
}
WEBUI_TREE = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
HELPER_NODES = [
    node
    for node in WEBUI_TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES
]


def _load_helpers(storage_dir, st=None):
    translations = json.loads(
        (ROOT_DIR / "webui" / "i18n" / "en.json").read_text(encoding="utf-8")
    )["Translation"]
    namespace = {
        "json": json,
        "os": os,
        "utils": SimpleNamespace(storage_dir=lambda: str(storage_dir)),
        "st": st if st is not None else MagicMock(),
        "tr": lambda key: translations.get(key, key),
    }
    module = ast.fix_missing_locations(ast.Module(body=HELPER_NODES, type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


def _write_report(storage_dir, report):
    task_dir = storage_dir / "tasks" / "custom-audio"
    task_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = task_dir / "subtitle.srt"
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nExample subtitle\n", encoding="utf-8"
    )
    (task_dir / "subtitle.review.json").write_text(json.dumps(report), encoding="utf-8")
    return {"subtitle_path": str(subtitle_path)}


def test_estimated_timing_report_is_loaded_without_suspicious_text(tmp_path):
    job = _write_report(tmp_path, {"timing_source": "script_estimate", "items": []})
    helpers = _load_helpers(tmp_path)

    report = helpers["_load_subtitle_suspicion_report"](job)

    assert report is not None
    assert report["timing_source"] == "script_estimate"
    assert report["suspicious_count"] == 0
    assert report["items"] == []


@pytest.mark.parametrize("source", [None, "whisper", "unknown", ["script_estimate"]])
def test_empty_non_estimated_reports_do_not_warn(tmp_path, source):
    job = _write_report(tmp_path, {"timing_source": source, "items": []})
    helpers = _load_helpers(tmp_path)

    helpers["_render_subtitle_suspicion_report"](job)

    helpers["st"].warning.assert_not_called()
    helpers["st"].dataframe.assert_not_called()


@pytest.mark.parametrize("source", [None, "whisper", "unknown", "script_estimate"])
def test_existing_suspicious_text_remains_visible(tmp_path, source):
    item = {"subtitle_text": " check  this ", "time_range": "00:00 --> 00:02"}
    raw_report = {"items": [None, {"subtitle_text": " "}, item]}
    if source is not None:
        raw_report["timing_source"] = source
    job = _write_report(tmp_path, raw_report)
    helpers = _load_helpers(tmp_path)

    helpers["_render_subtitle_suspicion_report"](job)

    rows = helpers["st"].dataframe.call_args.args[0]
    assert len(rows) == 1
    assert "check this" in rows[0].values()
    warnings = [call.args[0] for call in helpers["st"].warning.call_args_list]
    assert "1 subtitle line(s) may need review." in warnings
    assert (TIMING_WARNING in warnings) == (source == "script_estimate")


@pytest.mark.parametrize("report", [None, [], {"items": {}}, {"items": "bad"}])
def test_invalid_report_shapes_are_ignored(tmp_path, report):
    job = _write_report(tmp_path, report)
    helpers = _load_helpers(tmp_path)

    assert helpers["_load_subtitle_suspicion_report"](job) is None


def test_estimated_report_outside_task_storage_is_ignored(tmp_path):
    job = _write_report(
        tmp_path / "outside", {"timing_source": "script_estimate", "items": []}
    )
    helpers = _load_helpers(tmp_path / "storage")

    assert helpers["_load_subtitle_suspicion_report"](job) is None


@pytest.mark.parametrize("partial_success", [False, True])
def test_single_completion_keeps_timing_warning_for_partial_results(
    tmp_path, partial_success
):
    job = _write_report(tmp_path, {"timing_source": "script_estimate", "items": []})
    result = {**job, "partial_success": partial_success, "failed_aspects": ["9:16"]}
    helpers = _load_helpers(tmp_path)

    helpers["_render_generation_completion_status"](result)

    warnings = [call.args[0] for call in helpers["st"].warning.call_args_list]
    assert TIMING_WARNING in warnings
    helpers["st"].dataframe.assert_not_called()


def test_batch_result_retains_path_and_displays_timing_warning(tmp_path):
    result = _write_report(tmp_path, {"timing_source": "script_estimate", "items": []})
    helpers = _load_helpers(tmp_path)
    helpers.update(
        batch_results=[],
        result=result,
        subject="Custom audio",
        task_id="custom-audio",
        metadata=None,
        viral_analysis=None,
        _render_material_sources=lambda job: None,
        _cooldown_summary_text=lambda value: "",
        _render_thumbnail_candidates=lambda *args, **kwargs: None,
    )
    helpers["st"].button.return_value = False
    # Exercise the real batch payload and output loop without running generation.
    append = next(
        node
        for node in ast.walk(WEBUI_TREE)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "batch_results"
        and node.value.func.attr == "append"
    )
    render_loop = next(
        node
        for node in ast.walk(WEBUI_TREE)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "batch_results"
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[append, render_loop], type_ignores=[])
    )

    exec(compile(module, str(WEBUI_MAIN), "exec"), helpers)

    assert helpers["batch_results"][0].get("subtitle_path") == result["subtitle_path"]
    assert TIMING_WARNING in [
        call.args[0] for call in helpers["st"].warning.call_args_list
    ]


@pytest.mark.parametrize(
    "entrypoint",
    ["_render_generation_completion_status", "_render_subtitle_suspicion_report"],
)
def test_streamlit_shows_turkish_timing_warning_without_empty_table(
    tmp_path, entrypoint
):
    job = _write_report(tmp_path, {"timing_source": "script_estimate", "items": []})
    translations = json.loads(
        (ROOT_DIR / "webui" / "i18n" / "tr.json").read_text(encoding="utf-8")
    )["Translation"]
    helpers_source = ast.unparse(ast.Module(body=HELPER_NODES, type_ignores=[]))
    script = (
        "import json, os\nimport streamlit as st\nfrom types import SimpleNamespace\n"
        f"utils = SimpleNamespace(storage_dir=lambda: {str(tmp_path)!r})\n"
        f"translations = {translations!r}\n"
        "tr = lambda key: translations.get(key, key)\n"
        f"{helpers_source}\n{entrypoint}({job!r})\n"
    )

    app = AppTest.from_string(script).run()

    assert not app.exception
    assert len(app.warning) == 1
    assert app.warning[0].value == (
        "Konuşma tanıma kullanılamadığı için altyazı zamanlaması yaklaşıktır. "
        "Yayımlamadan önce zamanlamayı kontrol edin."
    )
    assert not app.dataframe


@pytest.mark.parametrize(
    "locale", ["en", "tr", "zh", "de", "es", "id", "pt", "ru", "vi"]
)
def test_timing_warning_has_a_translation_in_every_maintained_locale(locale):
    translations = json.loads(
        (ROOT_DIR / "webui" / "i18n" / f"{locale}.json").read_text(encoding="utf-8")
    )["Translation"]

    assert translations.get(TIMING_WARNING_KEY)
