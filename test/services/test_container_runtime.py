"""Image acceptance checks runnable with unittest, without installing dev tools."""

import importlib
import importlib.metadata
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[2]


class ContainerRuntimeTest(unittest.TestCase):
    def test_runtime_image_excludes_development_dependencies(self):
        development_names = {"coverage", "pytest", "ruff"}
        leaked = (
            development_names
            & {
                canonicalize_name(distribution.metadata["Name"])
                for distribution in importlib.metadata.distributions()
            }
            if Path(sys.prefix).as_posix() == "/opt/venv"
            else set()
        )
        self.assertEqual(leaked, set())

    def test_installed_runtime_matches_project_and_lock(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        requirements = [
            *project["project"]["dependencies"],
            *project["tool"]["uv"].get("override-dependencies", []),
        ]
        for specification in requirements:
            requirement = Requirement(specification)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            with self.subTest(dependency=requirement.name):
                installed = importlib.metadata.version(requirement.name)
                self.assertIn(installed, requirement.specifier)

        # Direct pins alone miss the transitive differences in a fresh pip install.
        for distribution in importlib.metadata.distributions():
            name = canonicalize_name(distribution.metadata["Name"])
            locked_versions = {
                package["version"]
                for package in lock["package"]
                if canonicalize_name(package["name"]) == name
            }
            if locked_versions:
                with self.subTest(locked_dependency=name):
                    self.assertIn(distribution.version, locked_versions)

    def test_critical_native_and_application_imports(self):
        for module in (
            "fastapi",
            "uvicorn",
            "streamlit",
            "moviepy",
            "google.genai",
            "azure.cognitiveservices.speech",
            "faster_whisper",
            "app.asgi",
        ):
            with self.subTest(module=module):
                importlib.import_module(module)

    def test_api_health_endpoint(self):
        from fastapi.testclient import TestClient

        from app.asgi import app

        with TestClient(app) as client:
            response = client.get("/ping")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), "pong")

    def test_real_render_and_upload_validation_without_network(self):
        # Reuse the same app-level render acceptance check as pytest, including
        # Turkish glyph visibility, audio/video decoding and unsafe upload rejection.
        from test.services import test_render_smoke

        with tempfile.TemporaryDirectory(prefix="mpt-image-smoke-") as directory:
            test_render_smoke.test_turkish_subtitle_render_and_material_validation(
                Path(directory)
            )


if __name__ == "__main__":
    unittest.main()
