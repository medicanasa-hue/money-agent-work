import re
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _requirements_specs() -> list[str]:
    requirements_path = PROJECT_ROOT / "requirements.txt"
    return [
        line.strip()
        for line in requirements_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _project_dependency_specs() -> list[str]:
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as file:
        pyproject = tomllib.load(file)
    return list(pyproject["project"]["dependencies"])


def _normalized_spec(spec: str) -> str:
    requirement = Requirement(spec)
    requirement.name = re.sub(
        r"[-_.]+", "-", requirement.name
    ).casefold()
    return str(requirement)


class TestDependencyContract(unittest.TestCase):
    def test_legacy_pip_requirements_match_project_dependencies(self):
        self.assertCountEqual(
            [_normalized_spec(spec) for spec in _requirements_specs()],
            [_normalized_spec(spec) for spec in _project_dependency_specs()],
        )

    def test_dependency_audit_scans_the_frozen_runtime_lock(self):
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "dependency-audit.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('version: "0.12.7"', workflow)
        self.assertIn("uv lock --check", workflow)
        self.assertIn("uv export --frozen --no-dev", workflow)
        self.assertIn("--output-file audit-requirements.txt", workflow)
        self.assertIn("inputs: audit-requirements.txt", workflow)
        self.assertIn("no-deps: true", workflow)
        self.assertIn("require-hashes: true", workflow)
        self.assertIn("internal-be-careful-extra-flags: --disable-pip", workflow)
        self.assertNotIn("inputs: requirements.txt", workflow)


if __name__ == "__main__":
    unittest.main()
